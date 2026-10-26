"""Owned, bounded synthetic workload. No agent account or credential is used."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main():
    mode = sys.argv[1]
    output = Path(sys.argv[2])
    control = Path(os.environ['HEADROOM_CONTROL_DIR'])
    output.mkdir(exist_ok=True)
    save(output / (mode + '-pid.json'), os.getpid())
    if mode == 'argv':
        save(output / 'argv.json', sys.argv[3:])
        return 0
    if mode == 'normal':
        time.sleep(0.5)
        return int(sys.argv[3]) if len(sys.argv) > 3 else 0
    if mode == 'resume':
        checkpoint = json.loads((output / 'checkpoint.json').read_text())
        start = checkpoint['progress']
        for progress in range(start, 20):
            time.sleep(0.01)
        save(output / 'resumed.json', {'started_from': start, 'completed': 20})
        return 0
    if mode in ('spawn', 'leader-exits'):
        child = subprocess.Popen([sys.executable, __file__, 'stubborn', str(output)])
        save(output / 'child-pid.json', child.pid)
        if mode == 'leader-exits':
            return 0
    if mode == 'breakaway':
        try:
            child = subprocess.Popen(
                [sys.executable, __file__, 'normal', str(output)],
                creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,
            )
        except OSError:
            save(output / 'breakaway.json', {'refused': True})
            return 0
        child.wait(timeout=5)
        save(output / 'breakaway.json', {'refused': False})
        return 1
    blocks = []
    progress = 0
    forged_ack_published = False
    maximum = 96 if mode in ('cooperative', 'stubborn', 'spawn', 'forged-ack', 'allocation') else 0
    until = time.monotonic() + (1.5 if mode == 'observe' else 30)
    while time.monotonic() < until:
        request = control / 'stop.request'
        if request.exists() and mode == 'cooperative':
            body = request.read_bytes()
            if body.startswith(b'headroom-stop-v1\n') and body.endswith(b'\n'):
                save(output / 'checkpoint.json', {'blocks': len(blocks), 'progress': progress, 'pid': os.getpid()})
                # Ack only after application-owned data is flushed. A real
                # application needs its own safe-point and restart contract.
                with (control / 'stop.ack').open('wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                return 0
        if request.exists() and mode == 'forged-ack' and not forged_ack_published:
            # Publish one wrong nonce, then remain alive through grace. Rewriting
            # on every loop races read-sharing handles and can crash the fixture
            # before the supervisor's required forced-stop observation.
            with (control / 'stop.ack').open('xb') as stream:
                stream.write(b'headroom-stop-v1\nwrong-run\n')
                stream.flush()
                os.fsync(stream.fileno())
            forged_ack_published = True
        if len(blocks) * 8 < maximum:
            try:
                blocks.append(bytearray(8 * 1024 * 1024))
            except MemoryError:
                save(output / 'allocation-denied.json', {'blocks': len(blocks)})
                return 71
        if mode == 'observe' and not blocks:
            blocks.append(bytearray(48 * 1024 * 1024))
        progress += 1
        time.sleep(0.1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
