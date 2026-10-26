param([string]$Port='COM6',[string]$Python='python')
$ErrorActionPreference='Stop'
$image=Join-Path $PSScriptRoot 'build/Pocket.ino.merged.bin'
if (-not (Test-Path -LiteralPath $image)) { throw 'Build the firmware first with build.ps1' }
& $Python -m esptool --chip esp32c6 --port $Port --baud 921600 write-flash 0 $image
if ($LASTEXITCODE -ne 0) { throw 'Firmware flash failed' }
