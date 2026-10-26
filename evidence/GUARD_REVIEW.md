# Independent guard and shared-helper audit

Date: 2026-10-23. Local review of `src/guard.c`, `src/support.c`,
`src/support.h`, and `tests/test_guard.py`, after reading `PROTOCOL.md` and
`PLAN.md` fully. Public helper signatures were preserved. This record covers
the review build, not certification of the subsequently integrated fleet.
All paths and commands below are relative to the project root.

## Findings and fixes, by severity

- **P1: acknowledgment open/read could block policy enforcement.** A valid
  `stop.ack` under an owned fixture's exclusive file oplock blocked the original
  synchronous open; the soft stop became timeout 124. The full open, metadata
  check, and bounded read now run off the policy thread, with one outstanding
  worker and at most a 5 ms configured wait per call. Worker storage outlives
  the operation; completed results are checked against both path and nonce.
  The regression now returns soft-stop 125 within its bounded grace tolerance.
- **P2: interrupt handling returned without confirming group exit.** Handler
  registration is checked before launch and removed at cleanup. Interrupts now
  use the bounded termination-monitoring path and record `interrupted` with
  zero active processes before returning 130. A real CTRL_BREAK regression
  verifies that both owned parent and descendant have exited.
- **P2: failure cleanup and error reporting were incomplete.** Assignment
  failure attempted termination without confirming it; cleanup could also
  obscure the originating error. Cleanup now distinguishes unassigned
  suspended children from assigned groups and checks termination with a
  five-second bound. Original errors are retained; helper buffer exhaustion,
  zero-byte writes, allocation failure, and GUID failure receive explicit
  errors. Regressions cover restrictive outer-job launch rejection and
  command expansion overflow (`ERROR_INSUFFICIENT_BUFFER`, 122).
- **P3: failed request publication left its created temporary file.** A target
  collision reproduced the residue. Publication still creates
  `stop.request.tmp` exclusively, flushes it, and renames with
  `MOVEFILE_WRITE_THROUGH` without replacement. Failure now removes only the
  temporary successfully created by that call and restores the originating
  error. Regressions preserve existing request/temporary contents and verify
  collision errors 183/80 respectively.

## Verification actually performed

Only `build/headroom-review.exe` was built by this reviewer; integration
binaries were not replaced. The two initial targeted regressions failed on
the original build before fixes. Final strict compilation and static analysis
passed:

```powershell
gcc -std=c11 -O2 -Wall -Wextra -Werror -municode src/guard.c src/support.c -o build/headroom-review.exe -lole32 -lpsapi
gcc -std=c11 -Wall -Wextra -Werror -fanalyzer -fsyntax-only src/guard.c src/support.c
```

The final guard suite ran serially: **29 tests passed in 22.340 seconds**,
including ten added tests and strengthened existing assertions. The executable
override was temporary and confined to the importing test process:

```powershell
python -B -c "import sys,unittest; from pathlib import Path; sys.path.insert(0,'tests'); import test_guard as t; t.GUARD=Path('build/headroom-review.exe').resolve(); s=unittest.defaultTestLoader.loadTestsFromModule(t); r=unittest.TextTestRunner(verbosity=2).run(s); sys.exit(not r.wasSuccessful())"
```

The existing evidence-reader suite then passed **11 tests in 0.045 seconds**:

```powershell
python -B -c "import sys,unittest; sys.path.insert(0,'tests'); import test_evidence; r=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(test_evidence)); sys.exit(not r.wasSuccessful())"
```

Coverage includes wrong-run, oversized, partial and directory acknowledgments;
acknowledged root exit with a stubborn descendant; unchanged grace; aggregate
child commit denial; breakaway refusal; guardian crash containment; Unicode
and CRT argument quoting; and full Windows exit codes, including 259 and high
bits. Fixtures used bounded allocations and only test-owned processes. No
host memory pressure experiment, external messaging, or publication occurred.

## Integration follow-up and untested boundaries

The integrating agent additionally changed `src/fleet.c` to defer empty-job
finalization (`return 2`) while the original grace deadline remains, allowing
an already flushed acknowledgment to finish its asynchronous read. The defer
condition was located in source; its fleet runtime behavior is not included
in this review's test counts. The reported integration does not extend grace
or terminate an already empty group. No suites were rerun for this record.

Slow or unreadable acknowledgment storage may still conservatively miss a
valid ack. The helper's 5 ms wait is not a hard real-time scheduling guarantee.
Other filesystem operations, including journal flush and request publication,
remain synchronous. Power loss, disk exhaustion, remote filesystem behavior,
direct assignment/ResumeThread failure injection, failed termination APIs,
and prolonged kernel I/O cancellation were not independently fault-injected.
The outer-job test exercises launch rejection, not every suspended-launch
failure branch. Acknowledgment bytes do not establish application checkpoint
correctness. The same-user trusted-workload boundary remains; committed-memory
caps are not physical-RAM guarantees. Fleet policy, recovery, and later source
changes require their own verification.
