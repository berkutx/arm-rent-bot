$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:LIVE = '0'
$env:PYTHONUTF8 = '1'
python scripts/seed_demo.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python server.py
