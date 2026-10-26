"""Bounded, application-owned fixtures for test_fleet.py and fleet_demo.py.

No fixture discovers other processes or allocates according to host capacity.
Each growing worker retains at most twelve 8 MiB blocks (96 MiB).
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

MIB = 1024 * 1024


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def save(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def wait_file(path, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).is_file():
            return
        time.sleep(0.025)
    raise RuntimeError("fixture barrier timed out")


def own_peak_commit():
    """PeakPagefileUsage is the kernel's peak process commit charge (bytes)."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    memory = Counters()
    memory.cb = ctypes.sizeof(memory)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(memory), memory.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(memory.PeakPagefileUsage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("protected", "grow", "noack", "wrongack",
        "resume", "regrow", "queued", "hold", "tree", "cap-tree", "cap-child", "argv", "exit", "capped", "startup-burst"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--barrier", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args(argv)
    if args.duration is not None and not 0.1 <= args.duration <= 12:
        parser.error("fixture duration must be between 0.1 and 12 seconds")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    mode = args.mode
    control = Path(os.environ.get("HEADROOM_CONTROL_DIR", str(output / "no-control")))
    started = time.monotonic_ns()
    record_path = output / ("run-" + str(os.getpid()) + ".json")
    record = {"mode": mode, "pid": os.getpid(), "started_ns": started,
              "control_attempt": control.name, "payload_mib": 0}
    save(record_path, record)
    blocks = []

    def allocate(mib):
        # Fixed 8 MiB increments; no transient duplicated payload.
        assert 0 <= mib <= 96 and mib % 8 == 0
        while len(blocks) * 8 < mib:
            blocks.append(bytearray(8 * MIB))
        record["payload_mib"] = len(blocks) * 8

    def finish(code=0, **fields):
        if mode == "protected":
            record["peak_private_bytes"] = own_peak_commit()
        record.update(fields)
        record.update(ended_ns=time.monotonic_ns(), exit_code=code)
        save(record_path, record)
        return code

    if mode == "argv":
        save(output / "argv.json", args.arguments)
        return finish()
    if mode == "exit":
        if args.barrier:
            wait_file(args.barrier)
        if args.duration:
            time.sleep(args.duration)
        return finish(args.exit_code)

    if mode == "capped":
        # Used by two separate logical jobs. Each fits its own 160 MiB cap;
        # their combined request plus a protected peer exceeds root's 192 MiB.
        own_peak_commit()  # Load the measurement API before filling the cap.
        if args.barrier:
            wait_file(args.barrier)
        save(output / "ready.json", {"pid": os.getpid(), "started_ns": started})
        denied = False
        for _ in range(12):
            try:
                blocks.append(bytearray(8 * MIB))
            except MemoryError:
                denied = True
                break
            time.sleep(0.08)
        record.update(payload_mib=len(blocks) * 8, allocation_denied=denied,
                      peak_private_bytes=own_peak_commit())
        save(output / "allocation.json", record)
        wait_file(args.release)
        return finish(71 if denied else 0)

    if mode == "cap-child":
        # Each child fits alone inside the test's 112 MiB group cap. Together
        # they request 128 MiB payload, plus three Python interpreters.
        denied = False
        for _ in range(8):
            try:
                blocks.append(bytearray(8 * MIB))
            except MemoryError:
                denied = True
                break
            time.sleep(0.08)
        save(output / "allocation.json", {"denied": denied, "payload_mib": len(blocks) * 8})
        wait_file(args.release)
        return finish(payload_mib=len(blocks) * 8, allocation_denied=denied)

    if mode in ("tree", "cap-tree"):
        children = []
        count = 2 if mode == "cap-tree" else 1
        try:
            for index in range(count):
                child_output = output / ("child-" + str(index))
                child_output.mkdir()
                command = [sys.executable, str(Path(__file__).resolve()),
                           "cap-child" if mode == "cap-tree" else "hold", str(child_output)]
                if mode == "cap-tree":
                    command += ["--release", str(output / "release")]
                else:
                    command += ["--duration", "12"]
                children.append(subprocess.Popen(command))
                save(output / ("child-" + str(index) + "-pid.json"), children[-1].pid)
            save(output / "ready.json", {"pid": os.getpid(), "started_ns": started})
            if mode == "cap-tree":
                for index in range(count):
                    wait_file(output / ("child-" + str(index)) / "allocation.json")
                allocation = [json.loads((output / ("child-" + str(i)) / "allocation.json")
                                        .read_text(encoding="utf-8")) for i in range(count)]
                save(output / "aggregate.json", {"children": allocation,
                     "payload_mib": sum(item["payload_mib"] for item in allocation)})
                save(output / "release", True)
                for child in children:
                    child.wait(timeout=5)
                return finish(0 if any(item["denied"] for item in allocation) else 72)
            time.sleep(args.duration or 12)
            return finish()
        finally:
            # This is also bounded when run outside the coordinator. On a
            # coordinator crash, kernel Job Object ownership kills this tree.
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    if mode == "protected":
        allocate(16)  # Interpreter + payload stays comfortably inside 48 MiB.
    elif mode == "startup-burst":
        allocate(96)  # Immediate bounded startup, with no deliberate growth sleeps.
    elif mode in ("queued", "resume", "hold"):
        allocate(8 if mode == "queued" else 16 if mode == "resume" else 0)
    save(output / "ready.json", {"pid": os.getpid(), "started_ns": started})
    if args.barrier:
        wait_file(args.barrier)
    progress = 0
    if mode in ("resume", "regrow"):
        checkpoint = json.loads((output / "checkpoint.json").read_text(encoding="utf-8"))
        progress = checkpoint["progress"]
        record["started_from"] = progress
        save(record_path, record)
    initial_progress = progress
    duration = args.duration or {"protected": 4, "queued": 0.8, "resume": 0.8,
                                "hold": 12, "startup-burst": 1.5}.get(mode, 3.5)
    deadline = time.monotonic() + duration
    next_growth = time.monotonic() + 0.10
    grow = mode in ("grow", "noack", "wrongack", "regrow")
    while time.monotonic() < deadline:
        request = control / "stop.request"
        if request.is_file() and grow:
            body = request.read_bytes()
            parts = body.split(b"\n")
            if len(parts) == 3 and parts[0] == b"headroom-stop-v1" and parts[1] and not parts[2]:
                if mode in ("grow", "regrow"):
                    record["request_seen_ns"] = time.monotonic_ns()
                    # The checkpoint is durable and atomically visible before
                    # an atomically published ack copies the exact nonce.
                    save(output / "checkpoint.json", {"progress": progress,
                         "payload_mib": len(blocks) * 8, "pid": os.getpid()})
                    record["checkpoint_published_ns"] = time.monotonic_ns()
                    atomic_bytes(control / "stop.ack", body)
                    record["ack_published_ns"] = time.monotonic_ns()
                    return finish(stopped=True, progress=progress, payload_mib=len(blocks) * 8)
                if mode == "wrongack":
                    atomic_bytes(control / "stop.ack", b"headroom-stop-v1\nwrong-attempt\n")
        if grow and len(blocks) < 12 and time.monotonic() >= next_growth:
            try:
                allocate((len(blocks) + 1) * 8)
            except MemoryError:
                return finish(71, allocation_denied=True)
            next_growth = time.monotonic() + 0.10
        progress += 1
        time.sleep(0.025)
    if args.release:
        wait_file(args.release)
    if mode in ("resume", "regrow"):
        save(output / "resumed.json", {"started_from": initial_progress, "completed": progress})
    save(output / "completed.json", {"progress": progress, "ended_ns": time.monotonic_ns()})
    return finish(progress=progress, payload_mib=len(blocks) * 8)


if __name__ == "__main__":
    raise SystemExit(main())
