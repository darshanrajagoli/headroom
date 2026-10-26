"""Prepare an offline recovery plan; never start, signal, or inspect a PID.

Usage: python recover.py PLAN.json OLD_STATE OUTPUT.hrp
Exit 0 means a new plan was prepared, or there was no work; 73 means refusal.
The JSON report is the verdict, not a claim that checkpoint contents work.

Conservative v1 policy (intentional refusals, not replay heuristics):
* Unknown events, missing queues, partial records, links, unjournaled attempt
  directories, and contradictory fleet/attempt journals require manual review.
  This includes pressure_revoked: v1 recovery does not guess revocation rules.
* deadline/interrupt/internal_error and unsuccessful finished runs cannot be
  recovered here. A consistent successful finished run only reports no work.
* Native resume_queued records the STOPPED attempt number and the remaining
  allowance in code. It reserves the NEXT attempt, even before started exists.
* A complete journal means whole newline-terminated records, not necessarily a
  terminal lifecycle: a crash after durable checkpoint intent and exact ack is
  recoverable, even if acknowledgement had not yet reached the fleet journal.
* An attempt's prepared UUID binds its exact request and ack. UUID reuse,
  claimed acknowledgements without those bytes, or normal failure never resume.
* launch_failed is an immutable task failure (code 70), even without started
  or a complete attempt journal. It never qualifies as an unstarted queue.

The owner lease is held through publication. Only ERROR_FILE_NOT_FOUND proves
the named outer job absent; an existing job is pinned and must remain empty.
Reservations are exclusive and durable and are NEVER automatically removed,
including after publication failure. Reparse points and hard-linked evidence
are refused. The contract assumes trusted local storage and same-user state;
it is not protection against an adversary rewriting evidence concurrently.
"""

import argparse
from contextlib import contextmanager
import copy
import ctypes
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid

import headroom


MAX_FILE = 16 * 1024 * 1024
MAX_LINE = 64 * 1024
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.ASCII)
FLEET_FIELDS = {'version', 'run_id', 'event', 'elapsed_ms', 'task', 'priority',
                'attempt', 'total_commit_bytes', 'available_bytes', 'code'}
ATTEMPT_FIELDS = {'version', 'run_id', 'event', 'elapsed_ms',
                  'job_commit_bytes', 'active_processes', 'code'}
TERMINALS = {'finished', 'deadline', 'interrupt', 'internal_error'}


class RecoveryError(ValueError):
    """Insufficient evidence or authority to publish recovery work."""


def _require(condition, message):
    if not condition:
        raise RecoveryError(message)


def _integer(value, maximum, name):
    _require(type(value) is int and 0 <= value <= maximum, f'invalid {name}')


def _uuid(value):
    _require(isinstance(value, str) and UUID.fullmatch(value), 'invalid UUID')
    return value


def _strict_json(data):
    # Only compile_plan is a public dependency. Keep decoding independent of
    # the concurrently maintained compiler's private helper names.
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, f'duplicate JSON key: {key}')
            result[key] = value
        return result

    def constant(value):
        raise RecoveryError(f'non-JSON numeric constant: {value}')

    return json.loads(data.decode('utf-8', errors='strict'),
                      object_pairs_hook=pairs, parse_constant=constant)


def _signature(value):
    # CPython 3.12 Windows stat() exposes creation time as st_ctime whereas
    # fstat() can expose change time there. Birth time is consistent for both.
    identity_time = (getattr(value, 'st_birthtime_ns', None) if os.name == 'nt'
                     else value.st_ctime_ns)
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, identity_time, value.st_mode, value.st_nlink,
            getattr(value, 'st_file_attributes', 0))


