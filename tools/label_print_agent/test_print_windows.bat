@echo off
chcp 65001 >nul
cd /d "%~dp0"
set /p PHONE=Phone for test label: 
set /p NAME=Name for test label: 
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0label_print_agent.ps1" -TestPhone "%PHONE%" -TestName "%NAME%"
pause
