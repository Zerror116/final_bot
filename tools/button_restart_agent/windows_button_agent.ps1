$ErrorActionPreference = "Stop"

$mutex = New-Object -TypeName System.Threading.Mutex -ArgumentList $false, "Local\FinalBotButtonAgent"
if (-not $mutex.WaitOne(0, $false)) {
    exit 0
}

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $baseDir "button_restart_agent.config.json"
$logPath = Join-Path $baseDir "button_restart_agent.log"

$defaultConfig = [ordered]@{
    restart_url = "http://109.196.100.116:8092/api/restart"
    token = "put-token-here"
    key = ""
    cooldown_seconds = 30
    bind_and_restart_on_first_press = $true
}

function Write-AgentLog($message) {
    try {
        $stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
        Add-Content -Path $logPath -Value "$stamp $message" -Encoding UTF8
    } catch {
    }
}

function Get-Win32LastErrorText {
    $code = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    if ($code -eq 0) {
        return "Win32 error 0"
    }

    try {
        $message = (New-Object ComponentModel.Win32Exception($code)).Message
        return "Win32 error ${code}: $message"
    } catch {
        return "Win32 error $code"
    }
}

function Save-AgentConfig($config) {
    try {
        $config | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
    } catch {
        Write-AgentLog "config save failed"
    }
}

function New-DefaultConfig {
    return [pscustomobject]$defaultConfig
}

function Read-AgentConfig {
    try {
        if (-not (Test-Path $configPath)) {
            $created = New-DefaultConfig
            Save-AgentConfig $created
            return $created
        }

        $raw = Get-Content -Path $configPath -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return New-DefaultConfig
        }

        $loaded = $raw | ConvertFrom-Json
        foreach ($name in $defaultConfig.Keys) {
            if (-not ($loaded.PSObject.Properties.Name -contains $name)) {
                $loaded | Add-Member -NotePropertyName $name -NotePropertyValue $defaultConfig[$name]
            }
        }
        return $loaded
    } catch {
        Write-AgentLog "config read failed"
        return New-DefaultConfig
    }
}

$script:lastRestartAt = [datetime]::MinValue

function Invoke-FinalBotRestart($config) {
    $cooldownSeconds = 30
    try {
        $cooldownSeconds = [int]$config.cooldown_seconds
    } catch {
    }

    if (((Get-Date) - $script:lastRestartAt).TotalSeconds -lt $cooldownSeconds) {
        Write-AgentLog "restart skipped: cooldown"
        return
    }

    $token = [string]$config.token
    if ([string]::IsNullOrWhiteSpace($token) -or $token -eq "put-token-here") {
        Write-AgentLog "restart skipped: missing token"
        return
    }

    try {
        $request = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create([string]$config.restart_url)
        $request.Method = "POST"
        $request.Timeout = 20000
        $request.Accept = "application/json"
        $request.Headers.Add("Authorization", "Bearer $token")

        $response = $request.GetResponse()
        $status = [int]$response.StatusCode
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $payload = $reader.ReadToEnd()
        $reader.Close()
        $response.Close()

        $script:lastRestartAt = Get-Date
        Write-AgentLog "restart response ${status}: $payload"
    } catch {
        Write-AgentLog "restart failed: $($_.Exception.Message)"
    }
}

