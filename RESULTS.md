# Verified supervisor results

Verified October27,2026 on Windows11, Python3.12 and MinGW-w64 GCC14.2.
Local prototype, not deployed software, a merged contribution or a claim of
research novelty. A public prerelease does not imply adoption or endorsement.

## Current live + batch verification

**479 tests passed**, zero failures/errors/skips, with implementation/fixture
inputs and all three native binaries unchanged throughout the final
aggregate pass. See evidence/native-tests.json. Repeated targeted suites and
demonstrations are NOT added to479. The report records the exact duration.

- Strict C11/O2/Wall/Wextra/Werror build passed; all six C translation units
  passed actual GCC O0/fanalyzer object compilation (static-analysis.json).
- Includes all255 previous batch tests plus20 live-client,15 native-live,
  91 live-recovery,3 initial live wrapper/recovery and7 live-status tests,
  plus4 follow-on wrapper regressions (relative recovery and provider refusal
  in both batch/live modes),48 offline agent-adapter/parser tests and9 local
  replay protocol/environment tests. The four installed-CLI demos are separate.
- Tool implementations are now included in the frozen source manifest because
  the new tests import them. The ordinary aggregate makes no model calls and
  does not require installed Codex/Claude executables or accounts.
- Prerelease review added8 native launch/cancellation regressions,7 package
  ownership cases,7 batch and3 live working-directory validation cases, and2
  real cross-directory recovery cases. Test C fixtures are included in the
  manifest; isolated compiler-interception tests require GCC. See
  evidence/PUBLICATION_REVIEW.md for before/after failures and fixes.
- Native live checks include independent submissions, priority, durable receipt
  before launch, malformed-request isolation, bounded scan fairness, seal,
  deadline with blocked input, and owned descendant/crash cleanup.
- Real wrapper checks cover submit/logs/seal, crash after checkpoint ack with
  exact-progress recovery once, recoverable canonical JSON companion, duplicate
  refusal, and unsupported interrupted work refused rather than replayed.
- Independent local reviews found request-local persistence and sharing failures
  that could affect peers, scan starvation, and partial metadata publication.
  They were fixed and regression-tested. This is not external certification.
- A later independent audit reproduced relative PowerShell recovery paths
  stranding a prepared recovery after Set-Location. Both wrappers now bind all
  FileSystem paths before irreversible preparation;11 targeted wrapper tests
  and that milestone's395-test aggregate passed; those regressions also pass
  in the current aggregate. Before/after raw logs remain private.

Current live/batch fleet SHA256:
`fcd81c198d824befb75c7084b2cf2613bf3ed1407ecd289b3f017076051fafe9`.
Guard and policy hashes plus source hashes are in aggregate evidence.
Rebuilding may change PE timestamps; do not reuse evidence for a different
binary merely because its filename is the same.

Initial public Windows CI run40398387773 failed because the hosted runner's
TEMP used an8.3 alias (`RUNNER~1`), which the local path contract intentionally
refuses. CI now puts fixtures under a canonical local RUNNER_TEMP subdirectory;
the alias-refusal checks were not weakened. A failed remote run is not counted
as successful verification.

## Actual installed agent replay ($0 external services)

All four final cases passed: Codex normal/crash-owner and Claude normal/
crash-owner, on identical frozen replay implementations. Each normal case ran
the actual CLI's shell tool, observed the live child inside the known owner
root, verified explicit provider-stream completion and native success, and
drained. Each crash case observed the child die before its fixed work finished;
the interrupted run was not reported as completed. These separate demos are
not additional aggregate test cases.

Only scripted loopback Responses/Messages were served. No real key, model
inference, paid service, purchase, publication or host OOM was involved. Codex's
external metadata CONNECT attempts reached the local proxy and were denied.
The proxy is not an OS-enforced egress boundary; the fixture is not a security
sandbox for arbitrary programs. Agent recovery and model quality are untested.

The native ancestor kernel hard cap was1GiB; declared task hard cap768MiB;
fixture child scratch8MiB/3s. The child in Claude directly observed768MiB.
Codex introduced a nearer job with limit0 but remained in the capped ancestor;
we do not claim its unnamed per-task cap was directly queried. One outer job
per case contained every fixture process and drained to zero active processes.
Crash injections intentionally killed the newly created owner; a zero kernel
terminated counter is not evidence that no deliberate termination occurred.

See evidence/agent-cli-replay.json and AGENT_REPLAY.md for pinned CLI/source
hashes, exact scope, safe structured records, reproduction and official docs.
Earlier failed attempts remain private under test-output/ar-* and are not
silently counted as successful cases.

