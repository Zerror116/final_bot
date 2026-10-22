@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1"
if errorlevel 1 (
    echo Install failed. Open this log:
    echo %APPDATA%\FinalBotButtonAgent\install_error.log
    pause
    exit /b 1
)
echo Installed and started.
timeout /t 3 >nul
exit /b 0
