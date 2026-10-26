"""Fixed logical work with disposable scratch; never allocates from host capacity.

Only this fixture understands its checkpoint. Scratch is a cache of work since
the current restart, not recoverable application state. No duration-based score.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import time

MIB = 1024 * 1024
STEPS = 120
STEP_SECONDS = 0.025
ROLES = ("protected", "growing", "small")


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def save(path, value):
    atomic_bytes(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def advance(checksum, role, step):
    return hashlib.sha256(bytes.fromhex(checksum) + f"{role}:{step}".encode()).hexdigest()


def checksum_at(role, progress):
    checksum = hashlib.sha256(("headroom-fixed-work-v1:" + role).encode()).hexdigest()
    for step in range(1, progress + 1):
        checksum = advance(checksum, role, step)
    return checksum


def read_checkpoint(path, role):
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    progress = saved.get("progress")
    if (saved.get("version") != 1 or saved.get("role") != role or
            saved.get("goal_steps") != STEPS or type(progress) is not int or
            not 0 <= progress < STEPS or
            saved.get("checksum") != checksum_at(role, progress)):
        raise ValueError("checkpoint is not valid for this exact logical work")
    return saved


def wait_barrier(path):
    deadline = time.monotonic() + 8
    while path and not Path(path).is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("bounded start barrier expired")
        time.sleep(0.01)


def read_stop_request(path, retry_seconds=0.2):
    """Published files can briefly be sharing-locked on Windows.

    Retry only recognized Windows access/sharing errors within a fixed app-side
    bound. No acknowledgment is produced without reading the exact bytes. The
    native guardian's grace/ownership enforcement remains independent of us.
    """
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            return Path(path).read_bytes()
        except PermissionError as error:
            windows_error = getattr(error, 'winerror', None)
            retriable = windows_error in (5, 32, 33) or (
                windows_error is None and error.errno == errno.EACCES)
            if (os.name != 'nt' or not retriable
                    or time.monotonic() >= deadline):
                raise
            time.sleep(0.01)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=(*ROLES, "hold"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--start-barrier", type=Path)
    parser.add_argument("--hold-seconds", type=float, default=6.0)
    parser.add_argument("--hold-barrier", type=Path,
                        help="idle fixture only: start hold timer after all jobs are ready")
    parser.add_argument("--checkpoint-at-step", type=int, help="deterministic self-test only")
    args = parser.parse_args(argv)
    if not 5 <= args.hold_seconds <= 6:
        parser.error("hold is bounded to 5..6 seconds")
    if args.checkpoint_at_step is not None and (args.role != "growing" or
            args.resume or not 1 <= args.checkpoint_at_step < STEPS):
        parser.error("self-test checkpoint must be within initial growing work")
    if args.resume and args.role != "growing":
        parser.error("only the disposable growing task has a resume recipe")
    if args.hold_barrier and args.role != "hold":
        parser.error("hold barrier is only for the zero-payload idle fixture")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    control_value = os.environ.get("HEADROOM_CONTROL_DIR")
    control = Path(control_value) if control_value else None
    wait_barrier(args.start_barrier)
    progress = 0
    checksum = checksum_at(args.role, 0)
    if args.resume:
        saved = read_checkpoint(output / "checkpoint.json", args.role)
        progress, checksum = saved["progress"], saved["checksum"]
    record = dict(version=1, role=args.role, pid=os.getpid(), resume=args.resume,
                  goal_steps=STEPS, step_seconds=STEP_SECONDS,
                  started_ns=time.monotonic_ns(), started_from=progress,
                  initial_checksum=checksum, executed_steps=[], payload_peak_mib=0,
                  scratch_initial_mib=0)
    record_path = output / ("attempt-" + str(os.getpid()) + ".json")
    save(record_path, record)
    blocks = []

    def allocate_blocks(count):
        assert 0 <= count <= 12
        while len(blocks) < count:
            blocks.append(bytearray(8 * MIB))
        record["payload_peak_mib"] = max(record["payload_peak_mib"], len(blocks) * 8)

    if args.role == "protected":
        allocate_blocks(2)
    elif args.role == "small":
        allocate_blocks(1)
    save(output / ("ready-" + str(os.getpid()) + ".json"),
         dict(pid=os.getpid(), role=args.role, payload_mib=len(blocks) * 8))

    def finish(stopped):
        record.update(progress=progress, checksum=checksum, stopped=stopped,
                      ended_ns=time.monotonic_ns(), exit_code=0)
        save(record_path, record)
        if not stopped:
            save(output / "completed.json", dict(role=args.role, progress=progress,
                 checksum=checksum, ended_ns=record["ended_ns"]))
        return 0

    if args.role == "hold":
        wait_barrier(args.hold_barrier)
        time.sleep(args.hold_seconds)
        return finish(False)
    while progress < STEPS:
        body = None
        if control and args.role == "growing" and (control / "stop.request").is_file():
            body = read_stop_request(control / "stop.request")
            parts = body.split(b"\n")
            if len(parts) != 3 or parts[0] != b"headroom-stop-v1" or not parts[1] or parts[2]:
                raise ValueError("malformed stop request")
        synthetic_stop = args.checkpoint_at_step == progress
        if body is not None or synthetic_stop:
            save(output / "checkpoint.json", dict(version=1, role=args.role,
                 goal_steps=STEPS, progress=progress, checksum=checksum,
                 scratch_discarded_mib=len(blocks) * 8))
            if body is not None:
                atomic_bytes(control / "stop.ack", body)
            return finish(True)
        next_step = progress + 1
        # Scratch is disposable: accumulate only steps executed this attempt.
        if args.role == "growing":
            allocate_blocks(min(12, (next_step - record["started_from"]) // 4))
        time.sleep(STEP_SECONDS)
        checksum = advance(checksum, args.role, next_step)
        progress = next_step
        record["executed_steps"].append(progress)
    return finish(False)


if __name__ == "__main__":
    raise SystemExit(main())
