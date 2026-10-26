$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
New-Item -ItemType Directory -Path (Join-Path $projectDirectory 'build') -Force | Out-Null
& gcc -std=c11 -O2 -Wall -Wextra -Werror -municode (Join-Path $projectDirectory 'src\guard.c') (Join-Path $projectDirectory 'src\support.c') -o (Join-Path $projectDirectory 'build\headroom.exe') -lole32 -lpsapi
if ($LASTEXITCODE -ne 0) { throw 'Native build failed' }
& gcc -std=c11 -O2 -Wall -Wextra -Werror -municode (Join-Path $projectDirectory 'src\fleet.c') (Join-Path $projectDirectory 'src\policy.c') (Join-Path $projectDirectory 'src\support.c') (Join-Path $projectDirectory 'src\output.c') (Join-Path $projectDirectory 'src\intake.c') -o (Join-Path $projectDirectory 'build\headroom-fleet.exe') -lole32 -lpsapi
if ($LASTEXITCODE -ne 0) { throw 'Fleet build failed' }
& gcc -std=c11 -O2 -Wall -Wextra -Werror -shared (Join-Path $projectDirectory 'src\policy.c') -o (Join-Path $projectDirectory 'build\headroom-policy.dll')
if ($LASTEXITCODE -ne 0) { throw 'Policy build failed' }
