# Real GCC compilation through the live owner

Successful separate post-suite demonstration. The existing baseline remains
479 passed tests; this separate demonstration does not increase that count.

The existing `build/headroom-fleet.exe` SHA-256 is
`fcd81c198d824befb75c7084b2cf2613bf3ed1407ecd289b3f017076051fafe9`. All native sources, used Python ownership/client helpers and binaries
matched the successful current full-suite proof. No build/ binary changed.
Every recorded input matched that proof before and after this demonstration.
The stable baseline is `evidence/native-tests.json`, SHA-256 `32c3d42ac8812f94ed4b8ff9f0dcbc9dca067ff7d41d32379edc1c22da5cac81`.


One accepted `gcc-compile` job invoked actual GCC with build.ps1's strict flags:

```text
gcc.exe -std=c11 -O2 -Wall -Wextra -Werror -municode src/guard.c src/support.c -o test-output/cc-517f0dd44b/headroom-demo.exe -lole32 -lpsapi
```

GCC exited 0 and created `test-output/cc-517f0dd44b/headroom-demo.exe` (154267 bytes), SHA-256
`e905dfbe57d2bf7907ffafeab2f23bc36108f5e1d2bc2e0c7767a26d31610c51`. The JSON records relative source names and hashes with the output
hash; this is an observed pairing, not a reproducible-build claim.

A second accepted `gcc-smoke` job ran that executable with no arguments under
a small Python assertion wrapper. Its actual usage exit was 64, exactly the
expected nonzero result. Only that result maps to wrapper exit 0. Child stdout
and stderr flowed directly into the owner's bounded capture, without Python
buffering. Both logical jobs completed once with code 0.

Both native receipts were accepted before seal. The durable seal, final
`finished` code 0, complete successful status, and EOF metadata for both output
streams of both jobs were checked. No output was discarded. The JSON preserves
receipt identities, request/validated-plan hashes, lifecycle, and output sizes
and hashes; raw command arguments and output remain private.

The fixture coordinator was created suspended and assigned to a fixture-owned
kill-on-close outer Job Object before resume. The native live owner, GCC's
compiler/linker descendants, smoke wrapper and compiled executable inherited
containment. The shared `serial_fleet` lock covered launch through cleanup.
Declared settings: 30-second live session, at most 64 jobs / 128 processed
requests; global commit budget 384 MiB, hard cap 512 MiB, reserve 0 MiB;
each of `gcc-compile` and `gcc-smoke` claims 192 MiB with a 384 MiB hard cap.
Limits: 768 MiB outer commit, 32 outer processes, 32-second fixture deadline,
40-second supervisor deadline, 5-second smoke deadline, and 1 MiB per job stream.
Coordinator stdio went to NUL. No host pressure, accounts, network or global
configuration changes were involved.

Cleanup confirmed zero active outer-job processes; all fixture/outer handles
were closed and the serial lock released. Ignored `test-output/cc-517f0dd44b` retains the new
executable and private raw state for audit. No existing process was terminated.

Re-run explicitly with `python tools/compiler_demo.py --go` after archiving the
two evidence files; existing evidence is refused. Different owner/native/used
source hashes are refused against this proof. This is one small compiler workload, not a
provider demonstration, pressure test or throughput benchmark.
