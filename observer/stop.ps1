[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Data)
$ErrorActionPreference = 'Stop'
$observerData = (Resolve-Path -LiteralPath $Data).ProviderPath
if (-not (Test-Path -LiteralPath (Join-Path $observerData 'observer.json')) -or
    -not (Test-Path -LiteralPath (Join-Path $observerData 'samples.jsonl'))) { throw 'Not an observer run directory.' }
# Cooperative request to this observer only. Never kill Claude or search by PID.
$stopFile = Join-Path $observerData 'stop.request'
$stopStream = [IO.File]::Open($stopFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
try { $stopStream.Flush($true) } finally { $stopStream.Dispose() }
Write-Output 'Stop requested. The observer will stop sampling and generate its local report.'
