$ErrorActionPreference = 'Stop'
$observerOutput = Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Path $observerOutput -Force | Out-Null
$observerCompiler = Get-Command gcc -ErrorAction SilentlyContinue
$observerCompilerPath = if ($observerCompiler) { $observerCompiler.Source } else { 'C:\msys64\ucrt64\bin\gcc.exe' }
& $observerCompilerPath -std=c11 -O2 -Wall -Wextra -Werror -municode (Join-Path $PSScriptRoot 'collector.c') -o (Join-Path $observerOutput 'claude-observer.exe') -lpsapi -luser32 -ladvapi32
if ($LASTEXITCODE -ne 0) { throw 'Read-only observer build failed' }
