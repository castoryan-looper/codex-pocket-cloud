$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$pythonExe = Join-Path $PSScriptRoot '.usb-config-venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) {
    py -3 -m venv .usb-config-venv
    if ($LASTEXITCODE -ne 0) { throw '请先安装 Python 3，并重新打开 USB 配置工具。' }
    & $pythonExe -m pip install 'pyserial==3.5'
    if ($LASTEXITCODE -ne 0) { throw 'USB 配置依赖安装失败，请检查电脑网络后重试。' }
}
& $pythonExe tools/usb_config.py --open
