# Capture-era measurement review

October24,2026. Final batch binary:
`26b043362ee0892b5b9cb36009810d7c86a624c1af250d24cb2306e8ccdb54cc`.
Fleet18-test report and aggregate255-test report passed on this executable.
Both final measurement sets passed with unchanged before/after manifests and
the matching fleet report. These are local measurements, not external review.

Run serially from the project root, without editing/rebuilding inputs:

```powershell
python tests/fleet_benchmark.py --expected-binary-sha256 26b043362ee0892b5b9cb36009810d7c86a624c1af250d24cb2306e8ccdb54cc --integration-report evidence/fleet-tests.json
python tests/compare_fleet.py --expected-binary-sha256 26b043362ee0892b5b9cb36009810d7c86a624c1af250d24cb2306e8ccdb54cc --integration-report evidence/fleet-tests.json
```

Coordinator-only medians from three5-second repeats each: private commit
1.094/1.164MiB, working set6.180/6.250MiB for1/8 idle jobs. One-core CPU medians
0.312%/0.000%; coarse short-window accounting does not prove zero cost/scaling.
Runner/workers and kernel job/pipe bookkeeping are not included in these figures.

Three alternating paired repeats complete the same120 checksummed steps per
protected/growing/small task. Default OS priorities, same declared claims and
initial allocation rules. Retained payload <=120MiB; growing scratch is
disposable and starts empty after resume. Both modes completed exact checksums
without missing/repeated progress; each native run recorded two matching
checkpoint acknowledgments and two explicit resumes.

Raw/native medians: worker peak149.527/131.684MiB; commit area402.698/302.538MiB·s;
batch3.344/7.328s; small-task wait0.235/4.109s. Protected completed in both modes.
Read RESULTS.md and individual trial JSON for every latency/counter. This is
retained-memory reduction with more queue/restart delay, NOT universal benefit.

Both modes run only owned synthetic children. No real host pressure, unrelated
process control, real agent accounts or credentials. Sampled sums/peaks can miss
transients; area uses trapezoids; observer and launch/checkpoint/restart effects
are included in wall time. Real applications may need costly state reconstruction.
Three short paired repeats are descriptive, not statistical/general throughput
claims or comparisons against MemMux/Pueue/WAVA/Process Governor. Source/binary
hashes record observed pairing, not reproducible-build attestation.

Historical pre-output/provisional results and failed raw runs are retained.
output-comparison-first-failure.json records the capture-era fixture PermissionError;
which external component held the original file was not proven. A real owned
exclusive-handle regression reproduced Python CRT EACCES without winerror.
Recognized Windows access errors now retry for<=200ms in the application only;
permanent/unclassified errors fail. The corrected comparison and full suite
then passed. No failed trial is silently included as a success or old metric
presented as a current measurement. Historical native-benchmark.json measures
an older single-job guard and is excluded from current coordinator claims.
