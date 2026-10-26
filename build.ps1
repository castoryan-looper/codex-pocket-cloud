param([string]$ArduinoCli='arduino-cli')
$ErrorActionPreference='Stop'
& $ArduinoCli compile --fqbn 'esp32:esp32:esp32c6:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB' --libraries (Join-Path $PSScriptRoot 'firmware/libraries') --build-path (Join-Path $PSScriptRoot 'build') (Join-Path $PSScriptRoot 'firmware/Pocket')
if ($LASTEXITCODE -ne 0) { throw 'Firmware compilation failed' }
