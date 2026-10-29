@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-usb-autostart.ps1"
if errorlevel 1 pause