def _local_path(path):
    raw = Path(path)
    path = Path(os.path.abspath(path))
    if os.name == 'nt':
        _require(not str(path).startswith(('\\\\', '//')), 'local paths required; device/remote path refused')
        devices = {'CON', 'PRN', 'AUX', 'NUL'} | {
            prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789¹²³'}
        # GetFullPathName (used by abspath) can trim a trailing dot. Check the
        # user spelling as well as the absolute spelling before any mutation.
        parts = [part for candidate in (raw, path) for part in candidate.parts
                 if part != candidate.anchor and part not in ('.', '..')]
        for part in parts:
            _require(not any(c in part for c in ':<>"|?*')
                     and not part.endswith(('.', ' '))
                     and part.split('.')[0].upper() not in devices,
                     'ambiguous/device/alternate-stream path refused')
    return path


def _unlinked(path):
    """Reject symlinks/junctions in every existing component, before opening."""
    path = _local_path(path)
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        _require(not stat.S_ISLNK(info.st_mode)
                 and not getattr(info, 'st_file_attributes', 0) & 0x400,
                 f'linked/reparse path refused: {component}')
    return path


def _working_directory(files, state):
    """Read the native launch directory without guessing from legacy state."""
    data = files.read(state / 'working-directory.hrc', 8 + 8190)
    _require(data.startswith(b'HRCWD001') and len(data) > 8,
             'invalid working-directory metadata')
    value = data[8:].decode('utf-16le', errors='strict')
    _require('\0' not in value and Path(value).is_absolute(),
             'working directory must be an absolute path without NUL')
    # Also rejects drive-relative paths, mapped remote drives and lexical aliases.
    headroom._log_state_path(value)
    path = _local_path(value)
    _require(str(path) == value, 'noncanonical working directory')
    path = _unlinked(path)
    info = path.stat()
    _require(stat.S_ISDIR(info.st_mode), 'working directory must be an existing directory')
    identity = (info.st_dev, info.st_ino)
    previous = files.working_directories.setdefault(path, identity)
    _require(previous == identity, 'working directory changed')
    return str(path)


def _directory_names(path, limit=256):
    """Bounded namespace observation; directory timestamps may be delayed."""
    names = set()
    with os.scandir(path) as entries:
        for entry in entries:
            names.add(entry.name)
            _require(len(names) <= limit, 'unexpectedly large task directory')
    return names


class _Files:
    """Stream journals and retain only bounded reducers plus file identities."""

    def __init__(self):
        self.seen = {}
        self.directories = {}
        self.working_directories = {}

    @contextmanager
    def open(self, path):
        path = _unlinked(path)
        before = path.stat()
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                 f'evidence must be a regular, singly linked file: {path}')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            _require(_signature(before) == _signature(opened), 'evidence changed on open')
            yield stream
            after = os.fstat(stream.fileno())
        _require(_signature(opened) == _signature(after)
                 == _signature(path.stat()), f'evidence changed: {path}')
        previous = self.seen.setdefault(path, _signature(after))
        _require(previous == _signature(after), f'evidence changed between reads: {path}')

    def read(self, path, limit):
        with self.open(path) as stream:
            data = stream.read(limit + 1)
            _require(len(data) <= limit, f'oversized file: {path}')
        return data

    def records(self, path):
        with self.open(path) as stream:
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                _require(len(line) <= MAX_LINE and line.endswith(b'\n'),
                         f'truncated/oversized journal record: {path}')
                yield _strict_json(line)

    def directory(self, path):
        path = _unlinked(path)
        before = path.stat()
        _require(stat.S_ISDIR(before.st_mode), f'expected directory: {path}')
        names = _directory_names(path)
        _require(_signature(before) == _signature(path.stat()), 'directory changed')
        previous = self.directories.setdefault(path, names)
        _require(previous == names, 'directory changed between observations')
        self.seen[path] = _signature(before)
        return names

    def unchanged(self):
        for path, signature in self.seen.items():
            _unlinked(path)
            _require(_signature(path.stat()) == signature, f'evidence changed: {path}')
        for path, names in self.directories.items():
            _require(_directory_names(path) == names, f'directory entries changed: {path}')
        for path, identity in self.working_directories.items():
            info = _unlinked(path).stat()
            _require(stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity,
                     'working directory changed')


