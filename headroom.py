"""Offline fleet plan compiler and read-only durable-evidence reader.

No command in this module launches, attaches to, or signals a workload. Relative
executables are resolved against the compiler's current working directory.
"""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile


MIB = 1024 * 1024
MAX_MIB = 1_048_576
MAX_PLAN_BYTES = 16 * MIB
MAX_EVENT_BYTES = 64 * 1024
MAX_LOG_BYTES = 1024 * 1024
MAX_LOG_TAIL_BYTES = 64 * 1024
MAX_OUTPUT_METADATA_BYTES = 4096
HEADER = struct.Struct('<8sIQQQIII')
ID_PATTERN = re.compile(r'[a-z0-9][a-z0-9-]{0,39}', re.ASCII)
RESERVED_IDS = {'con', 'prn', 'aux', 'nul'} | {
    f'{prefix}{number}' for prefix in ('com', 'lpt') for number in range(1, 10)
}
ROOT_KEYS = {'budget_mib', 'hard_mib', 'reserve_mib', 'sustain_ms',
             'grace_ms', 'timeout_ms', 'jobs'}
JOB_KEYS = {'id', 'priority', 'claim_mib', 'hard_mib', 'allow_stop',
            'argv', 'max_resumes', 'resume_argv'}


class PlanError(ValueError):
    """An input does not satisfy fleet protocol v1."""


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanError(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def _constant(value):
    raise PlanError(f'non-JSON numeric constant: {value}')


def strict_json_loads(value):
    """Decode UTF-8 bytes or text, rejecting duplicate keys and NaN/Infinity.

    Raises ValueError (usually PlanError) or UnicodeError on invalid input.
    Schema validation remains the caller's responsibility.
    """
    # Decode explicitly: json.loads(bytes) otherwise also accepts UTF-16/32.
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='strict')
    try:
        return json.loads(value, object_pairs_hook=_object_pairs,
                          parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise PlanError(str(error)) from error


def _object(value, allowed, required, where):
    if not isinstance(value, dict):
        raise PlanError(f'{where} must be an object')
    if any(not isinstance(key, str) for key in value):
        raise PlanError(f'{where} keys must be strings')
    unknown = value.keys() - allowed
    missing = required - value.keys()
    if unknown or missing:
        raise PlanError(f'{where}: unknown keys {sorted(unknown)}, '
                        f'missing keys {sorted(missing)}')


def _integer(value, low, high, where):
    if type(value) is not int or not low <= value <= high:
        raise PlanError(f'{where} must be an integer in {low}..{high}')
    return value


def _string(value, where):
    if not isinstance(value, str) or '\0' in value:
        raise PlanError(f'{where} must be a string without NUL')
    try:
        return value.encode('utf-16le', errors='strict')
    except UnicodeError as error:
        raise PlanError(f'{where} contains invalid Unicode') from error


def _wide(value, limit, where, *, empty=False):
    encoded = _string(value, where)
    units = len(encoded) // 2
    if not (0 if empty else 1) <= units <= limit:
        raise PlanError(f'{where} must contain '
                        f'{0 if empty else 1}..{limit} UTF-16 code units')
    return struct.pack('<I', units) + encoded


def _executable(value, where):
    _string(value, where)
    if not value:
        raise PlanError(f'{where} must not be empty')
    # Explicitly seek an .exe, independently of PATHEXT (.cmd/.bat are shells).
    candidate = value if value.lower().endswith('.exe') else value + '.exe'
    local = Path(candidate)
    resolved = local if local.is_file() else shutil.which(candidate)
    if resolved is None:
        raise PlanError(f'{where}: cannot resolve an existing .exe: {value!r}')
    try:
        result = Path(resolved).resolve(strict=True)
        if not result.is_file() or result.suffix.lower() != '.exe':
            raise PlanError(f'{where}: executable must be an existing .exe')
        return str(result)
    except (OSError, RuntimeError) as error:
        raise PlanError(f'{where}: cannot resolve executable: {error}') from error


def _command(argv, where, *, optional=False):
    if not isinstance(argv, list) or (not argv and not optional):
        raise PlanError(f'{where} must be a nonempty array of strings')
    if not argv:
        return _wide('', 4095, where, empty=True) + _wide(
            '', 32766, where, empty=True)
    # Checking the unquoted aggregate first also bounds quoting work for an
    # adversarial array of empty strings. Each argument adds at least a space.
    units = len(argv) - 1
    for index, argument in enumerate(argv):
        units += len(_string(argument, f'{where}[{index}]')) // 2
        if units > 32766:
            raise PlanError(f'{where} command exceeds 32766 UTF-16 code units')
    executable = _executable(argv[0], f'{where}[0]')
    command = subprocess.list2cmdline([executable, *argv[1:]])
    return (_wide(executable, 4095, f'{where} executable')
            + _wide(command, 32766, f'{where} command'))


def compile_plan(plan: dict) -> bytes:
    """Validate a v1 plan and return its exact little-endian wire format.

    This function performs filesystem/PATH resolution only; it never executes
    the programs. JSON files must additionally be decoded with duplicate-key
    rejection, as the ``compile`` command does before calling this function.
    """
    _object(plan, ROOT_KEYS, {'budget_mib', 'hard_mib', 'jobs'}, 'plan')
    budget = _integer(plan['budget_mib'], 1, MAX_MIB, 'budget_mib')
    hard = _integer(plan['hard_mib'], 1, MAX_MIB, 'hard_mib')
    if hard <= budget:
        raise PlanError('hard_mib must exceed budget_mib')
    reserve = _integer(plan.get('reserve_mib', 256), 0, MAX_MIB, 'reserve_mib')
    sustain = _integer(plan.get('sustain_ms', 500), 100, 60000, 'sustain_ms')
    grace = _integer(plan.get('grace_ms', 2000), 1, 60000, 'grace_ms')
    timeout = _integer(plan.get('timeout_ms', 60000), 1, 86400000, 'timeout_ms')
    jobs = plan['jobs']
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 64:
        raise PlanError('jobs must be an array containing 1..64 jobs')
    output = bytearray(HEADER.pack(b'HRFLEET1', len(jobs), budget * MIB,
                                  hard * MIB, reserve * MIB,
                                  sustain, grace, timeout))
    identifiers = set()
    for index, job in enumerate(jobs):
        where = f'jobs[{index}]'
        _object(job, JOB_KEYS, {'id', 'claim_mib', 'hard_mib', 'argv'}, where)
        identifier = job['id']
        if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
            raise PlanError(f'{where}.id must match [a-z0-9][a-z0-9-]{{0,39}}')
        if identifier in RESERVED_IDS:
            raise PlanError(f'{where}.id is a reserved Windows device name')
        if identifier in identifiers:
            raise PlanError(f'duplicate job id: {identifier}')
        identifiers.add(identifier)
        priority = _integer(job.get('priority', 50), 0, 100, f'{where}.priority')
        claim = _integer(job['claim_mib'], 1, budget, f'{where}.claim_mib')
        job_hard = _integer(job['hard_mib'], claim, hard, f'{where}.hard_mib')
        allow = job.get('allow_stop', False)
        if type(allow) is not bool:
            raise PlanError(f'{where}.allow_stop must be a boolean')
        resumes = _integer(job.get('max_resumes', 0), 0, 3,
                           f'{where}.max_resumes')
        resume_argv = job.get('resume_argv', [])
        if resumes and (not allow or not resume_argv):
            raise PlanError(f'{where}: max_resumes requires allow_stop '
                            'and nonempty resume_argv')
        argv_bytes = _command(job['argv'], f'{where}.argv')
        resume_bytes = _command(resume_argv, f'{where}.resume_argv', optional=True)
        identifier_bytes = identifier.encode('ascii')
        output.extend(struct.pack('<I', len(identifier_bytes)))
        output.extend(identifier_bytes)
        output.extend(struct.pack('<IIIQQ', priority, int(allow), resumes,
                                  claim * MIB, job_hard * MIB))
        output.extend(argv_bytes)
        output.extend(resume_bytes)
    if len(output) > MAX_PLAN_BYTES:
        raise PlanError('compiled plan exceeds 16 MiB')
    return bytes(output)


def _write_new(path, payload):
    """Publish a fully flushed file atomically, refusing even dangling links."""
    path = Path(path).absolute()
    descriptor, temporary = tempfile.mkstemp(prefix='.headroom-', suffix='.tmp',
                                            dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            # Windows rename is atomic and fails if the target already exists.
            os.rename(temporary, path)
        else:
            # POSIX rename would overwrite; an atomic hard-link cannot.
            os.link(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class _Evidence:
    """Small bounded reducer; retains at most 64 jobs x four attempts."""

    def __init__(self):
        self.run_id = None
        self.last = None
        self.records = 0
        self.jobs = {}
        self.finished = None
        self.deadline = False
        self.internal_error = False
        self.interrupted = False
        self.live = False
        self.sealed = False

    def accept(self, record):
        if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1:
            raise PlanError('unsupported or missing evidence version')
        identity = record.get('run_id')
        _string(identity, 'run_id')
        if not identity or len(identity) > 64:
            raise PlanError('invalid run_id')
        if self.run_id is not None and identity != self.run_id:
            raise PlanError('mixed run identities')
        event = record.get('event')
        _string(event, 'event')
        if not event or len(event) > 128:
            raise PlanError('invalid event name')
        task = record.get('task')
        if (not isinstance(task, str)
                or (task and (not ID_PATTERN.fullmatch(task) or task in RESERVED_IDS))):
            raise PlanError('invalid task id')
        for field, high in (('elapsed_ms', 2**64 - 1), ('priority', 100),
                            ('attempt', 4), ('total_commit_bytes', 2**64 - 1),
                            ('available_bytes', 2**64 - 1), ('code', 2**32 - 1)):
            _integer(record.get(field), 0, high, field)
        if self.finished is not None:
            raise PlanError('records follow finished')
        if self.last and record['elapsed_ms'] < self.last['elapsed_ms']:
            raise PlanError('elapsed time went backwards')
        if self.last is None and (event != 'prepared' or task):
            raise PlanError('evidence must begin with fleet prepared')
        if self.last is not None and event == 'prepared':
            raise PlanError('repeated prepared event')
        if event in {'prepared', 'finished', 'deadline', 'internal_error'} and task:
            raise PlanError(f'{event} must identify the fleet')
        if event == 'listening':
            if self.live or self.jobs or task or record['attempt'] or record['priority'] or record['code']:
                raise PlanError('invalid live-session opening')
            self.live = True
        elif event == 'sealed':
            if not self.live or self.sealed or task or record['attempt'] or record['priority'] or record['code']:
                raise PlanError('invalid live-session seal')
            self.sealed = True
        elif event == 'deadline':
            self.deadline = True
        elif event == 'internal_error':
            self.internal_error = True
        elif event == 'interrupt':
            if task:
                raise PlanError('interrupt must identify the fleet')
            self.interrupted = True
        elif event == 'finished':
            self.finished = record
        elif event in {'queued', 'started', 'pressure_intent',
                       'checkpoint_acknowledged', 'terminated', 'stopped',
                       'completed', 'resume_queued', 'pressure_revoked',
                       'launch_failed'}:
            self._job(record)
        # Unknown diagnostics never advance any lifecycle or imply completion.
        self.run_id = identity
        self.last = record

    @staticmethod
    def _attempt(number):
        return {'attempt': number, 'status': 'queued', 'root_exit_code': None,
                'logical_exit_code': None,
                'stop_intent': False, 'checkpoint_acknowledged': False,
                'terminated': False, 'completion_recorded': False,
                'revoked_stop_intents': 0, 'launch_failed': False}

    def _job(self, record):
        task, event, number = record['task'], record['event'], record['attempt']
        if not task:
            raise PlanError(f'{event} requires a task')
        if event == 'queued':
            if self.sealed or task in self.jobs or len(self.jobs) >= 64 or number not in (0, 1):
                raise PlanError('invalid initial queue event')
            self.jobs[task] = {'id': task, 'priority': record['priority'],
                               'attempts': [self._attempt(1)]}
            return
        job = self.jobs.get(task)
        if job is None or job['priority'] != record['priority']:
            raise PlanError('task missing queue event or changed priority')
        attempt = job['attempts'][-1]
        if event == 'resume_queued':
            if (attempt['status'] != 'stopped'
                    or not attempt['checkpoint_acknowledged']
                    or number != attempt['attempt'] or number >= 4):
                raise PlanError('resume requires acknowledged stopped predecessor')
            # Native resume_queued names the attempt just stopped. Only the
            # next started event names its fresh successor.
            job['attempts'].append(self._attempt(number + 1))
            return
        if event == 'launch_failed':
            # Directory creation can fail before native launch() increments the
            # first attempt counter. Preserve that durable attempt=0 identity.
            before_attempt = (number == 0 and len(job['attempts']) == 1
                              and attempt['status'] == 'queued')
            if ((number != attempt['attempt'] and not before_attempt)
                    or attempt['status'] not in {'queued', 'running_or_interrupted'}
                    or attempt['stop_intent'] or attempt['terminated']
                    or record['code'] != 70):
                raise PlanError('invalid launch failure or immutable attempt outcome changed')
            if before_attempt:
                attempt['attempt'] = 0
            attempt['status'] = 'failed'
            attempt['logical_exit_code'] = 70
            attempt['launch_failed'] = True
            # The event attributes a launch error; it does not prove a raw root
            # exit code, even if bounded cleanup terminated a suspended child.
            return
        if number != attempt['attempt']:
            raise PlanError('unexpected attempt number')
        status = attempt['status']
        if event == 'started':
            if status != 'queued':
                raise PlanError('attempt started twice or after terminal outcome')
            attempt['status'] = 'running_or_interrupted'
        elif event == 'pressure_intent':
            if status != 'running_or_interrupted' or attempt['stop_intent']:
                raise PlanError('stop intent without running attempt')
            attempt['stop_intent'] = True
        elif event == 'checkpoint_acknowledged':
            if not attempt['stop_intent'] or status not in {'running_or_interrupted', 'stopped'}:
                raise PlanError('acknowledgement without stop intent')
            attempt['checkpoint_acknowledged'] = True
        elif event == 'pressure_revoked':
            if (status != 'running_or_interrupted' or not attempt['stop_intent']
                    or attempt['checkpoint_acknowledged'] or attempt['terminated']):
                raise PlanError('revocation without an unaccepted stop intent')
            attempt['stop_intent'] = False
            attempt['revoked_stop_intents'] += 1
        elif event == 'terminated':
            if status != 'running_or_interrupted':
                raise PlanError('termination without running attempt')
            attempt['terminated'] = True
        elif event == 'stopped':
            if status != 'running_or_interrupted':
                raise PlanError('stop without running attempt')
            attempt['status'] = 'stopped'
            attempt['root_exit_code'] = record['code']
            attempt['logical_exit_code'] = 125
        elif event == 'completed':
            if status not in {'running_or_interrupted', 'stopped'} or attempt['completion_recorded']:
                raise PlanError('completion without running attempt or duplicate completion')
            attempt['completion_recorded'] = True
            attempt['root_exit_code'] = record['code']
            if attempt['stop_intent'] or status == 'stopped' or attempt['terminated']:
                attempt['status'] = 'stopped'
                attempt['logical_exit_code'] = 125
            else:
                attempt['status'] = 'completed' if record['code'] == 0 else 'failed'
                attempt['logical_exit_code'] = record['code']

    def report(self, error=None):
        status = 'running_or_interrupted'
        if error is not None or self.last is None:
            status = 'invalid_or_incomplete_evidence'
        elif self.finished is not None:
            code = self.finished['code']
            outcomes = [job['attempts'][-1]['status'] for job in self.jobs.values()]
            if self.internal_error:
                status = 'internal_error' if code else 'invalid_or_incomplete_evidence'
            elif self.deadline or code == 124:
                status = 'deadline' if code == 124 else 'invalid_or_incomplete_evidence'
            elif self.interrupted:
                status = 'interrupted' if code == 130 else 'invalid_or_incomplete_evidence'
            elif code == 130:
                status = 'interrupted'
            elif self.live and not self.sealed:
                status = 'invalid_or_incomplete_evidence'
            elif self.live and self.sealed and not outcomes and code == 0:
                status = 'no_work'
            elif not outcomes or any(s in {'queued', 'running_or_interrupted'} for s in outcomes):
                status = 'invalid_or_incomplete_evidence'
            elif code == 0:
                status = 'completed' if all(s == 'completed' for s in outcomes) else 'invalid_or_incomplete_evidence'
            elif code == 1 and any(s in {'failed', 'stopped'} for s in outcomes):
                status = 'failed'
            else:
                status = 'invalid_or_incomplete_evidence'
        elif self.internal_error:
            status = 'internal_error'
        elif self.deadline:
            status = 'deadline'
        elif self.interrupted:
            status = 'interrupted'
        confirmed = self.finished is not None and status != 'invalid_or_incomplete_evidence'
        if status == 'invalid_or_incomplete_evidence' and error is None:
            error = ('empty evidence' if self.last is None else
                     'finished evidence contradicts durable logical outcomes')
        jobs = []
        for job in self.jobs.values():
            outcome = job['attempts'][-1]['status']
            jobs.append({**job, 'observed_outcome': outcome,
                         'status': outcome if confirmed else 'unconfirmed'})
        return {'version': 1, 'run_id': self.run_id, 'status': status,
                'evidence_valid': error is None,
                'evidence_complete': confirmed, 'records_read': self.records,
                'last_event': self.last['event'] if self.last else None,
                'fleet_exit_code': self.finished['code'] if confirmed else None,
                'process_liveness_verified': False, 'jobs': jobs, 'error': error}


def read_status(state):
    """Stream fleet.jsonl without inspecting PIDs or assuming crash recovery.

    Attempt outcomes describe the valid evidence prefix. Logical outcomes are
    unconfirmed unless the entire observed batch has consistent final evidence.
    A later modification requires a fresh read; liveness is never inferred.

    Stable report keys include status, evidence_valid (valid observed prefix),
    evidence_complete (consistent finished event), run_id, error, and jobs.
    Each job includes id, priority, status, observed_outcome and attempts; each
    attempt includes attempt, status, root_exit_code and logical_exit_code.
    launch_failed marks a terminal attempt failure with logical code 70, never
    safe queued work; its raw root_exit_code remains unknown (None).
    An unfinished but valid prefix is not authorization to replay any job.
    """
    evidence = _Evidence()
    error = None
    path = Path(state) / 'fleet.jsonl'
    try:
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            # Read a finite snapshot even if a live writer keeps appending.
            remaining = before.st_size
            while remaining:
                line = stream.readline(min(MAX_EVENT_BYTES + 1, remaining))
                if not line:
                    raise PlanError('evidence truncated while being read')
                remaining -= len(line)
                evidence.records += 1
                if len(line) > MAX_EVENT_BYTES or not line.endswith(b'\n'):
                    raise PlanError('oversized or truncated evidence record')
                evidence.accept(strict_json_loads(line))
            if stream.read(1):
                raise PlanError('evidence changed while being read')
            after = os.fstat(stream.fileno())
            current = path.stat()
            # Python 3.12 Windows fstat/stat disagree about ctime (change time
            # versus creation time). Compare it only between handle samples.
            signature = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size,
                                      stat.st_mtime_ns)
            if (signature(before) != signature(after)
                    or before.st_ctime_ns != after.st_ctime_ns
                    or signature(after) != signature(current)):
                raise PlanError('evidence changed while being read')
    except (OSError, ValueError, UnicodeError, RecursionError) as failure:
        error = str(failure)
    return evidence.report(error)


def _log_state_path(state):
    """Keep lexical aliases, device paths and remote paths out of log reads."""
    raw = os.fspath(state)
    if type(raw) is not str or not raw or '\0' in raw:
        raise PlanError('state must be a nonempty local path without NUL')
    if raw.startswith(('\\', '//')):
        raise PlanError('remote and device state paths are refused')
    drive, rest = os.path.splitdrive(raw)
    if drive and not rest.startswith(('\\', '/')):
        raise PlanError('drive-relative state paths are refused')
    for part in re.split(r'[\\/]', rest):
        if not part or part == '.':
            continue
        if (part == '..' or part.endswith((' ', '.'))
                or any(char in part for char in ':~*?"<>|')
                or any(ord(char) < 32 for char in part)
                or part.split('.')[0].lower() in RESERVED_IDS):
            raise PlanError('state contains a traversal, device or alias component')
    path = Path(raw).absolute()
    if os.name == 'nt':
        # A mapped remote drive must be rejected before accessing its contents.
        import ctypes
        get_type = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        get_type.argtypes = [ctypes.c_wchar_p]
        get_type.restype = ctypes.c_uint
        if get_type(path.anchor) not in {2, 3, 6}:
            raise PlanError('state must be on a known local drive')
    return path


def _log_file_info(info, path, *, directory=False):
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise PlanError(f'symlink/reparse path refused: {path}')
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise PlanError(f'log path component is not a directory: {path}')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PlanError(f'log evidence must be a regular, unaliased file: {path}')


def _log_signature(info):
    # Windows fstat and lstat can use different ctime definitions. Compare
    # ctime only between repeated samples of the same kind below.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns,
            getattr(info, 'st_file_attributes', 0))


def _log_open(stack, path, limit, *, optional=False):
    try:
        before = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    _log_file_info(before, path)
    if before.st_size > limit:
        raise PlanError(f'log evidence exceeds {limit} byte limit: {path}')
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0)
             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    descriptor = os.open(path, flags)
    try:
        handle = os.fstat(descriptor)
        _log_file_info(handle, path)
        if _log_signature(before) != _log_signature(handle):
            raise PlanError('log evidence changed while opening; retry')
        stream = os.fdopen(descriptor, 'rb', buffering=0)
    except BaseException:
        os.close(descriptor)
        raise
    stack.enter_context(stream)
    return (path, stream, before, handle)


