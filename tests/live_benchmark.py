"""Bounded live-owner idle overhead; native execution requires parent --go.

Only the coordinator is measured. Raw state stays under test-output; exported
evidence contains relative source names and no private command lines or paths.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import headroom_live as live
from benchmark import kernel, snapshot
from comparison_worker import save
from test_fleet import FLEET, read_events, serial_fleet

WORKER = ROOT / "tests" / "comparison_worker.py"
SETTINGS = dict(budget_mib=512, hard_mib=768, reserve_mib=0,
                sustain_ms=250, grace_ms=1200, timeout_ms=20000)
SIGNATURES = {
    "OpenJobObjectW": ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
    "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
    "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
    "GetProcessId": ([w.HANDLE], w.DWORD),
    "QueryFullProcessImageNameW": ([w.HANDLE, w.DWORD, w.LPWSTR,
                                    ctypes.POINTER(w.DWORD)], w.BOOL),
    "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                     w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
}
for name, (arguments, result) in SIGNATURES.items():
    getattr(kernel, name).argtypes = arguments
    getattr(kernel, name).restype = result


def require(value, message):
    if not value:
        raise RuntimeError(message)
    return value


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance():
    paths = [ROOT / "LIVE_INTAKE.md", ROOT / "PROTOCOL.md"]
    paths += [p for p in (ROOT / "src").rglob("*") if p.suffix in (".c", ".h")]
    paths += list((ROOT / "tests").rglob("*.py"))
    paths += list((ROOT / "tests").glob("*.[ch]"))
    paths += list((ROOT / "tools").glob("*.py"))
    paths += list(ROOT.glob("*.py")) + list(ROOT.glob("*.ps1"))
    paths += list((ROOT / "examples").glob("*.py"))
    paths += list((ROOT / "examples").glob("*.json"))
    return dict(native_binary_sha256=sha(FLEET), source_sha256={
        p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(paths))})


def integration(path, expected, frozen):
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schema_version") == 2:
        valid = (report.get("passed") is True and report.get("inputs_unchanged") is True
                 and report.get("binaries_sha256", {}).get("build/headroom-fleet.exe") == expected
                 and report.get("failures") == 0 and report.get("errors") == 0
                 and report.get("skipped") == 0 and report.get("tests", 0) >= 15)
        kind, tests = "full-suite-schema-2", report.get("tests")
        for name, digest in report.get("sources_sha256", {}).items():
            require(frozen["source_sha256"].get(name) == digest,
                    "source differs from full-suite integration evidence")
    else:
        valid = (report.get("successful") is True and report.get("native_sha256") == expected
                 and type(report.get("tests_run")) is int and report["tests_run"] >= 15
                 and all(report.get(key) == [] for key in ("failures", "errors", "skipped")))
        kind, tests = "live-native-regressions", report.get("tests_run")
    require(valid, "matching successful integration report with at least 15 tests required")
    return dict(kind=kind, tests_run=tests, report_sha256=sha(path),
                native_binary_sha256=expected)


def image(handle):
    buffer = ctypes.create_unicode_buffer(32768)
    length = w.DWORD(len(buffer))
    require(kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)),
            "cannot verify pinned process executable")
    return Path(buffer.value).resolve()


class LiveObserver:
    """Version-2 live ownership only; never reuse the batch v1 observer.

    Coordinator HANDLE is borrowed directly from Popen, never reopened by PID.
    Worker PIDs come only from this fresh owner's journal and ready records.
    Query job handles must close before killing the owner on failure.
    """
    def __init__(self, process, state, compiled):
        self.process, self.state, self.compiled = process, state, compiled
        self.coordinator = int(process._handle)
        self.job, self.handles, self.run_id = None, {}, None
        require(kernel.GetProcessId(self.coordinator) == process.pid,
                "Popen coordinator HANDLE identity mismatch")
        require(image(self.coordinator) == FLEET.resolve(), "unexpected coordinator executable")

    def verify(self):
        require(self.process.poll() is None, "live owner exited prematurely")
        _, session = live._session(self.state)
        identifier = session["run_id"]
        require(str(uuid.UUID(identifier)) == identifier, "noncanonical live identity")
        require(self.run_id in (None, identifier), "live session identity changed")
        require(session == dict(version=1, run_id=identifier, max_jobs=64, settings=SETTINGS),
                "session settings mismatch")
        owner = live._json(self.state / "owner.json", 4096)
        require(owner == dict(version=2, mode="live", run_id=identifier,
                              job_name="Local\\Headroom-" + identifier),
                "live owner version/mode/job identity mismatch")
        require((self.state / "plan.hrp").read_bytes() == self.compiled,
                "persisted live plan differs")
        lease = self.state / "owner.lock"
        info = lease.lstat()
        require(lease.is_file() and info.st_nlink == 1
                and not getattr(info, "st_file_attributes", 0) & 0x400,
                "invalid owner lease file")
        # OPEN_EXISTING only: no write or lock is taken. A living owner holds
        # GENERIC_WRITE with FILE_SHARE_READ, so this must fail with sharing=32.
        handle = kernel.CreateFileW(str(lease), 0xC0000000, 1, None, 3, 0x00200000, None)
        if handle != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(handle)
            raise RuntimeError("owner lease is not held")
        require(ctypes.get_last_error() == 32, "owner lease probe failed unexpectedly")
        if not self.job:
            self.job = require(kernel.OpenJobObjectW(4, False, owner["job_name"]),
                               "cannot pin this live owner's named job")
        self.run_id = identifier
        events = read_events(self.state, partial=True)
        require(events and all(e["run_id"] == identifier for e in events),
                "live journal identity mismatch")
        require(not (self.state / "seal.request").exists()
                and not (self.state / "sealed.json").exists(), "session sealed before measurement ended")
        bad = {"measurement_unavailable", "pressure_intent", "pressure_unresolved",
               "stopped", "terminated", "deadline", "interrupt", "internal_error", "sealed"}
        require(not any(e["event"] in bad for e in events), "trial is not healthy idle")
        return events

    def ready(self, count, base):
        events = self.verify()
        starts = [e for e in events if e["event"] == "started"]
        require(len(starts) <= count, "unexpected extra worker attempt")
        for event in starts:
            task, pid = event["task"], event["code"]
            require(task in {f"idle-{i}" for i in range(count)} and event["attempt"] == 1,
                    "unexpected worker identity or restart")
            if task in self.handles:
                continue
            record = base / "outputs" / task / f"ready-{pid}.json"
            if not record.is_file():
                continue
            require(json.loads(record.read_text(encoding="utf-8")) ==
                    dict(pid=pid, role="hold", payload_mib=0), "worker readiness mismatch")
            handle = require(kernel.OpenProcess(0x100000 | 0x0400 | 0x0010, False, pid),
                             "cannot pin journal-started worker")
            try:
                member = w.BOOL()
                require(kernel.IsProcessInJob(handle, self.job, ctypes.byref(member))
                        and member.value, "worker is outside this owned root job")
                require(image(handle) == Path(sys.executable).resolve(), "unexpected worker executable")
                self.handles[task] = (handle, pid)
            except BaseException:
                kernel.CloseHandle(handle)
                raise
        self.alive()
        return len(self.handles) == count and any(e["event"] == "sample" for e in events)

    def alive(self):
        require(kernel.WaitForSingleObject(self.coordinator, 0) == 258, "coordinator exited")
        require(all(kernel.WaitForSingleObject(h, 0) == 258 for h, _ in self.handles.values()),
                "owned worker exited before the whole sample window ended")

    def close(self):
        if self.job:
            kernel.CloseHandle(self.job)
            self.job = None
        for handle, _ in self.handles.values():
            kernel.CloseHandle(handle)
        self.handles.clear()


def trial(count, repeat, base):
    base.mkdir(parents=True, exist_ok=False)
    state, barrier = base / "state", base / "release-hold-timers"
    compiled = live.compile_session(SETTINGS)
    (base / "settings.hrs").write_bytes(compiled)
    save(base / "settings.json", SETTINGS)
    process = observer = None
    samples = []
    try:
        with (base / "owner.stderr").open("xb") as stderr:
            process = subprocess.Popen([str(FLEET), "--live-plan", str(base / "settings.hrs"),
                "--state", str(state)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=stderr, close_fds=True)
        observer = LiveObserver(process, state, compiled)
        deadline = time.monotonic() + 5
        while not (state / "session.json").is_file():
            require(process.poll() is None and time.monotonic() < deadline,
                    "bounded session readiness failed")
            time.sleep(0.025)
        observer.verify()
        requests = {}
        # Publish all requests offline before waiting; no resident client loop
        # remains in the native process or participates in measured counters.
        for index in range(count):
            task = f"idle-{index}"
            job = dict(id=task, priority=50, claim_mib=24, hard_mib=64,
                allow_stop=False, max_resumes=0, resume_argv=[],
                argv=[sys.executable, str(WORKER), "hold", str(base / "outputs" / task),
                      "--hold-seconds", "5.5", "--hold-barrier", str(barrier)])
            receipt = live.submit(state, job, wait_seconds=0)
            requests[task] = receipt["request_id"]
        deadline = time.monotonic() + 6
        while True:
            receipts = [live.read_receipt(state, request) for request in requests.values()]
            require(all(r["status"] != "rejected" for r in receipts), "idle request rejected")
            admitted = all(r["status"] == "accepted" and r["task"] == task
                           and r["run_id"] == observer.run_id
                           for task, r in zip(requests, receipts))
            if admitted and observer.ready(count, base):
                break
            require(time.monotonic() < deadline, "bounded worker admission/readiness failed")
            time.sleep(0.025)
        save(base / "receipts.json", receipts)
        observer.verify()
        observer.alive()
        # Every worker's 5.5s timer starts only at this shared release, after
        # all roots are pinned and ready. The zero-job owner stays unsealed.
        save(barrier, True)
        start = time.perf_counter()
        samples.append(dict(seconds=0.0, **snapshot(observer.coordinator)))
        while time.perf_counter() - start < 5:
            time.sleep(min(0.05, max(0, 5 - (time.perf_counter() - start))))
            observer.alive()
            measured = snapshot(observer.coordinator)
            samples.append(dict(seconds=time.perf_counter() - start, **measured))
        seconds = samples[-1]["seconds"]
        require(5 <= seconds < 5.5, "sample window must be 5..<5.5 seconds")
        observer.verify()
        observer.alive()
        seal = live.seal(state)  # Only after the entire five-second sample.
        require(seal["run_id"] == observer.run_id, "seal identity mismatch")
        require(process.wait(timeout=5) == 0, "sealed live owner did not finish successfully")
        require(all(kernel.WaitForSingleObject(h, 0) == 0 for h, _ in observer.handles.values()),
                "owned workers remain alive after owner completion")
        require(live._json(state / "sealed.json", 4096) ==
                dict(version=1, run_id=observer.run_id), "missing durable live seal")
        events = read_events(state)
        completed = [e for e in events if e["event"] == "completed" and e["code"] == 0]
        require(all(e["run_id"] == observer.run_id for e in events)
                and {e["task"] for e in completed} == set(requests)
                and len(completed) == count and events[-1]["event"] == "finished"
                and events[-1]["code"] == 0, "missing durable completion evidence")
        payloads = [json.loads(p.read_text(encoding="utf-8"))
                    for p in (base / "outputs").glob("*/attempt-*.json")]
        require(len(payloads) == count and all(
            p["role"] == "hold" and p["payload_peak_mib"] == 0 and p["exit_code"] == 0
            and p["stopped"] is False and p["pid"] == observer.handles[task][1]
            for task, p in ((p.parent.name, json.loads(p.read_text(encoding="utf-8")))
                            for p in (base / "outputs").glob("*/attempt-*.json"))),
            "fixture identity/completion/zero-payload proof failed")
        cpu = samples[-1]["cpu_seconds"] - samples[0]["cpu_seconds"]
        result = dict(idle_jobs=count, repeat=repeat, seconds=seconds, cpu_seconds=cpu,
            cpu_percent_one_core=100 * cpu / seconds, samples=len(samples),
            synthetic_retained_payload_mib=0, live_owner_version=2,
            pinned_popen_coordinator=True, owner_session_lease_verified=True,
            all_worker_handles_pinned_and_membership_verified=True)
        for field in ("working_set_bytes", "private_commit_bytes"):
            result["peak_" + field] = max(s[field] for s in samples)
            result["median_" + field] = statistics.median(s[field] for s in samples)
        save(base / "result.json", result)
        return result
    finally:
        save(base / "samples.json", samples)
        if observer:
            observer.close()  # Drop named job query handle before owned kill.
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=8)
            save(base / "owner-outcome.json", dict(pid=process.pid, returncode=process.returncode))


def run(options):
    expected = options.expected_binary_sha256
    destination = options.output.resolve()
    require(destination.is_relative_to(ROOT / "evidence") and destination.suffix == ".json",
            "output must be a JSON file under evidence/")
    require(not destination.exists(), "output already exists; choose a distinct evidence filename")
    with serial_fleet():
        before = provenance()
        require(before["native_binary_sha256"] == expected, "binary differs from frozen expected SHA")
        proof = integration(options.integration_report, expected, before)
        identifier = uuid.uuid4().hex[:12]
        base = ROOT / "test-output" / ("live-benchmark-" + identifier)
        base.mkdir(parents=True, exist_ok=False)
        trials = []
        try:
            for repeat in range(1, options.repeats + 1):
                counts = options.counts if repeat % 2 else list(reversed(options.counts))
                for count in counts:
                    require(provenance() == before, "source/binary changed before trial")
                    row = trial(count, repeat, base / f"{repeat}-{count}")
                    require(provenance() == before, "source/binary changed during trial")
                    trials.append(row)
        finally:
            after = provenance()
            save(base / "provenance.json", dict(before=before, after=after))
        require(before == after, "source/binary changed; evidence refused")
        fields = ("peak_working_set_bytes", "peak_private_commit_bytes", "cpu_percent_one_core")
        medians = {str(count): {field: statistics.median(
            row[field] for row in trials if row["idle_jobs"] == count) for field in fields}
            for count in options.counts}
        evidence = dict(schema_version=1, successful=True, run_id=identifier,
            platform=dict(system=platform.system(), release=platform.release(), machine=platform.machine()),
            provenance_before=before, provenance_after=after, integration=proof,
            counts=options.counts, repeats=options.repeats, trials=trials, medians=medians,
            scope="Healthy idle native live coordinator only, including its intake thread. Python runner and all worker processes excluded; fixed zero retained synthetic payload; no host pressure induced.",
            methodology="Five-second windows after all accepted workers are ready and pinned; shared barrier starts 5.5-second holds. Owner remains unsealed throughout sampling, including zero jobs, then is explicitly sealed. Working set/private commit sampled about every 50ms; CPU is kernel+user process time divided by elapsed seconds, percent of one logical core.",
            limitations=["CPU accounting counters are coarse; near-zero deltas are not proof of zero CPU overhead. Short windows and scheduler/host noise limit precision.",
                "Sampled memory peaks can miss intervening peaks. Process counters exclude separately attributed kernel pipe/job bookkeeping and every worker/runner's memory and CPU.",
                "Fixed policy settings; no host-wide settings, accounts, actor PID discovery, workload throughput or minimum-RAM claim.",
                "Source/executable pairing is observed by hashes before/after every trial, not reproducible-build attestation. Live-only integration evidence does not assert the full batch suite passed."],
            raw_results=(base.relative_to(ROOT)).as_posix())
        save(destination, evidence)
    # Printed only after leaving serial_fleet, so parent can safely rebuild.
    print(json.dumps(dict(successful=True, shared_serial_fleet_lock_released=True,
        evidence=destination.relative_to(ROOT).as_posix(), medians=medians), indent=2))


def digest(value):
    value = value.lower()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise argparse.ArgumentTypeError("expected 64 hexadecimal SHA-256 characters")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", action="store_true", help="parent explicitly authorizes native measurements now")
    parser.add_argument("--expected-binary-sha256", type=digest, required=True)
    parser.add_argument("--integration-report", type=Path, default=ROOT / "evidence" / "native-tests.json",
                        help="schema-2 full-suite or successful live-native >=15-test JSON")
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--counts", default="0,1,8", help="comma-separated subset of 0,1,8 (default: 0,1,8)")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence" / "live-benchmark.json",
                        help="new evidence/*.json destination; choose distinct before/after files")
    options = parser.parse_args()
    try:
        options.counts = [int(c) for c in options.counts.split(",")]
        require(options.counts and len(set(options.counts)) == len(options.counts)
                and all(c in (0, 1, 8) for c in options.counts), "counts must be a unique subset of 0,1,8")
        require(options.go, "native measurements require explicit parent --go")
        require(os.name == "nt", "Windows native measurements required")
        run(options)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        # Exceptions may contain private paths: full diagnostics stay local.
        print("Live benchmark refused/failed; inspect private test-output artifacts (" +
              type(error).__name__ + ").", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