class WindowsSafety:
    """Query-only Windows job checks and an exclusive live-owner write lease."""

    def __init__(self):
        _require(os.name == 'nt', 'Windows owner lease and job query required')
        from ctypes import wintypes as w
        self.w = w
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateFileW': ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                             w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            'CloseHandle': ([w.HANDLE], w.BOOL),
            'OpenJobObjectW': ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
            'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            'GetFileInformationByHandleEx': ([w.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, w.DWORD], w.BOOL),
            'GetFileType': ([w.HANDLE], w.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    @contextmanager
    def lease(self, path):
        try:
            path = _unlinked(path)
            info = path.stat()
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                     'owner lease must be a regular, singly linked file')
        except OSError as error:
            raise RecoveryError(f'owner lease unavailable: {error}') from error
        # OPEN_EXISTING, FILE_SHARE_READ only: a living owner's write handle
        # makes this fail; recovery likewise excludes another recovery writer.
        handle = self.kernel.CreateFileW(str(path), 0xC0000000, 1, None, 3,
                                        0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            raise RecoveryError('LIVE/BUSY owner lease' if error == 32 else
                                f'owner lease unavailable (Windows error {error})')
        try:
            class Attributes(ctypes.Structure):
                _fields_ = [('attributes', self.w.DWORD), ('tag', self.w.DWORD)]
            info = Attributes()
            _require(self.kernel.GetFileInformationByHandleEx(handle, 9,
                     ctypes.byref(info), ctypes.sizeof(info))
                     and not info.attributes & (0x400 | 0x10)
                     and self.kernel.GetFileType(handle) == 1,
                     'owner lease must be a regular file')
            yield
        finally:
            self.kernel.CloseHandle(handle)

    @contextmanager
    def inactive_job(self, name):
        handle = self.kernel.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY
        if not handle:
            error = ctypes.get_last_error()
            _require(error == 2, f'outer job liveness unknown (Windows error {error})')
            # Recheck absence before reservation too. Do not infer absence from
            # access denied, invalid handle, a query failure, or a missing PID.
            def absent():
                with self.inactive_job(name) as check:
                    # An existing empty object is also safe; querying occurs
                    # on entering that context, without recursing on absence.
                    pass
            yield absent
            return
        try:
            class Accounting(ctypes.Structure):
                _fields_ = [(key, ctypes.c_int64) for key in
                            ('user', 'kernel', 'period_user', 'period_kernel')] + [
                    (key, self.w.DWORD) for key in
                    ('faults', 'total', 'active', 'terminated')]

            def empty():
                info = Accounting()
                _require(self.kernel.QueryInformationJobObject(handle, 1,
                         ctypes.byref(info), ctypes.sizeof(info), None),
                         'outer job query failed; liveness unknown')
                _require(info.active == 0, 'outer job still has active processes')
            empty()
            yield empty
        finally:
            self.kernel.CloseHandle(handle)


def _record(record, fields, run_id, last):
    _require(isinstance(record, dict) and record.keys() == fields,
             'unknown or missing journal fields')
    _require(type(record['version']) is int and record['version'] == 1,
             'invalid journal version')
    _require(record['run_id'] == run_id, 'mixed journal run identities')
    _require(isinstance(record['event'], str), 'invalid event')
    _integer(record['elapsed_ms'], 2**64 - 1, 'elapsed_ms')
    _require(record['elapsed_ms'] >= last, 'journal time went backwards')
    _integer(record['code'], 2**32 - 1, 'code')
    return record['elapsed_ms']


def _attempt(number):
    return {'number': number, 'intent': False, 'ack': False, 'terminated': False,
            'outcome': None, 'code': None, 'pid_code': None}


def _fleet(files, state, plan, identity, *, live=False):
    jobs = {j['id']: {'source': j, 'queued': False, 'attempts': [],
                      'reservations': 0, 'pending': False,
                      'launch_failure': None} for j in plan['jobs']}
    queue_order = iter(jobs)
    prepared, terminal, last, stop_pending = False, None, -1, None
    listening, sealed = False, False
    for record in files.records(state / 'fleet.jsonl'):
        last = _record(record, FLEET_FIELDS, identity, last)
        event, task = record['event'], record['task']
        for field, maximum in (('priority', 100), ('attempt', 4),
                                ('total_commit_bytes', 2**64 - 1),
                                ('available_bytes', 2**64 - 1)):
            _integer(record[field], maximum, field)
        _require(terminal is None, 'records follow fleet terminal event')
        if not prepared:
            _require(event == 'prepared' and task == '', 'missing initial prepared event')
            prepared = True
        else:
            _require(event != 'prepared', 'duplicate prepared event')
        if task == '':
            _require(record['priority'] == 0 and record['attempt'] == 0,
                     'invalid fleet event identity')
            if live and event in {'listening', 'sealed', 'request_rejected'}:
                if event == 'listening':
                    _require(not listening and not sealed and not any(
                        j['queued'] for j in jobs.values()) and record['code'] == 0,
                        'invalid live listening diagnostic')
                    listening = True
                elif event == 'sealed':
                    _require(listening and not sealed and record['code'] == 0,
                             'invalid live sealed diagnostic')
                    sealed = True
                else:
                    _require(listening and record['code'] in {64, 73, 75},
                             'invalid live rejection diagnostic')
                continue
            _require(event in {'prepared', 'sample', 'measurement_unavailable'} | TERMINALS,
                     f'unknown fleet event: {event}')
            if event in TERMINALS:
                terminal = record
            else:
                _require(record['code'] == 0, 'nonzero diagnostic code')
            continue
        _require(isinstance(task, str) and task in jobs, 'unknown task in fleet journal')
        job = jobs[task]
        source, number = job['source'], record['attempt']
        _require(record['priority'] == source.get('priority', 50), 'changed task priority')
        _require(job['launch_failure'] is None, 'immutable launch_failed outcome changed')
        if event == 'queued':
            if live:
                _require(listening and not sealed, 'live queue outside admission window')
            _require(not job['queued'] and number == 0 and record['code'] == 0,
                     'duplicate/invalid queue event')
            _require(next(queue_order, None) == task, 'initial queue order changed')
            job['queued'] = True
            continue
        if live:
            _require(job['queued'], 'task lifecycle before durable queue')
        else:
            _require(all(j['queued'] for j in jobs.values()), 'launch before all durable queues')
        if event == 'launch_failed':
            _require(record['code'] == 70 and stop_pending is None,
                     'invalid launch_failed code or pending stop')
            if not job['attempts'] or job['pending']:
                # Native launch can fail before or after incrementing attempt.
                # Both spellings are terminal failures, never replay authority.
                _require(number in {len(job['attempts']), len(job['attempts']) + 1},
                         'wrong launch_failed attempt number')
            else:
                attempt = job['attempts'][-1]
                _require(number == attempt['number'] and attempt['outcome'] is None
                         and not attempt['intent'],
                         'launch_failed cannot replace an immutable attempt outcome')
                attempt['outcome'], attempt['code'] = event, 70
            job['launch_failure'] = {'attempt': number, 'code': 70}
            job['pending'] = False
            continue
        if event == 'started':
            _require(number == len(job['attempts']) + 1
                     and (not job['attempts'] or job['pending'])
                     and stop_pending is None,
                     'unreserved or duplicate attempt start')
            _require(record['code'] > 0, 'invalid contained process code')
            attempt = _attempt(number)
            attempt['pid_code'] = record['code']
            job['attempts'].append(attempt)
            job['pending'] = False
            continue
        _require(job['attempts'] and not job['pending'], 'event without current attempt')
        attempt = job['attempts'][-1]
        _require(number == attempt['number'], 'wrong attempt number')
        if event == 'resume_queued':
            _require(attempt['outcome'] == 'stopped' and attempt['ack'],
                     'resume reservation without acknowledged stopped attempt')
            job['reservations'] += 1
            left = source.get('max_resumes', 0) - job['reservations']
            _require(source.get('allow_stop', False) and source.get('resume_argv')
                     and left >= 0 and record['code'] == left,
                     'resume reservation exceeds or contradicts bounded allowance')
            job['pending'] = True
            continue
        _require(attempt['outcome'] is None, 'immutable attempt outcome changed')
        if event == 'pressure_intent':
            _require(source.get('allow_stop', False) and not attempt['intent']
                     and record['code'] == 125 and stop_pending is None,
                     'invalid/overlapping pressure intent')
            attempt['intent'] = True
            stop_pending = task
        elif event == 'checkpoint_acknowledged':
            _require(attempt['intent'] and not attempt['ack'] and record['code'] == 0,
                     'invalid checkpoint acknowledgement')
            attempt['ack'] = True
        elif event == 'terminated':
            _require(attempt['intent'] and not attempt['terminated']
                     and record['code'] == 125, 'invalid termination event')
            attempt['terminated'] = True
        elif event in {'completed', 'stopped'}:
            _require((event == 'stopped') == attempt['intent'],
                     'stopped work cannot become normal completion')
            if event == 'stopped':
                _require(stop_pending == task, 'stopped task has no pending pressure intent')
                stop_pending = None
            attempt['outcome'], attempt['code'] = event, record['code']
        else:
            raise RecoveryError(f'unknown task event: {event}')
    _require(prepared, 'empty fleet journal')
    _require(all(j['queued'] for j in jobs.values()), 'missing durable task queue')
    return jobs, terminal


def _attempt_evidence(files, directory, fleet_attempt, nonces):
    phase, intent, ack, termination, outcome, code = 'new', False, False, False, None, None
    nonce, last, pid_code = None, -1, None
    for record in files.records(directory / 'events.jsonl'):
        if nonce is None:
            _require(isinstance(record, dict), 'invalid attempt record')
            nonce = _uuid(record.get('run_id'))
            _require(nonce not in nonces, 'attempt nonce reused')
            nonces.add(nonce)
        last = _record(record, ATTEMPT_FIELDS, nonce, last)
        _integer(record['job_commit_bytes'], 2**64 - 1, 'job_commit_bytes')
        _integer(record['active_processes'], 2**32 - 1, 'active_processes')
        event = record['event']
        _require(outcome is None, 'records follow immutable attempt outcome')
        if phase == 'new':
            _require(event == 'prepared' and record['active_processes'] == 0
                     and record['code'] == 0, 'attempt must start prepared')
            phase = 'prepared'
        elif event == 'contained':
            _require(phase == 'prepared' and record['active_processes'] > 0
                     and record['code'] > 0, 'invalid contained event')
            pid_code, phase = record['code'], 'running'
        elif event == 'checkpoint_intent':
            _require(phase == 'running' and not intent and record['code'] == 125,
                     'invalid per-attempt checkpoint intent')
            intent = True
        elif event == 'checkpoint_acknowledged':
            _require(phase == 'running' and intent and not ack and record['code'] == 0,
                     'invalid per-attempt acknowledgement')
            ack = True
        elif event == 'terminate_intent':
            _require(phase == 'running' and intent and not termination
                     and record['code'] == 125, 'invalid per-attempt termination')
            termination = True
        elif event in {'completed', 'stopped'}:
            _require(phase == 'running' and record['active_processes'] == 0
                     and ((event == 'stopped') == intent), 'invalid attempt outcome')
            outcome, code = event, record['code']
        else:
            raise RecoveryError(f'unknown per-attempt event: {event}')
    _require(phase == 'running' and pid_code == fleet_attempt['pid_code'],
             'missing/contradictory per-attempt containment')
    _require(intent == fleet_attempt['intent'], 'fleet/attempt checkpoint intent mismatch')
    _require(not fleet_attempt['ack'] or ack, 'fleet ack missing from attempt journal')
    _require(not fleet_attempt['terminated'] or termination,
             'fleet termination missing from attempt journal')
    if fleet_attempt['terminated'] or fleet_attempt['outcome'] is not None:
        _require(ack == fleet_attempt['ack'],
                 'later fleet event skipped a durable attempt acknowledgement')
    if fleet_attempt['outcome'] is not None:
        _require(outcome == fleet_attempt['outcome'] and code == fleet_attempt['code'],
                 'fleet/attempt immutable outcome mismatch')
        _require(termination == fleet_attempt['terminated'],
                 'fleet outcome skipped a durable attempt termination')
    exact = False
    if intent:
        expected = f'headroom-stop-v1\n{nonce}\n'.encode('ascii')
        exact = (files.read(directory / 'stop.request', 128) == expected
                 and files.read(directory / 'stop.ack', 128) == expected)
        _require(exact, 'missing or nonmatching exact attempt request/ack')
    else:
        _require(not any(os.path.lexists(directory / name)
                         for name in ('stop.request', 'stop.ack')),
                 'stop files without durable checkpoint intent')
    _require(not ack or exact, 'journal acknowledgement lacks exact bytes')
    return {'checkpoint': exact, 'outcome': outcome, 'code': code}


def _classify(files, state, jobs, identity):
    eligible, skipped, blocked, replacement = [], [], [], []
    nonces = {identity}
    for task, job in jobs.items():
        source = job['source']
        try:
            _require(job['launch_failure'] is None,
                     'launch_failed (code 70) is never replayed automatically')
            directory = state / task
            attempts = job['attempts']
            if not attempts:
                _require(not os.path.lexists(directory),
                         'unstarted queue has an unjournaled attempt directory')
                # Track the parent to catch a launch directory appearing later.
                item = {'id': task, 'mode': 'original', 'reason': 'durably queued; never started'}
                replacement.append(copy.deepcopy(source))
                eligible.append(item)
                continue
            expected = {f'attempt-{a["number"]}' for a in attempts}
            _require(files.directory(directory) == expected,
                     'missing or unjournaled attempt directory')
            evidence = []
            for attempt in attempts:
                evidence.append(_attempt_evidence(files,
                    directory / f'attempt-{attempt["number"]}', attempt, nonces))
            current = attempts[-1]
            if current['outcome'] == 'completed':
                _require(current['code'] == 0, 'normal failure is never replayed')
                skipped.append({'id': task, 'reason': 'durably completed with code zero'})
                continue
            _require(evidence[-1]['outcome'] != 'completed',
                     'normal per-attempt completion is not resumable')
            _require(source.get('allow_stop', False) and source.get('resume_argv'),
                     'explicit stoppable resume recipe required')
            _require(evidence[-1]['checkpoint'], 'interrupted work has no exact checkpoint ack')
            remaining = source.get('max_resumes', 0) - job['reservations']
            if not job['pending']:
                _require(remaining > 0, 'bounded resume allowance exhausted')
                remaining -= 1
            _require(remaining >= 0, 'bounded resume allowance exhausted')
            recovered = copy.deepcopy(source)
            recovered['argv'] = copy.deepcopy(source['resume_argv'])
            recovered['max_resumes'] = remaining
            replacement.append(recovered)
            eligible.append({'id': task, 'mode': 'resume', 'max_resumes': remaining,
                             'reason': 'exact checkpoint; existing reservation' if job['pending']
                             else 'exact checkpoint; one resume allowance consumed'})
        except (RecoveryError, OSError, ValueError, UnicodeError, RecursionError) as error:
            blocked.append({'id': task, 'reason': str(error)})
    return eligible, skipped, blocked, replacement


def _publish_new(path, payload):
    descriptor, temporary = tempfile.mkstemp(prefix='.headroom-recovery-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            os.rename(temporary, path)  # Atomic; refuses existing destination.
        else:
            os.link(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _reserve(state, identity, output, *, plan_json=None):
    reservation = {'version': 1, 'run_id': identity, 'output_path': str(output),
                   'recovery_id': str(uuid.uuid4())}
    if plan_json is not None:  # Live recovery publishes a batch JSON companion.
        reservation['plan_json_path'] = str(plan_json)
    # No cleanup: any failed write/flush leaves an intentional manual-review marker.
    with (state / 'recovery-reserved.json').open('xb') as stream:
        stream.write((json.dumps(reservation, sort_keys=True) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    return reservation


def prepare_recovery(plan_path, old_state, output_path, *, safety=None):
    """Return an explicit verdict; only a fully eligible batch publishes output.

    ``safety`` is an injectable lease/query adapter for offline unit tests. The
    CLI always uses WindowsSafety. This function never executes a workload.
    """
    report = {'version': 1, 'status': 'refused', 'run_id': None, 'eligible': [],
              'skipped': [], 'blocked': [], 'output': None, 'reservation': None,
              'error': None, 'working_directory': None}
    plan = None
    try:
        files = _Files()
        plan_path = _unlinked(plan_path)
        state = _unlinked(old_state)
        _require(state.is_dir(), 'old state must be a directory')
        output = _local_path(output_path)
        _unlinked(output.parent)
        _require(output.parent.is_dir(), 'output parent must be a directory')
        output_parent = _signature(output.parent.stat())
        output_parent_names = _directory_names(output.parent, 4096)
        _require(not output.is_relative_to(state), 'output must be outside old state')
        _require(output != plan_path and not os.path.lexists(output), 'output already exists')
        plan = _strict_json(files.read(plan_path, MAX_FILE))
        compiled = headroom.compile_plan(plan)
        safety = safety if safety is not None else WindowsSafety()
        with safety.lease(state / 'owner.lock'):
            _require(not os.path.lexists(state / 'recovery-reserved.json'),
                     'duplicate recovery reservation; manual review required')
            # A root snapshot also detects unjournaled attempt-directory creation.
            files.directory(state)
            _require(files.read(state / 'plan.hrp', MAX_FILE) == compiled,
                     'compiled input does not exactly match persisted plan.hrp')
            owner = _strict_json(files.read(state / 'owner.json', MAX_LINE))
            _require(isinstance(owner, dict) and owner.keys() == {'version', 'run_id', 'job_name'}
                     and type(owner['version']) is int and owner['version'] == 1,
                     'invalid owner metadata')
            identity = _uuid(owner['run_id'])
            report['run_id'] = identity
            _require(owner['job_name'] == 'Local\\Headroom-' + identity,
                     'owner root job name does not match run UUID')
            report['working_directory'] = _working_directory(files, state)
            with safety.inactive_job(owner['job_name']) as verify_empty:
                jobs, terminal = _fleet(files, state, plan, identity)
                eligible, skipped, blocked, replacement = _classify(files, state, jobs, identity)
                report.update(eligible=eligible, skipped=skipped, blocked=blocked)
                if terminal is not None:
                    successful = (terminal['event'] == 'finished' and terminal['code'] == 0
                                  and len(skipped) == len(plan['jobs']) and not blocked)
                    _require(successful, 'terminal fleet policy: final run requires manual review')
                _require(not blocked, 'one or more tasks blocked; no partial recovery')
                files.unchanged()
                verify_empty()
                if not replacement:
                    report['status'] = 'no_work'
                    return report
                recovered = copy.deepcopy(plan)
                recovered['jobs'] = replacement
                payload = headroom.compile_plan(recovered)
                _require(headroom.compile_plan(plan) == compiled,
                         'original executable resolution changed during recovery')
                # Revalidation precedes durable reservation. Re-read identities
                # and query liveness immediately before that irreversible step.
                files.unchanged()
                verify_empty()
                _unlinked(output.parent)
                _require(_signature(output.parent.stat()) == output_parent
                         and _directory_names(output.parent, 4096) == output_parent_names,
                         'output parent changed during recovery')
                _require(not os.path.lexists(output), 'output already exists')
                report['reservation'] = _reserve(state, identity, output)
                _publish_new(output, payload)
                report.update(status='prepared', output=str(output))
    except (RecoveryError, OSError, ValueError, UnicodeError, RecursionError) as error:
        report['error'] = str(error)
        if plan is not None and isinstance(plan, dict) and isinstance(plan.get('jobs'), list):
            classified = {j['id'] for key in ('eligible', 'skipped', 'blocked')
                          for j in report[key]}
            for job in plan['jobs']:
                if isinstance(job, dict) and isinstance(job.get('id'), str) and job['id'] not in classified:
                    report['blocked'].append({'id': job['id'], 'reason': str(error)})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    parser.add_argument('old_state')
    parser.add_argument('output')
    arguments = parser.parse_args(argv)
    report = prepare_recovery(arguments.plan, arguments.old_state, arguments.output)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 73 if report['status'] == 'refused' else 0


if __name__ == '__main__':
    sys.exit(main())
