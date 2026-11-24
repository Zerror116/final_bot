@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1"
if errorlevel 1 (
    echo Install failed. Log: %APPDATA%\FinalBotLabelAgent\install_error.log
    pause
    exit /b 1
)
echo Installed and started.
timeout /t 5 >nul
exit /b 0
