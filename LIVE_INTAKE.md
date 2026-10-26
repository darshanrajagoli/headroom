# Live intake implementation contract

Saved batch baseline: local commit58663f6,255 tests, fleet binary26b04336.
Live intake is implemented on feat/live-intake; current combined verification
and measured executable hashes are recorded in RESULTS.md and safe evidence.
No network service, privilege change or existing-process attachment.

## First scope

One finite live session, up to64 accepted logical jobs and128 processed
requests, max24h lifetime. Same native owner, policies, process-tree caps,
checkpoint recipes and logs as batch mode. A separate client may submit while
other jobs run. Priority/FIFO applies to known queued jobs, not unknown future
arrivals. Ordinary descendants still share caps; no new budget per child.
No interactive agent, arbitrary saved-session restoration or real credentials.

`headroom-fleet --live-plan SETTINGS.hrs --state NEW_DIRECTORY`.
SETTINGS.hrs uses the existing HRFLEET1 header with countZERO, no rows; only
the explicit live mode accepts that. Batch --plan still requires1..64 rows.
Root settings: budget_mib/hard_mib required; reserve256, sustain500, grace2000,
timeout60000 defaults; existing strict bounds. No jobs key in settings JSON.

The owner creates inbox/, processed/, receipts/, owner.lock and the existing
plan.hrp (empty session header), plus the captured `working-directory.hrc`
defined in PROTOCOL.md. All tasks use the owner's directory, not the submitting
terminal's directory; use absolute script/data arguments. owner.json is version2 with mode:"live",
run_id and job_name. Existing batch recovery MUST refuse this version rather
than guess dynamic queue history. Dedicated live recovery validates that
dynamic history before preparing supported work as an ordinary finite batch.

After root ownership is ready, publish session.json:
`{"version":1,"run_id":"UUID","max_jobs":64,"settings":{"budget_mib":N,"hard_mib":N,"reserve_mib":N,"sustain_ms":N,"grace_ms":N,"timeout_ms":N}}`.
All UUIDs are lowercase canonical36-character UUIDs.

## Offline client (headroom_live.py)

APIs: compile_session(settings)->bytes; submit(state,job,request_id=None,
wait_seconds=5)->receipt/pending dict; read_receipt(state,request_id);
seal(state)->seal_requested dict. CLI init SETTINGS.json OUTPUT.hrs;
submit STATE JOB.json [--request-id UUID] [--wait-seconds N];
receipt STATE UUID; seal STATE. No resident Python/AI/network loop.

Validate session metadata/path, settings, job via existing compile_plan.
Canonicalize executable argv[0]/resume_argv[0] to the exact absolute .exe
resolved by the compiler; assert canonical JSON recompile gives identical
bytes. Use a full one-job plan JSON (root settings plus jobs:[job]) so a future
recovery tool can independently compare JSON and recorded native bytes.

Create a fresh staging directory INSIDE inbox using a fresh random name;
flush exactly three files, then atomically rename without replacement to
inbox/REQUEST_UUID.ready:

- target: exact `headroom-submit-v1\nSESSION_UUID\nREQUEST_UUID\n` ASCII bytes.
- plan.hrp: existing compiled HRFLEET1 plan with exactlyONE job. <=512KiB.
- plan.json: canonical matching full one-job JSON, <=1MiB.

Never overwrite a request. Reusing an explicit request UUID is idempotent only
if target/plan bytes/JSON match the existing inbox or processed request; different
content is refused. A race may be conservatively refused for caller retry;
never publish a second logically different request with the same identity.
Remove only this invocation's three staging files/nonrecursive fresh directory.
Reject reparse/symlink paths and malformed/nonlocal state, bound reads and waits.
Use the existing trusted-same-user boundary, not claims of hostile-user security.

Submission is PENDING until a native receipt exists. Default wait5s, bound0..30s;
timeout is pending, NOT accepted/running/success. No PID/liveness guesses.
Receipt files receipts/REQUEST_UUID.json, <=4096bytes, strict keys:
`{"version":1,"run_id":"UUID","request_id":"UUID","task":"id-or-empty","status":"accepted-or-rejected","code":N,"ordinal":N}`.
Accepted: code0, ordinal1..64, valid task id. Rejected: code64/73/75, ordinal0,
task id or empty. CLI returns0 accepted,75 pending,64 invalid,73 refused/rejected.
Receipt confirms queue admission only, never completed work.

seal() atomically publishes exact `headroom-seal-v1\nSESSION_UUID\n` to
seal.request, idempotent only for identical bytes. It requests a barrier; callers
should await desired submission receipts BEFORE sealing. Once observed, native
accepts no more requests and completes existing accepted work. Native publishes
sealed.json `{"version":1,"run_id":"UUID"}` and a sealed root diagnostic.
Seal does not kill work or claim completion. Empty sealed session is no_work.

## Native side

An explicit one-worker intake channel performs bounded discovery/read/claim
off the policy thread: at most256 directory entries/scan, one result outstanding,
one worker, no job execution there. Poll at most4Hz. A blocked input read must
not block pressure/grace/timeout enforcement or spawn unbounded workers.
Worker storage owns copied paths/settings, not a pointer to a freed Fleet.
If worker cannot finish on shutdown, do not free its live context; process
exit reclaims it. Never terminate arbitrary worker threads or external PIDs.

Claim exactly inbox/UUID.ready -> processed/UUID by nonoverwriting atomic
rename. Ignore already processed duplicate UUIDs and unknown filenames. Check
nonreparse directory/files, exact target identity, plan and JSON size limits.
Only the policy thread parses/accepts a result, writes lifecycle/receipt and
launches children. A malformed request gets a rejection, not peer termination.

Require one compiled job, identical root settings, no accepted id collision,
within64 accepted/128 processed bounds. Persist exact validated native bytes
as processed/UUID/validated.hrp, flush queue event, then flush accepted receipt
BEFORE launch. Retain rejected inputs/receipt. Never silently rerun a submission.
Capacity seals admission; new/unfinished requests remain unaccepted, not success.

Idle session remains alive until seal, lifetime deadline or interrupt. Existing
batch completion behavior is unchanged. Version1 batch recovery is deliberately
not allowed to reinterpret live sessions. recover_live.py validates the actual
dynamic queue order, session/target/receipts, controller validated.hrp and each
canonical JSON/native pair under the original inactive-owner lease. Pending
unaccepted inputs are not replayed. A started task additionally needs existing
nonce-bound checkpoint and explicit recipe/allowance proof.

Recovery publishes OUTPUT.hrp.json first and then OUTPUT.hrp under one durable
irreversible reservation. The companion recompiles identically and permits
later ordinary batch recovery if the recovered batch crashes. resume_live.ps1
records a separate launch intent, then uses --plan, not --live-plan. Partial
preparation/launch needs manual review; never remove markers to force replay.

## Required checks

Independent clients while jobs run; one owner; admission priority; duplicate
UUID and task id; invalid/cross-session requests preserve peers; seal/deadline;
owned descendants/crash cleanup; intake oplock does not block deadline; bounded
output; durable receipt before launch; no batch regressions. Measure live idle
owner overhead separately; do not reuse batch overhead for the worker mode.
