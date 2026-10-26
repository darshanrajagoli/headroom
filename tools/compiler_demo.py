"""One bounded real GCC workload through the existing native live owner.

Run: python tools/compiler_demo.py --go
Never rebuilds build/. Raw requests, command lines and diagnostics remain in
ignored test-output/. This is a post-suite demonstration, not another test.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import headroom
import headroom_live as live
from compare_fleet import ExtendedLimits, kernel, new_owned_job
from test_fleet import serial_fleet

EXPECTED_OWNER = "fcd81c198d824befb75c7084b2cf2613bf3ed1407ecd289b3f017076051fafe9"
FLAGS = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-municode"]
SOURCES = ["src/guard.c", "src/support.c"]
LIBRARIES = ["-lole32", "-lpsapi"]
SETTINGS = dict(budget_mib=384, hard_mib=512, reserve_mib=0,
                sustain_ms=250, grace_ms=1200, timeout_ms=30000)
JOB_SETTINGS = dict(priority=50, claim_mib=192, hard_mib=384,
                    allow_stop=False, max_resumes=0)
EVIDENCE = ROOT / "evidence" / "compiler-demo.json"
NOTES = ROOT / "evidence" / "COMPILER_DEMO.md"
PROOF = ROOT / "evidence" / "native-tests.json"


def require(value, message):
    if not value:
        raise RuntimeError(message)
    return value


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def load(path, limit=1048576):
    with path.open("rb") as stream:
        body = stream.read(limit + 1)
    require(len(body) <= limit, "bounded JSON read exceeded")
    return headroom.strict_json_loads(body)


def relative(path):
    return path.relative_to(ROOT).as_posix()


def proof_snapshot():
    """Require a current frozen aggregate, not a relabeled historical proof."""
    proof = load(PROOF)
    require(proof.get("schema_version") == 2 and proof.get("passed") is True
            and type(proof.get("tests")) is int and proof["tests"] >= 479
            and proof.get("inputs_unchanged") is True
            and all(proof.get(k) == 0 for k in ("failures", "errors", "skipped")),
            "matching successful current full-suite proof required")
    sources = proof["sources_sha256"]
    binaries = proof["binaries_sha256"]
    require(binaries.get("build/headroom-fleet.exe") == EXPECTED_OWNER,
            "integration proof belongs to a different owner")
    require(proof["snapshot_after"] == dict(sources=sources, binaries=binaries),
            "baseline before/after pairing differs")
    current_sources, differences = {}, {}
    require({relative(Path(__file__)), "src/fleet.c", "src/guard.c",
             "headroom.py", "headroom_live.py"} <= sources.keys(),
            "full-suite proof lacks required demonstration sources")
    for name, digest in {**sources, **binaries}.items():
        path = ROOT / name
        require(not Path(name).is_absolute() and path.resolve().is_relative_to(ROOT),
                "unsafe proof input path")
        actual = sha(path)
        if name in sources:
            current_sources[name] = actual
        require(actual == digest, "current input differs from full-suite proof: " + name)
    build_line = (ROOT / "build.ps1").read_text(encoding="utf-8").splitlines()
    require(any("& gcc " + " ".join(FLAGS) + " " in line
                and "src\\guard.c" in line and "src\\support.c" in line
                and line.endswith("-lole32 -lpsapi") for line in build_line),
            "strict guard build flags changed")
    return dict(report=relative(PROOF), report_sha256=sha(PROOF), tests=proof["tests"],
                sources_sha256=current_sources, binaries_sha256=binaries,
                unrelated_baseline_source_differences=differences,
                owner_and_used_sources_match_proof=True,
                baseline_scope="Current successful frozen full-suite report; every recorded input matched.",
                demo_sha256=sha(Path(__file__)))


class Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_longlong * 4), ("faults", w.DWORD),
                ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]


for name, (arguments, result) in {
    "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                   w.DWORD, ctypes.c_void_p], w.BOOL),
    "ResumeThread": ([w.HANDLE], w.DWORD),
    "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
    "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
    "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
}.items():
    getattr(kernel, name).argtypes = arguments
    getattr(kernel, name).restype = result


def accounting(group):
    result = Accounting()
    require(kernel.QueryInformationJobObject(group, 1, ctypes.byref(result),
                                            ctypes.sizeof(result), None),
            "outer job accounting unavailable")
    return result


def in_job(process, group=None):
    member = w.BOOL()
    require(kernel.IsProcessInJob(process, group, ctypes.byref(member)),
            "job membership query failed")
    return bool(member.value)


def events(state):
    path = state / "fleet.jsonl"
    if not path.exists():
        return []
    with path.open("rb") as stream:
        body = stream.read(1048577)
    require(len(body) <= 1048576, "journal exceeds demo bound")
    return [json.loads(line) for line in body[:body.rfind(b"\n") + 1].splitlines()]


def wait_for(predicate, process, deadline):
    while True:
        value = predicate()
        if value:
            return value
        require(process.poll() is None, "owner exited before expected evidence")
        require(time.monotonic() < deadline, "bounded demo wait expired")
        time.sleep(0.025)


def output_summary(state, task):
    result = {}
    for stream in ("stdout", "stderr"):
        log = headroom.read_log(state, task, stream=stream, tail_bytes=4096)
        require(log["error"] is None and log["metadata_confirmed"]
                and log["eof"] is True and log["discarded_bytes"] == 0
                and log["retained_bytes"] <= 1048576, "unconfirmed or lost output")
        result[stream] = {key: log[key] for key in (
            "limit_bytes", "retained_bytes", "discarded_bytes", "eof", "metadata_confirmed")}
        result[stream]["sha256"] = sha(state / task / "attempt-1" / (stream + ".log"))
    return result


def submit(state, task, argv, ordinal):
    receipt = live.submit(state, dict(id=task, argv=argv, **JOB_SETTINGS), wait_seconds=5)
    require(receipt["status"] == "accepted" and receipt["code"] == 0
            and receipt["task"] == task and receipt["ordinal"] == ordinal,
            "accepted native receipt required")
    require(live.read_receipt(state, receipt["request_id"]) == receipt,
            "durable receipt differs")
    claimed = state / "processed" / receipt["request_id"]
    request = load(claimed / "plan.json")
    compiled = headroom.compile_plan(request)
    require((claimed / "plan.hrp").read_bytes() == compiled
            and (claimed / "validated.hrp").read_bytes() == compiled,
            "submitted JSON/native/validated pairing differs")
    return dict(receipt=receipt, request_json_sha256=sha(claimed / "plan.json"),
                validated_plan_sha256=sha(claimed / "validated.hrp"))


def resource_summary():
    return dict(live_session=dict(timeout_ms=SETTINGS["timeout_ms"], max_jobs=64,
                                  max_processed_requests=128),
                global_commit={key: SETTINGS[key] for key in
                               ("budget_mib", "hard_mib", "reserve_mib")},
                per_job_commit={task: {key: JOB_SETTINGS[key] for key in
                                      ("claim_mib", "hard_mib")}
                                for task in ("gcc-compile", "gcc-smoke")})


def smoke(base, expected):
    """Preserve usage exit 64, map ONLY that expected result to job success.

    Both child streams inherit the live owner's bounded drain pipes. No
    unbounded Python PIPE capture and no native task exit-code rewriting.
    """
    binary = base / "headroom-demo.exe"
    require(sha(binary) == expected, "compiled binary changed before smoke")
    process = subprocess.run([str(binary)], stdin=subprocess.DEVNULL, timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    result = dict(invocation="no arguments", expected_exit_code=64,
                  actual_exit_code=process.returncode, successful=process.returncode == 64,
                  executable_sha256=expected)
    save(base / "smoke-result.json", result)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["successful"] else 1


def fixture(base):
    before = proof_snapshot()
    require(in_job(kernel.GetCurrentProcess()), "fixture is outside outer job")
    gcc = Path(require(shutil.which("gcc.exe"), "actual GCC unavailable")).resolve()
    require(gcc.name.lower() == "gcc.exe", "GCC executable required")
    binary = base / "headroom-demo.exe"
    require(not binary.exists(), "demo output must be new")
    command = [str(gcc), *FLAGS, *SOURCES, "-o", relative(binary), *LIBRARIES]
    session = base / "session.hrs"
    session.write_bytes(live.compile_session(SETTINGS))
    state = base / "state"
    (base / "tmp").mkdir()
    environment = dict(os.environ, TEMP=str(base / "tmp"), TMP=str(base / "tmp"))
    owner_command = [str(ROOT / "build" / "headroom-fleet.exe"),
                     "--live-plan", str(session), "--state", str(state)]
    save(base / "private-commands.json", dict(owner=owner_command, gcc=command))
    start = time.monotonic()
    deadline = start + 32
    process = subprocess.Popen(owner_command, cwd=ROOT, env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    try:
        require(in_job(int(process._handle)), "owner did not inherit containment")
        wait_for(lambda: (state / "session.json").is_file(), process, deadline)
        metadata = load(state / "session.json", 4096)
        run_id = metadata["run_id"]
        require(metadata == dict(version=1, run_id=run_id, max_jobs=64, settings=SETTINGS),
                "live settings differ")
        require(load(state / "owner.json", 4096) == dict(version=2, mode="live",
                run_id=run_id, job_name="Local\\Headroom-" + run_id),
                "not the expected native live owner")
        require((state / "plan.hrp").read_bytes() == session.read_bytes(),
                "persisted session plan differs")
        compilation = submit(state, "gcc-compile", command, 1)
        wait_for(lambda: any(e["event"] == "completed" and e["task"] == "gcc-compile"
                             for e in events(state)), process, deadline)
        done = [e for e in events(state) if e["event"] == "completed"
                and e["task"] == "gcc-compile"]
        require(len(done) == 1 and done[0]["code"] == 0, "GCC compilation failed")
        compilation["output"] = output_summary(state, "gcc-compile")
        require(binary.is_file() and binary.stat().st_size > 0
                and binary.read_bytes()[:2] == b"MZ", "GCC did not create an executable")
        binary_hash = sha(binary)
        smoke_argv = [sys.executable, str(Path(__file__)), "--smoke", relative(base), binary_hash]
        smoke_job = submit(state, "gcc-smoke", smoke_argv, 2)
        barrier = live.seal(state)
        require(barrier == dict(version=1, run_id=run_id, status="seal_requested"),
                "seal request differs")
        require(process.wait(timeout=max(0.1, deadline - time.monotonic())) == 0,
                "live owner did not finish successfully")
        require(load(state / "sealed.json", 4096) == dict(version=1, run_id=run_id),
                "durable seal missing")
        status = headroom.read_status(state)
        require(status["status"] == "completed" and status["evidence_valid"]
                and status["evidence_complete"] and status["fleet_exit_code"] == 0
                and status["error"] is None and status["run_id"] == run_id
                and {j["id"] for j in status["jobs"]} == {"gcc-compile", "gcc-smoke"}
                and all(j["status"] == "completed" and len(j["attempts"]) == 1
                    and j["attempts"][0]["root_exit_code"] == 0
                    and j["attempts"][0]["logical_exit_code"] == 0 for j in status["jobs"]),
                "final status is not complete successful work")
        journal = events(state)
        bad = {"request_rejected", "deadline", "interrupt", "internal_error",
               "measurement_unavailable", "pressure_intent", "pressure_unresolved",
               "stopped", "terminated", "launch_failed", "allocation_denied"}
        require(all(e["run_id"] == run_id and e["event"] not in bad for e in journal)
                and journal[-1]["event"] == "finished" and journal[-1]["code"] == 0,
                "unexpected or unsuccessful native lifecycle")
        smoke_job["output"] = output_summary(state, "gcc-smoke")
        smoke_result = load(base / "smoke-result.json", 4096)
        require(smoke_result == dict(invocation="no arguments", expected_exit_code=64,
                actual_exit_code=64, successful=True, executable_sha256=binary_hash),
                "expected nonzero smoke outcome not confirmed")
        require(sha(binary) == binary_hash and proof_snapshot() == before,
                "source or binary changed during work")
        lifecycle = [{k: e[k] for k in ("event", "task", "attempt", "elapsed_ms", "code")}
                     for e in journal if e["event"] in
                     {"listening", "queued", "completed", "sealed", "finished"}]
        result = dict(schema_version=1, successful=True,
            scope="Separate post-suite real GCC compilation and smoke demonstration; no aggregate test count change.",
            baseline=before, provenance_unchanged=True, settings=SETTINGS,
            resource_summary=resource_summary(),
            compiler=dict(executable_name="gcc.exe", executable_sha256=sha(gcc),
                argv=["gcc.exe", *command[1:]], cwd=".",
                build_flags_source="build.ps1", exit_code=0),
            artifact=dict(path=relative(binary), bytes=binary.stat().st_size, sha256=binary_hash,
                sources_sha256={name: sha(ROOT / name) for name in [*SOURCES, "src/support.h"]},
                pairing="Observed source and output hashes, not reproducible-build attestation."),
            compilation=compilation, smoke_job=smoke_job, smoke=smoke_result,
            final_status=status, durable_seal=True, lifecycle=lifecycle,
            owner_returncode=process.returncode, seconds=round(time.monotonic() - start, 6),
            raw_results=relative(base),
            limits=dict(owner_deadline_ms=30000, fixture_deadline_seconds=32,
                supervisor_deadline_seconds=40, smoke_deadline_seconds=5,
                output_bytes_per_job_stream=1048576, owner_and_fixture_stdio="NUL",
                outer_job_commit_mib=768, outer_job_active_process_limit=32),
            limitations=["One small real compiler workload; no provider or throughput benchmark.",
                "Expected usage exit 64 is checked by the submitted smoke wrapper; the wrapper exits 0 only on that expected result.",
                "Untrusted output and full command arguments remain only in ignored private raw artifacts."])
        save(base / "result.json", result)
    finally:
        if process.poll() is None:
            process.kill()  # Exact owned handle; supervisor outer job contains descendants.
        process.wait(timeout=5)


def supervise(base):
    """Hold the only outer handle; assign the coordinator before first instruction."""
    import _winapi
    import msvcrt
    group = new_owned_job()
    process_handle = thread_handle = None
    assigned = False
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x200 | 0x8
        limits.BasicLimitInformation.ActiveProcessLimit = 32
        limits.JobMemoryLimit = 768 * 1024 * 1024
        require(kernel.SetInformationJobObject(group, 9, ctypes.byref(limits),
                                              ctypes.sizeof(limits)), "outer limits failed")
        argv = [sys.executable, str(Path(__file__)), "--fixture", relative(base)]
        startup = subprocess.STARTUPINFO()
        with open(os.devnull, "r+b", buffering=0) as null:
            handle = msvcrt.get_osfhandle(null.fileno())
            os.set_handle_inheritable(handle, True)
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES
            startup.hStdInput = startup.hStdOutput = startup.hStdError = handle
            startup.lpAttributeList = {"handle_list": [handle]}
            process_handle, thread_handle, _, _ = _winapi.CreateProcess(
                sys.executable, subprocess.list2cmdline(argv), None, None, True,
                4 | subprocess.CREATE_NO_WINDOW, None, str(ROOT), startup)
        require(kernel.AssignProcessToJobObject(group, process_handle),
                "suspended fixture assignment failed")
        assigned = True
        require(in_job(process_handle, group), "outer fixture membership differs")
        require(kernel.ResumeThread(thread_handle) == 1, "fixture resume failed")
        kernel.CloseHandle(thread_handle)
        thread_handle = None
        require(kernel.WaitForSingleObject(process_handle, 40000) == 0,
                "supervisor deadline expired")
        code = w.DWORD()
        require(kernel.GetExitCodeProcess(process_handle, ctypes.byref(code)),
                "fixture exit code unavailable")
        require(code.value == 0, "fixture failed; inspect private failure diagnostics")
        deadline = time.monotonic() + 3
        while accounting(group).active:
            require(time.monotonic() < deadline, "outer job did not drain")
            time.sleep(0.025)
        counts = accounting(group)
        return dict(fixture_assigned_before_resume=True, fixture_outer_membership_verified=True,
                    kill_on_close=True, breakaway_allowed=False, active_processes_after=0,
                    total_processes=counts.total, terminated_processes=counts.terminated,
                    fixture_returncode=code.value, outer_job_drained=True)
    finally:
        try:
            if process_handle and not assigned:
                # Closing a handle cannot kill an unassigned suspended child.
                require(kernel.TerminateProcess(process_handle, 70),
                        "unassigned suspended child termination failed")
            if assigned and accounting(group).active:
                require(kernel.TerminateJobObject(group, 70), "outer termination failed")
        finally:
            kernel.CloseHandle(group)  # No inherited/query handle keeps it alive.
            if process_handle:
                kernel.WaitForSingleObject(process_handle, 5000)
                kernel.CloseHandle(process_handle)
            if thread_handle:
                kernel.CloseHandle(thread_handle)


def report_notes(result):
    artifact = result["artifact"]
    return """# Real GCC compilation through the live owner

