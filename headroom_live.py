"""Finite, offline live-intake client. Receipts confirm admission, not completion."""

import argparse
from contextlib import ExitStack, contextmanager
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

import headroom


MAX_PLAN_BYTES = 512 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 4096
DEFAULTS = dict(reserve_mib=256, sustain_ms=500, grace_ms=2000,
                timeout_ms=60000)
SETTING_KEYS = headroom.ROOT_KEYS - {'jobs'}


class LiveError(ValueError):
    """Invalid input (64) or a refused operation (73)."""

    def __init__(self, message, code=64):
        super().__init__(message)
        self.code = code


def _api(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LiveError:
            raise
        except OSError as error:
            raise LiveError(str(error), 73) from error
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            raise LiveError(str(error)) from error
    return checked


def _settings(settings, *, complete=False):
    headroom._object(settings, SETTING_KEYS,
                     SETTING_KEYS if complete else {'budget_mib', 'hard_mib'},
                     'settings')
    result = {**DEFAULTS, **settings}
    for key, low, high in (
            ('budget_mib', 1, headroom.MAX_MIB),
            ('hard_mib', 1, headroom.MAX_MIB),
            ('reserve_mib', 0, headroom.MAX_MIB),
            ('sustain_ms', 100, 60000), ('grace_ms', 1, 60000),
            ('timeout_ms', 1, 86400000)):
        headroom._integer(result[key], low, high, key)
    if result['hard_mib'] <= result['budget_mib']:
        raise LiveError('hard_mib must exceed budget_mib')
    return result


@_api
def compile_session(settings):
    """Compile strict root settings to an HRFLEET1 header with zero jobs."""
    value = _settings(settings)
    return headroom.HEADER.pack(
        b'HRFLEET1', 0, value['budget_mib'] * headroom.MIB,
        value['hard_mib'] * headroom.MIB, value['reserve_mib'] * headroom.MIB,
        value['sustain_ms'], value['grace_ms'], value['timeout_ms'])


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise LiveError('identity must be a lowercase canonical UUID')
    return value


def _directory(path):
    # Inspect ancestors before any read/write through the supplied path.
    for component in [*reversed(path.parents), path]:
        headroom._log_file_info(component.lstat(), component, directory=True)
    return path


def _read(path, limit, *, optional=False):
    _directory(path.parent)
    with ExitStack() as stack:
        snapshot = headroom._log_open(stack, path, limit, optional=optional)
        if snapshot is None:
            return None
        body = snapshot[1].read(limit + 1)
        if len(body) > limit or len(body) != snapshot[2].st_size:
            raise LiveError('file exceeded its size bound or changed during read', 73)
        headroom._log_verify(snapshot)
        _directory(path.parent)
        return body


def _json(path, limit):
    return headroom.strict_json_loads(_read(path, limit))


def _session(state):
    path = _directory(headroom._log_state_path(state))
    value = _json(path / 'session.json', MAX_METADATA_BYTES)
    keys = {'version', 'run_id', 'max_jobs', 'settings'}
    headroom._object(value, keys, keys, 'session.json')
    headroom._integer(value['version'], 1, 1, 'session.version')
    headroom._integer(value['max_jobs'], 64, 64, 'session.max_jobs')
    _uuid(value['run_id'])
    _settings(value['settings'], complete=True)
    for name in ('inbox', 'processed', 'receipts'):
        _directory(path / name)
    return path, value


def _pending(session, request_id):
    return dict(version=1, run_id=session['run_id'], request_id=request_id,
                task='', status='pending', code=75, ordinal=0)


def _receipt(path, session, request_id):
    raw = _read(path / 'receipts' / f'{request_id}.json',
                MAX_METADATA_BYTES, optional=True)
    if raw is None:
        return _pending(session, request_id)
    value = headroom.strict_json_loads(raw)
    keys = {'version', 'run_id', 'request_id', 'task', 'status', 'code', 'ordinal'}
    headroom._object(value, keys, keys, 'receipt')
    headroom._integer(value['version'], 1, 1, 'receipt.version')
    if (value['run_id'] != session['run_id']
            or value['request_id'] != request_id):
        raise LiveError('receipt identity mismatch')
    task = value['task']
    if (type(task) is not str or (task and (
            not headroom.ID_PATTERN.fullmatch(task)
            or task in headroom.RESERVED_IDS))):
        raise LiveError('invalid receipt task')
    headroom._integer(value['code'], 0, 75, 'receipt.code')
    headroom._integer(value['ordinal'], 0, 64, 'receipt.ordinal')
    if value['status'] == 'accepted':
        if value['code'] != 0 or not 1 <= value['ordinal'] <= 64 or not task:
            raise LiveError('invalid accepted receipt')
    elif value['status'] == 'rejected':
        if value['code'] not in {64, 73, 75} or value['ordinal'] != 0:
            raise LiveError('invalid rejected receipt')
    else:
        raise LiveError('unknown receipt status')
    return value


@_api
def read_receipt(state, request_id):
    """Return a strict native receipt, or a session-bound pending result."""
    _uuid(request_id)
    path, session = _session(state)
    return _receipt(path, session, request_id)


def _payloads(session, job, request_id):
    original = {**session['settings'], 'jobs': [job]}
    compiled = headroom.compile_plan(original)
    # Round-trip makes a private JSON-only copy and bounds the persisted input.
    raw = json.dumps(original, ensure_ascii=True, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('ascii')
    if len(raw) > MAX_JSON_BYTES:
        raise LiveError('plan.json exceeds 1 MiB')
    canonical = headroom.strict_json_loads(raw)
    for key in ('argv', 'resume_argv'):
        argv = canonical['jobs'][0].get(key)
        if argv:
            argv[0] = headroom._executable(argv[0], f'job.{key}[0]')
    if headroom.compile_plan(canonical) != compiled:
        raise LiveError('executable resolution changed during compilation', 73)
    raw = json.dumps(canonical, ensure_ascii=True, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('ascii')
    if len(compiled) > MAX_PLAN_BYTES or len(raw) > MAX_JSON_BYTES:
        raise LiveError('request exceeds plan size limits')
    target = f'headroom-submit-v1\n{session["run_id"]}\n{request_id}\n'.encode('ascii')
    return {'target': target, 'plan.hrp': compiled, 'plan.json': raw}


def _existing(path, request_id, payloads):
    found = False
    # Native movement is inbox -> processed: scan in this order so a move
    # between the two checks cannot hide an existing published request.
    for folder in (path / 'inbox' / f'{request_id}.ready',
                   path / 'processed' / request_id):
        try:
            folder.lstat()
        except FileNotFoundError:
            continue
        _directory(folder)
        for name, expected in payloads.items():
            limit = {'target': 128, 'plan.hrp': MAX_PLAN_BYTES,
                     'plan.json': MAX_JSON_BYTES}[name]
            if _read(folder / name, limit) != expected:
                raise LiveError('request UUID already has different content', 73)
        found = True
    return found


@contextmanager
def _publication_guard(path, request_id):
    # Native may claim a request while a second client is compiling. Serialize
    # publishers of the same identity so an inbox->processed move cannot let
    # two clients publish different bytes. No lock files or resident process.
    if os.name != 'nt':
        raise LiveError('request publication requires Windows nonreplacing rename', 73)
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    create.restype = ctypes.c_void_p
    release = kernel.ReleaseMutex
    release.argtypes = [ctypes.c_void_p]
    release.restype = ctypes.c_int
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    identity = os.path.normcase(str(path)) + '\n' + request_id
    name = 'Local\\headroom-live-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
    ctypes.set_last_error(0)
    handle = create(None, True, name)
    error = ctypes.get_last_error()
    if not handle:
        raise LiveError(f'cannot guard request publication: Windows error {error}', 73)
    if error == 183:  # ERROR_ALREADY_EXISTS: conservatively refuse, never wait.
        close(handle)
        raise LiveError('concurrent publication of this request; retry', 73)
    try:
        yield
    finally:
        release(handle)
        close(handle)


def _publish(path, request_id, payloads):
    with _publication_guard(path, request_id):
        _publish_guarded(path, request_id, payloads)


def _publish_guarded(path, request_id, payloads):
    if _existing(path, request_id, payloads):
        return
    staging = Path(tempfile.mkdtemp(prefix='.headroom-live-', dir=path / 'inbox'))
    try:
        for name, body in payloads.items():
            headroom._write_new(staging / name, body)
        _directory(staging)
        if _existing(path, request_id, payloads):
            return
        os.rename(staging, path / 'inbox' / f'{request_id}.ready')
    finally:
        # Never touch published/processed inputs or recursively remove a tree.
        if staging.exists():
            _directory(staging)
            for name in payloads:
                try:
                    (staging / name).unlink()
                except FileNotFoundError:
                    pass
            staging.rmdir()


@_api
def submit(state, job, request_id=None, wait_seconds=5):
    """Publish one immutable request and wait at most 30 seconds for admission."""
    if (type(wait_seconds) not in (int, float)
            or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 30):
        raise LiveError('wait_seconds must be finite and in 0..30')
    request_id = _uuid(str(uuid.uuid4()) if request_id is None else request_id)
    path, session = _session(state)
    payloads = _payloads(session, job, request_id)
    _publish(path, request_id, payloads)
    deadline = time.monotonic() + wait_seconds
    while True:
        value = _receipt(path, session, request_id)
        if value['status'] != 'pending':
            if value['task'] and value['task'] != job['id']:
                raise LiveError('receipt task differs from submitted job')
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return value
        time.sleep(min(0.1, remaining))


@_api
def seal(state):
    """Request a barrier without claiming the session has completed."""
    path, session = _session(state)
    payload = f'headroom-seal-v1\n{session["run_id"]}\n'.encode('ascii')
    target = path / 'seal.request'
    existing = _read(target, 128, optional=True)
    if existing is None:
        try:
            headroom._write_new(target, payload)
        except FileExistsError:
            existing = _read(target, 128)
    if existing is not None and existing != payload:
        raise LiveError('existing seal request differs', 73)
    return dict(version=1, run_id=session['run_id'], status='seal_requested')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('settings')
    init.add_argument('output')
    send = commands.add_parser('submit')
    send.add_argument('state')
    send.add_argument('job')
    send.add_argument('--request-id')
    send.add_argument('--wait-seconds', type=float, default=5)
    receipt = commands.add_parser('receipt')
    receipt.add_argument('state')
    receipt.add_argument('request_id')
    barrier = commands.add_parser('seal')
    barrier.add_argument('state')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            payload = compile_session(_json(Path(args.settings).absolute(), MAX_JSON_BYTES))
            output = headroom._log_state_path(args.output)
            _directory(output.parent)
            headroom._write_new(output, payload)
            return 0
        if args.command == 'submit':
            result = submit(args.state, _json(Path(args.job).absolute(), MAX_JSON_BYTES),
                            args.request_id, args.wait_seconds)
        elif args.command == 'receipt':
            result = read_receipt(args.state, args.request_id)
        else:
            result = seal(args.state)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return {'accepted': 0, 'pending': 75, 'rejected': 73,
                'seal_requested': 0}[result['status']]
    except (LiveError, headroom.PlanError, OSError, ValueError,
            TypeError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return error.code if isinstance(error, LiveError) else (73 if isinstance(error, OSError) else 64)


if __name__ == '__main__':
    sys.exit(main())
