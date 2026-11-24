# Installs the label print agent for the current Windows user:
# copies files to %APPDATA%\FinalBotLabelAgent and starts it hidden at every logon.
$ErrorActionPreference = "Stop"

$sourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$targetDir = Join-Path $env:APPDATA "FinalBotLabelAgent"
$taskName = "FinalBotLabelAgent"
$taskPath = "\FinalBot\"

try {
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    Copy-Item (Join-Path $sourceDir "label_print_agent.ps1") $targetDir -Force
    Copy-Item (Join-Path $sourceDir "label_template.docx") $targetDir -Force

    $targetConfigPath = Join-Path $targetDir "label_print_agent.config.json"
    $sourceConfigPath = Join-Path $sourceDir "label_print_agent.config.json"
    if (Test-Path $sourceConfigPath) {
        Copy-Item $sourceConfigPath $targetConfigPath -Force
    } elseif (-not (Test-Path $targetConfigPath)) {
        Copy-Item (Join-Path $sourceDir "label_print_agent.config.example.json") $targetConfigPath -Force
    }

    Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like "*label_print_agent.ps1*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

    $agentPath = Join-Path $targetDir "label_print_agent.ps1"
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$agentPath`""

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    # Word can only be driven from the logged-on user's session.
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $taskName `
        -TaskPath $taskPath `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null

    Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    Write-Host "Label print agent installed to $targetDir and started."
    Write-Host "Config: $targetConfigPath"
} catch {
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    Add-Content -Path (Join-Path $targetDir "install_error.log") -Value "$(Get-Date -Format s) $($_.Exception.Message)"
    throw
}
