"""Bounded SHA256 manifests with file-boundary checkpoints. See README.md."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

MAX_STATE = 2 * 1024 * 1024
NONCE = re.compile(rb'headroom-stop-v1\n([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\n')
HEX = re.compile(r'[0-9a-f]{64}')


class Refusal(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def path_checked(value, *, exists=True):
    raw = Path(value)
    # Check before abspath: Windows can silently trim trailing dots/spaces.
    if os.name == 'nt':
        require(not str(raw).startswith(('\\\\', '//')), 'local paths required')
        devices = {'CON', 'PRN', 'AUX', 'NUL'} | {
            prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789¹²³'}
        for part in raw.parts:
            if part == raw.anchor or part in ('.', '..'):
                continue
            require(not any(c in part for c in ':<>"|?*')
                    and not part.endswith(('.', ' '))
                    and part.split('.')[0].upper() not in devices, 'ambiguous/device path')
    path = Path(os.path.abspath(raw))
    for component in (*reversed(path.parents), *((path,) if exists else ())):
        info = component.lstat()
        require(not stat.S_ISLNK(info.st_mode)
                and not getattr(info, 'st_file_attributes', 0) & 0x400,
                'symlink/reparse paths refused')
    return path


def metadata(info):
    # Windows 3.12 stat/fstat disagree on ctime; birthtime agrees on both.
    stamp = getattr(info, 'st_birthtime_ns', None) if os.name == 'nt' else info.st_ctime_ns
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'identity_ns': stamp, 'device': info.st_dev, 'inode': info.st_ino}


def regular(path):
    path = path_checked(path)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode), 'only regular files are accepted')
    return info


def read_small(path, limit=MAX_STATE):
    before = regular(path)
    require(before.st_nlink == 1, 'linked control/checkpoint file refused')
    with path.open('rb') as stream:
        require(metadata(os.fstat(stream.fileno())) == metadata(before), 'file changed on open')
        data = stream.read(limit + 1)
        require(len(data) <= limit, 'oversized checkpoint/control file')
        require(metadata(os.fstat(stream.fileno())) == metadata(before), 'file changed on read')
    require(metadata(regular(path)) == metadata(before), 'file changed on read')
    return data


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate checkpoint key')
            result[key] = value
        return result
    def constant(value):
        raise Refusal('non-JSON number in checkpoint')
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def atomic(path, data, *, replace=False):
    require(len(data) <= MAX_STATE, 'state exceeds bounded metadata limit')
    if os.path.lexists(path):
        require(replace and regular(path).st_nlink == 1, 'output already exists or is aliased')
    descriptor, temporary = tempfile.mkstemp(prefix='.hash-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        elif os.name == 'nt':
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def checkpoint(inputs, completed, nonce=None):
    payload = {'version': 1, 'inputs': inputs, 'completed': completed, 'stop_nonce': nonce}
    return {**payload, 'integrity': hashlib.sha256(encode(payload)).hexdigest()}


def load_checkpoint(old, inputs):
    value = decode(read_small(old / 'checkpoint.json'))
    require(isinstance(value, dict) and value.keys() == {
        'version', 'inputs', 'completed', 'stop_nonce', 'integrity'}, 'invalid checkpoint fields')
    require(type(value['version']) is int and value['version'] == 1, 'invalid checkpoint version')
    payload = {k: v for k, v in value.items() if k != 'integrity'}
    require(value['integrity'] == hashlib.sha256(encode(payload)).hexdigest(),
            'corrupt checkpoint integrity')
    require(encode(value['inputs']) == encode(inputs), 'input set or metadata changed')
    completed = value['completed']
    require(isinstance(completed, list) and len(completed) <= len(inputs), 'invalid completed progress')
    require(all(isinstance(digest, str) and HEX.fullmatch(digest) for digest in completed),
            'invalid completed checksum')
    nonce = value['stop_nonce']
    require(nonce is None or (isinstance(nonce, str) and
            NONCE.fullmatch(('headroom-stop-v1\n' + nonce + '\n').encode('utf-8'))),
            'invalid checkpoint nonce')
    return list(completed)


def verify_inputs(inputs):
    for item in inputs:
        require(metadata(regular(Path(item['path']))) == item['metadata'], 'input metadata changed')


def stop_request(control):
    if control is None:
        return None
    path = control / 'stop.request'
    if not os.path.lexists(path):
        return None
    deadline = time.monotonic() + 0.2
    while True:
        try:
            data = read_small(path, 128)
            break
        except PermissionError as error:
            # Python's Windows CRT may report only EACCES, not winerror32.
            # A bounded app-side wait does not extend the guardian's grace.
            code = getattr(error, 'winerror', None)
            retriable = code in (5, 32, 33) or (code is None and error.errno == errno.EACCES)
            if os.name != 'nt' or not retriable or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    require(NONCE.fullmatch(data), 'malformed stop.request; no acknowledgement written')
    return data


def run(files, state, *, resume=None, chunk_bytes=1024 * 1024, control=None):
    require(type(chunk_bytes) is int and 1024 <= chunk_bytes <= 1024 * 1024,
            'chunk bytes must be 1024..1048576')
    require(1 <= len(files) <= 1024, 'provide 1..1024 explicit input files')
    state = path_checked(state, exists=False)
    require(not os.path.lexists(state), 'state directory must be new')
    require(state.parent.is_dir(), 'state parent must exist')
    inputs, identities = [], set()
    for name in files:
        path = path_checked(name)
        info = regular(path)
        identity = (info.st_dev, info.st_ino)
        require(identity not in identities, 'duplicate/aliased input')
        identities.add(identity)
        require(not path.is_relative_to(state), 'input/output alias')
        inputs.append({'path': str(path), 'metadata': metadata(info)})
    inputs.sort(key=lambda item: (os.path.normcase(item['path']), item['path']))
    completed = []
    if resume is not None:
        old = path_checked(resume)
        require(old.is_dir() and not state.is_relative_to(old) and not old.is_relative_to(state),
                'resume and new state directories must be separate')
        completed = load_checkpoint(old, inputs)
    if control is not None:
        control = path_checked(control)
        require(control.is_dir(), 'control directory must exist')
    initial = encode(checkpoint(inputs, completed))
    require(len(initial) <= MAX_STATE, 'too much checkpoint metadata')
    verify_inputs(inputs)
    state.mkdir()
    atomic(state / 'checkpoint.json', initial)

    def halt_if_requested():
        request = stop_request(control)
        if request is None:
            return False
        verify_inputs(inputs)
        nonce = NONCE.fullmatch(request).group(1).decode('ascii')
        atomic(state / 'checkpoint.json', encode(checkpoint(inputs, completed, nonce)), replace=True)
        require(stop_request(control) == request, 'stop request changed before acknowledgement')
        # The only write outside --state is this explicitly supplied protocol
        # control directory. Ack follows flushed, atomically published state.
        atomic(control / 'stop.ack', request)
        return True

    for item in inputs[len(completed):]:
        if halt_if_requested():
            return 'checkpointed'
        path = Path(item['path'])
        digest = hashlib.sha256()
        require(metadata(regular(path)) == item['metadata'], 'input metadata changed')
        with path.open('rb') as stream:
            require(metadata(os.fstat(stream.fileno())) == item['metadata'], 'input changed on open')
            while True:
                if halt_if_requested():
                    return 'checkpointed'  # In-flight file restarts, digest is not serialized.
                block = stream.read(chunk_bytes)
                if not block:
                    break
                digest.update(block)
            require(metadata(os.fstat(stream.fileno())) == item['metadata'], 'input changed while hashing')
        require(metadata(regular(path)) == item['metadata'], 'input changed while hashing')
        completed.append(digest.hexdigest())
        atomic(state / 'checkpoint.json', encode(checkpoint(inputs, completed)), replace=True)
    if halt_if_requested():
        return 'checkpointed'
    verify_inputs(inputs)
    manifest = {'version': 1, 'algorithm': 'sha256', 'files': [
        {'path': item['path'], 'size': item['metadata']['size'], 'sha256': digest}
        for item, digest in zip(inputs, completed)]}
    atomic(state / 'manifest.json', encode(manifest))
    return 'completed'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, help='new application output directory')
    parser.add_argument('--resume', metavar='OLD_STATE', help='validate and reuse completed file digests')
    parser.add_argument('--chunk-bytes', type=int, default=1024 * 1024)
    parser.add_argument('files', nargs='+', help='explicit regular files; use -- before dash-prefixed paths')
    args = parser.parse_args(argv)
    try:
        result = run(args.files, args.state, resume=args.resume, chunk_bytes=args.chunk_bytes,
                     control=os.environ.get('HEADROOM_CONTROL_DIR'))
        print(json.dumps({'status': result, 'state': str(Path(args.state).absolute())}))
        return 75 if result == 'checkpointed' else 0
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(json.dumps({'status': 'refused', 'error': str(error)}))
        return 73


if __name__ == '__main__':
    sys.exit(main())
