"""Bounded byte-output fixtures for test_output.py; never discover host processes."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from fleet_worker import main as fleet_main, save, wait_file


LIMIT = 1024 * 1024


def binary_streams():
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stderr.fileno(), os.O_BINARY)


def write_all(descriptor, data):
    remaining = memoryview(data)
    while remaining:
        count = os.write(descriptor, remaining)
        if count <= 0:
            raise RuntimeError("fixture output write made no progress")
        remaining = remaining[count:]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("normal", "empty", "flood", "stdin",
        "nonzero", "root-late", "late-child", "root-closed", "closed-child",
        "checkpoint", "resume"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--exit-code", type=int, choices=(17, 259), default=17)
    parser.add_argument("--stdout-bytes", type=int, default=LIMIT + 65536)
    parser.add_argument("--stderr-bytes", type=int, default=LIMIT + 65536)
    parser.add_argument("--barrier", type=Path)
    args = parser.parse_args(argv)
    if any(not 0 <= size <= LIMIT + 65536 for size in
           (args.stdout_bytes, args.stderr_bytes)):
        parser.error("output fixture is bounded to 1 MiB + 64 KiB per stream")
    binary_streams()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    mode = args.mode

    if mode == "empty":
        return 0
    if mode == "normal":
        write_all(1, "stdout: नमस्ते 🌱\n".encode("utf-8") + bytes(range(256)) + b"\x00\xff\r\n\x1a")
        write_all(2, "stderr: Καλημέρα 🔥\n".encode("utf-8") + bytes(reversed(range(256))) + b"\x80\x00\n")
        return 0
    if mode == "nonzero":
        write_all(1, b"stdout before nonzero\x00\xff\n")
        write_all(2, b"stderr before nonzero\x00\xfe\n")
        return args.exit_code
    if mode == "flood":
        # Alternate fixed-size writes so either stream can reveal a stalled
        # reader. Total output is fixed, even if the coordinator stops draining.
        out_block = bytes(range(256)) * 16
        err_block = bytes(reversed(range(256))) * 16
        for offset in range(0, max(args.stdout_bytes, args.stderr_bytes), 4096):
            if offset < args.stdout_bytes:
                write_all(1, out_block[:min(4096, args.stdout_bytes - offset)])
            if offset < args.stderr_bytes:
                write_all(2, err_block[:min(4096, args.stderr_bytes - offset)])
        save(output / "writes-finished.json", True)
        return 0
    if mode == "stdin":
        # A pipe left open by the parent blocks here; the fleet deadline bounds
        # that failure. NUL must immediately return EOF without user input.
        data = os.read(0, 1)
        save(output / "stdin.json", {"bytes": list(data)})
        write_all(1, b"stdin EOF\n" if data == b"" else b"stdin was not EOF\n")
        return 0 if data == b"" else 73
    if mode in ("root-late", "root-closed"):
        write_all(1, b"root stdout\n")
        write_all(2, b"root stderr\n")
        child_mode = "late-child" if mode == "root-late" else "closed-child"
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                  child_mode, str(output)], stdin=subprocess.DEVNULL,
                                 stdout=sys.stdout.buffer, stderr=sys.stderr.buffer,
                                 close_fds=True)
        save(output / "tree.json", {"root_pid": os.getpid(), "child_pid": child.pid})
        wait_file(output / "child-ready.json")
        # Intentionally leave a bounded, owned descendant alive after root exit.
        return 0
    if mode in ("late-child", "closed-child"):
        if mode == "closed-child":
            os.close(1)
            os.close(2)
        save(output / "child-ready.json", {"pid": os.getpid()})
        wait_file(output / "release-child")
        if mode == "late-child":
            write_all(1, b"late child stdout\x00\xff\n")
            write_all(2, b"late child stderr\x00\xfe\n")
        save(output / "child-finished.json", True)
        return 0
    if mode in ("checkpoint", "resume"):
        prefix = mode.encode("ascii")
        write_all(1, prefix + b" stdout begin\n")
        write_all(2, prefix + b" stderr begin\n")
        command = ["grow" if mode == "checkpoint" else "resume", str(output)]
        if args.barrier:
            command += ["--barrier", str(args.barrier)]
        result = fleet_main(command)
        write_all(1, prefix + b" stdout end\n")
        write_all(2, prefix + b" stderr end\n")
        return result
    raise AssertionError(mode)


if __name__ == "__main__":
    raise SystemExit(main())
