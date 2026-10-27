# Observer verification and boundaries

The observer is a separate source component. Its tests are not part of the
coordinator's historical481-test proof or its v0.1.0 binary download.

Local verification on Windows11,Python3.12,MinGW-w64 GCC14.2:

```powershell
.\observer\build.ps1
gcc -std=c11 -O0 -Wall -Wextra -Werror -fanalyzer -c observer/collector.c -o observer/build/collector-analysis.o
python -B -m unittest discover -s observer -p 'test_*.py' -v
```

Final local run:47 observer/report/summary tests passed,zero failures/errors/skips,
in12.882s. [Public Windows verification40527012287](https://github.com/darshanrajagoli/headroom/actions/runs/40527012287)
passed on source commit `87e34bb6430cf3c66e8ac711c88aa19348e18ed3`:fresh native
build,actual observer analyzer compilation and47 tests in10.020s,zero failures,
errors or skips. The workflow separately passed the481 coordinator tests,
package-format checks and protected-peer checkpoint demonstration. The hosted
Windows Server2022 runner used Python3.12 and rebuilt its own binaries;it did
not install a downloaded observer binary or test a real Claude account.

The strict native build and actual analyzer object compilation passed.
The synthetic suite covers native deadlines,cooperative stop,failed reporting,
refusal to overwrite existing/hardlinked outputs,linked directory refusal,
numeric argument bounds and exclusion of the observer's own process tree.
Parser/report checks cover skipped sensitive strings,bounded work,opaque keyed
cursors,common truncation/regrowth,PID reuse,partial samples and sampling gaps.
Summary checks cover finalized inputs,overlap/duplicate refusal,independent
counter epochs,fresh output directories,privacy and absence of history reads.
An independent source review caught error-ended recordings being accepted as
successful summaries when the report helper itself exited successfully. Summary
generation now refuses both sampling-error and log-limit endings; a synthetic
regression covers each. Requested stops and normal deadlines remain supported.

Real recordings and private reports are deliberately absent from this repository.
No physical host exhaustion,paid inference,credentials or existing-Claude control
is required for the ordinary verification workflow. Public Windows CI rebuilds
the observer and runs these same synthetic tests separately from the coordinator.

The native collector requests query-only process access and checks token user
and Windows session. It excludes its own process subtree from Claude totals.
Only its own bounded Python helper is placed in a cleanup Job Object; existing
Claude processes are never attached to the coordinator or controlled.

Important interpretation limits:

- Private commit is not physical RAM; summed working sets can double-count
  shared pages. Reported ancestry is not kernel ownership.
- Collector memory/CPU counters exclude temporary Python helpers and kernel
  bookkeeping. A small collector counter is not a total monitoring-overhead claim.
- Event timestamps cover selected local Claude Code sources,not all desktop
  conversations. Prompt candidates may be repeated or injected;content is skipped.
- Bounded fingerprints detect common rewrites,not every possible hostile edit.
- Sleep,suspension and delayed sampling create unknown gaps. Observation does
  not prove productivity,prevented crashes,saved RAM or resumable Claude work.
- The finite observer is not a reboot-persistent service. Restarting to extend a
  deadline creates a separate segment with an explicit unmeasured boundary.

The existing coordinator demonstrates application-specific checkpoint/restart
and owned-process containment using bounded fixtures. Extending that guarantee
to arbitrary already-running applications remains unsupported.
