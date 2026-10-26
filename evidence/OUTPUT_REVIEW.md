# Bounded output follow-on review

Local implementation and independent read-only review, 2026-10-23. This is not
external certification. Inputs: src/output.c/h, fleet.c integration and the
single-job guard's late-ack follow-up. Use final aggregate/static-analysis
evidence for exact source and binary hashes.

## Implemented invariants

- Only new owned fleet processes. Explicit STARTUPINFOEX inheritance list:
  stdinNUL and two pipe writers. Ownership job handles, journals and parent
  readers are not inherited; coordinator copies of writers close immediately
  after CreateProcess. Suspended assignment ordering is unchanged.
- First1MiB retained per stream/attempt; excess drained and counted. One4KiB
  buffer, <=64KiB per stream/cycle, no capture threads. Busy output shortens
  sleep to25ms. Bounded pipes apply backpressure rather than unbounded RAM use.
- Whole owned group must exit AND both pipes reach EOF. Logs flush before
  output.json flush, before terminal attempt evidence. Pipe EOF alone does
  not complete a live descendant. Buffered descendant bytes survive root exit.
- Partial output on crash/launch failure is not final metadata or success.
  Log data is private and never interpreted as permission/instructions.

## Verification and separate review

Nine independent native tests passed, with raw evidence in ignored
test-output/output-capture/. They check Unicode/binary byte fidelity, stream
separation, exact retained/discarded counts on two-stream floods, empty/one-busy
streams, descendant late output, live-child EOF, stdinEOF, nonzero exits,
separate checkpoint/resume attempts and observable metadata-before-terminal
ordering. Existing coordinator-crash containment was rerun and passed.

The read-only reviewer found no concrete P1/P2 defect in the new paths. It
confirmed explicit handle inheritance does not retain an ownership handle in
children, failed launch cleanup stays isolated, and pending output finalization
does not hide the outer timeout/interrupt checks. Static review does not replace
those runtime tests or prove storage power-loss ordering.

The aggregate had previously exposed a real single-guard late-ack race:
guard-late-ack-first.json/log retain that failed run. Main aligned empty-job
finalization with the fleet's bounded collection behavior, without extending
grace or adding waits after interrupt/deadline. Thirty guard tests, including
six quick-exit repetitions, passed before the capture integration.

## Limits

This is noninteractive byte capture, not a PTY, shell or terminal-agent adapter.
Backpressure slows noisy jobs. Synchronous file writes/flushes can delay policy
under storage faults; no real-time guarantee. A disk/log error can fail closed
and end the owned fleet. Truncated output cannot be reconstructed by the viewer.
Same-user workloads can deliberately pass handles elsewhere or modify state;
the trusted-user assumption is unchanged. Long-duration/storage fault injection
and real-provider command validation remain release gates.
