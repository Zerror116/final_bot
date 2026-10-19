@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "$config=Join-Path (Join-Path $env:APPDATA 'FinalBotButtonAgent') 'button_restart_agent.config.json'; if(Test-Path $config){$json=Get-Content $config -Raw | ConvertFrom-Json; $json.key=''; $json | ConvertTo-Json | Set-Content $config -Encoding UTF8}; Start-ScheduledTask -TaskName 'FinalBotButtonAgent' -TaskPath '\FinalBot\' -ErrorAction SilentlyContinue"
exit /b
