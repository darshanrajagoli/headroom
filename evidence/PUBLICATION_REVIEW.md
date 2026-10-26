# Prerelease correctness review — 2026-10-27

AI-assisted independent local review, not third-party security certification.
Findings were treated as release blockers rather than waived by the existing
test count. No real accounts, paid inference, host OOM or unrelated-process
operations were used.

## Native findings and fixes

1. **P1: suspended create/assign crash window.** A controller dying after
   CreateProcessW but before AssignProcessToJobObject could leave a suspended
   child outside its Headroom jobs. Guard and fleet now use the documented
   Windows 10+ PROC_THREAD_ATTRIBUTE_JOB_LIST to bind ownership atomically
   during creation; jobs are ordered outermost to innermost.
2. **P2: pending deadline/interrupt overwritten by success.** Empty-group or
   all-done processing could precede cancellation checks. Both backends now
   check the pending conditions before success; fleet refreshes the clock
   before terminal publication as well. This is conservative observed-time
   enforcement, not real-time reconstruction of a child's exact exit instant.

`tests/launch_probe.c` intercepts creation/query/clock functions only in isolated
fixture builds. The harness stalls after actual process creation before it
returns to the controller, kills that exact controller, and keeps an external
cleanup job open while checking the pinned child handle. It also injects a
deadline/interrupt at an empty-group observation. No production hook exists.

The original source failed all six bug regressions and passed both normal
controls. The patched source passed all eight cases (14.472 seconds in the
targeted run). Raw runs remain ignored under test-output; tests can reproduce
the fixed behavior without the original private development history.

## Python/recovery findings

3. **P1: package smoke ownership after unsuspended launch.** The fixture now
   creates its wrapper suspended, assigns its own cleanup job, then resumes
   the exact primary thread. This is fixture ownership, not a new native ABI.
4. **P2: recovery could inherit a different working directory.** Native owners
   now persist a bounded versioned working-directory record before launching;
   tasks receive it explicitly. Preparers validate the file/directory and
   wrappers restore it. Missing legacy records are refused, not guessed.
   This does not capture executable/input contents or the environment.

The recovery/package slice passed194 targeted tests, including real batch and
live recovery launched from a different directory, exact saved-progress reuse,
assignment/resume failure cleanup and real exit code259. Targeted counts overlap
the final aggregate and are not added to it.

Final aggregate, package smoke and public CI outcomes are reported in RESULTS.md
and source-bound JSON evidence, not added to the targeted eight-case count.
Remaining product/safety limits are unchanged except where explicitly noted
in PROTOCOL.md and SAFETY.md.

## Fixture reliability

A fresh review run found one failing test among452: the level-1 oplock helper
published its seal file before acquiring the lock, so intake could win that
setup race. The failed aggregate is retained in
native-tests-public-review-first-failure.json, not counted as a pass.
A fixture-only schedule interception now gates the first scan until a real
oplock is granted. The test deliberately holds the old race window open,
then still requires an actual lock break, owner deadline exit124, no sealed
success and explicit helper release. Production intake has no setup hook.

## Second-environment recovery observations

Public CI first encountered hosted8.3 TEMP aliases; fixtures now use a canonical
directory without weakening path refusal. A subsequent Windows Server2022 run
40399224880 exposed two genuine namespace-observation gaps: a new receipt or
output-parent neighbor could escape a directory-metadata-only recheck. Both
were reproduced locally by holding directory signatures constant; both new
regressions failed before the fix. Recovery now compares bounded entry-name
sets as well as metadata before reservation (state directories<=256 entries;
output parent<=4096). This remains an observed snapshot, not an atomic namespace
lock or malicious-same-user security boundary. Device-path refusal diagnostics
also consistently identify path refusal across Windows normalization behavior.

Another hosted run found a fixture-only acknowledgment sharing race: the fake
wrong-nonce worker repeatedly rewrote stop.ack while a query reader could hold
read sharing. Its premature exit meant a test could not demand forced cleanup.
The reproducer now deliberately pins such a reader; the worker publishes its
invalid nonce once and remains alive. The test still requires terminate_intent,
no checkpoint acknowledgment and unchanged wrong-nonce bytes. Native rejection
logic was not relaxed or changed for this fixture fix.