## Current live coordinator overhead

Nine successful trials: three five-second sample windows each with0/1/8 owned
zero-payload idle jobs. Owner remains unsealed during sampling, then drains.

| Median sampled peak | 0 idle jobs | 1 idle job | 8 idle jobs |
| --- | ---: | ---: | ---: |
| Private commit | 1.234 MiB | 1.316 MiB | 1.348 MiB |
| Working set | 6.754 MiB | 7.105 MiB | 7.137 MiB |
| CPU, percent of one core | 0.0000% | 0.0000% | 0.0000% |

These are coordinator process counters, excluding Python clients/workers and
separately attributed kernel bookkeeping. Private commit is not physical RAM;
working set includes shared resident pages. Do not add the two counters.
CPU increments were coarse (15.625ms); zero does not establish zero cost.
No resident model/Python coordinator or network callback participated.

Historical layout experiment: replacing64 fixed8KiB path buffers with exact per-attempt allocation reduced
the zero-job observed private commit by520,192bytes (29.81%) versus one quick
pre-layout trial. Unequal repeat counts and host variability make this a
descriptive observation, not statistical proof or a minimum-memory theorem.
See evidence/live-benchmark.json for the CURRENT fixed native owner, paired
with the exact479-test report hash and frozen source/executable before/after
each trial. Historical figures (1.168–1.254MiB private,5.938–6.344MiB working set)
remain in live-benchmark-pre-public-review.json, bound to the older391-test
report and different native binary. They must not be relabeled as current.
live-memory-before-layout.json and LIVE_MEASUREMENT_REVIEW.md concern that
historical layout experiment, not a controlled causal comparison of this
release's ownership/recovery fixes. Host variation also affects these counters.

## Real compiler demonstration (current native owner)

Separately from the aggregate, the live owner accepted an actual GCC build of
guard.c/support.c into a new disposable executable and a second smoke job.
GCC exited0; the smoke explicitly checked the executable's expected usage
exit64 without relabeling a native failure as success. Both logical jobs
completed, seal and final output metadata were confirmed, and the fixture's
outer job drained to zero active processes with no forced terminations.
The duration is recorded in compiler-demo.json, not a throughput comparison.
No build/ binary changed.

Declared session budget/hard cap384/512MiB, per-job claim/hard cap192/384MiB,
reserve0, outer fixture cap768MiB and30-second session lifetime. See
evidence/compiler-demo.json and COMPILER_DEMO.md. This is one real compiler
workload, not a Claude/Codex provider test or an additional aggregate test.

## Local packaging verification

The explicit-allowlist ZIP builder requires the current479-test source/binary
proof and refused stale proof before creating output. Nine separate offline
format tests passed, including changed/extra/duplicate files, traversal, links,
Windows aliases/devices and missing manifest. They are not added to479.

A fresh extracted package completed the batch quickstart and foreground live
serve/submit/seal flow through its own wrappers/clients. Confirmed successful
status, accepted receipt, exact captured output and final metadata; all spawned
processes exited. No compiler, account or original-checkout code was needed by
the extracted workloads. The fixture runner itself used checkout ownership
helpers; this is a same-host smoke, not an isolated second-machine test.

See evidence/package-format-tests.json, package-smoke.json and LOCAL_PACKAGE.md.
The ZIP's manifest covers its payload bytes; the external smoke record names
the tested archive SHA256. Private operational notes, histories and raw state
are not packaged. Hashes are not signatures or publisher authenticity.

## Saved batch-only baseline

The following results concern the saved commit58663f6 and fleet26b04336,
not current live-mode performance. Its original255-test aggregate evidence is
retained in that local Git commit; evidence/native-tests.json now contains the
new combined479-test run. Batch regressions are also included in that run.
Baseline source/analysis evidence likewise refers to the saved commit; the
current static-analysis.json binds the current six-source implementation.

Short development commit identifiers in historical sections refer to the
preserved local history, not refs promised in the clean public repository.
Historical JSON retains its original source/binary hashes; it is not evidence
for different current code or a benchmark of existing competing tools.

### Baseline verification

**255 tests passed**, zero failures/errors/skips, with implementation/fixture
inputs and all three native binaries unchanged during the aggregate pass.
See the saved commit's evidence/native-tests.json. Repeated targeted suites are not added to255.

- Strict native C11/O2/Wall/Wextra/Werror build passed.
- All five C translation units passed actual object compilation with
  O0/fanalyzer/Wall/Wextra/Werror, unchanged source manifest; see
  evidence/static-analysis.json. Earlier syntax-only commands are not a substitute.
