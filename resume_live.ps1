[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Settings,
    [Parameter(Mandatory = $true)][string]$FromState,
    [Parameter(Mandatory = $true)][string]$State
)
$ErrorActionPreference = 'Stop'
$nativeExecutable = Join-Path $PSScriptRoot 'build\headroom-fleet.exe'
if (-not (Test-Path -LiteralPath $nativeExecutable -PathType Leaf)) {
    throw 'Build the native coordinator with build.ps1 first.'
}
function Resolve-RecoveryFileSystemPath([string]$InputPath) {
    $provider = $null
    $drive = $null
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
        $InputPath, [ref]$provider, [ref]$drive)
    if ($provider.Name -ne 'FileSystem') {
        throw 'Recovery paths must use the FileSystem provider.'
    }
    return $resolved
}
# PowerShell location can differ from the process CWD used by .NET file APIs.
# Bind every argument to the same absolute filesystem paths before reservation.
try {
    $Settings = Resolve-RecoveryFileSystemPath $Settings
    $FromState = Resolve-RecoveryFileSystemPath $FromState
    $newState = Resolve-RecoveryFileSystemPath $State
}
catch {
    Write-Error $_ -ErrorAction Continue
    exit 73
}
if ((Test-Path -LiteralPath $newState) -or -not
    (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($newState)) -PathType Container)) {
    Write-Error 'Recovery needs a new state directory under an existing parent.' -ErrorAction Continue
    exit 73
}
$compiledPlan = $newState + '.recovery.hrp'
$reportLines = & python (Join-Path $PSScriptRoot 'recover_live.py') $Settings $FromState $compiledPlan
$preparationResult = $LASTEXITCODE
$reportText = $reportLines -join [Environment]::NewLine
Write-Output $reportText
if ($preparationResult -ne 0) { exit $preparationResult }
$report = $reportText | ConvertFrom-Json
if ($report.status -eq 'no_work') { exit 0 }
if ($report.status -ne 'prepared' -or $report.plan_json -ne ($compiledPlan + '.json')) { exit 73 }

try {
    if ([string]::IsNullOrWhiteSpace($report.working_directory)) {
        throw 'Recovery report has no validated working directory.'
    }
    $workingDirectory = Resolve-RecoveryFileSystemPath $report.working_directory
    if (-not (Test-Path -LiteralPath $workingDirectory -PathType Container)) {
        throw 'The original working directory is unavailable.'
    }
    Push-Location -LiteralPath $workingDirectory
}
catch {
    Write-Error $_ -ErrorAction Continue
    exit 73
}
try {
    $hashAlgorithm = [Security.Cryptography.SHA256]::Create()
    $planStream = $null
    try {
        $planStream = [IO.File]::OpenRead($compiledPlan)
        $planHash = [BitConverter]::ToString($hashAlgorithm.ComputeHash($planStream)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        if ($null -ne $planStream) { $planStream.Dispose() }
        $hashAlgorithm.Dispose()
    }
    $receiptPath = Join-Path $FromState 'recovery-launch-intent.json'
    $receipt = @{version = 1; run_id = $report.run_id; new_state = $newState;
        prepared_plan = $compiledPlan; plan_json = $report.plan_json; plan_sha256 = $planHash;
        working_directory = $workingDirectory} |
        ConvertTo-Json -Compress
    $stream = $null
    try {
        $stream = [IO.File]::Open($receiptPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $bytes = [Text.Encoding]::UTF8.GetBytes($receipt + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    catch {
        Write-Error 'Recovery launch could not be reserved. Retain evidence for manual review.' -ErrorAction Continue
        exit 73
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    # Recovery finishes accepted saved work as a finite batch; it does not reopen intake.
    & $nativeExecutable --plan $compiledPlan --state $newState
    $coordinatorResult = $LASTEXITCODE
    if (Test-Path -LiteralPath (Join-Path $newState 'fleet.jsonl') -PathType Leaf) {
        & python (Join-Path $PSScriptRoot 'headroom.py') status $newState
    }
    exit $coordinatorResult
}
finally {
    Pop-Location
}
