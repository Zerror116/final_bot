$ErrorActionPreference = "Stop"

$sourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$targetDir = Join-Path $env:APPDATA "FinalBotButtonAgent"
$taskName = "FinalBotButtonAgent"
$taskPath = "\FinalBot\"

try {
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
Copy-Item (Join-Path $sourceDir "windows_button_agent.ps1") $targetDir -Force

$targetConfigPath = Join-Path $targetDir "button_restart_agent.config.json"
if (-not (Test-Path $targetConfigPath)) {
    $sourceConfigPath = Join-Path $sourceDir "button_restart_agent.config.json"
    if (-not (Test-Path $sourceConfigPath)) {
        $sourceConfigPath = Join-Path $sourceDir "button_restart_agent.config.example.json"
    }
    Copy-Item $sourceConfigPath $targetConfigPath -Force
}

$agentPath = Join-Path $targetDir "windows_button_agent.ps1"

$legacyStartup = Join-Path ([Environment]::GetFolderPath("Startup")) "final_bot_button_agent.vbs"
Remove-Item $legacyStartup -Force -ErrorAction SilentlyContinue

Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -like "*button_restart_agent.py*" -or
        $_.CommandLine -like "*windows_button_agent.ps1*"
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

$quotedAgentPath = '"' + $agentPath + '"'
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $quotedAgentPath"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel LeastPrivilege

Register-ScheduledTask `
    -TaskName $taskName `
    -TaskPath $taskPath `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
Start-Sleep -Seconds 1

$process = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*windows_button_agent.ps1*" } |
    Select-Object -First 1

if (-not $process) {
    Start-Process "powershell.exe" -ArgumentList $arguments -WindowStyle Hidden
}
} catch {
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    Add-Content -Path (Join-Path $targetDir "install_error.log") -Value "$(Get-Date -Format s) $($_.Exception.Message)"
    throw
}
