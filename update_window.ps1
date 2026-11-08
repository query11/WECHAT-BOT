param([string]$Root=$PSScriptRoot,[switch]$CheckNow,[switch]$Preview)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
[TrayDesign]::EnableDpi();[Windows.Forms.Application]::EnableVisualStyles()
# Shows the newest GitHub release and hands a prepared install to the tray (update-request.json).
# The tray then starts updater.py apply, stops the assistant and reopens it when done.
$window=New-Object Windows.Forms.Form
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.AutoScaleMode='Dpi'
$window.Text='微信助手 · 版本更新';$window.ClientSize=New-Object Drawing.Size(640,560);$window.MinimumSize=New-Object Drawing.Size(600,520)
$window.StartPosition='CenterScreen';$window.BackColor=[TrayDesign]::PageColor;$window.ForeColor=[TrayDesign]::Heading
$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$iconPath=Join-Path $PSScriptRoot 'wechat-assistant-v2.ico';if(Test-Path -LiteralPath $iconPath){$window.Icon=New-Object Drawing.Icon($iconPath)}
function Add-Label($parent,$text,$x,$y,$w,$h,$size=10,$color=$null){
 $l=New-Object Windows.Forms.Label;$l.Text=$text;$l.SetBounds($x,$y,$w,$h);$l.BackColor=[Drawing.Color]::Transparent;$l.AutoEllipsis=$true
 $l.Font=New-Object Drawing.Font('Microsoft YaHei UI',$size);$l.TextAlign='MiddleLeft';if($color){$l.ForeColor=$color};$parent.Controls.Add($l);return $l
}
function Add-Button($text,$x,$y,$w){$b=New-Object TrayButton;$b.Text=$text;$b.SetBounds($x,$y,$w,36);$window.Controls.Add($b);return $b}
$version=try{(Get-Content -LiteralPath (Join-Path $Root 'VERSION') -Raw).Trim()}catch{'未知'}
$title=Add-Label $window '版本更新' 28 18 400 40 16;$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold)
$intro=Add-Label $window ('当前版本 v'+$version+' · 新版本发布在 GitHub Releases') 28 58 584 26 10 ([TrayDesign]::MutedColor)
$card=New-Object TrayCard;$card.SetBounds(24,96,592,380);$card.Anchor='Top,Bottom,Left,Right';$window.Controls.Add($card)
$mark=New-Object Windows.Forms.PictureBox;$mark.SetBounds(20,18,28,28);$mark.SizeMode='Zoom';$mark.BackColor=[Drawing.Color]::Transparent
$mark.Image=[TrayDesign]::Glyph('upgrade',[TrayDesign]::Blue,64);$card.Controls.Add($mark)
$headline=Add-Label $card '正在检查更新…' 56 14 516 34 13;$headline.Font=New-Object Drawing.Font('Microsoft YaHei UI',13,[Drawing.FontStyle]::Bold)
$meta=Add-Label $card '' 20 52 552 24 9 ([TrayDesign]::MutedColor)
$notesLabel=Add-Label $card '更新内容' 20 84 300 24 10 ([TrayDesign]::MutedColor)
$notes=New-Object Windows.Forms.TextBox;$notes.Multiline=$true;$notes.ReadOnly=$true;$notes.ScrollBars='Vertical';$notes.SetBounds(20,110,552,212);$notes.Anchor='Top,Bottom,Left,Right';$card.Controls.Add($notes)
$link=New-Object Windows.Forms.LinkLabel;$link.Text='在 GitHub 查看';$link.SetBounds(20,334,160,26);$link.TextAlign='MiddleLeft';$link.Anchor='Bottom,Left'
$link.LinkColor=[TrayDesign]::Blue;$link.ActiveLinkColor=[TrayDesign]::Blue;$link.BackColor=[Drawing.Color]::Transparent;$link.Visible=$false;$card.Controls.Add($link)
$kindHint=Add-Label $card '' 188 334 384 26 9 ([TrayDesign]::MutedColor);$kindHint.TextAlign='MiddleRight';$kindHint.Anchor='Bottom,Left,Right'
$status=Add-Label $window '' 28 492 260 40 9.5 ([TrayDesign]::MutedColor);$status.Anchor='Bottom,Left,Right'
$skip=Add-Button '跳过此版本' 300 494 112;$skip.Anchor='Bottom,Right';$skip.Visible=$false
$later=Add-Button '稍后' 420 494 84;$later.Anchor='Bottom,Right'
$install=Add-Button '检查更新' 512 494 104;$install.Anchor='Bottom,Right';$install.Primary=$true
$script:state=$null;$script:job=$null;$script:closingSoon=$false

