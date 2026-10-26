[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Settings,
    [Parameter(Mandatory = $true)][string]$State
)
$ErrorActionPreference = 'Stop'
$nativeExecutable = Join-Path $PSScriptRoot 'build\headroom-fleet.exe'
if (-not (Test-Path -LiteralPath $nativeExecutable -PathType Leaf)) {
    throw 'Build the native coordinator with build.ps1 first.'
}
$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('headroom-live-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$compiledSettings = Join-Path $temporaryDirectory 'session.hrs'
try {
    & python (Join-Path $PSScriptRoot 'headroom_live.py') init $Settings $compiledSettings
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    # This is the explicit foreground owner; no service or global setting is installed.
    & $nativeExecutable --live-plan $compiledSettings --state $State
    $coordinatorResult = $LASTEXITCODE
    if (Test-Path -LiteralPath (Join-Path $State 'fleet.jsonl') -PathType Leaf) {
        & python (Join-Path $PSScriptRoot 'headroom.py') status $State
    }
    exit $coordinatorResult
}
finally {
    if (Test-Path -LiteralPath $compiledSettings -PathType Leaf) { Remove-Item -LiteralPath $compiledSettings }
    Remove-Item -LiteralPath $temporaryDirectory
}
