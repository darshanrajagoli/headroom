# Shared live submission

Experimental local prototype. All479 combined tests passed, including native
live intake and real wrapper/crash-recovery checks. Separate live-overhead
measurements passed on the same native binary (against the preserved391-test
pre-wrapper-fix proof); see RESULTS.md. No real agent-provider
integration, remote CI or production deployment is claimed.

## One owner, several submitters

Build once, then start an explicit foreground owner in one terminal:

```powershell
.\build.ps1
.\serve.ps1 -Settings .\examples\live-settings.json -State .\live-run
```

In another terminal, submit a noninteractive command:

```powershell
python headroom_live.py submit live-run examples/live-job.json
python headroom.py status live-run
python headroom.py logs live-run first-command --tail-bytes 4096
```

Different agents/terminals can submit their own job JSON into the same state
directory. A request receipt means accepted into the queue, NOT finished. A
pending response means no acceptance is yet proven, even if input files exist.
Inspect status for lifecycle outcomes. Higher priority wins among jobs already
known to the owner; future arrivals cannot preempt time that already elapsed.

To stop accepting new work and finish accepted work:

```powershell
python headroom_live.py seal live-run
```

Await the submission receipts you want BEFORE sealing. Seal is an admission
barrier, not task cancellation. The owner exits after accepted work finishes.
The example's ten-minute lifetime is measured from owner launch, including idle
time, not separately per job. Deadline/interrupt may terminate owned work.
Nothing installs a service, scheduler or global agent setting.

## Safe retry and supported recovery

`--request-id UUID` is useful when retrying an uncertain submission. Identical
request bytes reuse the existing identity; different contents are refused.
Generating a different request ID is NOT an exactly-once side-effect guarantee.
Task IDs are unique within one session; rejected/malformed inputs do not
authorize killing accepted peers.

Live owner metadata is version2. The old batch recovery tool deliberately
refuses it. After an actual owner crash, use the dedicated path:

```powershell
.\resume_live.ps1 -Settings .\examples\live-settings.json -FromState .\live-run -State .\recovered-run
```

Recovery checks original settings, inactive ownership, dynamic queue history,
the controller's validated native bytes, matching canonical input JSON, receipts,
application checkpoint proof and remaining allowance. Unsupported/ambiguous
work is refused, not blindly rerun. Pending inputs were never admitted and stay
in old state; they are not silently turned into accepted work.

Recovered accepted work finishes as an ordinary finite batch, without reopening
intake. Its canonical `recovered-run.recovery.hrp.json` companion supports the
existing batch recovery interface if that new batch later crashes. Preparation
and launch have irreversible receipts; interrupted/partial preparation needs
review. Do not delete markers just to force another run. Do not kill unrelated
applications to experiment: the regression fixtures own all of their processes.

## Limits

- At most64 accepted logical jobs,128 processed requests,24-hour lifetime.
  Capacity seals admission. This is not an unlimited production queue daemon.
- Shared kernel limits govern COMMIT, not physical RAM. Sampling/admission is
  not a guarantee against unrelated apps consuming the laptop's memory.
- Actual descendants share caps. A separately submitted logical job does not
  automatically inherit a previous task's priority or delegation authority.
- Jobs use stdinEOF and bounded separate output logs, not a terminal/PTY.
  Credentials are not copied between agent sessions or stored by this feature.
  Commands inherit the owner's environment; never put secrets in argv/logs.
- Checkpoints remain application-specific. No arbitrary Claude/Codex/browser
  session gets a restart feature merely by launching it under this owner.
- One bounded intake worker performs input discovery/read/claim away from the
  policy thread. No model/network call or resident Python coordinator. Measure
  its cost separately; the measured live owner used1.168–1.254MiB private
  commit and5.938–6.344MiB working set in short0/1/8-idle-job trials.
  These are process counters, not total system cost or a physical minimum.
- A CLI that dispatches work through an external daemon, VM, container or
  remote service may put that work outside the owned Windows process tree.
  Launching that CLI does not prove containment of its external worker.
- Trusted same-user/local storage assumptions remain. This is not an adversarial
  user sandbox, real-time scheduler or power-loss certification.

See LIVE_INTAKE.md for the implementation contract, SAFETY.md for shared limits,
and RESULTS.md for current live verification and the saved batch baseline. No publication or outreach has
occurred.
