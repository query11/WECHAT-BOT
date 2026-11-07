param([switch]$Show)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $projectDir 'process_launcher.ps1')
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
