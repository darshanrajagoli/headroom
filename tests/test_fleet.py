"""Serial native fleet integration suite; run: python tests/test_fleet.py -v.

No compiler/build step. Plans are encoded independently from PROTOCOL.md so
the native reader is exercised even while headroom.py is under construction.
All processes are newly launched owned fixtures. Payload per scenario <= 208
MiB, including the deliberate shared-root cap case. Run one suite at a time.
"""
import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FLEET = ROOT / "build" / "headroom-fleet.exe"
WORKER = ROOT / "tests" / "fleet_worker.py"
ARTIFACT_ROOT = None
MIB = 1024 * 1024
FIELDS = {"version", "run_id", "event", "elapsed_ms", "task", "priority",
          "attempt", "total_commit_bytes", "available_bytes", "code"}


def source_hashes():
    paths = [ROOT / "PROTOCOL.md", ROOT / "headroom.py", Path(__file__), WORKER,
             ROOT / "tests" / "fleet_demo.py"]
    paths += sorted((ROOT / "src").glob("*.c")) + sorted((ROOT / "src").glob("*.h"))
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def encode_plan(plan):
    """Exact v1 wire format, deliberately no dependency on the plan compiler."""
    jobs = plan["jobs"]
    result = bytearray(struct.pack("<8sIQQQIII", b"HRFLEET1", len(jobs),
        plan["budget_mib"] * MIB, plan["hard_mib"] * MIB,
        plan.get("reserve_mib", 0) * MIB, plan.get("sustain_ms", 250),
        plan.get("grace_ms", 1200), plan.get("timeout_ms", 15000)))
    for job in jobs:
        identifier = job["id"].encode("ascii")
        result += struct.pack("<I", len(identifier)) + identifier
        result += struct.pack("<IIIQQ", job.get("priority", 50),
            int(job.get("allow_stop", False)), job.get("max_resumes", 0),
            job["claim_mib"] * MIB, job["hard_mib"] * MIB)
        argv, resume = job["argv"], job.get("resume_argv", [])
        values = (str(Path(argv[0]).resolve()), subprocess.list2cmdline(argv),
                  str(Path(resume[0]).resolve()) if resume else "",
                  subprocess.list2cmdline(resume) if resume else "")
        for value in values:
            encoded = value.encode("utf-16le")
            result += struct.pack("<I", len(encoded) // 2) + encoded
    return bytes(result)


def make_plan(jobs, budget=128, hard=300, **options):
    plan = dict(budget_mib=budget, hard_mib=hard, reserve_mib=0,
                sustain_ms=250, grace_ms=1200, timeout_ms=15000, jobs=jobs)
    plan.update(options)
    return plan


def command(mode, output, *options):
    return [sys.executable, str(WORKER), mode, str(output), *map(str, options)]


def job(identifier, mode, output, *, priority=50, claim=32, hard=160,
        allow=False, resumes=0, resume_mode=None, options=()):
    result = {"id": identifier, "priority": priority, "claim_mib": claim,
              "hard_mib": hard, "allow_stop": allow,
              "max_resumes": resumes, "argv": command(mode, output, *options),
              "resume_argv": command(resume_mode, output) if resume_mode else []}
    return result


def pressure_plan(base, *, mode="grow", resume=True, protected_duration=4):
    base = Path(base)
    protected = base / "outputs" / "protected"
    low = base / "outputs" / "low"
    queued = base / "outputs" / "queued"
    return make_plan([
        job("protected", "protected", protected, priority=100, claim=48, hard=80,
            options=("--duration", protected_duration)),
        job("low", mode, low, priority=20, claim=32, allow=True,
            resumes=1 if resume else 0, resume_mode="resume" if resume else None,
            options=("--barrier", protected / "ready.json")),
        job("queued", "queued", queued, priority=10, claim=64, hard=100),
    ])


def read_events(state, *, partial=False):
    path = Path(state) / "fleet.jsonl"
    if not path.exists():
        return []
    raw = path.read_bytes()
    if partial:
        raw = raw[:raw.rfind(b"\n") + 1]
    return [json.loads(line) for line in raw.splitlines() if line]


def select(events, name, task=None):
    return [e for e in events if e["event"] == name and (task is None or e["task"] == task)]


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError("bounded fixture wait expired")


def alive(pid):
    """Only query a PID published by this test's newly owned fixture."""
    if os.name != "nt":
        raise RuntimeError("Windows integration helper")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def serial_fleet():
    """Reject concurrent fixture suites/demos on this host; do not wait forever."""
    path = Path(tempfile.gettempdir()) / "headroom-owned-fleet-fixtures.lock"
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def launch(plan, base, *, binary=FLEET, compiled_plan=None):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    plan_file = Path(compiled_plan) if compiled_plan else base / "plan.hrf"
    if compiled_plan is None:
        plan_file.write_bytes(encode_plan(plan))
    return subprocess.Popen([str(binary), "--plan", str(plan_file),
                             "--state", str(base / "state")],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def cleanup(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=8)


def records(output):
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(output).glob("run-*.json"))]


