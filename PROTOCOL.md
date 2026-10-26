# Fleet plan protocol v1 (implementation contract)

Native executable: `build/headroom-fleet.exe --plan FILE --state NEW_DIRECTORY`.
The plan compiler is an offline Python command; no Python coordinator remains
resident. The native coordinator runs a finite batch, writes durable evidence,
and returns 0 only if every logical job completes successfully, 1 for completed
batch with a failed/stopped job, 124 on fleet deadline, 130 on handled interrupt,
64 for malformed plan/options, 73 for existing/refused state, 70 internal error.

## User JSON

```json
{
  "budget_mib": 128,
  "hard_mib": 256,
  "reserve_mib": 256,
  "sustain_ms": 500,
  "grace_ms": 2000,
  "timeout_ms": 60000,
  "jobs": [
    {"id":"build", "priority":50, "claim_mib":48, "hard_mib":160,
     "allow_stop":false, "argv":["python", "script.py"],
     "max_resumes":0, "resume_argv":[]}
  ]
}
```

Root required: budget_mib, hard_mib, jobs. Defaults shown above for reserve,
sustain, grace, timeout. Job required: id, claim_mib, hard_mib, argv. Priority
defaults 50, allow_stop false, max_resumes 0, resume_argv empty. Reject unknown
keys, duplicate ids, bool-as-int, nonintegral numbers, embedded NULs and invalid
strings. IDs match `[a-z0-9][a-z0-9-]{0,39}` except reserved Windows device
names con/prn/aux/nul/com1..9/lpt1..9. One to 64 jobs. Priority 0..100.
Positive claims <= budget and <= job hard <= root hard. Root hard > budget.
MiB integral values <= 1,048,576 (1 TiB). Reserve may be zero to disable the
physical-availability gate for bounded tests; otherwise it gates admission.
Grace 1..60000ms; sustain 100..60000ms; timeout 1..86400000ms. max_resumes 0..3;
positive resume count requires allow_stop and a nonempty resume_argv.
argv/resume_argv are arrays of strings; resolve argv[0] to an existing .exe
(PATH lookup allowed); quote using Windows CRT conventions, never implicit
shell. Only executable names are resolved by the compiler; other argv strings
are passed literally. Use absolute script/data paths when submitting from a
different directory. Jobs run in the coordinator's captured working directory,
not each live submitter's directory.

Before any child launch, the owner durably creates `working-directory.hrc`:
eight ASCII bytes `HRCWD001`, followed by the nonempty absolute directory as
strict UTF-16LE, no terminator (at most4095 code units). Each child receives
that captured path explicitly as its working directory. Recovery requires this
metadata, an existing canonical local unlinked directory and stable identity
during preparation; the provided wrappers restore it for the recovered owner.
Missing/legacy or invalid metadata is refused before reservation. File contents,
environment variables, PATH dependencies and external effects are NOT captured
or rolled back; a working directory is not a hermetic workload snapshot.

## Compiled binary

Little endian, exact end of file required, no native struct padding.
Header `struct.pack('<8sIQQQIII',...)`:

- magic bytes `HRFLEET1`, count u32;
- budget bytes u64, root hard bytes u64, reserve bytes u64;
- sustain ms u32, grace ms u32, timeout ms u32.

For each job in original FIFO order:

- id: u32 byte length then ASCII bytes, 1..40 bytes, validated above;
- priority u32, allow_stop u32 (0/1), max_resumes u32;
- claim bytes u64, job hard bytes u64;
- four strings: executable, quoted command line, resume executable,
  quoted resume command line. Each is u32 UTF-16 code-unit count then UTF-16LE
  bytes, no terminator. Executables 1..4095 units; command 1..32766 units.
  Both resume strings must be empty when no resume recipe; otherwise valid.

Plan reader refuses >16 MiB, truncation, trailing bytes and unsafe bounds before
creating state or launching anything. It validates independently of Python.

## Native policy and evidence

One root Job Object with shared hard commit limit; each logical job has a
nested Job Object with its own aggregate hard limit. A
`PROC_THREAD_ATTRIBUTE_JOB_LIST` binds root then nested atomically as the kernel
creates the suspended process, closing the create/assign controller-crash gap.
Job handles are not inherited. No breakaway flag. Actual
descendants share both limits. This does NOT govern independent queue jobs
submitted by an agent as a new logical root, nor promise exact physical RAM.

Before admission, sample all owned job members via kernel job membership,
OpenProcess handles, IsProcessInJob and GetProcessMemoryInfo.PrivateUsage.
Failures invalidate the cycle: no admission or pressure stop. Admission uses
sum(max(claim, sampled private commit)) + next claim <= budget. Strict queued
priority descending, FIFO on ties; no backfilling past a blocked first job.
Host available physical memory must also cover reserve plus the next claim
and any unspent running claims. Kernel root hard
limit is separate from the soft budget; burst headroom is not guaranteed RAM.

Pressure is sustained sampled sum > budget OR available physical RAM < reserve.
Candidates: running, allow_stop, above claim, and sampled reclaim plausibly
covers the deficit (commit for budget, working-set sum for host reserve).
Choose lowest priority then greatest excess, FIFO tie. No protected job is
eligible. Only one stop pending. Write durable intent, publish per-attempt
nonce request, allow grace and collect ack; only terminate that nested job.
Invalid observations reset continuity. No stop merely because a job exceeds
claim on an otherwise healthy fleet. No model/network call in the loop.

State: root `fleet.jsonl`; per-job `<id>/attempt-N/events.jsonl` and stop files.
Fleet lines are JSON objects with version=1, run_id, event, elapsed_ms, task
(ASCII id or empty string), priority, attempt, total_commit_bytes,
available_bytes, code. Events include prepared, queued, started, sample,
pressure_intent, checkpoint_acknowledged, terminated, stopped, completed,
resume_queued, deadline, finished, internal_error. Some extra diagnostic
events may be added; readers must not infer completion from unknown events.

