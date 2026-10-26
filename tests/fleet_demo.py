"""Local demo: python tests/fleet_demo.py

Writes evidence/fleet-demo.json plus a unique raw run directory. No build,
network, credentials, or unrelated process operations. The JSON summary has
relative evidence paths and hashes, never raw argv or absolute machine paths.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

from fleet_worker import save
from test_fleet import (FLEET, ROOT, WORKER, cleanup, launch, pressure_plan,
                        pressure_proofs, read_events, records, serial_fleet, source_hashes)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=FLEET)
    args = parser.parse_args(argv)
    compiler = ROOT / "headroom.py"
    if os.name != "nt" or not args.binary.is_file() or not compiler.is_file():
        print("Demo not executed: a built Windows headroom-fleet.exe and headroom.py are required.", file=sys.stderr)
        return 2
    evidence = ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    with serial_fleet():
        base = Path(tempfile.mkdtemp(prefix="fleet-demo-", dir=evidence))
        plan = pressure_plan(base)
        binary_hash = sha256(args.binary)
        compiler_hash = sha256(compiler)
        initial_sources = source_hashes()
        save(base / "plan.json", plan)
        compiled = subprocess.run([sys.executable, str(compiler), "compile", str(base / "plan.json"),
                                   str(base / "plan.hrf")], capture_output=True, timeout=8)
        if compiled.returncode:
            save(evidence / "fleet-demo.json", {"schema_version": 1, "status": "compile_failed",
                 "exit_code": compiled.returncode, "raw_evidence": base.relative_to(evidence).as_posix(),
                 "hashes": {"native_binary": binary_hash, "plan_compiler": compiler_hash}})
            print("evidence/fleet-demo.json: compile_failed", file=sys.stderr)
            return 1
        started = time.monotonic()
        process = launch(plan, base, binary=args.binary, compiled_plan=base / "plan.hrf")
        timed_out = False
        try:
            try:
                process.communicate(timeout=24)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            cleanup(process)
        events = read_events(base / "state", partial=timed_out)
        proofs = pressure_proofs(base, events)
        proofs["successful_exit"] = process.returncode == 0
        proofs["binary_unchanged_during_run"] = binary_hash == sha256(args.binary)
        final_sources = source_hashes()
        proofs["sources_unchanged_during_run"] = initial_sources == final_sources
        hashes = {"native_binary": binary_hash, "python_executable": sha256(sys.executable),
                  "plan_compiler": compiler_hash,
                  "worker_source": sha256(WORKER), "suite_source": sha256(ROOT / "tests" / "test_fleet.py"),
                  "demo_source": sha256(Path(__file__)), "compiled_plan": sha256(base / "plan.hrf")}
        # Hash every raw evidence file without embedding raw event strings or
        # command lines in the portable summary.
        artifacts = {path.relative_to(evidence).as_posix(): sha256(path)
                     for path in sorted(base.rglob("*")) if path.is_file()}
        timelines = [{key: event[key] for key in ("event", "task", "attempt", "elapsed_ms", "code")}
                     for event in events if event["event"] != "sample"]
        protected = records(base / "outputs" / "protected")
        low = records(base / "outputs" / "low")
        measurements = {
            "protected_peak_private_bytes": protected[0].get("peak_private_bytes") if protected else None,
            "max_sampled_fleet_commit_bytes": max((e["total_commit_bytes"] for e in events), default=0),
            "low_attempt_progress": [{key: item[key] for key in
                ("mode", "payload_mib", "progress", "started_from", "started_ns", "ended_ns") if key in item}
                for item in sorted(low, key=lambda item: item["started_ns"])],
        }
        summary = {
            "schema_version": 1, "status": "passed" if all(proofs.values()) else "failed",
            "command": "python tests/fleet_demo.py", "platform": "Windows",
            "python_version": platform.python_version(), "payload_cap_mib": 120,
            "soft_budget_mib": plan["budget_mib"], "root_hard_mib": plan["hard_mib"],
            "reserve_mib": 0, "elapsed_seconds": round(time.monotonic() - started, 3),
            "exit_code": process.returncode, "timed_out": timed_out,
            "raw_evidence": base.relative_to(evidence).as_posix(),
            "hashes": hashes, "artifacts_sha256": artifacts,
            "source_sha256": initial_sources, "source_sha256_after": final_sources,
            "event_counts": dict(sorted(collections.Counter(e["event"] for e in events).items())),
            "timeline": timelines, "proofs": proofs,
            "measurements": measurements,
            "limits": ["Synthetic application-specific checkpoint and explicit resume recipe.",
                       "Commit accounting is not a physical RAM guarantee.",
                       "Crash cleanup does not imply automatic crash recovery."],
        }
        save(evidence / "fleet-demo.json", summary)
        print("evidence/fleet-demo.json: " + summary["status"])
        for name, passed in proofs.items():
            print(("PASS " if passed else "FAIL ") + name)
        return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