def audit_case(base):
    """Classify retained evidence without turning an unexplained failure green.

    A valid ack on disk is not proof that the native reader completed its read.
    Keep that distinction explicit in every diagnostic, particularly on exit.
    """
    base = Path(base)
    state = base / "state"
    events = read_events(state, partial=True)
    attempts = []
    findings = []
    for directory in sorted(state.glob("*/attempt-*")):
        task = directory.parent.name
        attempt = int(directory.name.removeprefix("attempt-"))
        journal = directory / "events.jsonl"
        raw = journal.read_bytes() if journal.is_file() else b""
        complete = raw[:raw.rfind(b"\n") + 1]
        local = [json.loads(line) for line in complete.splitlines() if line]
        request_path, ack_path = directory / "stop.request", directory / "stop.ack"
        request = request_path.read_bytes() if request_path.is_file() else None
        ack = ack_path.read_bytes() if ack_path.is_file() else None
        expected = (b"headroom-stop-v1\n" + local[0]["run_id"].encode("ascii") + b"\n") if local else None
        accepted = any(e["event"] == "checkpoint_acknowledged" and e["task"] == task and
                       e["attempt"] == attempt for e in events)
        stopped = [e for e in events if e["event"] == "stopped" and e["task"] == task and
                   e["attempt"] == attempt]
        valid_ack = expected is not None and ack == request == expected
        attempts.append({"task": task, "attempt": attempt, "valid_matching_ack_on_disk": valid_ack,
            "acknowledgement_observed": accepted,
            "terminal": [{key: e[key] for key in ("event", "code", "elapsed_ms")}
                         for e in local if e["event"] in ("stopped", "completed")],
            "peak_journal_commit_bytes": max((e.get("job_commit_bytes", 0) for e in local), default=0)})
        if valid_ack and stopped and not accepted:
            findings.append({"kind": "matching_ack_not_observed_before_finalization",
                "task": task, "attempt": attempt, "stopped_code": stopped[0]["code"],
                "qualification": "Native read-completion timing is unobserved; inspect fixture publication and exit timestamps."})
    first_attempt = {}
    for e in select(events, "started"):
        first_attempt.setdefault(e["task"], e["attempt"])
    for e in select(events, "pressure_intent"):
        if e["attempt"] > first_attempt.get(e["task"], e["attempt"]):
            findings.append({"kind": "pressure_on_resumed_attempt", "task": e["task"],
                             "attempt": e["attempt"], "total_commit_bytes": e["total_commit_bytes"]})
    fixture = []
    for path in sorted(base.rglob("run-*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        fields = ("mode", "pid", "control_attempt", "payload_mib", "peak_private_bytes", "exit_code",
                  "allocation_denied", "progress", "started_from", "started_ns", "request_seen_ns",
                  "checkpoint_published_ns", "ack_published_ns", "ended_ns")
        fixture.append({"evidence": path.relative_to(base).as_posix(),
                        **{key: item[key] for key in fields if key in item}})
        if item.get("allocation_denied"):
            findings.append({"kind": "fixture_allocation_denied", "evidence": path.relative_to(base).as_posix()})
    return {"schema_version": 1, "attempts": attempts, "findings": findings, "fixtures": fixture,
            "max_recorded_fleet_commit_bytes": max((e["total_commit_bytes"] for e in events), default=0),
            "timeline": [{key: e[key] for key in ("event", "task", "attempt", "code", "elapsed_ms")}
                         for e in events if e["event"] != "sample"]}


def pressure_proofs(base, events):
    """Evidence predicates shared with the demo; failures must be explicit."""
    base = Path(base)
    protected = records(base / "outputs" / "protected")
    low = records(base / "outputs" / "low")
    initial = [r for r in low if r["mode"] == "grow"]
    resumed = [r for r in low if r["mode"] == "resume"]
    checkpoint_path = base / "outputs" / "low" / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {}
    overlap = bool(protected and initial and
        min(protected[0].get("ended_ns", 0), initial[0].get("ended_ns", 0)) >
        max(protected[0]["started_ns"], initial[0]["started_ns"]))
    intents = select(events, "pressure_intent")
    attempts = select(events, "started", "low")
    return {
        "protected_completed": bool(select(events, "completed", "protected")) and
            bool(protected and protected[0].get("exit_code") == 0),
        "protected_within_48mib_claim": bool(protected and protected[0].get("payload_mib") == 16) and
            0 < (protected[0].get("peak_private_bytes") or 0) <= 48 * MIB,
        "overlap": overlap,
        "only_consented_low_selected": bool(intents) and all(e["task"] == "low" for e in intents),
        "checkpoint_acknowledged": bool(select(events, "checkpoint_acknowledged", "low")),
        "initial_zero_exit_stopped": bool(initial and initial[0].get("exit_code") == 0 and
                                         select(events, "stopped", "low")),
        "resume_recipe_used_once": len(attempts) == 2 and len(select(events, "resume_queued", "low")) == 1,
        "resumed_progress": bool(resumed and checkpoint and
            resumed[0].get("started_from") == checkpoint["progress"] and
            resumed[0].get("progress", 0) > checkpoint["progress"]),
        "queued_completed": bool(select(events, "completed", "queued")),
        "fleet_finished": bool(events and events[-1]["event"] == "finished"),
    }


class ProtocolEncodingTests(unittest.TestCase):
    def test_header_and_unicode_encoding(self):
        plan = make_plan([job("fixture", "argv", Path("नमस्ते output"))])
        raw = encode_plan(plan)
        self.assertEqual(struct.unpack_from("<8sI", raw), (b"HRFLEET1", 1))
        self.assertIn("नमस्ते output".encode("utf-16le"), raw)
        self.assertLess(len(raw), 16 * MIB)


@unittest.skipUnless(os.name == "nt" and FLEET.is_file(), "native fleet executable unavailable")
class FleetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.serial = serial_fleet()
        cls.serial.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.serial.__exit__(None, None, None)

    def setUp(self):
        if ARTIFACT_ROOT is None:
            temporary = tempfile.TemporaryDirectory(prefix="headroom-fleet-")
            self.addCleanup(temporary.cleanup)
            self.base = Path(temporary.name)
        else:
            self.base = ARTIFACT_ROOT / self._testMethodName
            self.base.mkdir()
        self.state = self.base / "state"
        self.addCleanup(self.save_diagnosis)

    def save_diagnosis(self):
        from fleet_worker import save
        save(self.base / "diagnosis.json", audit_case(self.base))

    def start(self, plan):
        process = launch(plan, self.base)
        self.addCleanup(cleanup, process)
        return process

    def finish(self, process, expected=0):
        from fleet_worker import atomic_bytes, save
        stdout, stderr = process.communicate(timeout=24)
        atomic_bytes(self.base / "stdout.log", stdout)
        atomic_bytes(self.base / "stderr.log", stderr)
        save(self.base / "process-outcome.json", {"expected_exit_code": expected,
                                                   "actual_exit_code": process.returncode})
        events = read_events(self.state)
        # Include terminal transitions even if the exit assertion fails; --report
        # also keeps each complete raw case in its unique evidence directory.
        diagnostic = [{key: event[key] for key in ("event", "task", "attempt", "code")}
                      for event in events if event["event"] != "sample"]
        self.assertEqual(process.returncode, expected,
                         stderr.decode(errors="replace") + "\n" + json.dumps(diagnostic))
        self.assertTrue(events, "fleet evidence missing")
        previous = -1
        run_ids = set()
        for event in events:
            self.assertTrue(FIELDS <= event.keys(), event)
            self.assertEqual(event["version"], 1)
            self.assertGreaterEqual(event["elapsed_ms"], previous)
            previous = event["elapsed_ms"]
            run_ids.add(event["run_id"])
        self.assertEqual(len(run_ids), 1)
        self.assertEqual(events[-1]["event"], "finished")
        self.assertFalse(select(events, "internal_error"))
        return events

    def test_priority_then_fifo_admission(self):
        jobs = [job("last", "queued", self.base / "last", priority=10, claim=48, hard=80),
                job("first", "queued", self.base / "first", priority=90, claim=48, hard=80),
                job("second", "queued", self.base / "second", priority=90, claim=48, hard=80)]
        events = self.finish(self.start(make_plan(jobs, budget=64)))
        self.assertEqual([e["task"] for e in select(events, "started")], ["first", "second", "last"])
        for before, after in (("first", "second"), ("second", "last")):
            self.assertLess(events.index(select(events, "completed", before)[0]),
                            events.index(select(events, "started", after)[0]))

    def test_completed_root_exit_259_is_not_a_live_process(self):
        output = self.base / "exit-259"
        plan = make_plan([job("exit-259", "exit", output, claim=48, hard=80,
                              options=("--exit-code", "259"))], timeout_ms=5000)
        events = self.finish(self.start(plan), expected=1)
        completed = select(events, "completed", "exit-259")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["code"], 259)
        self.assertEqual(records(output)[0]["exit_code"], 259)
        self.assertFalse(select(events, "deadline"))
        self.assertFalse(select(events, "resume_queued"))

    def test_strict_blocked_head_never_backfills(self):
        runner = self.base / "runner"
        jobs = [job("runner", "protected", runner, priority=100, claim=48, hard=80,
                    options=("--duration", "1.5", "--release", self.base / "release")),
                job("blocked", "queued", self.base / "blocked", priority=80, claim=64, hard=100),
                job("small", "queued", self.base / "small", priority=10, claim=16, hard=64)]
        process = self.start(make_plan(jobs, budget=96))
        until(lambda: (runner / "ready.json").exists())
        until(lambda: len(select(read_events(self.state, partial=True), "sample")) >= 3)
        observed = read_events(self.state, partial=True)
        self.assertEqual([e["task"] for e in select(observed, "started")], ["runner"])
        self.assertFalse((self.base / "small" / "ready.json").exists())
        (self.base / "release").write_text("release", encoding="ascii")
        events = self.finish(process)
        self.assertEqual([e["task"] for e in select(events, "started")], ["runner", "blocked", "small"])
        self.assertLess(events.index(select(events, "completed", "runner")[0]),
                        events.index(select(events, "started", "blocked")[0]))

    def test_startup_burst_is_sampled_before_admitting_queued_peer(self):
        from fleet_worker import save
        burst = self.base / "outputs" / "burst"
        queued = self.base / "outputs" / "queued"
        release = self.base / "release-burst"
        # Both claims fit exactly in an empty fleet (1 + 127 == 128).
        # Any observed startup commit above 1 MiB blocks the queued peer.
        # The burst retains 96 MiB until the test explicitly releases it.
        jobs = [job("burst", "startup-burst", burst, priority=100, claim=1, hard=160,
                    options=("--release", release)),
                job("queued", "queued", queued, priority=20, claim=127, hard=160)]
        process = self.start(make_plan(jobs))
        until(lambda: (burst / "ready.json").is_file())
        until(lambda: len(select(read_events(self.state, partial=True), "sample")) >= 2)
        observed = read_events(self.state, partial=True)
        self.assertEqual([e["task"] for e in select(observed, "started")], ["burst"])
        self.assertTrue(any(e["total_commit_bytes"] > MIB for e in select(observed, "sample")))
        self.assertFalse((queued / "ready.json").exists())
        save(release, True)
        events = self.finish(process)
        self.assertEqual([e["task"] for e in select(events, "started")], ["burst", "queued"])
        self.assertLess(events.index(select(events, "completed", "burst")[0]),
                        events.index(select(events, "started", "queued")[0]))
        self.assertEqual(records(burst)[0]["payload_mib"], 96)
        self.assertFalse(select(events, "pressure_intent"))

    def test_pressure_protects_honest_work_and_resumes_explicit_recipe(self):
        events = self.finish(self.start(pressure_plan(self.base)))
        proofs = pressure_proofs(self.base, events)
        self.assertTrue(all(proofs.values()), proofs)
        self.assertEqual([e["task"] for e in select(events, "pressure_intent")], ["low"])
        low_starts = select(events, "started", "low")
        self.assertEqual(low_starts[1]["attempt"], low_starts[0]["attempt"] + 1)
        order = [select(events, name, "low")[0] for name in
                 ("pressure_intent", "checkpoint_acknowledged", "stopped", "resume_queued")]
        self.assertEqual([events.index(e) for e in order], sorted(events.index(e) for e in order))
        self.assertLess(events.index(order[-1]), events.index(low_starts[1]))
        control = self.state / "low" / ("attempt-" + str(low_starts[0]["attempt"]))
        self.assertEqual((control / "stop.request").read_bytes(), (control / "stop.ack").read_bytes())
        self.assertTrue((control / "events.jsonl").is_file())

    def test_healthy_overclaim_does_not_request_stop(self):
        output = self.base / "low"
        plan = make_plan([job("low", "grow", output, claim=32, allow=True)], budget=200)
        events = self.finish(self.start(plan))
        self.assertFalse(select(events, "pressure_intent"))
        self.assertFalse(select(events, "stopped"))
        self.assertEqual(records(output)[0]["payload_mib"], 96)
        self.assertTrue(any(e["total_commit_bytes"] > 32 * MIB for e in select(events, "sample")))
        self.assertFalse(list(self.state.rglob("stop.request")))

    def test_accepted_zero_exit_without_recipe_remains_stopped(self):
        events = self.finish(self.start(pressure_plan(self.base, resume=False)), expected=1)
        self.assertTrue(select(events, "stopped", "low"))
        self.assertTrue(select(events, "checkpoint_acknowledged", "low"))
        self.assertFalse(select(events, "completed", "low"))
        self.assertFalse(select(events, "resume_queued"))
        self.assertEqual(records(self.base / "outputs" / "low")[0]["exit_code"], 0)
        self.assertTrue(select(events, "completed", "protected"))

    def test_no_resume_without_ack_even_with_recipe(self):
        events = self.finish(self.start(pressure_plan(self.base, mode="noack")), expected=1)
        self.assertTrue(select(events, "pressure_intent", "low"))
        self.assertTrue(select(events, "terminated", "low"))
        self.assertFalse(select(events, "checkpoint_acknowledged", "low"))
        self.assertFalse(select(events, "resume_queued"))
        self.assertEqual(len(select(events, "started", "low")), 1)
        self.assertTrue(select(events, "completed", "protected"))

    def test_wrong_nonce_cannot_authorize_resume(self):
        events = self.finish(self.start(pressure_plan(self.base, mode="wrongack")), expected=1)
        self.assertTrue(select(events, "pressure_intent", "low"))
        self.assertFalse(select(events, "checkpoint_acknowledged", "low"))
        self.assertFalse(select(events, "resume_queued"))
        self.assertEqual(len(select(events, "started", "low")), 1)

    def test_resume_count_is_exhausted_after_one_retry(self):
        # Both attempts become over-claim while the protected barrier stays up.
        output = self.base / "outputs"
        jobs = [job("protected", "protected", output / "protected", priority=100,
                    claim=48, hard=80, options=("--duration", "6")),
                job("low", "grow", output / "low", priority=10, claim=24, allow=True,
                    resumes=1, resume_mode="regrow",
                    options=("--barrier", output / "protected" / "ready.json"))]
        events = self.finish(self.start(make_plan(jobs, budget=80)), expected=1)
        self.assertEqual(len(select(events, "started", "low")), 2)
        self.assertEqual(len(select(events, "resume_queued", "low")), 1)
        self.assertEqual(len(select(events, "stopped", "low")), 2)
        self.assertEqual(len(select(events, "checkpoint_acknowledged", "low")), 2)
        self.assertFalse(select(events, "completed", "low"))
        self.assertTrue(select(events, "completed", "protected"))

    def test_nested_cap_is_shared_by_actual_children(self):
        output = self.base / "tree"
        plan = make_plan([job("tree", "cap-tree", output, claim=112, hard=112)], budget=192)
        events = self.finish(self.start(plan))
        aggregate = json.loads((output / "aggregate.json").read_text(encoding="utf-8"))
        self.assertEqual(len(aggregate["children"]), 2)
        self.assertTrue(any(child["denied"] for child in aggregate["children"]))
        self.assertLessEqual(aggregate["payload_mib"], 128)
        self.assertFalse(select(events, "pressure_intent"))
        for path in output.glob("child-*-pid.json"):
            self.assertFalse(alive(json.loads(path.read_text())))

    def test_shared_root_cap_across_logical_jobs_preserves_protected_peer(self):
        from fleet_worker import save
        protected = self.base / "outputs" / "protected"
        release = self.base / "release-capped"
        protected_release = self.base / "release-protected"
        jobs = [job("protected", "protected", protected, priority=100, claim=48, hard=80,
                    options=("--release", protected_release))]
        for identifier in ("cap-a", "cap-b"):
            jobs.append(job(identifier, "capped", self.base / "outputs" / identifier,
                priority=20, claim=32, hard=160,
                options=("--barrier", protected / "ready.json", "--release", release)))
        process = self.start(make_plan(jobs, budget=128, hard=192))
        paths = [self.base / "outputs" / identifier / "allocation.json"
                 for identifier in ("cap-a", "cap-b")]
        until(lambda: all(path.is_file() for path in paths))
        allocations = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        self.assertTrue(any(item["allocation_denied"] for item in allocations), allocations)
        self.assertTrue(all(0 < item["payload_mib"] <= 96 for item in allocations), allocations)
        # The separate nested caps have headroom. Denial is from the common
        # outer job, and both contributors still retain their payload here.
        self.assertTrue(all(item["peak_private_bytes"] < 160 * MIB for item in allocations), allocations)
        self.assertTrue(all(alive(item["pid"]) for item in allocations))
        self.assertTrue(alive(json.loads((protected / "ready.json").read_text())["pid"]))
        observed = read_events(self.state, partial=True)
        self.assertEqual({event["task"] for event in select(observed, "started")},
                         {"protected", "cap-a", "cap-b"})
        self.assertFalse(select(observed, "pressure_intent"))
        save(release, True)
        save(protected_release, True)
        events = self.finish(process, expected=1)
        self.assertEqual(select(events, "completed", "protected")[0]["code"], 0)
        self.assertTrue(any(e["code"] == 71 for e in select(events, "completed") if e["task"] != "protected"))
        self.assertFalse(select(events, "stopped"))
        self.assertFalse(select(events, "resume_queued"))
        self.assertFalse(list(self.state.rglob("stop.request")))
        protected_record = records(protected)[0]
        self.assertLessEqual(protected_record["peak_private_bytes"], 48 * MIB)
        self.assertFalse(alive(protected_record["pid"]))
        self.assertTrue(all(not alive(item["pid"]) for item in allocations))

    def test_normal_peer_failure_does_not_resume_or_stop_protected_work(self):
        protected = self.base / "outputs" / "protected"
        failed = self.base / "outputs" / "failed"
        jobs = [job("protected", "protected", protected, priority=100, claim=48, hard=80),
                job("failed", "exit", failed, priority=20, claim=32, allow=True,
                    resumes=1, resume_mode="resume",
                    options=("--barrier", protected / "ready.json", "--duration", "0.5", "--exit-code", "17")),
                job("queued", "queued", self.base / "outputs" / "queued", priority=10,
                    claim=64, hard=100)]
        events = self.finish(self.start(make_plan(jobs)), expected=1)
        self.assertEqual(select(events, "completed", "failed")[0]["code"], 17)
        self.assertEqual(len(select(events, "started", "failed")), 1)
        self.assertFalse(select(events, "resume_queued"))
        self.assertFalse(select(events, "pressure_intent"))
        self.assertFalse(select(events, "stopped", "protected"))
        self.assertEqual(select(events, "completed", "protected")[0]["code"], 0)
        self.assertEqual(select(events, "completed", "queued")[0]["code"], 0)
        self.assertLess(records(failed)[0]["ended_ns"], records(protected)[0]["ended_ns"])
        self.assertLessEqual(records(protected)[0]["peak_private_bytes"], 48 * MIB)

    @unittest.skipUnless((ROOT / "headroom.py").is_file(), "offline plan compiler unavailable")
    def test_invalid_executable_launch_failure_preserves_running_protected_peer(self):
        from fleet_worker import save
        protected = self.base / "outputs" / "protected"
        invalid_executable = self.base / "fresh invalid.exe"
        # An existing .exe passes offline existence validation, but these bytes
        # cannot be loaded as a Windows PE. Never use a host-installed program.
        with invalid_executable.open("xb") as stream:
            stream.write(b"headroom bounded invalid executable fixture\r\n")
        protected_job = job("protected", "protected", protected,
                            priority=100, claim=48, hard=80)
        bad_job = {"id": "bad", "priority": 20, "claim_mib": 32,
                   "hard_mib": 80, "allow_stop": False, "max_resumes": 0,
                   "argv": [str(invalid_executable)], "resume_argv": []}
        plan = make_plan([protected_job, bad_job])
        source = self.base / "plan.json"
        compiled_plan = self.base / "plan.hrf"
        save(source, plan)
        compiled = subprocess.run([sys.executable, str(ROOT / "headroom.py"), "compile",
                                   str(source), str(compiled_plan)],
                                  capture_output=True, timeout=8)
        self.assertEqual(compiled.returncode, 0, compiled.stderr.decode(errors="replace"))
        self.assertTrue(invalid_executable.is_file())
        self.assertTrue(compiled_plan.is_file())
        process = launch(plan, self.base, compiled_plan=compiled_plan)
        self.addCleanup(cleanup, process)
        events = self.finish(process, expected=1)
        failures = select(events, "launch_failed")
        self.assertEqual([event["task"] for event in failures], ["bad"])
        self.assertEqual(failures[0]["code"], 70)
        self.assertEqual([event["task"] for event in select(events, "started")], ["protected"])
        completed = select(events, "completed", "protected")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["code"], 0)
        # Admission must resume the first peer before attempting the invalid
        # executable, and its normal completion must occur after that failure.
        self.assertLess(events.index(select(events, "started", "protected")[0]),
                        events.index(failures[0]))
        self.assertLess(events.index(failures[0]), events.index(completed[0]))
        self.assertFalse(select(events, "stopped"))
        self.assertFalse(select(events, "terminated"))
        self.assertFalse(select(events, "resume_queued"))
        self.assertFalse(select(events, "pressure_intent"))
        self.assertFalse([event for event in select(events, "completed", "bad") if event["code"] == 0])
        protected_record = records(protected)[0]
        self.assertEqual(protected_record["exit_code"], 0)
        self.assertLessEqual(protected_record["peak_private_bytes"], 48 * MIB)
        self.assertTrue((protected / "completed.json").is_file())
        self.assertFalse(alive(protected_record["pid"]))

    def test_coordinator_crash_cleans_root_and_descendant(self):
        output = self.base / "tree"
        plan = make_plan([job("tree", "tree", output, claim=48, hard=112)], budget=192)
        process = self.start(plan)
        until(lambda: (output / "child-0" / "ready.json").exists())
        parent = json.loads((output / "ready.json").read_text())["pid"]
        child = json.loads((output / "child-0-pid.json").read_text())
        self.assertTrue(alive(parent))
        self.assertTrue(alive(child))
        process.kill()
        process.communicate(timeout=8)
        until(lambda: not alive(parent) and not alive(child))
        events = read_events(self.state, partial=True)
        self.assertFalse(select(events, "finished"))
        self.assertFalse(select(events, "completed"))
        self.assertFalse(select(events, "resume_queued"))

    def test_bad_binary_and_trailing_bytes_are_rejected_before_state(self):
        valid = encode_plan(make_plan([job("unused", "queued", self.base / "must-not-launch")]))
        variants = {"magic": b"BADMAGIC" + valid[8:], "truncated": valid[:-1],
                    "trailing": valid + b"\x00", "empty": b"", "count": valid[:8] + struct.pack("<I", 65) + valid[12:]}
        # Mutate native wire bytes directly, bypassing Python's strict Unicode
        # encoder. Keep every other field, length and EOF boundary well formed.
        resumable = encode_plan(make_plan([job("unused", "queued", self.base / "must-not-launch",
                                               allow=True, resumes=1, resume_mode="resume")]))
        offset = struct.calcsize("<8sIQQQIII")
        identifier_length = struct.unpack_from("<I", resumable, offset)[0]
        offset += 4 + identifier_length + struct.calcsize("<IIIQQ")
        malformed = {"lone-high": (0xD800,), "lone-low": (0xDC00,),
                     "high-ascii": (0xD800, 0x0078), "high-high": (0xD800, 0xD801),
                     "reversed": (0xDC00, 0xD800), "pair-then-high": (0xD83C, 0xDF31, 0xD800)}
        for field in ("executable", "command", "resume-executable", "resume-command"):
            length = struct.unpack_from("<I", resumable, offset)[0]
            end = offset + 4 + 2 * length
            for name, units in malformed.items():
                payload = struct.pack("<" + "H" * len(units), *units)
                replacement = struct.pack("<I", len(units)) + payload
                variants["surrogate-" + field + "-" + name] = resumable[:offset] + replacement + resumable[end:]
            offset = end
        for name, data in variants.items():
            with self.subTest(name=name):
                plan_file, state = self.base / (name + ".hrf"), self.base / (name + "-state")
                plan_file.write_bytes(data)
                process = subprocess.run([str(FLEET), "--plan", str(plan_file), "--state", str(state)],
                                         capture_output=True, timeout=6)
                self.assertEqual(process.returncode, 64, process.stderr.decode(errors="replace"))
                self.assertFalse(state.exists())
                self.assertFalse((self.base / "must-not-launch").exists())

    def test_unicode_and_windows_quoting_round_trip(self):
        output = self.base / "नमस्ते output"
        arguments = ["", "hello world", 'quoted"value', "trailing\\", 'backslash\\"quote',
                     "नमस्ते", "emoji-🌱", "C:\\a path\\"]
        plan = make_plan([job("unicode", "argv", output, claim=48, hard=80,
                              options=("--", *arguments))])
        self.state = self.base / "state"  # Plan and output carry Unicode paths.
        events = self.finish(self.start(plan))
        self.assertTrue(select(events, "completed", "unicode"))
        self.assertEqual(json.loads((output / "argv.json").read_text(encoding="utf-8")), arguments)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--log", type=Path)
    options, remaining = parser.parse_known_args()
    if options.report:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT_ROOT = Path(tempfile.mkdtemp(prefix="fleet-tests-", dir=options.report.parent)).resolve()
    binary_hash = hashlib.sha256(FLEET.read_bytes()).hexdigest() if FLEET.is_file() else None
    initial_sources = source_hashes()
    started = time.monotonic()
    with contextlib.ExitStack() as stack:
        streams = [sys.stderr]
        if options.log:
            options.log.parent.mkdir(parents=True, exist_ok=True)
            streams.append(stack.enter_context(options.log.open("w", encoding="utf-8")))

        class EvidenceStream:
            def write(self, value):
                for stream in streams:
                    stream.write(value)
                self.flush()

            def flush(self):
                for stream in streams:
                    stream.flush()

        runner = unittest.TextTestRunner(stream=EvidenceStream(), verbosity=2)
        program = unittest.main(argv=[sys.argv[0], *remaining], testRunner=runner, exit=False)
    result = program.result
    final_binary_hash = hashlib.sha256(FLEET.read_bytes()).hexdigest() if FLEET.is_file() else None
    binary_unchanged = binary_hash == final_binary_hash
    final_sources = source_hashes()
    sources_unchanged = initial_sources == final_sources
    if options.report:
        from fleet_worker import save
        save(options.report, {
            "schema_version": 1, "tests_run": result.testsRun,
            "successful": result.wasSuccessful() and binary_unchanged and sources_unchanged,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "native_binary_sha256": binary_hash,
            "native_binary_sha256_after": final_binary_hash,
            "binary_unchanged": binary_unchanged,
            "sources_unchanged": sources_unchanged,
            "raw_evidence": ARTIFACT_ROOT.relative_to(options.report.parent.resolve()).as_posix(),
            "source_sha256": initial_sources, "source_sha256_after": final_sources,
            "failures": [test.id() for test, _ in result.failures],
            "errors": [test.id() for test, _ in result.errors],
            "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        })
    raise SystemExit(0 if result.wasSuccessful() and binary_unchanged and sources_unchanged else 1)