def _log_verify(snapshot):
    path, stream, before, handle = snapshot
    after = os.fstat(stream.fileno())
    current = path.lstat()
    _log_file_info(after, path)
    _log_file_info(current, path)
    if (_log_signature(handle) != _log_signature(after)
            or handle.st_ctime_ns != after.st_ctime_ns
            or _log_signature(before) != _log_signature(current)
            or before.st_ctime_ns != current.st_ctime_ns):
        raise PlanError('log evidence changed while reading; retry')


def _log_metadata(value, snapshots):
    keys = {'version', 'limit_bytes', 'stdout', 'stderr'}
    _object(value, keys, keys, 'output.json')
    _integer(value['version'], 1, 1, 'output.json.version')
    _integer(value['limit_bytes'], MAX_LOG_BYTES, MAX_LOG_BYTES,
             'output.json.limit_bytes')
    for name in ('stdout', 'stderr'):
        entry = value[name]
        keys = {'retained_bytes', 'discarded_bytes', 'eof'}
        _object(entry, keys, keys, f'output.json.{name}')
        retained = _integer(entry['retained_bytes'], 0, MAX_LOG_BYTES,
                            f'{name}.retained_bytes')
        discarded = _integer(entry['discarded_bytes'], 0, 2**64 - 1,
                             f'{name}.discarded_bytes')
        if type(entry['eof']) is not bool or not entry['eof']:
            raise PlanError(f'output.json.{name}.eof must be true')
        if snapshots[name] is None or snapshots[name][2].st_size != retained:
            raise PlanError(f'output.json.{name} retained size mismatch')
        if discarded and retained != MAX_LOG_BYTES:
            raise PlanError(f'output.json.{name} discards before retention limit')
        if retained + discarded > 2**64 - 1:
            raise PlanError(f'output.json.{name} byte count overflow')
    return value