`launch_failed` is a terminal task outcome (logical code70), allowed after its
initial queue or started event. It is not queued work eligible for replay.
Expected launch failures clean up only that task's newly created process;
protected peers continue. Failure to clean up or attribute the failure is a
coordinator error and closes the owned fleet.

An accepted stop remains stopped for that attempt even if its root exits zero.
Automatic requeue is allowed ONLY with an explicit resume recipe, a matching
ack, all group processes gone and remaining max_resumes. Requeue retains
priority, claim and caps. No retry on normal failure, timeout or missing ack.
Persist resume_queued before starting the next fresh attempt. No automatic
queue recovery after coordinator crash yet: terminal absence is ambiguous and
must never be interpreted as success or a license to rerun arbitrary commands.

## Bounded fleet output

Fleet attempts launch with stdin at EOF (NUL), and separate stdout/stderr pipes.
STARTUPINFOEX's explicit handle list contains only those three child handles;
root/nested job handles, journals and parent pipe readers are not inherited.
The coordinator closes its child-handle copies immediately after CreateProcess.
This is for noninteractive commands, not a terminal/PTY implementation.

Per attempt, `stdout.log` and `stderr.log` each retain their FIRST 1,048,576
bytes. Excess is drained and counted, not buffered without bound or written
to disk. One owner thread reads at most64KiB per stream per cycle into a single
4KiB stack buffer. Output activity shortens polling sleep to25ms; otherwise
existing pressure/idle polling remains. Pipe backpressure may slow noisy jobs.
There are no resident capture threads or model calls.

After the whole owned group exits and both pipes reach EOF, flush both logs,
then exclusively create/flush `output.json` BEFORE terminal attempt evidence:

```json
{"version":1,"limit_bytes":1048576,"stdout":{"retained_bytes":0,"discarded_bytes":0,"eof":true},"stderr":{"retained_bytes":0,"discarded_bytes":0,"eof":true}}
```

An interrupted coordinator may leave partial logs without this metadata. Log
bytes/EOF metadata do not prove task success or checkpoint correctness. Launch
failure may also leave partial/empty files without final metadata. A read-only
log viewer must bound its tail, reject unsafe paths/malformed metadata, and
never promote logged text to instructions. Logs can contain secrets emitted by
the application: treat raw state as private. The single-job legacy guard does
not implement this fleet-output feature.

## Recovery contract

The native coordinator will hold `owner.lock` open for GENERIC_READ|GENERIC_WRITE
with FILE_SHARE_READ only for its whole lifetime, before launching jobs.
It will persist the exact validated input bytes as `plan.hrp` before launching.
`owner.json` is flushed before launch: `{ "version":1, "run_id":"UUID",
"job_name":"Local\\Headroom-UUID" }`. The named outer job has a default
same-user access boundary; names are for safe recovery, not secret authority.
Nested task jobs remain anonymous. OpenJobObjectW with JOB_OBJECT_QUERY can
check ActiveProcesses, and recovery must never signal/terminate an old job.

Offline recovery tool signature:
`python recover.py PLAN.json OLD_STATE OUTPUT.hrp` (no process launch).
Load JSON with strict compiler validation and require compile_plan(input) to
match OLD_STATE/plan.hrp byte for byte. Acquire the owner.lock write lease via
CreateFileW, GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ, OPEN_EXISTING: sharing
violation means LIVE/BUSY, refuse. Verify owner.json run_id matches fleet
journal and root job name. Query named job if it exists: nonzero active count
or any unknown error means REFUSE. Only ERROR_FILE_NOT_FOUND means absent.

Stream/validate fleet journal: truncated, malformed, mixed run or contradictory
state is not safe to recover. A final finished/deadline/interrupt/internal_error
requires an explicit policy verdict; do not turn a failed run into success.
Conservatively permit jobs durably queued with no started event to use their
original argv. A started/interrupted/stopped job needs an explicit resume recipe,
remaining bounded resume allowance, a complete valid per-attempt journal,
and exact nonce-bound stop.request/stop.ack for that attempt. Completed zero
jobs are skipped; normal failures and uncheckpointed work are blocked, never
automatically replayed. A resume_queued event has already consumed one allowance.

Return a report of eligible/skipped/blocked tasks. Default refuse output if any
task is blocked; no hidden partial recovery. If none eligible, report no work.
For an eligible resumed task, replace argv with resume_argv and decrement its
remaining max_resumes without elevating priority/claims/caps. Keep original
budget/reserve/hard settings. Revalidate the new plan with compile_plan.

Before publishing any recovery output, exclusively create and flush
OLD_STATE/recovery-reserved.json (version/run_id/output-path/unique recovery id).
If that marker exists, refuse duplicate recovery; no automatic removal after
failure. Hold the lease through publication. The output is a plan, not restarted
work; an interrupted reservation is intentionally a manual-review state. No
checkpoint/queue is silently deleted or reclassified as completed.

## Parallel work boundaries

- Main agent: src/fleet.c, src/policy.c/h, build integration and overall merge.
- Plan client worker: headroom.py and tests/test_plan.py only.
- Fleet verification worker: tests/test_fleet.py, tests/fleet_worker.py,
  tests/fleet_demo.py only. Do not modify native code or build while main edits.
- Safety reviewer: src/guard.c, src/support.c/h and tests/test_guard.py only;
  preserve shared helper API. No one contacts anyone or publishes anything.
- Recovery worker: recover.py and tests/test_recovery.py only; implement the
  conservative offline recovery contract above without touching compiler/native.