Successful separate post-suite demonstration. The existing baseline remains
{tests} passed tests; this separate demonstration does not increase that count.

The existing `build/headroom-fleet.exe` SHA-256 is
`{owner}`. All native sources, used Python ownership/client helpers and binaries
matched the successful current full-suite proof. No build/ binary changed.
Every recorded input matched that proof before and after this demonstration.
The stable baseline is `{proof}`, SHA-256 `{proof_hash}`.
{reporting_note}

One accepted `gcc-compile` job invoked actual GCC with build.ps1's strict flags:

```text
{command}
```

GCC exited 0 and created `{artifact}` ({size} bytes), SHA-256
`{digest}`. The JSON records relative source names and hashes with the output
hash; this is an observed pairing, not a reproducible-build claim.

A second accepted `gcc-smoke` job ran that executable with no arguments under
a small Python assertion wrapper. Its actual usage exit was 64, exactly the
expected nonzero result. Only that result maps to wrapper exit 0. Child stdout
and stderr flowed directly into the owner's bounded capture, without Python
buffering. Both logical jobs completed once with code 0.

Both native receipts were accepted before seal. The durable seal, final
`finished` code 0, complete successful status, and EOF metadata for both output
streams of both jobs were checked. No output was discarded. The JSON preserves
receipt identities, request/validated-plan hashes, lifecycle, and output sizes
and hashes; raw command arguments and output remain private.

