"""Bounded equal-work comparison; worker commit is separate from coordinator cost.

Native runs require a main-approved, frozen binary SHA. Source/binary pairing is
recorded, not proof of reproducible compilation. All raw paths remain private.
Run --self-test before supplying --expected-binary-sha256 for measurements.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import uuid

from benchmark import kernel, snapshot
from comparison_worker import STEPS, STEP_SECONDS, ROLES, checksum_at, save, read_checkpoint
from test_fleet import ROOT, FLEET, MIB, encode_plan, make_plan, read_events, serial_fleet

WORKER = ROOT / "tests" / "comparison_worker.py"
ACCESS = 0x100000 | 0x0400 | 0x0010
kernel.OpenJobObjectW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
kernel.OpenJobObjectW.restype = w.HANDLE
kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
kernel.CreateJobObjectW.restype = w.HANDLE
kernel.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
kernel.IsProcessInJob.restype = w.BOOL
kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
kernel.AssignProcessToJobObject.restype = w.BOOL
kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
kernel.SetInformationJobObject.restype = w.BOOL
kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
kernel.WaitForSingleObject.restype = w.DWORD


class BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
               ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", w.DWORD),
               ("MinimumWorkingSetSize", ctypes.c_size_t),
               ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
               ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD),
               ("SchedulingClass", w.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters)] + [
        (name, ctypes.c_size_t) for name in ("ProcessMemoryLimit", "JobMemoryLimit",
                                           "PeakProcessMemoryUsed", "PeakJobMemoryUsed")]


def require(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def new_owned_job():
    handle = require(kernel.CreateJobObjectW(None, None))
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE only
        require(kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        return handle
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def provenance():
    paths = [ROOT / "PROTOCOL.md", ROOT / "build.ps1", ROOT / "tests" / "benchmark.py",
             ROOT / "tests" / "test_fleet.py", WORKER, Path(__file__),
             ROOT / "tests" / "fleet_benchmark.py"]
    paths += sorted((ROOT / "src").glob("*.c")) + sorted((ROOT / "src").glob("*.h"))
    return {"native_binary_sha256": sha(FLEET), "source_sha256": {
        path.relative_to(ROOT).as_posix(): sha(path) for path in paths if path.is_file()}}


def check_frozen(expected):
    before = provenance()
    if before["native_binary_sha256"].lower() != expected.lower():
        raise RuntimeError("binary differs from the explicitly frozen SHA; native run refused")
    return before


def check_integration(expected, report_path=None):
    report_path = Path(report_path) if report_path else ROOT / "evidence" / "fleet-tests.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (not report.get("successful") or report.get("tests_run", 0) < 14 or
            report.get("skipped") or not report.get("binary_unchanged") or
            report.get("native_binary_sha256", "").lower() != expected.lower() or
            report.get("native_binary_sha256_after", "").lower() != expected.lower()):
        raise RuntimeError("matching full successful fleet integration evidence is required before performance runs")
    current = provenance()["source_sha256"]
    for name, digest in current.items():
        if name.startswith("src/") and report.get("source_sha256_after", {}).get(name) != digest:
            raise RuntimeError("native source differs from successful integration report")
    return dict(report_sha256=sha(report_path), tests_run=report["tests_run"],
                native_binary_sha256=expected.lower())


def command(role, output, *extra):
    return [sys.executable, str(WORKER), role, str(output), *map(str, extra)]


def comparison_plan(base):
    jobs = []
    for role, priority, claim, hard in (("protected", 100, 48, 80),
            ("growing", 20, 32, 160), ("small", 10, 64, 80)):
        argv = command(role, base / "outputs" / role)
        jobs.append(dict(id=role, priority=priority, claim_mib=claim, hard_mib=hard,
            allow_stop=role == "growing", max_resumes=3 if role == "growing" else 0,
            argv=argv, resume_argv=[*argv, "--resume"] if role == "growing" else []))
    return make_plan(jobs, budget=96, hard=256, sustain_ms=200,
                     grace_ms=1500, timeout_ms=18000)


class NativeObserver:
    """Pin journal-started roots, prove owned root membership, then sample.

    The query job handle is closed BEFORE killing a failed coordinator. Holding
    it during kill would mask kernel kill-on-last-job-handle-close containment.
    """
    def __init__(self, process, state):
        self.process, self.state = process, Path(state)
        self.job = None
        self.handles = {}
        self.coordinator = require(kernel.OpenProcess(ACCESS, False, process.pid))
        self.run_id = None

    def update(self):
        events = read_events(self.state, partial=True)
        if not self.job:
            owner_path = self.state / "owner.json"
            if not owner_path.exists() or not events:
                return events
            # owner.json is flushed but not atomically published; wait for JSON.
            try:
                owner = json.loads(owner_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return events
            identifier = str(uuid.UUID(owner["run_id"]))
            if (owner.get("version") != 1 or owner["job_name"] != "Local\\Headroom-" + identifier or
                    any(event["run_id"] != identifier for event in events)):
                raise RuntimeError("named job ownership evidence mismatch")
            self.job = kernel.OpenJobObjectW(4, False, owner["job_name"])
            if not self.job:
                # owner precedes CreateJobObject; it may not exist yet.
                if ctypes.get_last_error() == 2 and self.process.poll() is None:
                    return events
                raise ctypes.WinError(ctypes.get_last_error())
            self.run_id = identifier
        if self.job:
            if any(event["run_id"] != self.run_id for event in events):
                raise RuntimeError("mixed native journal identities")
            for event in events:
                if event["event"] != "started":
                    continue
                key = (event["task"], event["attempt"])
                if key in self.handles:
                    continue
                pid = event["code"]
                handle = require(kernel.OpenProcess(ACCESS, False, pid))
                try:
                    member = w.BOOL()
                    require(kernel.IsProcessInJob(handle, self.job, ctypes.byref(member)))
                    if not member.value:
                        raise RuntimeError("journal PID is outside the owned named root job")
                    self.handles[key] = (handle, pid)
                except BaseException:
                    kernel.CloseHandle(handle)
                    raise
        return events

    def sample(self):
        return aggregate([handle for handle, _ in self.handles.values()])

    def close(self):
        if self.job:
            kernel.CloseHandle(self.job)
            self.job = None
        for handle, _ in self.handles.values():
            kernel.CloseHandle(handle)
        self.handles.clear()
        if self.coordinator:
            kernel.CloseHandle(self.coordinator)
            self.coordinator = None


def aggregate(handles):
    total = 0
    active = 0
    for handle in handles:
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait == 0:
            continue
        if wait != 258:
            raise RuntimeError("owned process liveness query failed")
        try:
            sample = snapshot(handle)
        except OSError:
            if kernel.WaitForSingleObject(handle, 0) == 0:
                continue
            raise
        total += sample["private_commit_bytes"]
        active += 1
    return dict(worker_private_commit_bytes=total, active_workers=active)


def memory_summary(samples):
    area = sum((right["seconds"] - left["seconds"]) *
               (left["worker_private_commit_bytes"] + right["worker_private_commit_bytes"]) / 2
               for left, right in zip(samples, samples[1:]))
    gaps = [b["seconds"] - a["seconds"] for a, b in zip(samples, samples[1:])]
    return dict(sampled_peak_worker_private_commit_bytes=max(
        sample["worker_private_commit_bytes"] for sample in samples),
        worker_private_commit_area_byte_seconds=area, samples=len(samples),
        max_sample_gap_seconds=max(gaps, default=0))


def verify_outputs(base, events=None):
    outcomes = {}
    for role in ROLES:
        directory = Path(base) / "outputs" / role
        attempts = sorted((json.loads(path.read_text(encoding="utf-8"))
            for path in directory.glob("attempt-*.json")), key=lambda record: record["started_ns"])
        completed = json.loads((directory / "completed.json").read_text(encoding="utf-8"))
        steps = []
        progress = 0
        previous_end = None
        for index, record in enumerate(attempts):
            if (record["role"] != role or record["goal_steps"] != STEPS or
                    record["step_seconds"] != STEP_SECONDS or
                    bool(record["resume"]) != bool(index)):
                raise AssertionError("attempt did not use identical fixed logical work")
            if record["started_from"] != progress or record["initial_checksum"] != checksum_at(role, progress):
                raise AssertionError("resume lost or replayed logical progress")
            if index and (not record["resume"] or role != "growing" or record["scratch_initial_mib"] != 0):
                raise AssertionError("resume did not restart from empty disposable scratch")
            if previous_end and previous_end > record["started_ns"]:
                raise AssertionError("overlapping attempts of one logical job")
            executed = record["executed_steps"]
            if executed != list(range(progress + 1, record["progress"] + 1)):
                raise AssertionError("attempt skipped/repeated steps")
            progress = record["progress"]
            if record["checksum"] != checksum_at(role, progress) or record["exit_code"] != 0:
                raise AssertionError("incorrect attempt result")
            if record["stopped"] != (index != len(attempts) - 1):
                raise AssertionError("non-final attempts must be checkpointed stops")
            steps.extend(executed)
            previous_end = record["ended_ns"]
        if (not attempts or steps != list(range(1, STEPS + 1)) or
                completed["progress"] != STEPS or completed["checksum"] != checksum_at(role, STEPS)):
            raise AssertionError("logical work goal/checksum mismatch")
        if events is not None:
            starts = [event for event in events if event["event"] == "started" and event["task"] == role]
            if len(starts) != len(attempts) or [e["code"] for e in starts] != [a["pid"] for a in attempts]:
                raise AssertionError("worker records do not match journal-started process identities")
            if not any(event["event"] == "completed" and event["task"] == role and event["code"] == 0 for event in events):
                raise AssertionError("native job did not complete")
        outcomes[role] = dict(progress=progress, checksum=completed["checksum"], attempts=len(attempts),
            first_started_ns=attempts[0]["started_ns"], ended_ns=completed["ended_ns"],
            active_work_seconds=sum((record["ended_ns"] - record["started_ns"]) / 1e9 for record in attempts),
            resume_wait_seconds=sum((later["started_ns"] - earlier["ended_ns"]) / 1e9
                for earlier, later in zip(attempts, attempts[1:])),
            peak_retained_payload_mib=max(record["payload_peak_mib"] for record in attempts))
    if (outcomes["protected"]["peak_retained_payload_mib"] != 16 or
            outcomes["small"]["peak_retained_payload_mib"] != 8 or
            outcomes["growing"]["peak_retained_payload_mib"] > 96):
        raise AssertionError("fixture exceeded the 120 MiB aggregate retained-payload bound")
    return outcomes


def run_trial(mode, base):
    base.mkdir(parents=True, exist_ok=False)
    plan = comparison_plan(base)
    save(base / "plan.json", plan)
    processes, direct_handles = [], []
    observer = None
    owned_job = None
    samples = []
    events = None
    start_ns = time.monotonic_ns()
    try:
        if mode == "native":
            plan_path = base / "plan.hrp"
            plan_path.write_bytes(encode_plan(plan))
            with (base / "coordinator.stderr").open("wb") as stderr:
                process = subprocess.Popen([str(FLEET), "--plan", str(plan_path),
                    "--state", str(base / "state")], stdout=subprocess.DEVNULL, stderr=stderr)
            processes.append(process)
            observer = NativeObserver(process, base / "state")
        else:
            owned_job = new_owned_job()
            for task in plan["jobs"]:
                # Barrier prevents payload/work until all new children are owned.
                process = subprocess.Popen([*task["argv"], "--start-barrier", str(base / "release")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={key: value for key, value in os.environ.items() if key != "HEADROOM_CONTROL_DIR"})
                processes.append(process)
                handle = require(kernel.OpenProcess(ACCESS | 0x0100 | 0x0001, False, process.pid))
                direct_handles.append(handle)
                require(kernel.AssignProcessToJobObject(owned_job, handle))
                member = w.BOOL()
                require(kernel.IsProcessInJob(handle, owned_job, ctypes.byref(member)))
                if not member.value:
                    raise RuntimeError("baseline child ownership verification failed")
            save(base / "release", True)
        deadline = time.monotonic() + 22
        while True:
            if observer:
                events = observer.update()
                sample = observer.sample()
            else:
                sample = aggregate(direct_handles)
            sample["seconds"] = (time.monotonic_ns() - start_ns) / 1e9
            samples.append(sample)
            if all(process.poll() is not None for process in processes):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("bounded comparison deadline expired")
            time.sleep(0.025)
        for process in processes:
            if process.wait(timeout=3) != 0:
                raise RuntimeError(f"{mode} process returned {process.returncode}; inspect private raw evidence")
        if observer:
            events = read_events(base / "state")
            if not events or events[-1]["event"] != "finished" or events[-1]["code"] != 0:
                raise AssertionError("native batch lacks a successful terminal record")
            starts = [event for event in events if event["event"] == "started"]
            if len(observer.handles) != len(starts):
                raise AssertionError("not every native attempt was pinned")
        outcomes = verify_outputs(base, events)
        if events is not None:
            intents = [event for event in events if event["event"] == "pressure_intent"]
            if not intents or any(event["task"] != "growing" for event in intents):
                raise AssertionError("scenario did not checkpoint only disposable growing work")
            if not any(event["event"] == "resume_queued" for event in events):
                raise AssertionError("comparison did not exercise explicit resume")
            for event in (event for event in events if event["event"] == "checkpoint_acknowledged"):
                control = base / "state" / event["task"] / ("attempt-" + str(event["attempt"]))
                if (control / "stop.request").read_bytes() != (control / "stop.ack").read_bytes():
                    raise AssertionError("checkpoint ack nonce mismatch")
        for result in outcomes.values():
            result["initial_wait_seconds"] = (result.pop("first_started_ns") - start_ns) / 1e9
            result["completion_latency_seconds"] = (result.pop("ended_ns") - start_ns) / 1e9
        result = dict(mode=mode, **memory_summary(samples), tasks=outcomes,
            batch_completion_latency_seconds=samples[-1]["seconds"],
            native_events={name: sum(e["event"] == name for e in events or [])
                           for name in ("pressure_intent", "checkpoint_acknowledged", "resume_queued")})
        save(base / "samples.json", samples)
        save(base / "result.json", result)
        return result
    finally:
        # Must precede coordinator kill: observer must not keep root job alive.
        if observer:
            observer.close()
        if owned_job:
            kernel.CloseHandle(owned_job)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=8)
        for handle in direct_handles:
            kernel.CloseHandle(handle)


def self_test(base):
    """One deterministic stop/resume, corrupt checkpoint refusal, fixed goals."""
    base.mkdir(parents=True, exist_ok=False)
    owned_job = new_owned_job()
    try:
        def invoke(argv):
            barrier = base / ("release-" + uuid.uuid4().hex)
            process = subprocess.Popen([*argv, "--start-barrier", str(barrier)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env={key: value for key, value in os.environ.items() if key != "HEADROOM_CONTROL_DIR"})
            try:
                require(kernel.AssignProcessToJobObject(owned_job, int(process._handle)))
                save(barrier, True)
                _, stderr = process.communicate(timeout=9)
                return process.returncode, stderr.decode(errors="replace")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        for role in ROLES:
            argv = command(role, base / "outputs" / role)
            if role == "growing":
                code, error = invoke([*argv, "--checkpoint-at-step", "37"])
                if code:
                    raise AssertionError(error)
                saved = read_checkpoint(base / "outputs" / role / "checkpoint.json", role)
                if saved["progress"] != 37:
                    raise AssertionError("synthetic checkpoint did not record actual progress")
                code, error = invoke([*argv, "--resume"])
            else:
                code, error = invoke(argv)
            if code:
                raise AssertionError(error)
        outcomes = verify_outputs(base)
        corrupt = base / "corrupt-checkpoint.json"
        save(corrupt, dict(version=1, role="growing", goal_steps=STEPS, progress=37, checksum="0" * 64))
        try:
            read_checkpoint(corrupt, "growing")
        except ValueError:
            pass
        else:
            raise AssertionError("corrupt checkpoint accepted")
        baseline = run_trial("ungoverned", base / "baseline-sampler-smoke")
        return dict(successful=True, exact_progress_and_checksums=True,
                    corrupt_checkpoint_refused=True, tasks=outcomes,
                    concurrent_owned_baseline_sampler_verified=True,
                    baseline_smoke=baseline)
    finally:
        kernel.CloseHandle(owned_job)


def run_comparison(expected, repeats=3, integration_report=None):
    with serial_fleet():
        before = check_frozen(expected)
        integration = check_integration(expected, integration_report)
        run_id = uuid.uuid4().hex[:12]
        base = ROOT / "test-output" / ("comparison-" + run_id)
        base.mkdir(parents=True)
        trials = []
        for repeat in range(repeats):
            # Alternate ordering to reduce a fixed warmup/order advantage.
            modes = ("ungoverned", "native") if repeat % 2 == 0 else ("native", "ungoverned")
            for mode in modes:
                if provenance() != before:
                    raise RuntimeError("source/binary changed before trial; freeze required")
                trials.append(dict(repeat=repeat + 1, **run_trial(mode, base / f"{repeat + 1}-{mode}")))
        after = provenance()
        evidence = dict(schema_version=1, successful=before == after,
            review_status="Measurements remain provisional until main confirms final audit and source freeze.",
            run_id=run_id, platform=platform.platform(), provenance_before=before, provenance_after=after,
            integration=integration,
            trials=trials, retained_payload_bound_mib=120,
            workload=dict(steps=STEPS, seconds_per_step=STEP_SECONDS,
                payload_mib=dict(protected=16, growing_max=96, small=8),
                claim_mib=dict(protected=48, growing=32, small=64),
                priority=dict(protected=100, growing=20, small=10),
                scratch="8 MiB per four steps this attempt, capped at 96 MiB; disposable on restart"),
            method="Same work, priorities, claims and initial payload rules. Ungoverned launches all in priority order; native applies admission and explicit checkpoint/resume. Same default OS priority. Observer and coordinator excluded from worker memory.",
            limitations=["Healthy-host synthetic cache workload; no physical-RAM guarantee or real pressure induced.",
                "Sampled private commit peak may miss transients; area uses trapezoids across observed wall time.",
                "Restart discards scratch; real applications may need to restore state and incur different costs.",
                "Wall time includes launch/checkpoint/restart and observer effects; observer memory is excluded.",
                "Three short serial paired trials are descriptive, not a throughput or statistical significance claim.",
                "Hashes pair observed source and executable; no reproducible-build attestation."])
        medians = {}
        for mode in ("ungoverned", "native"):
            rows = [trial for trial in trials if trial["mode"] == mode]
            medians[mode] = {field: statistics.median(row[field] for row in rows) for field in (
                "sampled_peak_worker_private_commit_bytes", "worker_private_commit_area_byte_seconds",
                "batch_completion_latency_seconds")}
            medians[mode]["task_latencies"] = {role: {field: statistics.median(row["tasks"][role][field] for row in rows)
                for field in ("initial_wait_seconds", "completion_latency_seconds")} for role in ROLES}
        evidence["medians"] = medians
        evidence["tradeoff"] = "Read both worker commit peak/area and per-task waits/completions. Native admission/checkpoint can lower retained memory while delaying queued work and adding restart overhead; no universal improvement is claimed."
        destination = ROOT / "evidence" / "fleet-comparison.json"
        save(destination, evidence)
        if before != after:
            raise RuntimeError("source/binary changed; measurements marked unsuccessful")
        print(json.dumps(dict(successful=True, evidence=str(destination), medians=medians), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--expected-binary-sha256")
    parser.add_argument("--integration-report", type=Path)
    args = parser.parse_args()
    if args.self_test:
        with serial_fleet():
            result = self_test(ROOT / "test-output" / ("comparison-selftest-" + uuid.uuid4().hex[:12]))
            save(ROOT / "evidence" / "comparison-selftest.json", dict(schema_version=1,
                worker_sha256=sha(WORKER), runner_sha256=sha(Path(__file__)), **result))
            print(json.dumps(dict(successful=True, exact_progress_and_checksums=True)))
    elif args.expected_binary_sha256:
        run_comparison(args.expected_binary_sha256, integration_report=args.integration_report)
    else:
        parser.error("use --self-test, or supply the main-approved frozen --expected-binary-sha256")


if __name__ == "__main__":
    main()
