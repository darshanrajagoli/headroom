# Live intake review record

Local AI-assisted review, not third-party certification. Native ownership and
policy remain the existing batch mechanisms; dynamic inputs add a bounded
off-policy worker, session identity, immutable requests and durable receipts.

## Findings fixed before the current aggregate run

- A request that supplied its own validated.hrp could trigger a persistence
  failure affecting accepted peers. Intake now requires exactly three input
  files; request-local validated-write failures reject that request with73.
  Only failed root attribution escalates the session.
- A sharing/access failure when claiming one ready directory could abort
  discovery. Windows errors5/32/33 defer that request and continue the scan.
- Restarting enumeration at every256-entry scan could starve valid requests
  behind unknown entries. A retained enumeration cursor makes forward progress.
- Direct session/receipt writes exposed partially visible metadata. Exclusive
  temporary writes, flush, close and nonreplacing rename publish complete files.

The independent native reviewer rechecked these changes and reported no
remaining static P1/P2 blocker in its scope. Fifteen native-live integration
cases and the full391-test aggregate passed afterward. Test fixture cleanup
confirmed all50 tracked native-live fixture PIDs inactive in the targeted run.

## Memory-layout follow-on

Task paths no longer reserve64 fixed8KiB arrays. An attempt allocates its exact
bounded path length and frees it on closure. Formatting uses the actual
allocated capacity; asynchronous ack helpers copy paths rather than borrow the
freed allocation. All391 tests and six-source object analysis passed on this
layout. LIVE_MEASUREMENT_REVIEW.md records before/after measurements and limits.

## Runtime/recovery coverage and limits

Version2 live ownership prevents old recovery from silently treating a dynamic
queue as a batch. The dedicated recovery path validates actual queue order,
validated native bytes, canonical JSON, matching session/request/receipts,
inactive ownership and existing checkpoint/resume constraints. It publishes a
canonical companion before the recovered binary under one irreversible
reservation; partial preparation remains a refusal/manual-review state.

Three real wrapper cases exercise submit/logs/seal, acknowledged crash recovery
once at saved progress with a usable batch companion, and unsupported crash
refusal. Offline recovery includes91 new cases. No real provider, credential,
host OOM, power-loss or adversarial same-user test is claimed.

## Follow-on wrapper audit

A separate read-only Python/client/recovery audit found a P2 path-consistency
defect in both PowerShell recovery wrappers. After Set-Location, .NET
GetFullPath could resolve against the process directory while Python resolved
against the PowerShell location. Preparation could reserve the right old state
but launch-intent publication target the wrong directory, stranding recovery.

The fix resolves every plan/settings, old-state and destination argument through
PowerShell's path resolver before irreversible preparation and rejects non-
FileSystem providers. Real relative-path checkpoint-recovery regressions and
provider refusal checks passed in11 targeted wrapper cases and the final395-
test aggregate (zero failures/errors/skips, frozen inputs). See RESULTS.md
for the final count; native-overhead evidence remains bound to the preserved
391-test pre-wrapper-fix report and unchanged native binary.