The fixture coordinator was created suspended and assigned to a fixture-owned
kill-on-close outer Job Object before resume. The native live owner, GCC's
compiler/linker descendants, smoke wrapper and compiled executable inherited
containment. The shared `serial_fleet` lock covered launch through cleanup.
Declared settings: 30-second live session, at most 64 jobs / 128 processed
requests; global commit budget 384 MiB, hard cap 512 MiB, reserve 0 MiB;
each of `gcc-compile` and `gcc-smoke` claims 192 MiB with a 384 MiB hard cap.
Limits: 768 MiB outer commit, 32 outer processes, 32-second fixture deadline,
40-second supervisor deadline, 5-second smoke deadline, and 1 MiB per job stream.
Coordinator stdio went to NUL. No host pressure, accounts, network or global
configuration changes were involved.

Cleanup confirmed zero active outer-job processes; all fixture/outer handles
were closed and the serial lock released. Ignored `{raw}` retains the new
executable and private raw state for audit. No existing process was terminated.

Re-run explicitly with `python tools/compiler_demo.py --go` after archiving the
two evidence files; existing evidence is refused. Different owner/native/used
source hashes are refused against this proof. This is one small compiler workload, not a
provider demonstration, pressure test or throughput benchmark.
""".format(owner=EXPECTED_OWNER, command=subprocess.list2cmdline(result["compiler"]["argv"]),
           artifact=artifact["path"], size=artifact["bytes"], digest=artifact["sha256"],
           raw=result["raw_results"], tests=result["baseline"]["tests"], differences=", ".join(
               "`" + name + "`" for name in result["baseline"]["unrelated_baseline_source_differences"]
           ) or "none", proof=result["baseline"]["report"],
           proof_hash=result["baseline"]["report_sha256"],
           reporting_note=("\nFinal reporting rebound the baseline to the byte-identical archive and added\n"
               "the declared resource summary, without rerunning GCC or smoke. The JSON\n"
               "distinguishes the executed script hash from the delivered reporting script hash.\n"
               if result.get("reporting") else ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", action="store_true")
    parser.add_argument("--fixture", metavar="PRIVATE_RELATIVE_DIRECTORY", help=argparse.SUPPRESS)
    parser.add_argument("--smoke", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    base = None
    try:
        require(os.name == "nt", "Windows native demo required")
        if args.fixture or args.smoke:
            name = args.fixture or args.smoke[0]
            base = (ROOT / name).resolve()
            require(base.parent == ROOT / "test-output" and base.name.startswith("cc-"),
                    "private fixture directory refused")
            require(in_job(kernel.GetCurrentProcess()), "outer containment required")
            if args.smoke:
                return smoke(base, args.smoke[1])
            fixture(base)
            return 0
        require(args.go, "explicit --go required")
        require(not EVIDENCE.exists() and not NOTES.exists(), "existing evidence refused")
        with serial_fleet():
            before = proof_snapshot()
            base = ROOT / "test-output" / ("cc-" + uuid.uuid4().hex[:10])
            base.mkdir(parents=True, exist_ok=False)
            cleanup = supervise(base)
            result = load(base / "result.json")
            require(result["baseline"] == before and proof_snapshot() == before,
                    "baseline changed across supervised fixture")
        cleanup.update(outer_handle_closed=True, shared_serial_fleet_lock_released=True,
                       private_raw_retained=True)
        result["cleanup"] = cleanup
        # Export structured values only: no raw stderr/stdout or private argv.
        serialized = json.dumps(result, indent=2, sort_keys=True)
        require(str(ROOT).lower() not in serialized.lower()
                and ":\\\\" not in serialized and "C:/Users/" not in serialized,
                "personal absolute path in evidence")
        save(EVIDENCE, result)
        with NOTES.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(report_notes(result))
        print(json.dumps(dict(successful=True, evidence=relative(EVIDENCE),
                              notes=relative(NOTES), artifact=result["artifact"], cleanup=cleanup)))
        return 0
    except Exception as error:
        if base is not None and base.is_dir():
            with (base / ("failure-" + uuid.uuid4().hex[:8] + ".txt")).open("x", encoding="utf-8") as stream:
                traceback.print_exc(file=stream)
        print("Compiler demo refused/failed (" + type(error).__name__
              + "); diagnostics remain in ignored test-output/.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
