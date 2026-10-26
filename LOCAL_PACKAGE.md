# Local review bundle

This is an unsigned experimental review bundle. The packaging tools do not
upload anything, install a service or edit global agent settings. Official
downloads are attached to this repository's GitHub Releases; verify the
published SHA256 before extracting and inspect the supported safety boundaries.

## Run without compiling

Extract the verified ZIP to a fresh local directory. Windows10/11 x86-64,
Windows PowerShell and Python3.12 are required. From that directory:

```powershell
.\run.ps1 -Plan .\examples\quickstart.json -State .\first-run
python headroom.py status .\first-run
```

Use a new state directory each time. No compiler, account or API key is needed
for this quickstart. Follow LIVE_GUIDE.md for two-terminal submission, receipts,
sealing and supported recovery. Commands are noninteractive, stdin is EOF,
and printed output is retained only up to1MiB per stream per attempt.

The native executables import Windows system/UCRT libraries; no MinGW-specific
runtime DLL was found in this build's imports. That is inspection on one host,
not a second-host installation test. Source/tests are included for review;
rebuilding or running native static analysis additionally needs MinGW-w64 GCC.

## Produce and check a new bundle

From the development checkout, after successful verification:

```powershell
New-Item -ItemType Directory -Path dist -Force
python tools/package_local.py build dist/headroom-local.zip
python tools/package_local.py verify dist/headroom-local.zip
python tools/package_smoke.py dist/headroom-local.zip
python tools/test_package_local.py
```

The builder requires matching current source/binary hashes from a successful
frozen aggregate test report. It uses an explicit allowlist and exclusive
output creation; an existing ZIP is never overwritten. Private plans/handoffs,
chat histories, credentials, raw logs and run-state directories are excluded.
Do not ship a failed or partially written output; retain it for inspection and
use a new filename after correcting the cause.

PACKAGE_MANIFEST.json records every payload file's size and SHA256. The verifier
rejects extra/missing/duplicate members, unsafe paths, links and mismatched
bytes, including Windows device-name and case-alias traps. Checksums detect
change, not publisher authenticity: a malicious party
could replace both files and manifest. There is no signing or update channel.

The smoke tool verifies before extraction into a fresh ignored test-output
directory, runs the extracted batch and live wrappers/clients, checks status
and captured output, and contains its newly created processes in test-owned
kill-on-close jobs. It never rebuilds the archive's executables. These are
additional packaging checks, not extra aggregate test cases or remote CI.

The ZIP includes current live-overhead evidence. Older batch comparison evidence
is available in the development checkout and its saved baseline commit; do not
mistake it for a comparison of the packaged live binary. The public technical
documents describe remaining boundaries; operational handoff notes stay local.

Optional actual-CLI replay instructions and consolidated normal/crash evidence
are in AGENT_REPLAY.md. The package includes the fixture source, not Codex or
Claude binaries. These opt-in checks require trusted installed CLIs and use
scripted loopback responses, never real credentials or paid inference. The
quickstart and ordinary aggregate tests do not require those CLIs.
