param([switch]$Show, [switch]$SkipSetup)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $projectDir 'process_launcher.ps1')
# First run: until the setup guide is completed, open it instead of the tray when WeChat
# is not logged in or nobody is bound yet. Existing installs with both never see it.
if (-not $SkipSetup -and -not (Test-Path -LiteralPath (Join-Path $projectDir '.runtime\onboarding.json'))) {
    $needsSetup = $true
    $configPath = Join-Path $projectDir 'config.json'
    if (Test-Path -LiteralPath $configPath) {
        try {
            $cfg = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $users = @($cfg.allowed_user_ids | Where-Object { "$_".Trim() })
            if ($cfg.bot_token -and $users.Count -gt 0) { $needsSetup = $false }
        } catch { $needsSetup = $true }
    }
    if ($needsSetup) {
        $wizardArgs = '-NoProfile -STA -ExecutionPolicy Bypass -File "' + (Join-Path $projectDir 'setup_wizard.ps1') + '"'
        $wizard = Start-WeChatIndependentProcess (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') $wizardArgs $projectDir
        $wizard.Dispose()
        exit
    }
}
# Start UI independently: configuration or connection failures remain visible.
$arguments = '-NoProfile -STA -ExecutionPolicy Bypass -File "' + (Join-Path $projectDir 'tray_app.ps1') + '"'
if ($Show) { $arguments += ' -Show' }
$ui = Start-WeChatIndependentProcess (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') $arguments $projectDir
$ui.Dispose()
try {
    & (Join-Path $projectDir 'toggle.ps1') -On
} catch {
    $runtimeDir = Join-Path $projectDir '.runtime'
    [IO.Directory]::CreateDirectory($runtimeDir) | Out-Null
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $failure = @{pid=0;heartbeat=$now;started=$now;state='error';message='Backend failed to start';last_success=0;task='';queued=0;downloads=0;error='Check configuration and bridge.log';events=@('Backend startup failed')} | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $runtimeDir 'status.json'),$failure,(New-Object Text.UTF8Encoding($false)))
    throw
}
