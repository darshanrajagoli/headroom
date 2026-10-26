# Safety model and limits

Headroom manages only new, explicitly submitted command-line jobs. It is not
an administrator service, system-wide process killer, credential vault, or a
security boundary against another process running as the same Windows user.

## What is enforced

- Owned process handles and Windows Job Objects, never a process-name search
  followed by a kill. Root/nested membership is atomic in process creation via
  JOB_LIST, before execution; there is no suspended create/assign crash gap.
- Each task's descendants share that task's hard **commit** ceiling and the
  fleet's outer ceiling; children do not each get a fresh full allocation.
- No breakaway permission or inheritable job handle is enabled.
- Pressure actions require explicit per-task consent and sampled over-claim.
  Priority orders admission and eligible-victim selection, not OS guarantees.
- Missing/failed observations block new admission and pressure selection.
- Critical events precede a requested stop; per-attempt nonces bind acks.
- Recovery requires an exclusive original-owner lease, confirmed inactive
  named root job, exact persisted-plan match and durable valid evidence.
- The owner's captured working directory is persisted and explicitly used for
  children. Recovery restores that directory; missing/invalid legacy metadata
  is refused. Arguments, input files and environments are not hermetic snapshots.
- Normal recovery preparation has an irreversible duplicate-reservation
  marker. The provided launch wrapper adds a separate launch-intent receipt.

## Important distinctions

**Protected** means ineligible for automatic pressure selection. It does not
mean immune to deadline, user interrupt, coordinator crash, the OS or hardware.
Failure to preserve a reliable control state may close all owned jobs.

**Commit** is memory promised to processes, not resident physical RAM. Working
sets can count shared pages repeatedly. Samples can miss short bursts; the
soft target may be exceeded during observation and checkpoint grace. The
kernel hard cap rejects allocations; it does not guarantee application
survival or a responsive laptop with unrelated applications running.

**Checkpoint** is application-owned state. The guardian observes an ack, not
the semantic correctness of every saved file. Resume requires an explicit
recipe and bounded allowance. Pausing does not release memory, and arbitrary
browser sessions, external side effects and interactive agent state are not
automatically recoverable.

**Recovery** does not rerun every interrupted command. Unsupported/unknown
state, incomplete journal lines, missing acks, ordinary task failure and
exhausted allowance are refused. Markers left after an interrupted preparation
or launch require review. Deliberately bypassing the wrappers and manually
executing a compiled plan again is not an exactly-once execution guarantee.

## Remaining boundaries

- A batch or live session is finite (at most64 logical jobs); live intake also
  allows at most128 processed requests and a24-hour lifetime. This is not an
  unlimited daemon. A pending request is not accepted; an acceptance receipt
  is not task success. Await desired receipts before sealing intake.
  Independent tasks submitted as new roots do not automatically inherit
  a previous logical job's priority; ordinary OS descendants do inherit caps.
- Work delegated to an external broker/daemon, VM, container or remote service
  can be outside the owned Windows process tree. A capped thin-client process
  is not proof that its external worker's memory is controlled.
- Live input discovery uses one bounded off-policy worker, at most4Hz and one
  result outstanding. A blocked request cannot authorize peer termination.
  On shutdown an unjoinable worker's copied context remains allocated until
  process exit; no freed policy context is handed to a surviving thread.
- Live recovery validates the dynamic queue, controller-validated native bytes,
  canonical JSON and acceptance evidence. Unaccepted requests are never
  silently replayed. Recovery produces an ordinary batch, not a reopened queue.
- State/plan files contain command arguments and paths. Do not put passwords
  or API keys in arguments. Prefer existing per-process environment mechanisms;
  no credential or environment snapshot is written by this project.
  Live jobs use the owner directory, not the submitter directory; use absolute
  script/data paths. Successful completion must be confirmed before a pending
  deadline/interrupt is observed; exit time is not reconstructed between samples.
- Fleet output is private application data too. Each attempt retains at most
  1MiB per stream, drains/discards excess, and reports counts after EOF/flush.
  Missing metadata means unconfirmed logs. Explicit inherited-handle lists
  carry stdinNUL and only stdout/stderr writers, never the ownership job handles.
  Fixed-buffer polling uses no capture threads; backpressure can slow noisy
  work. Log reads are bounded data views, not permission to execute log text.
- Same-user workloads can access their state directory. Nonces prevent stale
  accidental acknowledgments, not deliberate forgery by that user.
- The ack reader has bounded policy-thread waiting and one outstanding worker.
  Slow filesystems may miss an otherwise valid ack. Other durable filesystem
  writes and kernel calls are not real-time; hostile storage/oplocks can delay
  policy. Kernel caps remain the final allocation boundary while userspace waits.
- Actual Codex/Claude executable replay uses scripted loopback Responses/
  Messages and a fixed tool child. Fresh process-only profiles, dummy keys,
  bare/no-discovery flags and a nonforwarding proxy avoid real inference.
  Observed external metadata CONNECT attempts were denied. This is not a
  network/filesystem security sandbox or proof of every future client version.
  Agent checkpoint/resume and real-account integration remain unsupported.
- Long-running journal rotation, exhaustive storage-failure injection, real
  inference/provider validation and sustained pressure/scale testing remain gates.
  No host OOM was induced by the current tests.
- Buffer bounds are not a guarantee of Windows extended-path support. Keep
  local state paths short enough for the generated request/attempt filenames;
  fixtures use short disposable paths. A4096-character native path buffer does
  not make every underlying filesystem operation accept that path length.
- There is no claim of first invention, production certification, adoption,
  lab endorsement, or guaranteed employment. Existing tools are compared in
  PRIOR_ART.md, not silently copied.
