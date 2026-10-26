"""Deterministic checkpoint-before-crash window; <=96 MiB synthetic payload."""
import argparse
import json
import os
from pathlib import Path
import time


def save(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        stream.write(json.dumps(data).encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(exist_ok=True)
    checkpoint = args.output / 'checkpoint.json'
    if args.resume:
        progress = json.loads(checkpoint.read_text())['progress']
        save(args.output / 'resumed.json', {'started_from': progress, 'completed': 100})
        return 0
    control = Path(os.environ['HEADROOM_CONTROL_DIR'])
    save(args.output / 'ready.json', {'pid': os.getpid()})
    blocks = []
    for progress in range(1, 200):
        if (control / 'stop.request').exists():
            body = (control / 'stop.request').read_bytes()
            parts = body.split(b'\n')
            if len(parts) == 3 and parts[0] == b'headroom-stop-v1' and parts[1] and not parts[2]:
                save(checkpoint, {'progress': progress})
                with (control / 'stop.ack').open('wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                # The test kills only its own coordinator after a durable ack.
                # Without that test action the normal grace stops this fixture.
                time.sleep(10)
                return 0
        if len(blocks) < 12:
            blocks.append(bytearray(8 * 1024 * 1024))
        time.sleep(0.1)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
