@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-usb-config.ps1"
if errorlevel 1 pause