if (-not ("KeyboardHook" -as [type])) {
    try {
        Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class KeyboardHook
{
    public delegate IntPtr LowLevelKeyboardProc(int nCode, IntPtr wParam, IntPtr lParam);

    public const int WH_KEYBOARD_LL = 13;
    public const int WM_KEYDOWN = 0x0100;
    public const int WM_KEYUP = 0x0101;
    public const int WM_SYSKEYDOWN = 0x0104;
    public const int WM_SYSKEYUP = 0x0105;

    [StructLayout(LayoutKind.Sequential)]
    public struct KBDLLHOOKSTRUCT
    {
        public int vkCode;
        public int scanCode;
        public int flags;
        public int time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT
    {
        public int x;
        public int y;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct MSG
    {
        public IntPtr hwnd;
        public uint message;
        public IntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public POINT pt;
    }

    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr SetWindowsHookEx(int idHook, LowLevelKeyboardProc lpfn, IntPtr hMod, uint dwThreadId);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool UnhookWindowsHookEx(IntPtr hhk);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern IntPtr GetModuleHandle(string lpModuleName);

    [DllImport("user32.dll")]
    public static extern int GetMessage(out MSG lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

    [DllImport("user32.dll")]
    public static extern bool TranslateMessage(ref MSG lpMsg);

    [DllImport("user32.dll")]
    public static extern IntPtr DispatchMessage(ref MSG lpMsg);
}
"@
    } catch {
        Write-AgentLog "keyboard hook type compile failed: $($_.Exception.Message)"
        exit 1
    }
}

Write-AgentLog "agent started"
$pressedKeys = @{}

$script:hookCallback = [KeyboardHook+LowLevelKeyboardProc]{
    param([int]$nCode, [IntPtr]$wParam, [IntPtr]$lParam)

    if ($nCode -ge 0) {
        $message = $wParam.ToInt32()
        $isDown = (
            $message -eq [KeyboardHook]::WM_KEYDOWN -or
            $message -eq [KeyboardHook]::WM_SYSKEYDOWN
        )
        $isUp = (
            $message -eq [KeyboardHook]::WM_KEYUP -or
            $message -eq [KeyboardHook]::WM_SYSKEYUP
        )

        if ($isDown -or $isUp) {
            $info = [System.Runtime.InteropServices.Marshal]::PtrToStructure(
                $lParam,
                [type][KeyboardHook+KBDLLHOOKSTRUCT]
            )
            $signature = "vk:$($info.vkCode)"

            if ($isUp) {
                $script:pressedKeys.Remove($signature)
            }

            $config = Read-AgentConfig
            $configuredKey = [string]$config.key

            if ($isDown) {
                if ((-not $script:pressedKeys.ContainsKey($signature)) -or (-not $script:pressedKeys[$signature])) {
                    $script:pressedKeys[$signature] = $true

                    if ([string]::IsNullOrWhiteSpace($configuredKey)) {
                        $config.key = $signature
                        Save-AgentConfig $config
                        Write-AgentLog "button bound: $signature"

                        if ([bool]$config.bind_and_restart_on_first_press) {
                            Invoke-FinalBotRestart $config
                        }

                        return [IntPtr]1
                    }

                    if ($configuredKey -eq $signature) {
                        Invoke-FinalBotRestart $config
                        return [IntPtr]1
                    }
                } elseif ($configuredKey -eq $signature) {
                    return [IntPtr]1
                }
            }

            if ($configuredKey -eq $signature) {
                return [IntPtr]1
            }
        }
    }

    return [KeyboardHook]::CallNextHookEx([IntPtr]::Zero, $nCode, $wParam, $lParam)
}

$currentProcess = [System.Diagnostics.Process]::GetCurrentProcess()
$currentModule = $currentProcess.MainModule
$moduleHandle = [KeyboardHook]::GetModuleHandle($currentModule.ModuleName)
$hook = [KeyboardHook]::SetWindowsHookEx(
    [KeyboardHook]::WH_KEYBOARD_LL,
    $script:hookCallback,
    $moduleHandle,
    0
)

if ($hook -eq [IntPtr]::Zero) {
    Write-AgentLog "keyboard hook failed: $(Get-Win32LastErrorText)"
    exit 1
}

try {
    $message = New-Object KeyboardHook+MSG
    while ([KeyboardHook]::GetMessage([ref]$message, [IntPtr]::Zero, 0, 0) -ne 0) {
        [KeyboardHook]::TranslateMessage([ref]$message) | Out-Null
        [KeyboardHook]::DispatchMessage([ref]$message) | Out-Null
    }
} catch {
    Write-AgentLog "message loop failed: $($_.Exception.Message)"
    exit 1
} finally {
    try {
        [KeyboardHook]::UnhookWindowsHookEx($hook) | Out-Null
    } catch {
        Write-AgentLog "keyboard hook cleanup failed: $($_.Exception.Message)"
    }
    try {
        $mutex.ReleaseMutex()
    } catch {
    }
}