def read_log(state, task, *, attempt=1, stream='stdout', tail_bytes=4096):
    """Read a finite, read-only output snapshot; never inspect task liveness.

    Errors return an empty text field and an explanatory error. Successful
    reads return at most tail_bytes raw bytes, decoded as UTF-8 with replacement.
    output.json confirms capture counters/EOF only, never task success. Missing
    metadata leaves discarded_bytes, eof and capture_truncated unknown (None).
    truncated includes capture loss and omitted tail bytes, or is unknown when
    neither can be established. Both streams must match any present metadata.
    Path/handle identities and timestamps are rechecked; concurrent changes
    conservatively refuse the snapshot. This is not a filesystem transaction.
    """
    report = {'version': 1, 'task': task if type(task) is str else None,
              'attempt': attempt if type(attempt) is int else None,
              'stream': stream if type(stream) is str else None,
              'tail_bytes': tail_bytes if type(tail_bytes) is int else None,
              'limit_bytes': MAX_LOG_BYTES, 'text': '', 'returned_bytes': 0,
              'retained_bytes': None, 'discarded_bytes': None, 'eof': None,
              'tail_truncated': None, 'capture_truncated': None,
              'truncated': None, 'metadata_confirmed': False,
              'process_liveness_verified': False, 'error': None}
    try:
        if (type(task) is not str or not ID_PATTERN.fullmatch(task)
                or task in RESERVED_IDS):
            raise PlanError('invalid task id')
        _integer(attempt, 1, 4, 'attempt')
        if type(stream) is not str or stream not in {'stdout', 'stderr'}:
            raise PlanError('stream must be stdout or stderr')
        _integer(tail_bytes, 0, MAX_LOG_TAIL_BYTES, 'tail_bytes')
        state_path = _log_state_path(state)
        folder = state_path / task / f'attempt-{attempt}'
        directories = []
        for component in [*reversed(folder.parents), folder]:
            info = component.lstat()
            _log_file_info(info, component, directory=True)
            directories.append((component, info))

        def verify_directories():
            for component, before in directories:
                after = component.lstat()
                _log_file_info(after, component, directory=True)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise PlanError('log path changed while reading; retry')

        with ExitStack() as stack:
            metadata_path = folder / 'output.json'
            metadata = _log_open(stack, metadata_path, MAX_OUTPUT_METADATA_BYTES,
                                 optional=True)
            snapshots = {name: _log_open(stack, folder / f'{name}.log',
                                        MAX_LOG_BYTES, optional=name != stream)
                         for name in ('stdout', 'stderr')}
            verify_directories()
            value = None
            if metadata is not None:
                body = metadata[1].read(metadata[2].st_size + 1)
                if len(body) != metadata[2].st_size:
                    raise PlanError('output.json changed while reading; retry')
                value = _log_metadata(strict_json_loads(body), snapshots)
            selected = snapshots[stream]
            size = selected[2].st_size
            count = min(size, tail_bytes)
            selected[1].seek(size - count)
            body = selected[1].read(count)
            if len(body) != count:
                raise PlanError('log evidence changed while reading; retry')
            for snapshot in [metadata, *snapshots.values()]:
                if snapshot is not None:
                    _log_verify(snapshot)
            # An absent file becoming present also invalidates this snapshot.
            for path, snapshot in [(metadata_path, metadata), *[
                    (folder / f'{name}.log', snapshots[name])
                    for name in ('stdout', 'stderr')]]:
                if snapshot is None:
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    raise PlanError('log evidence appeared while reading; retry')
            verify_directories()
        capture = value[stream]['discarded_bytes'] > 0 if value else None
        tail = count < size
        report.update(text=body.decode('utf-8', errors='replace'),
                      returned_bytes=count, retained_bytes=size,
                      discarded_bytes=value[stream]['discarded_bytes'] if value else None,
                      eof=True if value else None, metadata_confirmed=value is not None,
                      tail_truncated=tail, capture_truncated=capture,
                      truncated=True if tail or capture else capture)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        report['error'] = str(error)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    compiler = commands.add_parser(
        'compile', help='compile JSON to a fresh .hrp file',
        description='Compile a UTF-8 JSON plan (maximum input size: 16 MiB).')
    compiler.add_argument('plan')
    compiler.add_argument('output')
    status = commands.add_parser('status', help='read durable fleet evidence only')
    status.add_argument('state')
    logs = commands.add_parser('logs', help='read a bounded offline output snapshot')
    logs.add_argument('state')
    logs.add_argument('task')
    logs.add_argument('--attempt', type=int, default=1)
    logs.add_argument('--stream', choices=('stdout', 'stderr'), default='stdout')
    logs.add_argument('--tail-bytes', type=int, default=4096)
    args = parser.parse_args(argv)
    if args.command == 'logs':
        report = read_log(args.state, args.task, attempt=args.attempt,
                          stream=args.stream, tail_bytes=args.tail_bytes)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 1 if report['error'] is not None else 0
    if args.command == 'status':
        report = read_status(args.state)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 1 if report['status'] == 'invalid_or_incomplete_evidence' else 0
    try:
        with Path(args.plan).open('rb') as stream:
            source = stream.read(MAX_PLAN_BYTES + 1)
        if len(source) > MAX_PLAN_BYTES:
            raise PlanError('JSON input exceeds 16 MiB')
        payload = compile_plan(strict_json_loads(source))
        _write_new(args.output, payload)
    except FileExistsError as error:
        print(f'Output already exists: {error}', file=sys.stderr)
        return 73
    except (ValueError, UnicodeError, RecursionError) as error:
        print(f'Invalid plan: {error}', file=sys.stderr)
        return 64
    except OSError as error:
        print(f'Cannot compile plan: {error}', file=sys.stderr)
        return 70
    return 0


if __name__ == '__main__':
    sys.exit(main())
