# Contributing

Small reproducible correctness fixes are welcome. Read README.md, SAFETY.md
and PROTOCOL.md before changing lifecycle or recovery behavior.

Use Windows x64, Python 3.12, PowerShell and MinGW-w64 GCC. Keep process-heavy
checks serial and do not rebuild or change sources during verification:

The aggregate includes isolated compiler-interception regressions, so GCC is
required for the full suite even when using prebuilt Headroom executables.
If Windows TEMP/TMP uses an8.3 alias such as `RUNNER~1`, point those variables
in the test process to an existing canonical local directory first. CI does
this for its fixtures. Do not disable alias-refusal checks to make tests pass.

```powershell
.\build.ps1
python tests/analyze.py
python tests/verify.py
python tools/test_package_local.py
python tests/fleet_demo.py
```

Tests must use bounded newly owned fixtures, never real host OOM, real keys,
paid inference or unrelated-process termination. Include a regression for
behavior changes. Evidence is source/binary-bound: do not copy old success
reports onto new code or relabel synthetic results as real-agent productivity.
The standard workflow does not run the optional installed-agent replay.

Keep private raw state/logs, usernames, absolute local paths, tokens and
credentials out of commits and issues. Provide a minimal synthetic reproducer,
expected/actual outcome, platform/compiler version and relevant sanitized
evidence. Do not upload private histories or real application output.

AI assistance is permitted; contributors remain responsible for understanding,
testing, attribution and honest limitations. Existing projects are credited in
PRIOR_ART.md; do not disguise copied implementations as new inventions.
