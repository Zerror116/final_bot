# FinalBot label print agent for the admin PC (Windows + Microsoft Word).
# Every few seconds asks the bot for a label job, fills label_template.docx
# ({PHONE} and {NAME}) and prints it through Word on the default printer
# (or on "printer" from the config). Runs hidden, started by Task Scheduler at logon.
param(
    [string]$TestPhone = "",
    [string]$TestName = ""
)
$ErrorActionPreference = "Stop"
$testMode = -not [string]::IsNullOrWhiteSpace($TestPhone)

if (-not $testMode) {
    $mutex = New-Object -TypeName System.Threading.Mutex -ArgumentList $false, "Local\FinalBotLabelAgent"
    if (-not $mutex.WaitOne(0, $false)) {
        exit 0
    }
}

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $baseDir "label_print_agent.config.json"
$templatePath = Join-Path $baseDir "label_template.docx"
$logPath = Join-Path $baseDir "label_print_agent.log"

$defaultConfig = [ordered]@{
    api_url = "http://192.168.0.238:8093"
    token = "put-token-here"
    printer = ""
    poll_seconds = 3
}

function Write-AgentLog($message) {
    try {
        if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 1MB)) {
            Move-Item -Path $logPath -Destination "$logPath.old" -Force
        }
        $stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
        Add-Content -Path $logPath -Value "$stamp $message" -Encoding UTF8
    } catch {
    }
}

function Read-AgentConfig {
    if (-not (Test-Path $configPath)) {
        ([pscustomobject]$defaultConfig) | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
    }
    $loaded = (Get-Content -Path $configPath -Raw -Encoding UTF8) | ConvertFrom-Json
    foreach ($name in $defaultConfig.Keys) {
        if (-not ($loaded.PSObject.Properties.Name -contains $name)) {
            $loaded | Add-Member -NotePropertyName $name -NotePropertyValue $defaultConfig[$name]
        }
    }
    return $loaded
}

function Invoke-BotApi($config, $path, $body) {
    $headers = @{ Authorization = "Bearer $($config.token)" }
    $json = if ($null -eq $body) { "{}" } else { $body | ConvertTo-Json -Compress }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $url = ($config.api_url.TrimEnd("/")) + $path
    return Invoke-RestMethod -Method Post -Uri $url -Headers $headers -Body $bytes `
        -ContentType "application/json; charset=utf-8" -TimeoutSec 20
}

$script:word = $null

function Get-Word {
    if ($null -ne $script:word) {
        try {
            $null = $script:word.Version
            return $script:word
        } catch {
            $script:word = $null
        }
    }
    $script:word = New-Object -ComObject Word.Application
    $script:word.Visible = $false
    $script:word.DisplayAlerts = 0
    # Print synchronously, so the document is not closed before it reaches the printer.
    $script:word.Options.PrintBackground = $false
    return $script:word
}

function Close-Word {
    if ($null -ne $script:word) {
        try { $script:word.Quit(0) } catch { }
        try { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($script:word) } catch { }
        $script:word = $null
    }
}

function Set-Placeholder($document, $placeholder, $value) {
    $range = $document.Content
    # Execute(FindText, MatchCase, WholeWord, Wildcards, SoundsLike, AllForms, Forward,
    #         Wrap=wdFindStop, Format, ReplaceWith, Replace=wdReplaceAll)
    $null = $range.Find.Execute($placeholder, $true, $false, $false, $false, $false, $true, 0, $false, $value, 2)
}

function Print-Label($config, $phone, $name) {
    if (-not (Test-Path $templatePath)) {
        throw "template not found: $templatePath"
    }
    $word = Get-Word
    if (-not [string]::IsNullOrWhiteSpace($config.printer)) {
        $word.ActivePrinter = $config.printer
    }
    $document = $null
    try {
        # Open read-only: the template file itself is never changed.
        $document = $word.Documents.Open($templatePath, $false, $true, $false)
        Set-Placeholder $document "{PHONE}" $phone
        Set-Placeholder $document "{NAME}" $name
        $document.PrintOut()
    } finally {
        if ($null -ne $document) {
            try { $document.Close(0) } catch { }
            try { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($document) } catch { }
        }
    }
}

if ($testMode) {
    # Test print without the bot: label_print_agent.ps1 -TestPhone 89991234567 -TestName Test
    try {
        Print-Label (Read-AgentConfig) $TestPhone $TestName
        Write-AgentLog "test label printed"
        Write-Host "Test label sent to the printer."
    } finally {
        Close-Word
    }
    exit 0
}

Write-AgentLog "agent started"
$lastConfigError = ""
while ($true) {
    $config = $null
    try {
        $config = Read-AgentConfig
        $lastConfigError = ""
    } catch {
        if ($lastConfigError -ne $_.Exception.Message) {
            $lastConfigError = $_.Exception.Message
            Write-AgentLog "config error: $lastConfigError"
        }
        Start-Sleep -Seconds 10
        continue
    }

    $pollSeconds = [Math]::Max([int]$config.poll_seconds, 1)
    try {
        $response = Invoke-BotApi $config "/api/labels/next" $null
        $job = $response.job
        if ($null -ne $job) {
            try {
                Print-Label $config ([string]$job.phone) ([string]$job.name)
                $null = Invoke-BotApi $config "/api/labels/$($job.id)/result" @{ ok = $true }
                Write-AgentLog "printed job $($job.id)"
            } catch {
                $message = $_.Exception.Message
                Write-AgentLog "print failed job $($job.id): $message"
                Close-Word
                try {
                    $null = Invoke-BotApi $config "/api/labels/$($job.id)/result" @{ ok = $false; error = $message }
                } catch {
                }
            }
            # A job was handled: check the queue again right away.
            continue
        }
    } catch {
        Write-AgentLog "api error: $($_.Exception.Message)"
        Start-Sleep -Seconds ([Math]::Max($pollSeconds, 10))
        continue
    }
    Start-Sleep -Seconds $pollSeconds
}