- The separate18-test native wire-format suite and pressure demo passed on
  the same executable, including fresh admission, descendant caps, peer survival,
  checkpoint/resume, malformed strings and coordinator-crash containment.
- Nine output tests verify exact per-stream retention/discard counts, binary
  fidelity, late descendant output, stdinEOF, nonzero exits and separate attempts.
- Sixteen offline log-viewer tests verify bounded tails, strict counters,
  unsafe/mutating files refused and logs treated only as data.
- Four wrapper tests cover successful launch, invalid input, real crash/checkpoint
  recovery with duplicate refusal, and a real SHA256 application stopping under
  native pressure then resuming to correct digests from a bounded64MiB fixture.
- Nineteen reference-app tests and four request-reader regressions include a
  real Windows exclusive-handle reproduction. Recognized access errors get
  <=200ms application-side retry; permanent errors fail without a false ack.
- Quickstart and comparison self-test passed. Windows CI YAML was parsed locally;
  remote CI and a second host have NOT been validated.

Measured/integration-tested fleet executable SHA256:
`26b043362ee0892b5b9cb36009810d7c86a624c1af250d24cb2306e8ccdb54cc`.
Guard/policy hashes and all source hashes are in aggregate evidence. Rebuilding
can change PE timestamps: a new executable needs a new verified pairing.

### Baseline batch coordinator overhead

Three repeats each with one/eight zero-payload idle jobs, five-second sample
windows. Coordinator only: Python runner/workers and kernel bookkeeping excluded.

| Median sampled peak | 1 idle job | 8 idle jobs |
| --- | ---: | ---: |
| Private commit | 1.094 MiB | 1.164 MiB |
| Working set | 6.180 MiB | 6.250 MiB |
| CPU, percent of one core | 0.312% | 0.000% |

Zero means no resolved counter increment, not zero cost. Short idle trials
do not establish CPU scaling, long-run leak freedom or a memory minimum.
The native loop makes no model/network calls; development still uses tokens.

### Baseline equal-work raw-process comparison

Three serial paired repeats, alternating order. Same priorities, claims,
initial payload rules and exactly120 checksummed steps/task. Native soft target
96MiB, root hard commit ceiling256MiB; retained synthetic payload <=120MiB plus
interpreter overhead. Scratch is disposable and need not be restored on resume.
All six runs preserved exact progress/checksums. Protected work completed in
both modes; growing work checkpointed/resumed twice in each native run.

| Median | Ungoverned | Headroom |
| --- | ---: | ---: |
| Sampled aggregate worker private commit peak | 149.527 MiB | 131.684 MiB |
| Worker private commit area | 402.698 MiB·s | 302.538 MiB·s |
| Whole batch completion | 3.344 s | 7.328 s |
| Protected wait / completion | 0.203 / 3.266 s | 0.281 / 3.343 s |
| Small task wait / completion | 0.235 / 3.281 s | 4.109 / 7.156 s |

This is a **memory/latency tradeoff**, not a speedup or physical-RAM guarantee.
Worker sums exclude coordinator/observer memory; account for coordinator cost
separately. Samples can miss transient peaks. Short healthy-host cache/sleep/hash
trials are descriptive, not general performance claims or competitor benchmarks.

Final measurement manifests stayed unchanged and reference the matching18-test
report. See evidence/fleet-benchmark.json, fleet-comparison.json and
MEASUREMENT_REVIEW.md for individual trials, checksums and exact commands.
Pre-output/provisional results are historical only. The first capture-era
comparison failed on a request-file PermissionError; its raw run and diagnosis
remain retained, not counted as success. No external Windows component causing
that original lock was independently identified. The reproduced error class
and bounded retry are tested; the subsequent full comparison passed.

## Scope and release gates

Working: finite <=64-job priority/FIFO batch or shared live queue, measured admission, inherited
process-tree commit caps, explicit checkpoint/restart, conservative offline
batch/live recovery, bounded output and read-only status/logs. Independent local reviews
found real defects that were fixed; this is not external security certification.

Not established: interactive terminals,
transparent Claude/Codex/browser recovery, host-wide RAM safety, production
adoption, power-loss correctness or exhaustive storage-failure coverage.
Protected means pressure-ineligible, not immune to owner crash/deadline/OS.
See SAFETY.md and the evidence review records.

Live intake now has explicit receipts, bounded off-policy discovery and fixed
session policy. Interrupted supported work recovers as an ordinary batch;
unaccepted inputs remain unaccepted. Work delegated outside the owned Windows
process tree is not proven contained. Further release gates are real-provider
adapter boundaries, second-environment CI, sustained runs and storage faults,
not a claim that all existing agents are now safely managed.
