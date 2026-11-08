# Invoked by the desktop Explorer broker; requests contain paths and safe debug flags only.
param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference='Stop'
$launchDirectory=[IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $PSScriptRoot) '.runtime\process-launch'))
$Request=[IO.Path]::GetFullPath($Request)
if([IO.Path]::GetDirectoryName($Request) -ne $launchDirectory -or
    [IO.Path]::GetFileName($Request) -notmatch '^request-([a-f0-9]{32})\.json$'){
    throw 'Invalid process launch request path.'
}
$nonce=$Matches[1]
$resultPath=Join-Path $launchDirectory ('result-'+$nonce+'.json')
$result=@{nonce=$nonce;ok=$false;independent=$false;error=''}
$process=$null
try {
    $payload=[IO.File]::ReadAllText($Request,[Text.Encoding]::UTF8)|ConvertFrom-Json
    $age=[TimeSpan]::FromTicks([DateTime]::UtcNow.Ticks-[long]$payload.created_ticks).TotalSeconds
    if($payload.nonce -ne $nonce -or $age -lt -5 -or $age -gt 20){throw 'Expired or mismatched process launch request.'}
    Add-Type -Path (Join-Path $PSScriptRoot 'independent_process.cs')
    $result.bootstrap_pid=$PID
    $result.parent_pid=(Get-CimInstance Win32_Process -Filter ('ProcessId = '+$PID)).ParentProcessId
    $result.job_flags=[WeChatIndependentProcess]::CurrentJobFlags()
    $result.caller_in_bootstrap_job=[WeChatIndependentProcess]::CurrentJobContains([int]$payload.caller_pid)
    # Explorer may itself use a shared job that allows breakaway without killing children.
    # Reject a kill-on-close host; the native launcher also enforces this lifetime policy.
    if([WeChatIndependentProcess]::IsCurrentProcessInJob() -and ($result.job_flags -band 0x2000)){
        throw 'Explorer launch still belongs to a kill-on-close host job.'
    }
    if($payload.encoding -eq 'utf-8'){$env:PYTHONIOENCODING='utf-8'}
    $env:BRIDGE_DEBUG=if($payload.debug_log){'1'}else{''}
    $result.program_path=[string]$payload.executable
    $result.program_exists=[IO.File]::Exists([string]$payload.executable)
    $process=[WeChatIndependentProcess]::Start([string]$payload.executable,[string]$payload.arguments,[string]$payload.directory)
    $result.pid=$process.Id;$result.ok=$true;$result.independent=$true
} catch {$result.error=$_.Exception.Message} finally {
    $temporaryResult=$resultPath+'.tmp'
    [IO.File]::WriteAllText($temporaryResult,($result|ConvertTo-Json -Compress),(New-Object Text.UTF8Encoding($false)))
    [IO.File]::Move($temporaryResult,$resultPath)
    if($process){$process.Dispose()}
}
