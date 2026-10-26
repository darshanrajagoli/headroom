# Live coordinator idle measurement review

All nine optimized trials passed: three five-second windows each with 0, 1,
and 8 accepted, owned, idle jobs. The native owner and every worker finished
successfully. The shared `serial_fleet` lock was released before reporting
completion. No additional native run or core edit was made for this review.

## Optimized measurements

These are medians of each trial's sampled peak process counters. MiB means
1,048,576 bytes. CPU is the median percentage of one logical core, calculated
from the coordinator's kernel plus user CPU time over the measured interval.

| Owned idle jobs | Trials | Private commit, bytes (MiB) | Working set, bytes (MiB) | CPU, one core |
| --- | --- | --- | --- | --- |
| 0 | 3 | 1,224,704 (1.168) | 6,225,920 (5.938) | 0.3125% |
| 1 | 3 | 1,310,720 (1.250) | 6,643,712 (6.336) | 0.0000% |
| 8 | 3 | 1,314,816 (1.254) | 6,651,904 (6.344) | 0.3125% |

Private commit describes this process's private committed memory; it is not
the amount of physical RAM currently resident. Working set describes resident
process pages, including shared pages; it is not uniquely owned physical RAM.
The two counters must not be added together.

CPU deltas ranged from 0 to 0.046875 seconds per window, in observed increments
of 0.015625 seconds. One such increment over five seconds is about 0.3125% of
one core. Zero readings do not establish zero CPU cost, and the short windows
do not support a conclusion that CPU cost improves or worsens with job count.
The individual values are retained in [the complete evidence](live-benchmark.json).

## Descriptive comparison with the quick baseline

The pre-layout quick probe had one zero-job trial. The optimized result has
three zero-job trials; the comparison uses their median sampled peak. There
is no pre-layout 1-job or 8-job measurement in this experiment.

| Zero-job metric | Before: one trial | After: median of three | Observed change |
| --- | --- | --- | --- |
| Private commit | 1,744,896 bytes (1.664 MiB) | 1,224,704 bytes (1.168 MiB) | -520,192 bytes (-0.496 MiB; -29.81%) |
| Working set | 6,750,208 bytes (6.438 MiB) | 6,225,920 bytes (5.938 MiB) | -524,288 bytes (-0.500 MiB) |

The measured reduction is consistent with replacing 64 fixed 8-KiB task path
buffers with pointers and allocating exact path storage for each actual
attempt, then freeing it on attempt closure. Only `src/fleet.c` changed among
the source hashes shared by the before and after captures; the benchmark
script's hash is identical in both captures. Heap accounting and page rounding
mean the observed process counter delta need not equal the nominal buffer sum.

This is an unequal-repeat descriptive observation, not a statistical estimate
of the optimization's effect. Host, loader, and scheduling variation remain.
Neither counter establishes an absolute physical-memory minimum, total system
cost, or a workload capacity guarantee.

## Method and provenance

Each trial starts a fresh native `--live-plan` session compiled by the offline
client. Requests use the `comparison_worker.py` hold fixture with zero retained
synthetic payload. All workers must be admitted, ready, pinned by HANDLE, and
verified as members of this fresh owner's named job before the shared barrier
starts their 5.5-second hold timers. The runner then samples for five seconds,
about every 50 ms. Count order is 0/1/8, 8/1/0, then 0/1/8.

The coordinator HANDLE comes directly from its owned `Popen`; it is not
rediscovered through a PID scan. Version-2 live owner metadata, version-1
session metadata, matching run IDs, the persisted empty plan, and the held
owner lease are checked. The owner stays unsealed throughout the sample,
including the zero-job case. Explicit sealing follows measurement, and durable
completion plus worker exit is required.

The counters include the native coordinator's intake thread. The external
Python sampling/client runner and all Python workers are excluded. No AI
runtime, model callback, network service, real account, or global setting
change participates. Policy limits are fixed; no host pressure is induced.
Kernel pipe/job bookkeeping is not separately attributed to these process
counters. Sampled peaks can miss peaks between observations.

The optimized binary SHA-256 is
`6a8ee6c0ff3506d536f90ddf345c741cf0c5236b61549a6b39c9851db6f6ca6d`.
The matching schema-2 [full-suite report](native-tests-pre-path-fix.json) records 391 tests,
zero failures/errors/skips, `passed: true`, and unchanged inputs. Its captured
SHA-256 is `2b9b7108979bea9ae56e9ea946c03a5007278712cb62dbe7b1a6bae1ddabf0e0`.
The benchmark verified the expected executable and matching report before
launch; binary and source hashes stayed unchanged before/after every trial.
These hashes bind the observed pairing; they are not a reproducible-build proof.
That391-test report is retained separately because a later wrapper-only review
found a relative-path defect. Current final aggregate evidence may include its
regressions; do not substitute that new report's hash for this captured report.
The native executable was not changed by that wrapper fix. Technical documents
may be revised after a measurement; the JSON retains the exact original hashes.

The baseline binary SHA-256 is
`b696cdb4cddff2d65e92f699e0334e9c6186e0531acc0d426ede8506c6415a29`.
Its proof was the successful 15-case live regression report, not the optimized
391-case full-suite report.

Portable evidence files:

- [Baseline JSON](live-memory-before-layout.json), SHA-256
  `222e83d23d65860e889dbaa93afb64e73eeacd2572587dbcf528f94a73296d3e`.
- [Optimized JSON](live-benchmark.json), SHA-256
  `263fe34693a5f92d55ee946ebdc24fa44fab3fe515bda27bcaff2585a6d30752`.

Raw samples, receipts, state, and outcomes remain in the ignored private
`test-output/live-benchmark-81372e469472` directory. The baseline raw directory
is `test-output/live-benchmark-789d314ff955`. Portable summaries contain only
relative source/artifact names; private absolute paths and command lines stay
in local raw state.
