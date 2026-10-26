"""Owned crash-window and completion-precedence regression fixtures.

Run serially: python -B tests/test_launch_atomic.py --go -v
Optional: --source-root PATH builds another checkout's src/ for before/after.
Fixture compilation uses a forced include plus a separate interception unit;
production sources/binaries are never edited. All generated files stay under
ignored build/ and test-output/. No model, network, credentials or host pressure.
Discovery is also authorized by HEADROOM_LAUNCH_ATOMIC_GO=1 or the verifier's
HEADROOM_LIVE_NATIVE_GO=1. This suite (including aggregate discovery) requires
GCC, unlike the ordinary prebuilt-native suites. Compiler selection is
HEADROOM_LAUNCH_ATOMIC_CC, then gcc on PATH, then the known local MSYS2 fallback.
It takes serial_fleet's lock
before compilation and holds it through every process/cleanup operation.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT
PROBE = ROOT / "tests" / "launch_probe.c"
GO = any(os.environ.get(key) == "1" for key in
         ("HEADROOM_LAUNCH_ATOMIC_GO", "HEADROOM_LIVE_NATIVE_GO"))
MIB = 1024 * 1024
CREATE_SUSPENDED = 4


class BasicLimit(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", w.DWORD), ("minimum_ws", ctypes.c_size_t),
                ("maximum_ws", ctypes.c_size_t), ("active_limit", w.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                ("scheduling", w.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [("basic", BasicLimit), ("io", IoCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


def kernel_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    declarations = {
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        "CreateEventW": ([ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
        "SetEvent": ([w.HANDLE], w.BOOL),
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "CloseHandle": ([w.HANDLE], w.BOOL),
    }
    for name, (arguments, result) in declarations.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


@unittest.skipUnless(os.name == "nt" and GO, "Windows fixture; explicitly enable with --go")
class LaunchAtomicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_fleet import serial_fleet
        cls.serial = serial_fleet()
        cls.serial.__enter__()
        cls.addClassCleanup(cls.serial.__exit__, None, None, None)
        cls.kernel = kernel_api()
        bundled = Path("C:/msys64/ucrt64/bin/gcc.exe")
        compiler = os.environ.get("HEADROOM_LAUNCH_ATOMIC_CC") or shutil.which("gcc") or (
            str(bundled) if bundled.is_file() else None)
        if not compiler:
            raise RuntimeError("launch interception fixtures require GCC")
        (ROOT / "build").mkdir(exist_ok=True)
        (ROOT / "test-output").mkdir(exist_ok=True)
        cls.build = Path(tempfile.mkdtemp(prefix="launch-atomic-", dir=ROOT / "build"))
        cls.artifacts = Path(tempfile.mkdtemp(prefix="launch-atomic-", dir=ROOT / "test-output"))
        print("Launch fixtures: " + str(cls.artifacts), file=sys.stderr)
        common = [compiler, "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror"]
        cls.child_binary = cls.build / "child.exe"
        cls.compile(common + ["-municode", "-DHEADROOM_LAUNCH_PROBE_CHILD", str(PROBE),
                              "-o", str(cls.child_binary)])
        shim = cls.build / "launch_probe.o"
        cls.compile(common + ["-c", str(PROBE), "-o", str(shim)])
        objects = {}
        for name in ("guard", "fleet", "support", "policy", "output", "intake"):
            target = cls.build / (name + ".o")
            cls.compile(common + ["-DHEADROOM_LAUNCH_PROBE_HEADER", "-include", str(PROBE),
                "-c", str(SOURCE_ROOT / "src" / (name + ".c")), "-o", str(target)])
            objects[name] = target
        cls.binaries = {}
        for backend, names in (("guard", ("guard", "support")),
                ("fleet", ("fleet", "support", "policy", "output", "intake"))):
            binary = cls.build / (backend + ".exe")
            cls.compile(common + ["-municode", *map(str, (objects[name] for name in names)),
                str(shim), "-o", str(binary), "-lole32", "-lpsapi"])
            cls.binaries[backend] = binary

    @classmethod
    def compile(cls, command):
        result = subprocess.run(command, capture_output=True, timeout=45,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stdout.decode(errors="replace")
                               + result.stderr.decode(errors="replace"))

    def setUp(self):
        self.base = self.artifacts / self._testMethodName
        self.base.mkdir()
        self.state = self.base / "state"
        self.handles = []
        self.groups = []
        self.children = []
        self.addCleanup(self.close_handles)

    def close_handles(self):
        # Assertions run while the external job is still open. Afterwards,
        # close that job and prove even a baseline orphan has been reclaimed.
        for group in self.groups:
            self.kernel.CloseHandle(group)
            self.handles.remove(group)
        remaining = [child for child in self.children
                     if self.kernel.WaitForSingleObject(child, 5000) != 0]
        for handle in reversed(self.handles):
            self.kernel.CloseHandle(handle)
        self.assertFalse(remaining, "external cleanup did not confirm fixture child exit")

    def own(self, handle):
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handles.append(handle)
        return handle

    def start(self, backend, mode):
        # Own the controller before its first instruction. Its children inherit
        # this external job, which stays open throughout the crash assertion.
        group = self.own(self.kernel.CreateJobObjectW(None, None))
        self.groups.append(group)
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000 | 0x200  # KILL_ON_JOB_CLOSE | JOB_MEMORY
        limits.job_memory = 256 * MIB
        self.assertTrue(self.kernel.SetInformationJobObject(group, 9, ctypes.byref(limits),
                        ctypes.sizeof(limits)), ctypes.get_last_error())
        names = {key: "Local\\Headroom-launch-test-" + str(uuid.uuid4())
                 for key in ("READY", "RELEASE")}
        ready = self.own(self.kernel.CreateEventW(None, True, False, names["READY"]))
        release = self.own(self.kernel.CreateEventW(None, True, False, names["RELEASE"]))
        environment = os.environ.copy()
        environment.update({"HEADROOM_LAUNCH_PROBE_MODE": mode,
                            "HEADROOM_LAUNCH_PROBE_DIR": str(self.base)})
        environment.update({"HEADROOM_LAUNCH_PROBE_" + key: value for key, value in names.items()})
        executable = str(self.child_binary)
        argv = [executable]
        if backend == "guard":
            command = [str(self.binaries[backend]), "--state", str(self.state),
                       "--soft-mib", "64", "--hard-mib", "128", "--timeout-ms", "5000",
                       "--", *argv]
        else:
            plan = bytearray(struct.pack("<8sIQQQIII", b"HRFLEET1", 1,
                64 * MIB, 128 * MIB, 0, 100, 100, 5000))
            plan += struct.pack("<I", 5) + b"probe"
            plan += struct.pack("<IIIQQ", 50, 0, 0, 32 * MIB, 96 * MIB)
            for value in (executable, subprocess.list2cmdline(argv), "", ""):
                encoded = value.encode("utf-16le")
                plan += struct.pack("<I", len(encoded) // 2) + encoded
            path = self.base / "plan.hrp"
            path.write_bytes(plan)
            command = [str(self.binaries[backend]), "--plan", str(path), "--state", str(self.state)]
        # _winapi returns the exact primary-thread handle without a PID/thread
        # scan; retain it until the externally owned controller is resumed.
        import _winapi
        startup = subprocess.STARTUPINFO()
        process_handle, thread_handle, pid, _ = _winapi.CreateProcess(
            command[0], subprocess.list2cmdline(command), None, None, False,
            CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW,
            environment, str(ROOT), startup)
        process = self.own(process_handle)
        thread = self.own(thread_handle)
        # Register termination before assignment, including assignment failure.
        self.addCleanup(self.cleanup_controller, process)
        self.assertTrue(self.kernel.AssignProcessToJobObject(group, process), ctypes.get_last_error())
        self.assertNotEqual(self.kernel.ResumeThread(thread), 0xFFFFFFFF)
        self.assertEqual(self.kernel.WaitForSingleObject(ready, 10000), 0,
                         "CreateProcess interception did not publish a child")
        record = json.loads((self.base / "created.json").read_text())
        child = self.own(self.kernel.OpenProcess(0x100000 | 0x1000, False, record["child_pid"]))
        self.children.append(child)
        belongs = w.BOOL()
        self.assertTrue(self.kernel.IsProcessInJob(child, group, ctypes.byref(belongs)))
        self.assertTrue(belongs.value, "fixture child escaped external cleanup ownership")
        self.assertTrue(record["creation_flags"] & CREATE_SUSPENDED)
        if mode != "crash":
            self.assertTrue(self.kernel.SetEvent(release), ctypes.get_last_error())
        return process, child, record, pid

    def cleanup_controller(self, process):
        if self.kernel.WaitForSingleObject(process, 0) == 258:
            import _winapi
            _winapi.TerminateProcess(process, 94)
            self.kernel.WaitForSingleObject(process, 5000)

    def finish(self, process, backend):
        import _winapi
        self.assertEqual(self.kernel.WaitForSingleObject(process, 10000), 0,
                         "fixture controller did not exit")
        code = _winapi.GetExitCodeProcess(process)
        name = "events.jsonl" if backend == "guard" else "fleet.jsonl"
        records = [json.loads(line) for line in (self.state / name).read_text().splitlines()]
        return code, records

    def crash_case(self, backend):
        import _winapi
        controller, child, record, _ = self.start(backend, "crash")
        self.assertTrue(record["creation_flags"] & CREATE_SUSPENDED)
        self.assertEqual(self.kernel.WaitForSingleObject(child, 0), 258)
        _winapi.TerminateProcess(controller, 95)
        self.assertEqual(self.kernel.WaitForSingleObject(controller, 5000), 0)
        # Do not close the external cleanup job until AFTER this assertion.
        self.assertEqual(self.kernel.WaitForSingleObject(child, 3000), 0,
                         "child survived controller crash inside CreateProcessW; "
                         "creation was not atomically Headroom-contained")

    def completion_case(self, backend, mode):
        controller, _, _, _ = self.start(backend, mode)
        code, records = self.finish(controller, backend)
        injected = json.loads((self.base / "injected.json").read_text())
        self.assertEqual(injected["condition"], mode)
        self.assertEqual(injected["active_processes"], 0)
        self.assertEqual(code, 124 if mode == "deadline" else 130, records)
        self.assertFalse(any(row["event"] == "finished" and row["code"] == 0
                             for row in records), records)
        if backend == "guard":
            self.assertNotEqual(records[-1]["event"], "completed", records)

    def test_guard_create_crash_is_atomically_contained(self):
        self.crash_case("guard")

    def test_fleet_create_crash_is_atomically_contained(self):
        self.crash_case("fleet")

    def test_guard_deadline_at_empty_observation_is_not_success(self):
        self.completion_case("guard", "deadline")

    def test_fleet_deadline_at_empty_observation_is_not_success(self):
        self.completion_case("fleet", "deadline")

    def test_guard_interrupt_at_empty_observation_is_not_success(self):
        self.completion_case("guard", "interrupt")

    def test_fleet_interrupt_at_empty_observation_is_not_success(self):
        self.completion_case("fleet", "interrupt")

    def normal_case(self, backend):
        controller, _, _, _ = self.start(backend, "normal")
        code, records = self.finish(controller, backend)
        self.assertEqual(code, 0, records)
        self.assertEqual(records[-1]["event"], "completed" if backend == "guard" else "finished")
        self.assertFalse((self.base / "injected.json").exists())

    def test_guard_probe_preserves_normal_success(self):
        self.normal_case("guard")

    def test_fleet_probe_preserves_normal_success(self):
        self.normal_case("fleet")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", action="store_true", help="authorize bounded owned fixtures")
    parser.add_argument("--source-root", type=Path, default=ROOT,
                        help="checkout supplying native src/; fixture remains local")
    options, remaining = parser.parse_known_args()
    SOURCE_ROOT = options.source_root.resolve()
    # skipUnless is evaluated at import; explicit CLI authorization updates it.
    if options.go and os.name == "nt":
        LaunchAtomicTests.__unittest_skip__ = False
    unittest.main(argv=[sys.argv[0], *remaining])
