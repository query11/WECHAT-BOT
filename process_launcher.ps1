# Dot-source this helper for persistent processes. Explorer owns launches from managed hosts.
if (-not ('WeChatIndependentProcess' -as [type])) {
    Add-Type -Path (Join-Path $PSScriptRoot 'independent_process.cs')
}

function Start-WeChatIndependentProcess([string]$Executable,[string]$Arguments,[string]$Directory) {
    if (-not [WeChatIndependentProcess]::IsCurrentProcessInJob() -or
        -not ([WeChatIndependentProcess]::CurrentJobFlags() -band 0x2000)) {
        return [WeChatIndependentProcess]::Start($Executable,$Arguments,$Directory)
    }
    $launcherRoot=$PSScriptRoot
    $requestDirectory=Join-Path $launcherRoot '.runtime\process-launch'
    [void][IO.Directory]::CreateDirectory($requestDirectory)
    $nonce=[Guid]::NewGuid().ToString('N')
    $requestPath=Join-Path $requestDirectory ('request-'+$nonce+'.json')
    $resultPath=Join-Path $requestDirectory ('result-'+$nonce+'.json')
    $request=@{nonce=$nonce;caller_pid=$PID;created_ticks=[DateTime]::UtcNow.Ticks;executable=$Executable;arguments=$Arguments;directory=$Directory;
        encoding=$env:PYTHONIOENCODING;debug_log=($env:BRIDGE_DEBUG -eq '1')}
    [IO.File]::WriteAllText($requestPath,($request|ConvertTo-Json -Compress),(New-Object Text.UTF8Encoding($false)))
    try {
        $bootstrapArgs='-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $launcherRoot 'process_bootstrap.ps1')+'" -Request "'+$requestPath+'"'
        $desktopLaunch={param($Program,$ProgramArguments,$WorkingDirectory)
            $shell=$null;$windows=$null;$desktop=$null;$application=$null
            try {
                $shell=New-Object -ComObject Shell.Application
                $windows=$shell.Windows();[int]$desktopHandle=0
                $desktop=$windows.FindWindowSW(0,0,8,[ref]$desktopHandle,1)
                if(-not $desktop){throw 'Windows Explorer desktop is unavailable; independent launch was not attempted.'}
                # This Application belongs to the existing desktop Explorer COM server.
                $application=$desktop.Document.Application
                [void]$application.ShellExecute($Program,$ProgramArguments,$WorkingDirectory,'open',0)
            } finally {
                foreach($comObject in @($application,$desktop,$windows,$shell)){
                    if($comObject -and [Runtime.InteropServices.Marshal]::IsComObject($comObject)){
                        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($comObject)
                    }
                }
            }
        }
        $program=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        if([Threading.Thread]::CurrentThread.ApartmentState -eq 'STA'){
            & $desktopLaunch $program $bootstrapArgs $launcherRoot
        }else{
            # Shell automation requires STA, including callers from PowerShell 7 command tools.
            $runspace=[RunspaceFactory]::CreateRunspace();$invoker=[PowerShell]::Create()
            try {
                $runspace.ApartmentState='STA';$runspace.ThreadOptions='ReuseThread';$runspace.Open()
                $invoker.Runspace=$runspace
                [void]$invoker.AddScript($desktopLaunch.ToString()).AddArgument($program).AddArgument($bootstrapArgs).AddArgument($launcherRoot)
                [void]$invoker.Invoke()
                if($invoker.HadErrors){throw [string]$invoker.Streams.Error[0]}
            }finally{$invoker.Dispose();$runspace.Dispose()}
        }
        $deadline=[DateTime]::UtcNow.AddSeconds(15)
        while(-not [IO.File]::Exists($resultPath) -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 50}
        if(-not [IO.File]::Exists($resultPath)){throw 'Windows Explorer did not confirm the independent launch.'}
        $result=[IO.File]::ReadAllText($resultPath,[Text.Encoding]::UTF8)|ConvertFrom-Json
        [IO.File]::WriteAllText((Join-Path $launcherRoot '.runtime\process-broker-last.json'),($result|ConvertTo-Json -Compress),(New-Object Text.UTF8Encoding($false)))
        if($result.nonce -ne $nonce -or -not $result.ok -or -not $result.independent){
            throw ('Independent launch failed: '+$result.error)
        }
        return [Diagnostics.Process]::GetProcessById([int]$result.pid)
    } finally {
        foreach($temporaryPath in @($requestPath,$resultPath)){
            if([IO.File]::Exists($temporaryPath)){[IO.File]::Delete($temporaryPath)}
        }
    }
}
