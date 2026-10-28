# Five-minute checkpoint walkthrough

Use this to explain what the released program actually does. The example uses
small synthetic jobs that explicitly support checkpoints, not a real Claude chat.

## Verified run on another Windows environment

[Released demo run40657517845](https://github.com/darshanrajagoli/headroom/actions/runs/40657517845)
downloaded the published v0.1.0 ZIP on a fresh standard Windows Server2025
GitHub runner with Python3.12. It verified the pinned archive checksum and
all99 archive members, then ran the extracted batch/live commands and checkpoint
demo. No compiler step ran and the executable hash stayed unchanged.
The [portable evidence](evidence/released-demo-windows2025.json) includes the
runner image, exact hashes, event timeline and every proof predicate.

The lower-priority job saved progress at **68**, its second attempt started
from **68** and finished at **100**. Protected and queued work also completed.
All13 checkpoint-demo predicates passed. The checkpoint scenario took4.219s;
this is one functional demonstration, not a throughput benchmark.

This is an independent hosted Windows VM. It is not a human usability study,
a Windows11 consumer-laptop installation test or a demonstration of recovering
an arbitrary existing Claude session.

## 0:00–0:45: the problem and boundary

“Several programs can grow beyond their memory estimates while they run.
Headroom coordinates jobs I launch through it. Users provide priorities and
estimates, while Windows Job Objects cap committed memory for owned descendants.
The scheduler does not call an AI model.”

Explain that commit is different from physical RAM. Headroom cannot reserve
physical pages or control every other application on the laptop.

## 0:45–1:30: run the released demo

Download the ZIP and SHA256SUMS.txt from the [release](https://github.com/darshanrajagoli/headroom/releases/tag/v0.1.0).
Verify its SHA256 before extraction:

```powershell
Get-FileHash .\headroom-v0.1.0-windows-x64.zip -Algorithm SHA256
```

Expected SHA256:

```text
4e7e0640041e2cbeb9a2e6904e2d871bafc37f78a09427be00b94d07f906935b
```

Extract to a fresh directory and, from that directory, run:

```powershell
python -B tests/fleet_demo.py
```

Python3.12 and Windows PowerShell are required; rebuilding is unnecessary.
The demo uses at most120MiB of synthetic payload plus runtime overhead, with
a finite native deadline. It does not exhaust host RAM or stop existing apps.

## 1:30–2:45: explain the events

Read `evidence/fleet-demo.json`, especially `proofs`, `timeline` and
`measurements.low_attempt_progress`:

1. Protected and lower-priority jobs overlap.
2. The lower-priority job grows beyond its estimate. Sustained measured
   pressure selects that job because it explicitly consented to stopping.
3. Headroom writes a nonce-bound stop request. The application saves progress
   and acknowledges that exact request.
4. The old attempt ends. Its zero exit is recorded as a stop, not completion.
5. The explicit resume recipe starts the next attempt from saved progress.
6. Protected, resumed and queued work all complete.

The exact saved number can vary by run. Check that the resumed attempt's
`started_from` equals the saved progress and that its final progress increases.
Do not require a new run to reproduce the number68.

## 2:45–3:45: explain the design decisions

- **Owned trees:** children must be inside Windows Job Objects before their
  code runs. Merely discovering a process afterward is not equivalent ownership.
- **Checkpoint instead of freezing:** a suspended process retains its memory.
  A cooperating application can save progress, exit and later reconstruct work.
- **Explicit recovery:** a resume recipe identifies supported behavior.
  Unknown interrupted work is refused rather than blindly repeated.
- **Fixed policies:** deterministic priorities and bounded grace periods keep
  scheduling overhead predictable; no model call is needed in the control loop.

## 3:45–5:00: discuss evidence and limitations

“The core has481 recorded tests, and the separate observer has47. The release
demo also passed on a fresh Windows VM using downloaded executables. These are
different checks; I do not count the demo as extra unit tests.”

Be ready to answer:

- **Does this recover any Claude session?** No. This demo's application has an
  explicit checkpoint contract. The observer only measures existing Claude.
- **What if allocation fails before saving?** The task may fail. A memory cap
  cannot force an application to publish a useful checkpoint.
- **What about a network request that already happened?** Exactly-once external
  side effects are not guaranteed; safe replay needs application support.
- **Why not just use an existing queue?** Existing queues are valuable prior art.
  This project explores Windows owned-tree limits and explicit checkpoint/recovery
  boundaries, with reproducible evidence. It does not invent scheduling.

You can rehearse this explanation and compare your answers against the source.
Running the automated demonstration does not establish personal interview fluency.
