# Changelog

## v0.1.0 — 2026-10-27 (experimental prerelease)

First public engineering prototype for Windows x64.

- Native finite batch/live queue, priority/FIFO admission and fresh measured
  accounting before launch; no resident model or Python scheduler.
- Suspended owned launch and nested Windows Job Object commit ceilings.
- Explicitly consenting pressure stops, nonce-bound cooperative checkpoint
  acknowledgments, bounded grace and application-specific resume recipes.
- Conservative batch/live crash recovery with immutable attempts and separate
  preparation/launch receipts; ambiguous or unsupported work is refused.
- Bounded stdout/stderr capture, read-only inspection and offline clients.
- Synthetic checkpoint demo, file-hashing example and controlled installed
  Codex/Claude replay using local fake model replies.
- Source-bound test/measurement evidence and a checksummed unsigned review ZIP.
- Prerelease review closed suspended-launch crash gaps, deadline/interrupt
  false-success ordering, package-test ownership and recovery working-directory
  drift. Regressions reproduce the original failures and the corrected behavior.
- Second-environment CI uncovered delayed directory metadata and fixture
  sharing/alias differences. Recovery now rechecks bounded entry sets directly;
  fixture setup remains deterministic without weakening the safety checks.

### Not supported

Linux/macOS, interactive terminals, generic agent checkpointing, physical-RAM
guarantees, arbitrary-process control, an always-on unlimited service, real
provider adapters, exactly-once external effects or production certification.
Long-duration and exhaustive storage-fault testing remain future work.

Test counts and measurements are recorded in [RESULTS.md](RESULTS.md), not
claims of deployment/adoption or competitive superiority. The release is
unsigned; checksums detect changed bytes, not publisher authenticity.