function Show-State($u){
 $script:state=$u;$available=[bool]($u -and $u.available -and $u.latest)
 $link.Visible=[bool]($u -and ([string]$u.html_url).StartsWith('https://github.com/'))
 $skip.Visible=$available
 if($available){
  $headline.Text='发现新版本 '+$u.latest;$headline.ForeColor=[TrayDesign]::Heading
  $date=if($u.published_at){([string]$u.published_at).Substring(0,[Math]::Min(10,([string]$u.published_at).Length))}else{''}
  $meta.Text=(@($(if($date){'发布于 '+$date}),$(if($u.asset){'已附带校验的安装包'}else{'来自源码压缩包'})) | Where-Object {$_}) -join ' · '
  # Release notes are Markdown: drop heading marks, show list items as bullets, strip bold marks.
  $lines=([string]$u.notes -replace "`r",'') -split "`n" | ForEach-Object {(($_ -replace '^\s{0,3}#{1,6}\s*','') -replace '^\s*[-*]\s+','• ') -replace '\*\*(.+?)\*\*','$1'}
  $notes.Text=if($u.notes){$lines -join "`r`n"}else{'这个版本没有填写更新说明。'};$notes.Select(0,0)
  $install.Text='立即更新'
 }elseif($u -and $u.error){
  $headline.Text='暂时无法检查更新';$headline.ForeColor=[TrayDesign]::WarningText;$meta.Text=[string]$u.error
  $notes.Text='网络恢复后会自动重试；也可以点击“检查更新”。';$install.Text='检查更新'
 }else{
  $headline.Text='已是最新版本';$headline.ForeColor=[TrayDesign]::SuccessText
  $meta.Text=if($u -and $u.latest){'GitHub 上最新发布的是 '+$u.latest}else{''}
  $notes.Text='当前版本 v'+$version+' 已是最新。发布新版本后，运行中心会提示并可在这里一键更新。';$install.Text='检查更新'
 }
 $git=[bool]($u -and $u.install_kind -eq 'git')
 $kindHint.Text=if($git){'当前是 Git 开发目录：请用 git pull 更新'}elseif($u -and $u.repo){'来源：'+$u.repo}else{''}
 Set-Actions
}
function Set-Actions {
 $busy=[bool]$script:job;$available=[bool]($script:state -and $script:state.available)
 $git=[bool]($script:state -and $script:state.install_kind -eq 'git')
 $install.Enabled=-not $busy -and -not $script:closingSoon -and -not ($available -and $git)
 $skip.Enabled=-not $busy -and -not $script:closingSoon;$later.Enabled=-not $script:closingSoon
 if($busy){$install.Text=if($script:job.action -eq 'prepare'){'正在准备…'}else{'正在检查…'}}
}
function Start-Updater($action,$extra=''){
 if($script:job){return}
 try{
  $info=New-Object Diagnostics.ProcessStartInfo;$info.FileName=$WeChatPython
  $info.Arguments='"'+(Join-Path $PSScriptRoot 'updater.py')+'" '+$action+' --root "'+$Root.TrimEnd('\')+'" '+$extra
  $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
  $info.StandardOutputEncoding=New-Object Text.UTF8Encoding($false);$info.EnvironmentVariables['PYTHONIOENCODING']='utf-8'
  $p=New-Object Diagnostics.Process;$p.StartInfo=$info;[void]$p.Start()
  $script:job=@{process=$p;output=$p.StandardOutput.ReadToEndAsync();error=$p.StandardError.ReadToEndAsync();action=$action;started=[DateTime]::UtcNow}
  $status.ForeColor=[TrayDesign]::MutedColor
  $status.Text=switch($action){'prepare'{'正在下载并校验新版本…'};'skip'{'正在保存…'};default{'正在连接 GitHub…'}}
 }catch{$status.Text='无法启动更新程序，请检查 Python。';$status.ForeColor=[TrayDesign]::DangerText}
 Set-Actions
}
function Show-Progress {
 try{
  $p=Get-Content -LiteralPath (Join-Path $Root '.runtime\update\progress.json') -Raw -Encoding UTF8|ConvertFrom-Json
  if($p.stage -eq 'download' -and $p.done){
   $done=[Math]::Round([double]$p.done/1MB,1);$status.Text=if($p.total){'正在下载 '+$done+' / '+[Math]::Round([double]$p.total/1MB,1)+' MB'}else{'正在下载 '+$done+' MB'}
  }elseif($p.stage -eq 'extract'){$status.Text='正在校验并解压…'}
 }catch{}
}
function Assistant-Busy {
 try{
  $s=Get-Content -LiteralPath (Join-Path $Root '.runtime\status.json') -Raw -Encoding UTF8|ConvertFrom-Json
  $fresh=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()-[double]$s.heartbeat -le 10
  return [bool]($fresh -and (($s.task -and $s.task -ne '暂无执行中的任务') -or $s.stage -or $s.queued -gt 0 -or $s.downloads -gt 0))
 }catch{return $false}
}
function Finish-Job {
 $j=$script:job;if(-not $j -or -not $j.process.HasExited){return}
 try{
  $answer=$j.output.GetAwaiter().GetResult()|ConvertFrom-Json
  if($j.action -eq 'check'){Show-State $answer;$status.Text=if($answer.ok){''}else{[string]$answer.message}}
  elseif($j.action -eq 'skip'){$status.Text='已跳过 '+$script:state.latest+'；有更新的版本时会再提醒。';$script:closingSoon=$true}
  elseif(-not $answer.ok){$status.Text=[string]$answer.message;$status.ForeColor=[TrayDesign]::DangerText}
  else{
   $question='将关闭微信助手并安装 '+$answer.tag+'，完成后会自动重新打开。'+$(if($answer.verified){''}else{"`r`n此版本未提供校验值，只经 HTTPS 从 GitHub 下载。"})+"`r`n是否继续？"
   if($Preview -or [Windows.Forms.MessageBox]::Show($window,$question,'安装更新','YesNo','Question') -eq 'Yes'){
    $request=@{staging=[string]$answer.staging;version=[string]$answer.tag;at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()}|ConvertTo-Json -Compress
    $temp=Join-Path $Root ('.runtime\update-request-'+[Guid]::NewGuid().ToString('N')+'.tmp')
    [IO.File]::WriteAllText($temp,$request,(New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination (Join-Path $Root '.runtime\update-request.json') -Force
    $status.Text='已交给运行中心安装，助手会短暂关闭后自动重新打开。';$status.ForeColor=[TrayDesign]::Blue;$script:closingSoon=$true
   }else{$status.Text='已取消安装；下载的文件会在下次更新时覆盖。'}
  }
 }catch{$status.Text='更新程序没有正常返回，请稍后重试。';$status.ForeColor=[TrayDesign]::DangerText}
 finally{$j.process.Dispose();$script:job=$null;Set-Actions}
 if($script:closingSoon){$closer=New-Object Windows.Forms.Timer;$closer.Interval=2500;$closer.Add_Tick({$this.Stop();$window.Close()});$closer.Start()}
}
$install.Add_Click({
 if($script:state -and $script:state.available -and $script:state.install_kind -ne 'git'){
  if(Assistant-Busy){$status.Text='有任务或附件正在处理，请完成后再更新。';$status.ForeColor=[TrayDesign]::DangerText;return}
  Start-Updater 'prepare'
 }else{Start-Updater 'check' '--force'}
})
$skip.Add_Click({if($script:state.latest){Start-Updater 'skip' ('--tag '+[string]$script:state.latest)}})
$later.Add_Click({$window.Close()})
$link.Add_LinkClicked({$url=[string]$script:state.html_url;if($url.StartsWith('https://github.com/')){Start-Process $url}})
$window.Add_FormClosing({param($sender,$e)if($script:job -and $script:job.action -eq 'prepare'){$status.Text='正在下载，请稍候完成后再关闭。';$e.Cancel=$true}})
[TrayDesign]::StyleFields($window);$null=[TrayDesign]::Frame($notes)
$window.Add_Shown({$window.ActiveControl=$later;$notes.Select(0,0)})
$window.PerformAutoScale()
$timer=New-Object Windows.Forms.Timer;$timer.Interval=200
$timer.Add_Tick({
 if($script:job){
  if($script:job.action -eq 'prepare'){Show-Progress}
  if(-not $script:job.process.HasExited -and ([DateTime]::UtcNow-$script:job.started).TotalMinutes -gt 15){$script:job.process.Kill()}
  Finish-Job
 }
})
$timer.Start()
$cached=Join-Path $Root '.runtime\update.json'
if((Test-Path -LiteralPath $cached) -and -not $CheckNow){try{Show-State (Get-Content -LiteralPath $cached -Raw -Encoding UTF8|ConvertFrom-Json)}catch{}}
Start-Updater 'check' $(if($CheckNow){'--force'}else{''})
if($Preview){$window.Show();[Windows.Forms.Application]::DoEvents()}else{[Windows.Forms.Application]::Run($window)}
$timer.Stop();$timer.Dispose();$window.Dispose()
