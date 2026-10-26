[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Plan,
    [Parameter(Mandatory = $true)][string]$State
)
$ErrorActionPreference = 'Stop'
$nativeExecutable = Join-Path $PSScriptRoot 'build\headroom-fleet.exe'
if (-not (Test-Path -LiteralPath $nativeExecutable -PathType Leaf)) {
    throw 'Build the native coordinator with build.ps1 first.'
}
$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('headroom-plan-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$compiledPlan = Join-Path $temporaryDirectory 'input.hrp'
try {
    & python (Join-Path $PSScriptRoot 'headroom.py') compile $Plan $compiledPlan
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    # The offline compiler has exited before the native coordinator starts.
    & $nativeExecutable --plan $compiledPlan --state $State
    $coordinatorResult = $LASTEXITCODE
    if (Test-Path -LiteralPath (Join-Path $State 'fleet.jsonl') -PathType Leaf) {
        & python (Join-Path $PSScriptRoot 'headroom.py') status $State
    }
    exit $coordinatorResult
}
finally {
    # This directory was just created by this invocation. No recursive cleanup.
    if (Test-Path -LiteralPath $compiledPlan -PathType Leaf) { Remove-Item -LiteralPath $compiledPlan }
    Remove-Item -LiteralPath $temporaryDirectory
}
