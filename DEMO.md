# Five-minute demo

For a timed explanation and a verified downloaded-binary run on Windows2025,
see the [five-minute walkthrough](DEMO_WALKTHROUGH.md).

Headroom is a traffic controller for command-line work it launches itself.
It is not an agent brain: no model decides whom to stop, and no API key is
required. This demonstration uses small synthetic jobs, not host-wide memory
exhaustion or real accounts.

## 1. Build and run a small queue

On Windows x64 with PowerShell, Python 3.12 and MinGW-w64 GCC on PATH:

```powershell
git clone https://github.com/darshanrajagoli/headroom.git
cd headroom
.\build.ps1
.\run.ps1 -Plan .\examples\quickstart.json -State .\demo-run
python headroom.py status .\demo-run
```

Use a fresh state directory every time; do not remove recovery markers to
force a retry. The quickstart launches two small commands. Look for successful
task completion in the final status, not just a queue-acceptance receipt.
The [review bundle](LOCAL_PACKAGE.md) includes prebuilt unsigned executables
if you do not have a compiler. Inspect code/checksums before running it.

## 2. Watch saved-work recovery

```powershell
python tests/fleet_demo.py
```

This bounded scenario has an important protected job, a lower-priority job
that deliberately grows beyond its estimate, and queued work. Expected proof:

```text
protected + low-priority work overlap
  -> sustained measured pressure selects only the consenting low job
  -> that application saves progress and acknowledges the exact request
  -> its old process tree ends
  -> its explicit resume recipe continues saved work
  -> protected, resumed and queued work complete
```

The command prints individual PASS/FAIL checks and writes the portable summary
to `evidence/fleet-demo.json`. Raw runs are private and Git-ignored. A stopped
job exiting zero is still a stop, not successful completion. A real file-hashing
checkpoint example is described in [examples/README.md](examples/README.md).

## 3. Try independent submitters

Follow [LIVE_GUIDE.md](LIVE_GUIDE.md) to start one foreground owner, submit
from separate terminals, inspect bounded logs and seal intake. The live owner
is finite, not an always-on unlimited system service.

## Optional installed-agent test

[AGENT_REPLAY.md](AGENT_REPLAY.md) documents opt-in tests using installed
Codex/Claude executables with deterministic local fake model replies. This
tests real tool-child containment and crash cleanup, not model intelligence,
productive real-account work or resumable agent sessions. Ordinary tests do
not launch either agent or make model calls.

## What to take away

Windows Job Objects enforce **commit** ceilings for owned descendants;
sampling and priority control admission and consenting pressure stops.
Application checkpoint contracts—not frozen process memory—preserve progress.
This cannot reserve physical RAM, save arbitrary agents, control unrelated
apps or guarantee exactly-once external side effects. See [SAFETY.md](SAFETY.md).
