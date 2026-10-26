"""Fresh, bounded live-intake fixtures; never discover or signal host processes.

Workloads retain at most 16 MiB each. The oplock helper holds only a file made
by its invoking test, with a finite lifetime and an explicit release barrier.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from fleet_worker import own_peak_commit, save, wait_file

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
LIMIT = MIB


def hold_request_lock(path, destination, output, release, seconds):
    """Keep one freshly published request unrenamable, without an oplock.

    Initialize before publication so opening its target follows immediately.
    The parent requires the locked marker before asserting intake behavior.
    """
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.MoveFileExW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
    kernel.MoveFileExW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    if not kernel.MoveFileExW(str(path.parent), str(destination), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    handle = kernel.CreateFileW(str(destination / path.name), 0x80000000, 0,
                                None, 3, 0x80, None)
    if handle == w.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        save(output / "locked.json", {"pid": os.getpid(), "path": str(destination)})
        wait_file(release, timeout=seconds)
    finally:
        kernel.CloseHandle(handle)
    save(output / "released.json", {"pid": os.getpid()})
    return 0


def hold_oplock(path, output, release, seconds, destination=None, gate=None):
    """Do not acknowledge a legacy level-1 break until explicitly released.

    FILE_FLAG_OVERLAPPED is required for the asynchronous oplock request. No
    synchronous read, external process handle, or unrelated file is involved.
    Unsupported filesystems are reported to the parent for an explicit skip.
    """
    import ctypes
    from ctypes import wintypes as w

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    kernel.CreateEventW.restype = w.HANDLE
    kernel.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD,
        ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped)]
    kernel.DeviceIoControl.restype = w.BOOL
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.MoveFileExW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
    kernel.MoveFileExW.restype = w.BOOL
    if destination:
        # The parent gates its fixture owner's intake before publication and
        # releases it only after our granted marker. A separate helper gate
        # deliberately exposes the former publish/acquire race for regression.
        # A DELETE-access rename handle must not remain open: the native
        # reader intentionally opens with FILE_SHARE_READ only.
        if not kernel.MoveFileExW(str(path), str(destination), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        path = destination
        save(output / "published.json", {"pid": os.getpid(), "path": str(path)})
    if gate:
        wait_file(gate, timeout=seconds)
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x40000000, None)
    if handle == w.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    event = kernel.CreateEventW(None, True, False, None)
    if not event:
        kernel.CloseHandle(handle)
        raise ctypes.WinError(ctypes.get_last_error())
    overlap = Overlapped(hEvent=event)
    returned = w.DWORD()
    try:
        ok = kernel.DeviceIoControl(handle, 0x00090000, None, 0, None, 0,
                                    ctypes.byref(returned), ctypes.byref(overlap))
        error = ctypes.get_last_error() if not ok else 0
        if ok or error != 997:  # ERROR_IO_PENDING proves a granted oplock.
            save(output / "oplock.json", {"granted": False, "error": error})
            return 0
        save(output / "oplock.json", {"granted": True, "pid": os.getpid()})
        deadline = time.monotonic() + seconds
        broken = False
        while time.monotonic() < deadline and not release.exists():
            if not broken and kernel.WaitForSingleObject(event, 0) == 0:
                broken = True
                save(output / "break.json", {"pid": os.getpid(),
                                              "observed_ns": time.monotonic_ns()})
            time.sleep(0.01)
        save(output / "released.json", {"explicit": release.exists(), "broken": broken})
        return 0
    finally:
        # Closing our own file acknowledges/cancels the pending oplock; keep
        # overlap/event storage alive until that handle has been closed.
        kernel.CloseHandle(handle)
        kernel.CloseHandle(event)


def receipt_snapshot(state, request_id):
    """Observe immediately on entry, without retrying until a receipt appears."""
    receipt = state / "receipts" / (request_id + ".json")
    processed = state / "processed" / request_id
    observed = {"observed_ns": time.monotonic_ns()}
    try:
        raw = receipt.read_bytes()
        observed["receipt"] = json.loads(raw)
        observed["receipt_bytes"] = len(raw)
        observed["validated_matches"] = ((processed / "validated.hrp").read_bytes()
                                          == (processed / "plan.hrp").read_bytes())
        journal = (state / "fleet.jsonl").read_bytes()
        complete = journal[:journal.rfind(b"\n") + 1]
        observed["queued"] = any(row.get("event") == "queued" and
            row.get("task") == observed["receipt"]["task"]
            for row in map(json.loads, complete.splitlines()))
    except (OSError, ValueError, KeyError) as error:
        observed["error"] = str(error)
    return observed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("client", "work", "tree", "descendant", "probe",
                                         "flood", "oplock", "witness", "rename-blocker"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--request-id")
    parser.add_argument("--task")
    parser.add_argument("--job-file", type=Path)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--wait-seconds", type=float, default=5)
    parser.add_argument("--payload-mib", type=int, choices=(0, 8, 16), default=8)
    args = parser.parse_args(argv)
    if not 0.01 <= args.seconds <= 25 or not 0 <= args.wait_seconds <= 10:
        parser.error("fixture duration/wait exceeds fixed bounds")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    if args.mode == "rename-blocker":
        if not args.lock_file or not args.destination or not args.release:
            parser.error("rename-blocker requires private input, destination, and release")
        return hold_request_lock(args.lock_file, args.destination, output, args.release, args.seconds)
    if args.mode == "witness":
        # Observe the first complete native started event independently of the
        # submitting client; never wait for/retry a missing receipt afterward.
        save(output / "ready.json", {"pid": os.getpid()})
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            journal = args.state / "fleet.jsonl"
            raw = journal.read_bytes() if journal.is_file() else b""
            complete = raw[:raw.rfind(b"\n") + 1]
            if any(row.get("event") == "started" and row.get("task") == args.task
                   for row in map(json.loads, complete.splitlines())):
                save(output / "receipt-at-started.json",
                     receipt_snapshot(args.state, args.request_id))
                return 0
            time.sleep(0.002)
        raise RuntimeError("bounded native-start witness expired")
    if args.mode == "client":
        sys.path.insert(0, str(ROOT))
        import headroom_live
        save(output / "ready.json", {"pid": os.getpid()})
        if args.gate:
            wait_file(args.gate, timeout=20)
        started = time.monotonic_ns()
        try:
            result = headroom_live.submit(args.state,
                json.loads(args.job_file.read_bytes()), request_id=args.request_id,
                wait_seconds=args.wait_seconds)
        except Exception as error:
            save(output / "result.json", {"pid": os.getpid(), "started_ns": started,
                "ended_ns": time.monotonic_ns(), "error_type": type(error).__name__,
                "error": str(error)})
            return 73
        save(output / "result.json", {"pid": os.getpid(), "started_ns": started,
            "ended_ns": time.monotonic_ns(), "result": result})
        return 0
    if args.mode == "oplock":
        if not args.lock_file or not args.release:
            parser.error("oplock requires a private lock file and release barrier")
        return hold_oplock(args.lock_file, output, args.release, args.seconds,
                           args.destination, args.gate)

    # Probe before publishing any readiness marker or allowing a retry to hide
    # an ordering failure. Visibility is tested; power-loss durability cannot
    # be established by a userspace regression fixture.
    if args.mode == "probe":
        snapshot = receipt_snapshot(args.state, args.request_id)
        save(output / "receipt-at-entry.json", snapshot)
        if (snapshot.get("error") or not snapshot.get("validated_matches") or
                not snapshot.get("queued") or
                snapshot.get("receipt", {}).get("status") != "accepted"):
            return 77
    started = time.monotonic_ns()
    blocks = [bytearray(8 * MIB) for _ in range(args.payload_mib // 8)]
    record_path = output / ("run-" + str(os.getpid()) + ".json")
    record = {"pid": os.getpid(), "parent_pid": os.getppid(),
              "mode": args.mode, "started_ns": started,
              "payload_mib": len(blocks) * 8}
    save(record_path, record)
    save(output / "ready.json", record)
    child = None
    try:
        if args.mode == "tree":
            child_output = output / "child"
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                "descendant", str(child_output), "--seconds", str(args.seconds),
                "--payload-mib", "8"], stdin=subprocess.DEVNULL, close_fds=True)
            save(output / "tree.json", {"root_pid": os.getpid(), "child_pid": child.pid})
            wait_file(child_output / "ready.json", timeout=8)
        if args.mode == "flood":
            if os.name == "nt":
                import msvcrt
                for descriptor in (1, 2):
                    msvcrt.setmode(descriptor, os.O_BINARY)
            # Fixed-size chunks, at most 1 MiB + 64 KiB on either stream.
            for _ in range((LIMIT + 65536) // 4096):
                for descriptor, block in ((1, b"O" * 4096), (2, b"E" * 4096)):
                    remaining = memoryview(block)
                    while remaining:
                        count = os.write(descriptor, remaining)
                        if count <= 0:
                            raise RuntimeError("output made no progress")
                        remaining = remaining[count:]
        if args.release:
            wait_file(args.release, timeout=args.seconds)
        else:
            time.sleep(args.seconds)
        record.update(ended_ns=time.monotonic_ns(), exit_code=0,
                      peak_private_bytes=own_peak_commit())
        save(record_path, record)
        return 0
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()  # Only our freshly launched bounded descendant.
            child.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
