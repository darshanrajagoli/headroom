"""Native live regression fixtures, opt-in only after main's build/GO notice.

Run: python tests/test_live_native.py --go -v
Uses shared serial_fleet and fresh private state only. The oplock regression
compiles a private owner with a setup barrier; shipped binaries stay untouched.
All evidence (including failures/cleanup) stays in ignored test-output/live-intake.
Payload is <=32 MiB per scenario, fixed independently of available host memory;
workload groups and root ownership have 128 MiB hard caps.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import headroom
import recover
from fleet_worker import atomic_bytes, save
from test_fleet import FLEET, alive, read_events, records, select, serial_fleet, until

WORKER = ROOT / "tests" / "live_worker.py"
# verify.py may opt in before unittest discovery after main has built/frozen.
NATIVE_GO = os.environ.get("HEADROOM_LIVE_NATIVE_GO") == "1"
ARTIFACT_ROOT = None
RECEIPT_KEYS = {"version", "run_id", "request_id", "task", "status", "code", "ordinal"}
MIB = 1024 * 1024


# Only fleet.c sees the renamed schedule call. The wrapper gates its first
# call, after session publication but before any intake scan, then delegates
# to the unchanged intake.c. The real lifetime clock includes all setup time.
INTAKE_BARRIER = r'''
#include "intake.h"
int fixture_intake_schedule(HrIntake *intake) {
    static int released = 0;
    if (!released) {
        wchar_t directory[PATH_CAP], path[PATH_CAP];
        DWORD length = GetEnvironmentVariableW(L"HEADROOM_FIXTURE_INTAKE_DIR",
                                               directory, PATH_CAP);
        if (!length || length >= PATH_CAP
            || !join_path(path, directory, L"intake-waiting")) return 0;
        HANDLE marker = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                                    CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
        if (marker == INVALID_HANDLE_VALUE) return 0;
        if (!CloseHandle(marker)) return 0;
        if (!join_path(path, directory, L"release-intake")) return 0;
        ULONGLONG deadline = GetTickCount64() + 8000;
        while (GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES) {
            if (GetLastError() != ERROR_FILE_NOT_FOUND) return 0;
            if (GetTickCount64() >= deadline) {
                SetLastError(ERROR_TIMEOUT); return 0;
            }
            Sleep(10);
        }
        released = 1;
    }
    return hr_intake_schedule(intake);
}
'''


def load(path):
    return headroom.strict_json_loads(Path(path).read_bytes())


def fingerprint():
    return hashlib.sha256(FLEET.read_bytes()).hexdigest()


def fixture_command(mode, output, *options):
    return [sys.executable, str(WORKER), mode, str(output), *map(str, options)]


@unittest.skipUnless(os.name == "nt" and FLEET.is_file(), "Windows native fleet unavailable")
class LiveNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NATIVE_GO:
            raise unittest.SkipTest("main GO requires --go or HEADROOM_LIVE_NATIVE_GO=1")
        cls.live = importlib.import_module("headroom_live")
        cls.serial = serial_fleet()
        cls.serial.__enter__()
        cls.addClassCleanup(cls.serial.__exit__, None, None, None)
        cls.native_hash = fingerprint()
        cls.artifact_root = ARTIFACT_ROOT
        if cls.artifact_root is None:
            directory = ROOT / "test-output" / "live-intake"
            directory.mkdir(parents=True, exist_ok=True)
            cls.artifact_root = Path(tempfile.mkdtemp(prefix="native-", dir=directory))
        print("Retained live evidence: " + str(cls.artifact_root), file=sys.stderr)

    def setUp(self):
        # Full unittest method names can push validated.hrp.tmp over Win32's
        # path limit in this workspace. Keep fixture paths short and preserve
        # the complete test identity separately for evidence navigation.
        case_id = hashlib.sha256(self._testMethodName.encode("utf-8")).hexdigest()[:12]
        self.base = self.artifact_root / ("case-" + case_id)
        self.base.mkdir()
        save(self.base / "case.json", {"test": self.id(), "method": self._testMethodName})
        self.state = self.base / "state"
        self.process = None
        self.settings = dict(budget_mib=96, hard_mib=128, reserve_mib=0,
                             sustain_ms=250, grace_ms=800, timeout_ms=25000)
        self.addCleanup(self.save_diagnosis)

    def save_diagnosis(self):
        save(self.base / "diagnosis.json", {"native_sha256_before": self.native_hash,
            "native_sha256_after": fingerprint(),
            "owner_pid": self.process.pid if self.process else None,
            "events": read_events(self.state, partial=True)})
        self.assertEqual(fingerprint(), self.native_hash, "main binary changed during fixtures")

    def launch_owned(self, command, label, *, env=None):
        # File-backed coordinator/helper logs avoid unread PIPE backpressure.
        out = (self.base / (label + "-stdout.log")).open("wb")
        err = (self.base / (label + "-stderr.log")).open("wb")
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, close_fds=True, env=env)
        finally:
            out.close()
            err.close()
        self.addCleanup(self.cleanup_owned, process, label)
        return process

    def cleanup_owned(self, process, label):
        killed = process.poll() is None
        if killed:
            process.kill()  # This exact Popen is owned by this invocation.
        process.wait(timeout=8)
        save(self.base / (label + "-outcome.json"),
             {"pid": process.pid, "returncode": process.returncode, "cleanup_killed": killed})

    def barrier_owner(self):
        """Build only an ignored fixture copy, while holding serial_fleet."""
        bundled = Path("C:/msys64/ucrt64/bin/gcc.exe")
        compiler = shutil.which("gcc") or (str(bundled) if bundled.is_file() else None)
        self.assertIsNotNone(compiler, "oplock setup barrier requires GCC")
        directory = self.base / "barrier-build"
        directory.mkdir()
        wrapper = directory / "barrier.c"
        wrapper.write_text(INTAKE_BARRIER, encoding="utf-8")
        owner = directory / "owner.exe"
        fleet = directory / "fleet.o"
        common = [compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror"]
        commands = [common + ["-Dhr_intake_schedule=fixture_intake_schedule", "-c",
            str(ROOT / "src" / "fleet.c"), "-o", str(fleet)],
            common + ["-municode", "-I", str(ROOT / "src"), str(wrapper), str(fleet),
                *[str(ROOT / "src" / (name + ".c"))
                  for name in ("policy", "support", "output", "intake")],
                "-o", str(owner), "-lole32", "-lpsapi"]]
        save(directory / "commands.json", commands)
        for index, command in enumerate(commands):
            with (directory / (str(index) + ".log")).open("wb") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                    timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0,
                (directory / (str(index) + ".log")).read_text(errors="replace"))
        return owner

    def start(self, *, timeout_ms=25000, intake_barrier=False):
        self.settings["timeout_ms"] = timeout_ms
        compiled = self.live.compile_session(self.settings)
        expected = struct.pack("<8sIQQQIII", b"HRFLEET1", 0, 96 * MIB, 128 * MIB,
                               0, 250, 800, timeout_ms)
        self.assertEqual(compiled, expected, "live settings must contain only the zero-row header")
        save(self.base / "settings.json", self.settings)
        atomic_bytes(self.base / "settings.hrs", compiled)
        binary = self.barrier_owner() if intake_barrier else FLEET
        environment = os.environ.copy()
        if intake_barrier:
            environment["HEADROOM_FIXTURE_INTAKE_DIR"] = str(self.base)
        self.process = self.launch_owned([str(binary), "--live-plan",
            str(self.base / "settings.hrs"), "--state", str(self.state)], "owner",
            env=environment)
        if intake_barrier:
            self.addCleanup(save, self.base / "release-intake", True)
        self.deadline = time.monotonic() + 35
        until(lambda: (self.state / "session.json").is_file() or
              self.process.poll() is not None, timeout=5)
        self.assertIsNone(self.process.poll(), "owner failed before publishing session.json")
        session = load(self.state / "session.json")
        for key in ("version", "max_jobs"):
            self.assertIs(type(session[key]), int)
        self.assertTrue(all(type(value) is int for value in session["settings"].values()))
        self.run_id = session["run_id"]
        self.assertEqual(str(uuid.UUID(self.run_id)), self.run_id)
        self.assertEqual(session, {"version": 1, "run_id": self.run_id,
            "max_jobs": 64, "settings": self.settings})
        self.owner = load(self.state / "owner.json")
        self.assertIs(type(self.owner["version"]), int)
        self.assertEqual(self.owner, {"version": 2, "mode": "live", "run_id": self.run_id,
                                     "job_name": "Local\\Headroom-" + self.run_id})
        self.assertEqual((self.state / "plan.hrp").read_bytes(), compiled)
        for name in ("inbox", "processed", "receipts"):
            self.assertTrue((self.state / name).is_dir())
        self.assertTrue((self.state / "owner.lock").is_file())
        return self.process

    def job(self, identifier, *, mode="work", claim=96, priority=50, release=None,
            seconds=0.05, request_id=None, payload=8):
        options = ["--seconds", seconds, "--payload-mib", payload]
        if release:
            options += ["--release", release]
        if request_id:
            options += ["--state", self.state, "--request-id", request_id]
        return {"id": identifier, "claim_mib": claim, "hard_mib": 128,
                "priority": priority, "allow_stop": False,
                "argv": fixture_command(mode, self.base / identifier, *options)}

    def receipt(self, request_id, *, accepted=True, task=None):
        path = self.state / "receipts" / (request_id + ".json")
        until(path.is_file, timeout=6)
        raw = path.read_bytes()
        self.assertLessEqual(len(raw), 4096)
        result = headroom.strict_json_loads(raw)
        self.assertEqual(set(result), RECEIPT_KEYS)
        for key in ("version", "code", "ordinal"):
            self.assertIs(type(result[key]), int)
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(result["status"], "accepted" if accepted else "rejected")
        if accepted:
            self.assertEqual(result["code"], 0)
            self.assertTrue(1 <= result["ordinal"] <= 64)
            self.assertRegex(result["task"], r"^[a-z0-9][a-z0-9-]{0,39}$")
        else:
            self.assertIn(result["code"], (64, 73, 75))
            self.assertEqual(result["ordinal"], 0)
            self.assertTrue(result["task"] == "" or
                            headroom.ID_PATTERN.fullmatch(result["task"]))
            self.assertNotIn(result["task"], headroom.RESERVED_IDS)
        if task is not None:
            self.assertEqual(result["task"], task)
        self.assertEqual(self.live.read_receipt(self.state, request_id), result)
        processed = self.state / "processed" / request_id
        self.assertTrue(processed.is_dir(), "claim must be processed/UUID without .ready")
        self.assertFalse((self.state / "inbox" / (request_id + ".ready")).exists())
        if accepted:
            self.assertEqual((processed / "target").read_bytes(),
                f"headroom-submit-v1\n{self.run_id}\n{request_id}\n".encode("ascii"))
            self.assertEqual((processed / "validated.hrp").read_bytes(),
                             (processed / "plan.hrp").read_bytes())
            full_plan = load(processed / "plan.json")
            self.assertEqual({k: v for k, v in full_plan.items() if k != "jobs"}, self.settings)
            self.assertEqual(len(full_plan["jobs"]), 1)
            self.assertEqual(headroom.compile_plan(full_plan), (processed / "validated.hrp").read_bytes())
            executable = full_plan["jobs"][0]["argv"][0]
            self.assertEqual(executable, str(Path(executable).resolve(strict=True)))
        return result

    def submit(self, job, request_id=None):
        request_id = request_id or str(uuid.uuid4())
        result = self.live.submit(self.state, job, request_id=request_id, wait_seconds=5)
        receipt = self.receipt(request_id, task=job["id"])
        self.assertEqual(result, receipt)
        return request_id, receipt

    def protect(self):
        release = self.base / "release-protected"
        self.submit(self.job("protected", claim=48, priority=100, release=release,
                             seconds=20, payload=16))
        until(lambda: (self.base / "protected" / "ready.json").is_file())
        return release

    def events(self):
        return read_events(self.state, partial=True)

    def seal(self):
        result = self.live.seal(self.state)
        self.assertEqual(result["status"], "seal_requested")
        self.assertEqual((self.state / "seal.request").read_bytes(),
                         f"headroom-seal-v1\n{self.run_id}\n".encode("ascii"))
        until(lambda: (self.state / "sealed.json").is_file(), timeout=5)
        self.assertEqual(load(self.state / "sealed.json"), {"version": 1, "run_id": self.run_id})
        return result

    def finish(self, expected=0):
        self.process.wait(timeout=max(0.1, self.deadline - time.monotonic()))
        events = read_events(self.state)
        self.assertEqual(self.process.returncode, expected,
            (self.base / "owner-stderr.log").read_text(errors="replace") + repr(events))
        self.assertTrue(events)
        self.assertEqual({e["run_id"] for e in events}, {self.run_id})
        self.assertFalse(select(events, "internal_error"), events)
        if expected == 0:
            self.assertEqual(events[-1]["event"], "finished")
            self.assertFalse(select(events, "deadline"))
        for event in select(events, "started"):
            self.assertEqual(len(select(events, "started", event["task"])), 1)
            queued = select(events, "queued", event["task"])
            self.assertEqual(len(queued), 1)
            self.assertLess(events.index(queued[0]), events.index(event))
        return events

    def raw_request(self, job, *, session=None, plan_bytes=None, json_bytes=None,
                    settings=None, request_id=None, publish=True):
        request_id = request_id or str(uuid.uuid4())
        stage = self.state / "inbox" / ("fixture-" + str(uuid.uuid4()))
        stage.mkdir()
        full = dict(settings or self.settings, jobs=[job])
        atomic_bytes(stage / "target", f"headroom-submit-v1\n{session or self.run_id}\n{request_id}\n".encode())
        atomic_bytes(stage / "plan.hrp", headroom.compile_plan(full) if plan_bytes is None else plan_bytes)
        atomic_bytes(stage / "plan.json", json.dumps(full, sort_keys=True,
            separators=(",", ":")).encode() if json_bytes is None else json_bytes)
        if publish:
            stage.rename(self.state / "inbox" / (request_id + ".ready"))
        return request_id, stage

    def test_session_metadata_zero_rows_empty_seal_and_batch_recovery_refusal(self):
        self.start()
        # The batch CLI must continue refusing zero rows before creating state.
        batch = self.launch_owned([str(FLEET), "--plan", str(self.base / "settings.hrs"),
                                  "--state", str(self.base / "batch-state")], "batch-negative")
        self.assertEqual(batch.wait(timeout=5), 64)
        self.assertFalse((self.base / "batch-state").exists())
        self.seal()
        self.assertEqual(self.live.seal(self.state)["status"], "seal_requested")
        events = self.finish()
        self.assertTrue(select(events, "sealed"))
        self.assertFalse(select(events, "started"))
        self.assertEqual(headroom.read_status(self.state)["status"], "no_work")
        # Reach old recovery's owner schema gate independently of its earlier
        # zero-row compiler gate; it must explicitly refuse native version 2.
        compiled = (self.state / "plan.hrp").read_bytes()
        with mock.patch.object(recover.headroom, "compile_plan", return_value=compiled):
            verdict = recover.prepare_recovery(self.base / "settings.json", self.state,
                                               self.base / "forbidden-recovery.hrp")
        self.assertEqual(verdict["status"], "refused", verdict)
        self.assertIn("owner", verdict["error"].lower())
        self.assertFalse((self.base / "forbidden-recovery.hrp").exists())
        self.assertFalse((self.state / "recovery-reserved.json").exists())

    def test_independent_concurrent_clients_queue_under_one_owner_while_protected_runs(self):
        self.start()
        release = self.protect()
        gate = self.base / "release-clients"
        clients = []
        for index in range(2):
            identifier = "client-" + str(index)
            request_id = str(uuid.uuid4())
            source = self.base / (identifier + ".json")
            save(source, self.job(identifier))
            output = self.base / (identifier + "-submitter")
            process = self.launch_owned(fixture_command("client", output, "--state", self.state,
                "--job-file", source, "--request-id", request_id, "--gate", gate), identifier)
            clients.append((process, output, identifier, request_id))
        for _, output, _, _ in clients:
            until(lambda output=output: (output / "ready.json").is_file())
        save(gate, True)
        ordinals = []
        for process, output, identifier, request_id in clients:
            self.assertEqual(process.wait(timeout=8), 0)
            result = load(output / "result.json")
            receipt = self.receipt(request_id, task=identifier)
            self.assertEqual(result["result"], receipt)
            ordinals.append(receipt["ordinal"])
            self.assertNotEqual(result["pid"], self.process.pid)
        self.assertEqual(len(set(ordinals)), 2)
        self.assertIsNone(self.process.poll())
        self.assertEqual(load(self.state / "owner.json"), self.owner)
        self.assertEqual([e["task"] for e in select(self.events(), "started")], ["protected"])
        with self.assertRaisesRegex(recover.RecoveryError, "LIVE/BUSY"):
            with recover.WindowsSafety().lease(self.state / "owner.lock"):
                self.fail("second owner acquired live lease")
        self.seal()
        save(release, True)
        events = self.finish()
        self.assertEqual({e["task"] for e in select(events, "completed")},
                         {"protected", "client-0", "client-1"})
        protected = records(self.base / "protected")[0]
        self.assertEqual(protected["parent_pid"], self.process.pid)
        for _, output, _, _ in clients:
            result = load(output / "result.json")
            self.assertLess(protected["started_ns"], result["started_ns"])
            self.assertLess(result["ended_ns"], protected["ended_ns"])
        for identifier in ("client-0", "client-1"):
            self.assertEqual(records(self.base / identifier)[0]["parent_pid"], self.process.pid)

    def test_priority_then_fifo_only_among_known_accepted_queued_tasks(self):
        self.start()
        release = self.protect()
        for identifier, priority in (("low", 5), ("high-first", 90), ("high-second", 90)):
            self.submit(self.job(identifier, priority=priority))
        self.assertEqual([e["task"] for e in select(self.events(), "started")], ["protected"])
        self.seal()
        save(release, True)
        events = self.finish()
        self.assertEqual([e["task"] for e in select(events, "started")],
                         ["protected", "high-first", "high-second", "low"])

    def test_exact_uuid_retry_and_replayed_ready_directory_never_duplicate_execution(self):
        self.start()
        release = self.protect()
        job = self.job("once")
        request_id, first = self.submit(job)
        processed = self.state / "processed" / request_id
        snapshot = {p.name: p.read_bytes() for p in processed.iterdir() if p.is_file()}
        self.assertEqual(self.live.submit(self.state, job, request_id=request_id, wait_seconds=5), first)
        # A second producer can reintroduce an old ready name; native must
        # honor the already processed UUID instead of admitting/running twice.
        stage = self.state / "inbox" / ("fixture-" + str(uuid.uuid4()))
        stage.mkdir()
        for name in ("target", "plan.hrp", "plan.json"):
            atomic_bytes(stage / name, snapshot[name])
        stage.rename(self.state / "inbox" / (request_id + ".ready"))
        # A later receipt proves intake progressed past an already processed
        # directory; no fixed sleep substitutes for observing native progress.
        self.submit(self.job("after-replay"))
        self.assertEqual({p.name: p.read_bytes() for p in processed.iterdir() if p.is_file()}, snapshot)
        self.assertEqual(self.live.read_receipt(self.state, request_id), first)
        self.seal()
        save(release, True)
        events = self.finish()
        self.assertEqual(len(select(events, "queued", "once")), 1)
        self.assertEqual(len(select(events, "completed", "once")), 1)
        self.assertEqual(len(records(self.base / "once")), 1)

    def test_uuid_with_different_content_refused_without_overwriting_original(self):
        self.start()
        release = self.protect()
        request_id, receipt = self.submit(self.job("original"))
        processed = self.state / "processed" / request_id
        before = {p.name: p.read_bytes() for p in processed.iterdir() if p.is_file()}
        with self.assertRaises(ValueError):
            self.live.submit(self.state, self.job("replacement"), request_id=request_id, wait_seconds=0)
        self.assertEqual({p.name: p.read_bytes() for p in processed.iterdir() if p.is_file()}, before)
        self.assertEqual(self.live.read_receipt(self.state, request_id), receipt)
        self.seal()
        save(release, True)
        events = self.finish()
        self.assertFalse(select(events, "started", "replacement"))
        self.assertEqual(len(select(events, "started", "original")), 1)

    def test_duplicate_job_id_is_rejected_without_stopping_protected_peer(self):
        self.start()
        release = self.protect()
        request_id, _ = self.raw_request(self.job("protected"))
        receipt = self.receipt(request_id, accepted=False)
        self.assertIn(receipt["task"], ("", "protected"))
        self.assertIsNone(self.process.poll())
        self.assertFalse(select(self.events(), "stopped", "protected"))
        self.submit(self.job("healthy"))
        self.seal()
        save(release, True)
        events = self.finish()
        self.assertEqual(len(select(events, "queued", "protected")), 1)
        self.assertEqual({e["task"] for e in select(events, "completed")}, {"protected", "healthy"})

    def test_malformed_cross_session_and_settings_mismatch_requests_preserve_peer(self):
        self.start()
        release = self.protect()
        multi_plan = dict(self.settings, jobs=[self.job("multi-first"), self.job("multi-second")])
        variants = [("bad-plan", {"plan_bytes": b"HRFLEET1\x01"}),
                    ("zero-jobs", {"plan_bytes": self.live.compile_session(self.settings)}),
                    ("multi-jobs", {"plan_bytes": headroom.compile_plan(multi_plan),
                                    "json_bytes": json.dumps(multi_plan).encode()}),
                    ("oversized-plan", {"plan_bytes": b"x" * (512 * 1024 + 1)}),
                    ("oversized-json", {"json_bytes": b" " * (MIB + 1)}),
                    ("cross-session", {"session": str(uuid.uuid4())}),
                    ("wrong-settings", {"settings": dict(self.settings, grace_ms=801)})]
        for identifier, options in variants:
            with self.subTest(variant=identifier):
                request_id, _ = self.raw_request(self.job(identifier), **options)
                self.receipt(request_id, accepted=False)
                retained = self.state / "processed" / request_id
                self.assertTrue(all((retained / name).is_file() for name in
                                    ("target", "plan.hrp", "plan.json")))
                self.assertFalse(select(self.events(), "started", identifier))
                self.assertIsNone(self.process.poll())
        self.submit(self.job("healthy"))
        self.seal()
        save(release, True)
        events = self.finish()
        self.assertEqual(select(events, "completed", "protected")[0]["code"], 0)
        self.assertFalse(select(events, "terminated"))
        self.assertFalse(select(events, "stopped"))

    def test_seal_waits_for_accepted_jobs_and_no_new_request_can_be_accepted(self):
        self.start()
        release = self.protect()
        self.submit(self.job("already-accepted"))
        self.seal()
        self.assertIsNone(self.process.poll(), "seal killed or abandoned accepted work")
        late_id, _ = self.raw_request(self.job("too-late"))
        save(release, True)
        events = self.finish()
        path = self.state / "receipts" / (late_id + ".json")
        if path.exists():
            result = load(path)
            self.assertEqual(result["status"], "rejected")
            self.assertIn(result["code"], (64, 73, 75))
            self.assertEqual(result["ordinal"], 0)
        else:
            result = self.live.read_receipt(self.state, late_id)
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["code"], 75)
            self.assertEqual(result["ordinal"], 0)
        self.assertFalse(select(events, "queued", "too-late"))
        self.assertFalse(select(events, "started", "too-late"))
        self.assertEqual({e["task"] for e in select(events, "completed")},
                         {"protected", "already-accepted"})
        self.assertLess(events.index(select(events, "sealed")[0]),
                        events.index(select(events, "completed", "already-accepted")[0]))

    def test_idle_owner_remains_alive_until_lifetime_deadline(self):
        started = time.monotonic()
        self.start(timeout_ms=3000)
        until(lambda: bool(select(self.events(), "sample")), timeout=2.5)
        self.assertIsNone(self.process.poll())
        events = self.finish(expected=124)
        self.assertEqual(len(select(events, "deadline")), 1)
        self.assertGreaterEqual(select(events, "deadline")[0]["elapsed_ms"], 3000)
        self.assertLess(time.monotonic() - started, 9)
        self.assertFalse(select(events, "started"))
        self.assertFalse((self.state / "sealed.json").exists())

    def test_owner_crash_closes_job_and_contains_owned_descendant(self):
        self.start()
        self.submit(self.job("tree", mode="tree", seconds=20, claim=96))
        until(lambda: (self.base / "tree" / "child" / "ready.json").is_file())
        tree = load(self.base / "tree" / "tree.json")
        self.assertTrue(alive(tree["root_pid"]))
        self.assertTrue(alive(tree["child_pid"]))
        self.process.kill()  # Crash only this newly launched native owner.
        self.process.wait(timeout=5)
        until(lambda: not alive(tree["root_pid"]) and not alive(tree["child_pid"]), timeout=5)
        events = self.events()
        self.assertFalse(select(events, "finished"))
        self.assertFalse(select(events, "completed", "tree"))
        self.assertFalse(select(events, "resume_queued"))

    def test_receipt_and_validated_bytes_visible_before_entry_and_output_is_bounded(self):
        self.start()
        request_id = str(uuid.uuid4())
        witness_output = self.base / "witness"
        witness = self.launch_owned(fixture_command("witness", witness_output,
            "--state", self.state, "--request-id", request_id, "--task", "probe",
            "--seconds", 8, "--payload-mib", 0), "witness")
        until(lambda: (witness_output / "ready.json").is_file())
        self.submit(self.job("probe", mode="probe", request_id=request_id), request_id)
        until(lambda: (self.base / "probe" / "receipt-at-entry.json").is_file())
        observed = load(self.base / "probe" / "receipt-at-entry.json")
        self.assertNotIn("error", observed)
        self.assertEqual(observed["receipt"], self.live.read_receipt(self.state, request_id))
        self.assertTrue(observed["validated_matches"])
        self.assertTrue(observed["queued"])
        self.assertEqual(witness.wait(timeout=3), 0)
        witnessed = load(witness_output / "receipt-at-started.json")
        self.assertNotIn("error", witnessed)
        self.assertEqual(witnessed["receipt"], observed["receipt"])
        self.assertTrue(witnessed["validated_matches"])
        self.assertTrue(witnessed["queued"])
        self.submit(self.job("flood", mode="flood"))
        self.seal()
        events = self.finish()
        for task in ("probe", "flood"):
            self.assertEqual(select(events, "completed", task)[0]["code"], 0)
            record = records(self.base / task)[0]
            self.assertLessEqual(record["payload_mib"], 16)
            self.assertLess(record["peak_private_bytes"], 128 * MIB)
        attempt = self.state / "flood" / "attempt-1"
        metadata = load(attempt / "output.json")
        self.assertEqual(metadata["limit_bytes"], MIB)
        for name, byte in (("stdout", b"O"), ("stderr", b"E")):
            self.assertEqual((attempt / (name + ".log")).read_bytes(), byte * MIB)
            self.assertEqual(metadata[name], {"retained_bytes": MIB,
                "discarded_bytes": 65536, "eof": True})

    def test_owned_input_oplock_cannot_block_lifetime_deadline(self):
        self.start(timeout_ms=5000, intake_barrier=True)
        until(lambda: (self.base / "intake-waiting").is_file() or
              self.process.poll() is not None, timeout=3)
        self.assertTrue((self.base / "intake-waiting").is_file())
        # A seal file uses the very same bounded intake worker and avoids an
        # unrelated Windows directory-rename restriction on open child files.
        locked_file = self.state / ("fixture-seal-" + str(uuid.uuid4()))
        atomic_bytes(locked_file, f"headroom-seal-v1\n{self.run_id}\n".encode("ascii"))
        helper_output = self.base / "locker"
        release = self.base / "release-oplock"
        acquire = self.base / "acquire-oplock"
        helper = self.launch_owned(fixture_command("oplock", helper_output,
            "--lock-file", locked_file, "--destination", self.state / "seal.request",
            "--gate", acquire, "--release", release, "--seconds", 15), "locker")
        # Always release our own helper before cleanup waits/kills it.
        self.addCleanup(save, acquire, True)
        self.addCleanup(save, release, True)
        # Force the former race window to exist. The fixture owner cannot scan
        # this published, unlocked seal until the helper confirms its oplock.
        until(lambda: (helper_output / "published.json").is_file() or
              helper.poll() is not None, timeout=3)
        self.assertTrue((helper_output / "published.json").is_file())
        self.assertTrue((self.state / "seal.request").is_file())
        self.assertFalse((helper_output / "oplock.json").exists())
        self.assertIsNone(self.process.poll())
        self.assertFalse(select(self.events(), "sealed"))
        save(acquire, True)
        until(lambda: (helper_output / "oplock.json").is_file() or helper.poll() is not None,
              timeout=3)
        self.assertTrue((helper_output / "oplock.json").is_file(), "oplock helper failed")
        lock = load(helper_output / "oplock.json")
        if not lock["granted"]:
            if lock["error"] in (1, 50, 87):
                self.skipTest("private filesystem does not support level-1 oplock: " + str(lock))
            self.fail("private fixture oplock was not granted: " + str(lock))
        save(self.base / "release-intake", True)
        until(lambda: (helper_output / "break.json").is_file() or
              self.process.poll() is not None, timeout=3)
        self.assertTrue((helper_output / "break.json").is_file(), "intake never attempted locked input")
        self.assertTrue((self.state / "seal.request").is_file())
        self.assertIsNone(helper.poll())
        self.assertFalse(release.exists())
        began_wait = time.monotonic()
        events = self.finish(expected=124)
        self.assertLess(time.monotonic() - began_wait, 8,
                        "blocked intake delayed owner deadline/shutdown")
        self.assertEqual(len(select(events, "deadline")), 1)
        self.assertGreaterEqual(select(events, "deadline")[0]["elapsed_ms"], 5000)
        self.assertFalse(select(events, "started"))
        self.assertFalse(select(events, "sealed"), "intake bypassed the unacknowledged file oplock")
        self.assertFalse((self.state / "sealed.json").exists())
        self.assertIsNone(helper.poll(), "owner shutdown terminated external owned lock helper")
        save(release, True)
        self.assertEqual(helper.wait(timeout=3), 0)
        self.assertEqual(load(helper_output / "released.json"),
                         {"explicit": True, "broken": True})

    def test_preexisting_validated_file_rejects_request_without_killing_peer(self):
        """An untrusted extra artifact is attributed without terminating peers."""
        self.start()
        protected_release = self.protect()
        request_id, stage = self.raw_request(self.job("extra-validated"), publish=False)
        evidence = b"fixture-owned preexisting validated bytes; never overwrite\n"
        atomic_bytes(stage / "validated.hrp", evidence)
        stage.rename(self.state / "inbox" / (request_id + ".ready"))
        self.receipt(request_id, accepted=False)
        self.assertEqual((self.state / "processed" / request_id / "validated.hrp").read_bytes(), evidence)
        self.assertIsNone(self.process.poll(), "untrusted extra file killed the owner")
        self.submit(self.job("healthy-after-extra"))
        self.seal()
        save(protected_release, True)
        events = self.finish()
        self.assertFalse(select(events, "queued", "extra-validated"))
        self.assertFalse(select(events, "started", "extra-validated"))
        self.assertEqual({e["task"] for e in select(events, "completed")},
                         {"protected", "healthy-after-extra"})

    def test_unrenamable_request_defers_only_itself_and_preserves_peer_admission(self):
        """One privately locked request cannot prevent healthy peer admission."""
        self.start()
        protected_release = self.protect()
        request_id, stage = self.raw_request(self.job("locked-request"), publish=False)
        destination = self.state / "inbox" / (request_id + ".ready")
        lock_output = self.base / "request-locker"
        lock_release = self.base / "release-request-lock"
        helper = self.launch_owned(fixture_command("rename-blocker", lock_output,
            "--lock-file", stage / "target", "--destination", destination,
            "--release", lock_release, "--seconds", 20), "request-locker")
        self.addCleanup(save, lock_release, True)
        until(lambda: (lock_output / "locked.json").is_file() or helper.poll() is not None,
              timeout=3)
        self.assertTrue((lock_output / "locked.json").is_file(), "private lock setup failed")
        self.assertIsNone(helper.poll())
        self.submit(self.job("healthy-beside-lock"))
        self.assertIsNone(self.process.poll(), "one unrenamable input killed its peer")
        self.assertTrue(destination.is_dir(), "locked request must remain deferred in inbox")
        self.assertEqual(self.live.read_receipt(self.state, request_id)["status"], "pending")
        self.assertFalse(select(self.events(), "internal_error"))
        save(lock_release, True)
        self.assertEqual(helper.wait(timeout=3), 0)
        self.receipt(request_id, task="locked-request")
        self.seal()
        save(protected_release, True)
        events = self.finish()
        self.assertEqual({e["task"] for e in select(events, "completed")},
                         {"protected", "healthy-beside-lock", "locked-request"})

    def test_scan_cursor_reaches_valid_request_after_256_ignored_entries(self):
        """Bounded scans retain enough progress to reach later valid input."""
        self.start()
        protected_release = self.protect()
        inbox = self.state / "inbox"
        # Fixed, small noise files sort before the UUID on this Windows fixture
        # volume. Neither filenames nor bytes resemble a published request.
        for index in range(300):
            atomic_bytes(inbox / ("00000000-noise-" + str(index).zfill(4)), b"ignored\n")
        request_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        self.raw_request(self.job("after-noise"), request_id=request_id)
        with os.scandir(inbox) as entries:
            prefix = [entry.name for _, entry in zip(range(256), entries)]
        self.assertNotIn(request_id + ".ready", prefix,
                         "fixture must place valid input beyond one bounded discovery page")
        self.receipt(request_id, task="after-noise")
        self.assertIsNone(self.process.poll())
        self.assertEqual(len(list(inbox.glob("00000000-noise-*"))), 300)
        self.seal()
        save(protected_release, True)
        events = self.finish()
        self.assertEqual({e["task"] for e in select(events, "completed")},
                         {"protected", "after-noise"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--go", action="store_true", help="main has built and authorized native tests")
    options, remaining = parser.parse_known_args()
    NATIVE_GO = NATIVE_GO or options.go
    program = unittest.main(argv=[sys.argv[0], *remaining], exit=False)
    result = program.result
    if getattr(LiveNativeTests, "artifact_root", None):
        save(LiveNativeTests.artifact_root / "test-result.json", {
            "tests_run": result.testsRun, "successful": result.wasSuccessful(),
            "native_sha256": LiveNativeTests.native_hash,
            "failures": [test.id() for test, _ in result.failures],
            "errors": [test.id() for test, _ in result.errors],
            "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped]})
    raise SystemExit(0 if result.wasSuccessful() else 1)
