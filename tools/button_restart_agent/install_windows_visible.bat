@echo off
cd /d "%~dp0"
echo Installing FinalBot button agent...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1"
echo.
call "%~dp0check_windows_status.bat"
