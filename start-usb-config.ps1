$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$pythonExe = Join-Path $PSScriptRoot '.usb-config-venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) {
    py -3 -m venv .usb-config-venv
    if ($LASTEXITCODE -ne 0) { throw '���Ȱ�װ Python 3�������´� USB ���ù��ߡ�' }
    & $pythonExe -m pip install 'pyserial==3.5'
    if ($LASTEXITCODE -ne 0) { throw 'USB ����������װʧ�ܣ����������������ԡ�' }
}
& $pythonExe tools/usb_config.py --open
