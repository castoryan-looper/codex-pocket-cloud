$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$pythonExe = Join-Path $PSScriptRoot '.usb-config-venv/Scripts/python.exe'
$pythonwExe = Join-Path $PSScriptRoot '.usb-config-venv/Scripts/pythonw.exe'
if (!(Test-Path -LiteralPath $pythonExe)) {
    py -3 -m venv .usb-config-venv
    if ($LASTEXITCODE -ne 0) { throw 'Please install Python 3 first.' }
}
& $pythonExe -m pip install 'pyserial==3.5'
if ($LASTEXITCODE -ne 0) { throw 'USB configuration dependency installation failed.' }
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Codex Pocket USB Config.lnk'
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwExe
$shortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'tools/usb_config.py') + '" --auto-open'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'Codex Pocket USB configuration auto-start'
$shortcut.Save()
Start-Process -FilePath $pythonwExe -ArgumentList $shortcut.Arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
Write-Host 'USB configuration auto-start enabled for the current Windows user.'
