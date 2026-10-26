"""Three short 1-job/8-idle-job native coordinator-only trials.

Uses benchmark.snapshot on a pinned coordinator handle. Zero-payload Python
workers and the Python runner are excluded. Never rebuilds native binaries.
"""
import argparse
import json
from pathlib import Path
import platform
import statistics
import subprocess
import time
import uuid

from benchmark import snapshot
from comparison_worker import save
from compare_fleet import (ROOT, FLEET, NativeObserver, check_frozen, provenance,
                           check_integration, command, sha)
from test_fleet import encode_plan, make_plan, read_events, serial_fleet


def trial(count, repeat, base):
    base.mkdir(parents=True, exist_ok=False)
    jobs = [dict(id=f"idle-{index}", priority=50, claim_mib=24, hard_mib=64,
                 allow_stop=False, max_resumes=0,
                 argv=command("hold", base / "outputs" / str(index), "--hold-seconds", "5.5",
                              "--hold-barrier", base / "release-idle-timers"),
                 resume_argv=[]) for index in range(count)]
    plan = make_plan(jobs, budget=512, hard=768, timeout_ms=12000)
    plan_path = base / "plan.hrp"
    plan_path.write_bytes(encode_plan(plan))
    save(base / "plan.json", plan)
    process = None
    observer = None
    try:
        with (base / "coordinator.stderr").open("wb") as stderr:
            process = subprocess.Popen([str(FLEET), "--plan", str(plan_path),
                "--state", str(base / "state")], stdout=subprocess.DEVNULL, stderr=stderr)
        observer = NativeObserver(process, base / "state")
        readiness_deadline = time.monotonic() + 4
        while True:
            observer.update()
            if (len(observer.handles) == count and
                    len(list((base / "outputs").glob("*/ready-*.json"))) == count):
                break
            if process.poll() is not None or time.monotonic() >= readiness_deadline:
                raise RuntimeError("bounded idle-job readiness failed")
            time.sleep(0.025)
        save(base / "release-idle-timers", True)
        start = time.perf_counter()
        first = snapshot(observer.coordinator)
        samples = [dict(seconds=0, **first)]
        while time.perf_counter() - start < 5.0:
            time.sleep(min(0.05, max(0, 5.0 - (time.perf_counter() - start))))
            observer.update()
            if process.poll() is not None or observer.sample()["active_workers"] != count:
                raise RuntimeError("idle job/coordinator exited before five-second interval ended")
            samples.append(dict(seconds=time.perf_counter() - start, **snapshot(observer.coordinator)))
        seconds = samples[-1]["seconds"]
        if not 5 <= seconds <= 6:
            raise RuntimeError("measurement interval outside the bounded 5..6 second window")
        if process.wait(timeout=3) != 0:
            raise RuntimeError("idle fleet did not finish successfully")
        events = read_events(base / "state")
        completed = [event for event in events if event["event"] == "completed" and event["code"] == 0]
        if len(completed) != count or not events or events[-1]["event"] != "finished":
            raise AssertionError("idle trial lacks durable success evidence")
        payloads = [json.loads(path.read_text(encoding="utf-8"))
                    for path in (base / "outputs").glob("*/attempt-*.json")]
        if len(payloads) != count or any(record["payload_peak_mib"] != 0 for record in payloads):
            raise AssertionError("idle payload is not zero")
        result = dict(idle_jobs=count, repeat=repeat, seconds=seconds,
            cpu_seconds=samples[-1]["cpu_seconds"] - first["cpu_seconds"],
            cpu_percent_one_core=100 * (samples[-1]["cpu_seconds"] - first["cpu_seconds"]) / seconds,
            peak_working_set_bytes=max(sample["working_set_bytes"] for sample in samples),
            peak_private_commit_bytes=max(sample["private_commit_bytes"] for sample in samples),
            median_working_set_bytes=statistics.median(sample["working_set_bytes"] for sample in samples),
            median_private_commit_bytes=statistics.median(sample["private_commit_bytes"] for sample in samples),
            samples=len(samples), all_worker_pids_pinned_and_membership_verified=True,
            synthetic_retained_payload_mib=0)
        save(base / "samples.json", samples)
        save(base / "result.json", result)
        return result
    finally:
        # Close job observer first: do not mask coordinator kill-on-close.
        if observer:
            observer.close()
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=8)


def run(expected, integration_report=None):
    with serial_fleet():
        before = check_frozen(expected)
        integration = check_integration(expected, integration_report)
        run_id = uuid.uuid4().hex[:12]
        base = ROOT / "test-output" / ("fleet-benchmark-" + run_id)
        base.mkdir(parents=True)
        trials = []
        for repeat in range(1, 4):
            for count in ((1, 8) if repeat % 2 else (8, 1)):
                if provenance() != before:
                    raise RuntimeError("source/binary changed before trial; freeze required")
                trials.append(trial(count, repeat, base / f"{repeat}-{count}"))
        after = provenance()
        medians = {str(count): {field: statistics.median(row[field] for row in trials if row["idle_jobs"] == count)
            for field in ("peak_working_set_bytes", "peak_private_commit_bytes", "cpu_percent_one_core")}
            for count in (1, 8)}
        evidence = dict(schema_version=1, successful=before == after, run_id=run_id,
            review_status="Measurements remain provisional until main confirms final audit and source freeze.",
            platform=platform.platform(), provenance_before=before, provenance_after=after,
            integration=integration,
            scope="Native fleet coordinator only; pinned process snapshot helper from tests/benchmark.py. Python runner and every worker excluded. Three five-second sample intervals each for 1 and 8 zero-payload idle jobs.",
            trials=trials, medians=medians,
            limitations=["Healthy-host idle synthetic measurements; no workload throughput or physical-RAM guarantee.",
                "Sampled process private commit and working set; kernel job bookkeeping not separately attributed.",
                "Source/executable hashes record observed pairing, not reproducible build attestation.",
                "Single-job guard cost is separate and must not be added to this coordinator-only figure."])
        guard_evidence = ROOT / "evidence" / "native-benchmark.json"
        if guard_evidence.is_file():
            guard = json.loads(guard_evidence.read_text(encoding="utf-8"))
            guard_binary = ROOT / "build" / "headroom.exe"
            evidence["separate_single_guard_measurement"] = dict(
                evidence="evidence/native-benchmark.json", evidence_sha256=sha(guard_evidence),
                measured_binary_sha256=guard.get("binary_sha256"),
                matches_current_guard_binary=guard_binary.is_file() and guard.get("binary_sha256") == sha(guard_binary),
                note="Historical independent guard experiment; inspect its provenance before using its numbers.")
        destination = ROOT / "evidence" / "fleet-benchmark.json"
        save(destination, evidence)
        if before != after:
            raise RuntimeError("source/binary changed; overhead measurement marked unsuccessful")
        print(json.dumps(dict(successful=True, evidence=str(destination), medians=medians), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-binary-sha256", required=True,
                        help="main-approved frozen executable SHA, after integration passes")
    parser.add_argument("--integration-report", type=Path)
    options = parser.parse_args()
    run(options.expected_binary_sha256, options.integration_report)
