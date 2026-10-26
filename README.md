# Headroom

[![Windows verification](https://github.com/darshanrajagoli/headroom/actions/workflows/windows.yml/badge.svg)](https://github.com/darshanrajagoli/headroom/actions/workflows/windows.yml)
[![MIT license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A small native Windows coordinator for command-line jobs you explicitly give
it. Queue important work first, keep descendants within shared commit limits,
and checkpoint/restart only applications that actually support it.

This is a local, AI-assisted engineering prototype, not a transparent manager
for every Claude/Codex session, a credential vault, or a claim that scheduling
is new. See [prior art](PRIOR_ART.md), [results](RESULTS.md), and
[safety boundaries](SAFETY.md).

**Experimental v0.1.0 · Windows x64 · no AI in the scheduling loop.**
Start with the [five-minute demo](DEMO.md) or download the unsigned review
bundle from [Releases](https://github.com/darshanrajagoli/headroom/releases).
See [release notes](CHANGELOG.md) for supported scope and known limitations.

## What works

- One native coordinator for a finite batch or a live session of up to 64 jobs.
  Independent clients can submit while other work runs; explicit receipts prove
  queue admission, not completion. Python is an offline client, not a resident
  coordinator; no model/network call runs in the control loop.
- Priority/FIFO admission, measured over-claim accounting, a host-availability
  gate, and a fresh fleet measurement before each admission.
- Windows Job Objects contain newly launched processes before execution.
  Ordinary descendants share their parent's cap and the fleet's outer cap.
- Sustained pressure can select only explicitly stoppable, over-claim jobs;
  protected peers are not selected. The decision is rechecked before publishing
  a nonce-bound checkpoint request.
- Bounded checkpoint grace, owned-tree cleanup, immutable attempt history,
  and explicit, bounded resume recipes. Stopping is not successful completion.
- Conservative offline crash recovery: require inactive ownership, matching
  plan and checkpoint evidence, and durable duplicate-prevention markers.
  Unknown, failed or unsupported work is refused, not silently replayed.
- A broken task executable fails that task without destroying running peers.
- Bounded per-attempt stdout/stderr logs: retain the first1MiB of each stream,
  drain/count excess, and flush final metadata before terminal task evidence.
  A read-only tail viewer never interprets logged text as instructions.
- Actual installed Codex/Claude CLIs have passed controlled local replay:
  real shell-tool children stayed in the owned group, normal work completed,
  and deliberate controller crashes cleaned up children without false success.
  The model replies were scripted on loopback: no real inference or credentials.

The hard limits are **committed virtual memory**, not physical RAM. Claims
are estimates, samples can miss bursts, and allocation denial can make a job
fail before it saves anything. Pausing a process would not free its memory.

## Build and try

Requires Windows 10/11, 64-bit MinGW-w64 GCC on PATH, PowerShell and Python 3.12
(the tested version). No elevation, account, API key, installer or global
agent-setting change is needed. Build and process-heavy tests run serially.

```powershell
.\build.ps1
.\run.ps1 -Plan .\examples\quickstart.json -State .\first-run
python headroom.py status .\first-run
python tests\verify.py
python tests\analyze.py
python tests\fleet_demo.py
```

Use a **new** state directory with an existing parent. The quickstart runs two
small Python commands and prints the final status. The pressure demo is separate:
it checks overlap, protected completion, a valid checkpoint, and an explicit
resume using saved progress, with a bounded synthetic payload.

A [local review bundle](LOCAL_PACKAGE.md) also supports running the verified
executables without a compiler. Its allowlist excludes private notes and raw
state; hashes detect changed bytes, not publisher authenticity. It is not an
installed service or a signed public release.

## Your own plan

```json
{
  "budget_mib": 256,
  "hard_mib": 384,
  "reserve_mib": 512,
  "timeout_ms": 60000,
  "jobs": [
    {
      "id": "important",
      "priority": 90,
      "claim_mib": 96,
      "hard_mib": 192,
      "argv": ["python", "-c", "print('replace with your own command')"]
    }
  ]
}
```

Paths/arguments are passed directly, not through an implicit shell. Executables
must exist. Higher priority means earlier admission; equal priorities use input
order. A blocked first queued job is not bypassed by smaller lower-priority jobs.
The host gate reserves estimated unspent claims plus `reserve_mib`, but does
not physically reserve pages or control unrelated applications.

All jobs use the owner's captured working directory, including after supported
recovery. A live submitter's directory is not inherited: use absolute script/data
paths. Recovery refuses old state without validated working-directory metadata;
it does not freeze input files or reproduce the entire environment.

To allow pressure stops, explicitly set `allow_stop: true`. Automatic saved-work
resume additionally needs `resume_argv` and `max_resumes` (1–3). Do not enable
these for commands without a real application checkpoint implementation.
Default sustain is 500 ms, grace 2000 ms, and timeout 60000 ms.

Fleet stdout/stderr are captured separately under each task's `attempt-N`
directory. Input is EOF, not an interactive terminal. Excess output is discarded
after1MiB per stream and counted in `output.json`; pipe backpressure can slow
noisy commands. Missing final output metadata means partial/unconfirmed logs,
not task success. Logs may contain secrets printed by an application: keep raw
state private. The legacy single-job backend does not capture output.
Read a bounded tail with:

```powershell
python headroom.py logs .\first-run important --stream stdout --tail-bytes 4096
```

Interactive terminal agents are not yet supported.
Plan/state files contain arguments and paths: **never put secrets in argv**.

## Interrupted work

First inspect the old state. If the coordinator crashed, use the original plan
and a fresh destination:

```powershell
python headroom.py status .\old-run
.\resume.ps1 -Plan .\plan.json -FromState .\old-run -State .\recovered-run
```

The wrapper refuses a live original owner, mismatched plans, ambiguous journals,
normal task failures, missing checkpoint proof, and exhausted resume allowance.
Completed successful jobs are skipped. Never-started jobs can be admitted;
started jobs need an explicit valid saved-work recipe. Recovery preparation
and launch create separate irreversible receipts. Interrupted preparation is a
manual-review state; do not delete markers just to make a retry run.

This prevents duplicate normal-wrapper recovery, not arbitrary manual replay
or exactly-once external effects. Coordinator death kills its owned children;
it cannot take an emergency checkpoint after it has died.

## Shared live queue

Follow [the live guide](LIVE_GUIDE.md) to start one foreground owner, submit
commands from several terminals, inspect logs, and seal the queue when done.
Crash recovery validates the dynamic acceptance history and supported saved
work, then finishes recovered work as a finite batch. It does not reopen intake
or replay commands without checkpoint proof.

## Evidence and internals

The optional [zero-spend agent replay](AGENT_REPLAY.md) documents four real-CLI
cases, pinned source/executable evidence, process-only configuration isolation
and limitations. It is not a productive real-account adapter or generic agent
checkpoint/resume feature. The ordinary test suite makes no model calls.

**481 tests passed**, with no failures, errors or skips and frozen inputs.
The [public Windows CI run](https://github.com/darshanrajagoli/headroom/actions/runs/40401663922)
also rebuilt the source and passed all481 tests, static analysis and the checkpoint demo.
The [measurement report](RESULTS.md) records native-owner private commit,
working set and coarse CPU across 0/1/8 idle jobs (three short trials each).
Median sampled peak private commit was 1.242–1.359 MiB in those short idle trials.
These are different process counters, not total system cost or a host-RAM guarantee. See
[results](RESULTS.md) for CPU resolution, provenance and the older batch
memory/latency comparison; the two experiments are not interchangeable.

`src/fleet.c` owns native lifecycle and measurements; `src/policy.c` contains
portable admission/selection policy. `headroom.py` validates plans and reads
status; `recover.py` prepares conservative recovery without launching a process.
`run.ps1` and `resume.ps1` are offline entry points, not resident services.
`headroom.exe` retains the separately tested single-job backend.

[PROTOCOL.md](PROTOCOL.md) defines the plan, journal and checkpoint contracts.
[The output review](evidence/OUTPUT_REVIEW.md) and
[fleet review](evidence/FLEET_REVIEW.md) record findings and limitations.
Machine-readable evidence binds results to binary and source hashes. Raw runs
can contain local paths and are deliberately ignored by Git.

For a real application-specific checkpoint example, see
[the file-hashing walkthrough](examples/README.md). It produces SHA256 manifests
with bounded chunk reads and reuses completed-file digests after a validated
resume. Interrupted files restart from their beginning, not an imaginary
serialized Python execution state.

This does **not** yet provide real-agent resume adapters, interactive terminals,
host-wide physical-RAM guarantees, journal rotation or production fault-tolerance
certification. A CLI that delegates work to an external broker, VM, container
or remote server may perform that work outside its owned Windows process tree.
See SAFETY.md for remaining release gates.

## Provenance

New source, not a renamed qex fork. Existing approaches include MemMux, Process
Governor, Pueue, WAVA, AgentCgroup, qex, earlyoom and systemd-oomd. Native Windows
ownership/policy/recovery integration is the engineering scope, not proof of
first invention, superiority, adoption, lab endorsement or a hiring guarantee.
Released under the MIT license. No endorsement or adoption is implied.
