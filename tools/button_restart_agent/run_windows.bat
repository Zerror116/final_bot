@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0windows_button_agent.ps1" >nul 2>nul
exit /b
