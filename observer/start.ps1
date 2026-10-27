[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][DateTimeOffset]$Until,
    [string]$OutputParent = (Join-Path $env:LOCALAPPDATA 'Headroom\observations'),
    [ValidateRange(200,60000)][int]$IntervalMs = 5000,
    [ValidateRange(1000,3600000)][int]$MetadataMs = 60000
)
$ErrorActionPreference = 'Stop'
$observerBinary = Join-Path $PSScriptRoot 'build\claude-observer.exe'
if (-not (Test-Path -LiteralPath $observerBinary -PathType Leaf)) { throw 'Run observer/build.ps1 first.' }
$observerPython = (Get-Command python -ErrorAction Stop).Source
$deadlineMilliseconds = $Until.ToUnixTimeMilliseconds()
$remainingMilliseconds = $deadlineMilliseconds - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
if ($remainingMilliseconds -le 0 -or $remainingMilliseconds -gt 86400000) { throw 'Deadline must be in the next24 hours.' }
$OutputParent = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputParent)
if ($OutputParent -notmatch '^[A-Za-z]:\\') { throw 'Observation output must be on a local drive.' }
$observerDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($OutputParent))
if ($observerDrive.DriveType -notin @('Fixed','Removable','Ram')) { throw 'Observation output must be local.' }
# Check existing ancestors before creating anything under a user-supplied path.
$observerCheck = [IO.DirectoryInfo]::new($OutputParent)
while ($null -ne $observerCheck) {
    if ($observerCheck.Exists -and ($observerCheck.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked output directories are unsupported.' }
    if (Test-Path -LiteralPath (Join-Path $observerCheck.FullName '.git')) { throw 'Observation data must stay outside Git.' }
    $observerCheck = $observerCheck.Parent
}
New-Item -ItemType Directory -Path $OutputParent -Force | Out-Null
$observerDirectory = Join-Path $OutputParent ([DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $observerDirectory | Out-Null
# Local app data only. Restrict this run to this user and SYSTEM; never change
# ACLs on Claude's directories, user profiles, or any pre-existing data folder.
$observerAcl = [Security.AccessControl.DirectorySecurity]::new()
$observerAcl.SetAccessRuleProtection($true, $false)
$observerCurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$observerAcl.SetOwner($observerCurrentSid)
foreach ($observerSid in @($observerCurrentSid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
    $observerRule = [Security.AccessControl.FileSystemAccessRule]::new($observerSid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    [void]$observerAcl.AddAccessRule($observerRule)
}
Set-Acl -LiteralPath $observerDirectory -AclObject $observerAcl
$observerRuntime = Join-Path $observerDirectory 'runtime'
New-Item -ItemType Directory -Path $observerRuntime | Out-Null
# Freeze this finite run so future source edits cannot change its reporter.
$observerFrozenBinary = Join-Path $observerRuntime 'claude-observer.exe'
$observerFrozenReport = Join-Path $observerRuntime 'report.py'
Copy-Item -LiteralPath $observerBinary -Destination $observerFrozenBinary
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'report.py') -Destination $observerFrozenReport
$observerArguments = @('--output', $observerDirectory, '--deadline-ms', [string]$deadlineMilliseconds,
    '--python', $observerPython, '--report', $observerFrozenReport,
    '--history', (Join-Path $env:USERPROFILE '.claude\history.jsonl'),
    '--projects', (Join-Path $env:USERPROFILE '.claude\projects'),
    '--interval-ms', [string]$IntervalMs, '--metadata-ms', [string]$MetadataMs)
# These are fixed option names and filesystem paths, never shell code. Windows
# forbids quotes in these paths, and no argument may end with a backslash.
foreach ($observerArgument in $observerArguments) {
    if ($observerArgument.Contains('"') -or $observerArgument.Contains("`n") -or $observerArgument.EndsWith('\')) { throw 'Unsafe observer argument.' }
}
$observerCommandLine = ($observerArguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
$observerProcess = Start-Process -FilePath $observerFrozenBinary -ArgumentList $observerCommandLine -WorkingDirectory $observerDirectory -WindowStyle Hidden -PassThru
$readyDeadline = [DateTimeOffset]::UtcNow.AddSeconds(8)
while (-not (Test-Path -LiteralPath (Join-Path $observerDirectory 'samples.jsonl'))) {
    if ($observerProcess.HasExited -or [DateTimeOffset]::UtcNow -ge $readyDeadline) { throw 'Observer did not become ready; inspect the new private run folder.' }
    Start-Sleep -Milliseconds 100
}
$observerManifest = @{version=2; observer_pid=$observerProcess.Id; observer_started_utc=$observerProcess.StartTime.ToUniversalTime().ToString('o');
    deadline_utc=$Until.ToUniversalTime().ToString('o'); interval_ms=$IntervalMs;
    output=$observerDirectory; checkout_commit=(& git -C $PSScriptRoot rev-parse HEAD);
    collector_source_sha256=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'collector.c') -Algorithm SHA256).Hash;
    collector_binary_sha256=(Get-FileHash -LiteralPath $observerFrozenBinary -Algorithm SHA256).Hash;
    reporter_sha256=(Get-FileHash -LiteralPath $observerFrozenReport -Algorithm SHA256).Hash} | ConvertTo-Json
$observerManifestHandle = [IO.File]::Open((Join-Path $observerDirectory 'observer.json'), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
try {
    $observerManifestBytes = [Text.UTF8Encoding]::new($false).GetBytes($observerManifest)
    $observerManifestHandle.Write($observerManifestBytes, 0, $observerManifestBytes.Length)
    $observerManifestHandle.Flush($true)
} finally { $observerManifestHandle.Dispose() }
[PSCustomObject]@{ObserverPID=$observerProcess.Id; Until=$Until.ToString('o'); Output=$observerDirectory; ReadOnly=$true; PaidCalls=0}
