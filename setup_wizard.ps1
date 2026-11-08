param([string]$Root=$PSScriptRoot,[ValidateRange(1,7)][int]$Step=1)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
[TrayDesign]::EnableDpi();[Windows.Forms.Application]::EnableVisualStyles()
# First-run guide: steps on the left, the current step in a card, back/next at the bottom right.
# All checks and writes go through onboarding.py; credentials are never shown here.
$window=New-Object Windows.Forms.Form
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.AutoScaleMode='Dpi'
$window.Text='微信助手 · 使用引导';$window.ClientSize=New-Object Drawing.Size(880,600);$window.MinimumSize=New-Object Drawing.Size(896,639)
$window.StartPosition='CenterScreen';$window.BackColor=[TrayDesign]::PageColor;$window.ForeColor=[TrayDesign]::Heading
$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$iconPath=Join-Path $PSScriptRoot 'wechat-assistant-v2.ico';if(Test-Path -LiteralPath $iconPath){$window.Icon=New-Object Drawing.Icon($iconPath)}
function Add-Label($parent,$text,$x,$y,$w,$h,$size=10,$color=$null,$bold=$false){
 $l=New-Object Windows.Forms.Label;$l.Text=$text;$l.SetBounds($x,$y,$w,$h);$l.BackColor=[Drawing.Color]::Transparent;$l.AutoEllipsis=$true;$l.TextAlign='MiddleLeft'
 $l.Font=New-Object Drawing.Font('Microsoft YaHei UI',$size,$(if($bold){[Drawing.FontStyle]::Bold}else{[Drawing.FontStyle]::Regular}))
 if($color){$l.ForeColor=$color};$parent.Controls.Add($l);return $l
}
function Add-Button($parent,$text,$x,$y,$w,$h=36){$b=New-Object TrayButton;$b.Text=$text;$b.SetBounds($x,$y,$w,$h);$parent.Controls.Add($b);return $b}
function Add-Card($x,$y,$w,$h){$c=New-Object TrayCard;$c.SetBounds($x,$y,$w,$h);$window.Controls.Add($c);return $c}
function Add-Glyph($parent,$kind,$x,$y,$color){$p=New-Object Windows.Forms.PictureBox;$p.SetBounds($x,$y,20,20);$p.SizeMode='Zoom';$p.BackColor=[Drawing.Color]::Transparent;$p.Image=[TrayDesign]::Glyph($kind,$color,64);$parent.Controls.Add($p);return $p}

$side=Add-Card 24 24 224 552
$brand=New-Object Windows.Forms.PictureBox;$brand.SetBounds(20,20,40,40);$brand.SizeMode='Zoom';$brand.BackColor=[Drawing.Color]::Transparent
$brand.Image=[TrayDesign]::Symbol([TrayDesign]::BrandBlue,96,'standby');$side.Controls.Add($brand)
$null=Add-Label $side '微信助手' 70 18 140 24 12 $null $true
$null=Add-Label $side '使用引导' 70 42 140 20 9 ([TrayDesign]::MutedColor)
$stepNames=@('欢迎','检查环境','选择工作目录','模型服务','登录微信','绑定你的微信','完成')
$stepLabels=@();for($i=0;$i -lt $stepNames.Count;$i++){$stepLabels+=,(Add-Label $side ('  '+($i+1)+'   '+$stepNames[$i]) 12 (88+$i*48) 200 40 10)}
$card=Add-Card 264 24 592 488
$stepTitle=Add-Label $card '' 28 20 536 36 15 $null $true
$stepIntro=Add-Label $card '' 28 58 536 48 10 ([TrayDesign]::MutedColor);$stepIntro.TextAlign='TopLeft'
$pages=@{}
for($i=1;$i -le 7;$i++){$p=New-Object Windows.Forms.Panel;$p.SetBounds(28,112,536,356);$p.BackColor=[Drawing.Color]::Transparent;$p.Visible=$false;$card.Controls.Add($p);$pages[$i]=$p}
$status=Add-Label $window '' 268 528 280 44 9.5 ([TrayDesign]::MutedColor)
$back=Add-Button $window '上一步' 560 530 112 40
$next=Add-Button $window '下一步' 684 530 172 40;$next.Primary=$true

# 1 Welcome
$p=$pages[1]
$null=Add-Label $p '通过个人微信给这台电脑发任务：整理文件、取回结果、打开应用、定时提醒。' 0 0 536 28 10.5
$null=Add-Label $p '开始前请准备' 0 44 536 26 10 ([TrayDesign]::MutedColor)
$y=76;foreach($line in @('Windows 10 / 11，以及 Python 3.10 以上','Claude Code 命令行（由它在电脑上执行任务）','一个能扫码登录的微信')){$null=Add-Glyph $p 'check' 0 ($y+6) ([TrayDesign]::Blue);$null=Add-Label $p $line 30 $y 500 32 10.5;$y+=38}
$null=Add-Label $p '全程约 5 分钟。之后可在托盘菜单“设置 → 使用引导”重新打开。' 0 206 536 28 10
$safety=Add-Label $p '助手以当前 Windows 账户运行，可以操作你授权目录中的文件和本机应用；任务内容会发送给你选择的模型服务处理。' 0 250 536 52 9 ([TrayDesign]::MutedColor);$safety.TextAlign='TopLeft'
$accept=New-Object Windows.Forms.CheckBox;$accept.Text='我已阅读并同意使用说明与免责声明';$accept.SetBounds(0,316,360,30);$accept.BackColor=[Drawing.Color]::Transparent;$p.Controls.Add($accept)
$readTerms=New-Object Windows.Forms.LinkLabel;$readTerms.Text='阅读免责声明';$readTerms.SetBounds(376,316,160,30);$readTerms.TextAlign='MiddleRight'
$readTerms.LinkColor=[TrayDesign]::Blue;$readTerms.ActiveLinkColor=[TrayDesign]::Blue;$readTerms.BackColor=[Drawing.Color]::Transparent;$p.Controls.Add($readTerms)

# 2 Environment
$p=$pages[2]
$envRows=@{}
$y=0;foreach($row in @(@('python','Python'),@('qr','二维码组件'),@('claude','Claude Code'),@('config','配置文件'))){
 $null=Add-Label $p $row[1] 0 $y 120 36 10 ([TrayDesign]::MutedColor)
 $envRows[$row[0]]=Add-Label $p '检查中…' 128 $y 280 36 10;$y+=52
}
$installDeps=Add-Button $p '安装组件' 420 52 116;$installDeps.Visible=$false
$installClaude=Add-Button $p '一键安装' 420 104 116;$installClaude.Visible=$false
$loginClaude=Add-Button $p '登录 Claude' 420 104 116;$loginClaude.Visible=$false
$claudeHelp=New-Object Windows.Forms.LinkLabel;$claudeHelp.Text='Claude Code 安装说明';$claudeHelp.SetBounds(136,223,180,30);$claudeHelp.TextAlign='MiddleLeft'
$claudeHelp.LinkColor=[TrayDesign]::Blue;$claudeHelp.BackColor=[Drawing.Color]::Transparent;$p.Controls.Add($claudeHelp)
$recheck=Add-Button $p '重新检查' 0 220 120;$recheck.IconKind='restart'
$envHint=Add-Label $p '使用 Claude 时需要登录一次（点“登录 Claude”，按窗口提示完成）；改用 DeepSeek、Kimi 等服务时只需安装，不必登录。缺少 Claude Code 也能继续，但任务无法执行。' 0 268 536 64 9 ([TrayDesign]::MutedColor);$envHint.TextAlign='TopLeft'

# 3 Folders
$p=$pages[3]
$null=Add-Label $p '工作目录 · 助手在这里处理文件' 0 0 536 26 10 ([TrayDesign]::MutedColor)
$workdir=New-Object Windows.Forms.TextBox;$workdir.SetBounds(0,30,424,36);$p.Controls.Add($workdir)
$browseWork=Add-Button $p '浏览…' 436 30 100
$null=Add-Label $p '收件目录 · 微信发来的附件保存在这里' 0 90 536 26 10 ([TrayDesign]::MutedColor)
$inbox=New-Object Windows.Forms.TextBox;$inbox.SetBounds(0,120,424,36);$p.Controls.Add($inbox)
$browseInbox=Add-Button $p '浏览…' 436 120 100
$folderHint=Add-Label $p '不要选择整个磁盘或系统目录。助手只处理工作目录和你另外授权的位置；以后可在配置文件中修改。' 0 180 536 52 9 ([TrayDesign]::MutedColor);$folderHint.TextAlign='TopLeft'

# 4 Model
$p=$pages[4]
$backendNow=Add-Label $p '当前后端：Claude' 0 0 536 32 11 $null $true
$modelText=Add-Label $p '默认使用这台电脑上 Claude Code 自己的登录。也可以改用 DeepSeek、Kimi、智谱 GLM、通义千问、MiniMax 或自定义的兼容接口，在“后端配置”中填写 Key 并测试连接。' 0 40 536 72 10;$modelText.TextAlign='TopLeft'
$openBackend=Add-Button $p '打开后端配置' 0 124 160;$openBackend.IconKind='settings'
$null=Add-Label $p '可以先跳过，之后在运行中心“当前模型”卡片里随时切换。' 0 176 536 28 9 ([TrayDesign]::MutedColor)

# 5 Login
$p=$pages[5]
$loginState=Add-Label $p '尚未登录' 0 0 536 30 11 $null $true
$qrFrame=New-Object TrayCard;$qrFrame.SetBounds(0,40,232,232);$p.Controls.Add($qrFrame)
$qr=New-Object Windows.Forms.PictureBox;$qr.SetBounds(16,16,200,200);$qr.SizeMode='Zoom';$qr.BackColor=[Drawing.Color]::White;$qrFrame.Controls.Add($qr)
$loginText=Add-Label $p '点击“获取二维码”，用微信扫描并在手机上确认登录。二维码几分钟后过期，可重新获取。' 252 40 284 96 10;$loginText.TextAlign='TopLeft'
$getQr=Add-Button $p '获取二维码' 252 148 160;$getQr.Primary=$true

# 6 Bind
$p=$pages[6]
$bindState=Add-Label $p '' 0 0 536 30 11 $null $true
$bindText=Add-Label $p '在微信里打开刚才扫码后出现的助手对话，发送下面这句话。绑定后只有你的微信能给助手发任务。' 0 36 536 52 10;$bindText.TextAlign='TopLeft'
$codeLabel=Add-Label $p '' 0 96 536 64 26 ([TrayDesign]::Blue) $true
$codeHint=Add-Label $p '' 0 164 536 28 9 ([TrayDesign]::MutedColor)
$makeCode=Add-Button $p '生成绑定码' 0 208 160;$makeCode.Primary=$true

# 7 Done
$p=$pages[7]
$doneState=Add-Label $p '一切就绪' 0 0 536 30 11 ([TrayDesign]::SuccessText) $true
$y=40;foreach($tip in @('在微信发送“开始对话”唤醒助手，然后直接描述任务','发送“结束对话”让它待机；发送 /help 或“示例”随时查看用法')){$null=Add-Glyph $p 'chat' 0 ($y+6) ([TrayDesign]::Blue);$null=Add-Label $p $tip 30 $y 506 32 10;$y+=36}
$null=Add-Label $p '试试发送（点击复制，到微信里粘贴）' 0 120 536 26 10 ([TrayDesign]::MutedColor)
$starters=@('看看工作目录里有哪些文件','明天九点提醒我交周报','打开浏览器','看看画面')
for($i=0;$i -lt $starters.Count;$i++){
 $b=Add-Button $p $starters[$i] $(if($i % 2){276}else{0}) (150+[Math]::Floor($i/2)*44) 260;$b.Tag=$starters[$i];$b.IconKind='chat'
 $b.Add_Click({try{[Windows.Forms.Clipboard]::SetText([string]$this.Tag);Say ('已复制“'+$this.Tag+'”。先在微信发送“开始对话”，再粘贴发送。') 'info'}catch{Say '复制失败，请手动输入。' 'error'}})
}
$autostart=New-Object Windows.Forms.CheckBox;$autostart.Text='登录 Windows 后自动启动助手（推荐）';$autostart.Checked=$true;$autostart.SetBounds(0,250,536,30);$autostart.BackColor=[Drawing.Color]::Transparent;$p.Controls.Add($autostart)
$null=Add-Label $p '托盘图标右键可打开运行中心、后端配置、检查更新和使用引导。' 0 290 536 26 9 ([TrayDesign]::MutedColor)

[TrayDesign]::StyleFields($window);foreach($box in @($workdir,$inbox)){$field=[TrayDesign]::Frame($box);$field.Height=36}
$script:step=1;$script:state=$null;$script:job=$null;$script:claudeJob=$null;$script:login=$null;$script:qrStamp=0;$script:bridgeStarted=$false;$script:lastPoll=0

function Invoke-Setup($payload){
 $info=New-Object Diagnostics.ProcessStartInfo;$info.FileName=$WeChatPython
 $info.Arguments='"'+(Join-Path $PSScriptRoot 'onboarding.py')+'" --root "'+$Root.TrimEnd('\')+'"'
 $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true
 $info.StandardOutputEncoding=New-Object Text.UTF8Encoding($false);$info.EnvironmentVariables['PYTHONIOENCODING']='utf-8'
 $p=New-Object Diagnostics.Process;$p.StartInfo=$info
 try{
  [void]$p.Start();$writer=New-Object IO.StreamWriter($p.StandardInput.BaseStream,(New-Object Text.UTF8Encoding($false)))
  $writer.Write(($payload|ConvertTo-Json -Compress));$writer.Dispose()
  $out=$p.StandardOutput.ReadToEnd();[void]$p.WaitForExit(30000)
  $answer=$out|ConvertFrom-Json
  if($answer.ok -and $answer.config){$script:state=$answer}
  return $answer
 }catch{return [pscustomobject]@{ok=$false;message='无法运行 Python，请确认已安装 Python 3.10 以上并加入 PATH。'}}
 finally{$p.Dispose()}
}
function Start-Hidden($file,$arguments,$shell=$false){
 $info=New-Object Diagnostics.ProcessStartInfo
 if($shell){$info.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe';$info.Arguments='-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot $file)+'" '+$arguments}
 else{$info.FileName=$WeChatPython;$info.Arguments='"'+(Join-Path $PSScriptRoot $file)+'" '+$arguments}
 $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.WorkingDirectory=$Root
 return [Diagnostics.Process]::Start($info)
}
function Say($text,$tone='muted'){$status.Text=$text;$status.ForeColor=switch($tone){'error'{[TrayDesign]::DangerText};'ok'{[TrayDesign]::SuccessText};'info'{[TrayDesign]::Blue};default{[TrayDesign]::MutedColor}}}
function Mark($label,$ok,$good,$bad){$label.Text=if($ok){'✓ '+$good}else{$bad};$label.ForeColor=if($ok){[TrayDesign]::SuccessText}else{[TrayDesign]::WarningText}}
function Refresh-Environment {
 $s=$script:state;if(-not $s){return}
 Mark $envRows['python'] $s.python.ok ('Python '+$s.python.version) ('Python '+$s.python.version+'，需要 3.10 以上')
 Mark $envRows['qr'] $s.qr_library '已安装' '未安装（用于显示登录二维码）';$installDeps.Visible=-not $s.qr_library
 Mark $envRows['claude'] ([bool]$s.claude) $s.claude '未找到 Claude Code';$installClaude.Visible=-not $s.claude;$loginClaude.Visible=[bool]$s.claude
 Mark $envRows['config'] $s.config.exists '已创建' '下一步会自动创建';if(-not $s.config.exists){$envRows['config'].ForeColor=[TrayDesign]::MutedColor}
}
function Refresh-Step {
 $s=$script:state
 switch($script:step){
  2{Refresh-Environment}
  3{
   if(-not $workdir.Text){$workdir.Text=if($s -and $s.config.workdir -and $s.config.exists -and $s.config.workdir -notlike 'C:\WeChatBot\*'){$s.config.workdir}else{Join-Path $env:USERPROFILE 'WeChatBot\workspace'}}
   if(-not $inbox.Text){$inbox.Text=if($s -and $s.config.inbox -and $s.config.exists -and $s.config.inbox -notlike 'C:\WeChatBot\*'){$s.config.inbox}else{Join-Path $workdir.Text 'inbox'}}
  }
  4{$backendNow.Text='当前后端：'+$(if($s){[string]$s.config.backend}else{'claude'})}
  5{$loginState.Text=if($s.config.logged_in){'✓ 已登录，可以继续'}else{'尚未登录'};$loginState.ForeColor=if($s.config.logged_in){[TrayDesign]::SuccessText}else{[TrayDesign]::Heading};$getQr.Text=if($s.config.logged_in){'重新扫码'}else{'获取二维码'}}
  6{
   if($s.config.allowed -gt 0){$bindState.Text='✓ 已绑定 '+$s.config.allowed+' 个微信账号';$bindState.ForeColor=[TrayDesign]::SuccessText;$codeLabel.Text='';$codeHint.Text=''}
   else{$bindState.Text=if($s.bridge_running){'助手后台已启动'}else{'正在启动助手后台…'};$bindState.ForeColor=[TrayDesign]::Heading}
   if(-not $s.bridge_running -and -not $script:bridgeStarted -and $s.config.logged_in){try{$x=Start-Hidden 'toggle.ps1' '-On' $true;$x.Dispose();$script:bridgeStarted=$true}catch{Say '无法启动助手后台，请稍后重试。' 'error'}}
  }
 }
 Update-Navigation
}
function Can-Advance {
 $s=$script:state
 switch($script:step){1{return [bool]$accept.Checked};2{return [bool]($s -and $s.python.ok)};5{return [bool]($s -and $s.config.logged_in)};6{return [bool]($s -and $s.config.allowed -gt 0)};default{return $true}}
}
function Update-Navigation {
 $back.Enabled=$script:step -gt 1 -and -not $script:job;$next.Enabled=(Can-Advance) -and -not $script:job
 $next.Text=if($script:step -eq 7){'完成并打开运行中心'}elseif($script:step -eq 4){'下一步（可跳过）'}else{'下一步'}
 $s=$script:state
 for($i=0;$i -lt $stepLabels.Count;$i++){
  $n=$i+1;$label=$stepLabels[$i]
  $done=switch($n){2{[bool]($s -and $s.python.ok)};3{[bool]($s -and $s.config.exists)};5{[bool]($s -and $s.config.logged_in)};6{[bool]($s -and $s.config.allowed -gt 0)};default{$n -lt $script:step}}
  $label.Text=$(if($done -and $n -ne $script:step){'✓'}else{' '+$n})+'   '+$stepNames[$i]
  $label.Font=New-Object Drawing.Font('Microsoft YaHei UI',10,$(if($n -eq $script:step){[Drawing.FontStyle]::Bold}else{[Drawing.FontStyle]::Regular}))
  $label.ForeColor=if($n -eq $script:step){[TrayDesign]::Blue}elseif($done){[TrayDesign]::SuccessText}else{[TrayDesign]::MutedColor}
 }
}
function Show-Step($n){
 $script:step=$n
 for($i=1;$i -le 7;$i++){$pages[$i].Visible=($i -eq $n)}
 $titles=@{1='欢迎使用微信助手';2='检查运行环境';3='选择工作目录';4='选择模型服务';5='扫码登录微信';6='绑定你的微信';7='设置完成'}
 $intros=@{1='接下来几步会帮你完成安装后的设置。';2='助手需要 Python 运行后台，用 Claude Code 在电脑上执行任务。';3='告诉助手在哪里处理文件、把微信附件存到哪里。';4='选择由哪个模型理解任务。不确定就先保持默认。';5='用你自己的微信扫码，助手会登录成一个可以对话的账号。';6='最后一步：确认只有你的微信能使用这个助手。';7='助手已在后台运行。以下是开始使用的几个要点。'}
 $stepTitle.Text=$titles[$n];$stepIntro.Text=$intros[$n];Say ''
 $null=Invoke-Setup @{operation='status'};Refresh-Step
}
function Advance {
 switch($script:step){
  3{
   $answer=Invoke-Setup @{operation='init';workdir=$workdir.Text;inbox=$inbox.Text}
   if(-not $answer.ok){Say $answer.message 'error';return}
   Say $answer.message 'ok'
  }
  7{
   $answer=Invoke-Setup @{operation='complete';accepted=[bool]$accept.Checked}
   if(-not $answer.ok){Say $answer.message 'error';return}
   if($autostart.Checked){try{$x=Start-Hidden 'install_autostart.ps1' '' $true;[void]$x.WaitForExit(15000);$x.Dispose()}catch{}}
   try{$x=Start-Hidden 'launch_tray.ps1' '-Show' $true;$x.Dispose()}catch{Say '无法打开运行中心，请双击“微信助手.bat”。' 'error';return}
   Stop-Login;$window.Close();return
  }
 }
 Show-Step ($script:step+1)
}
function Stop-Login {if($script:login -and -not $script:login.HasExited){try{$script:login.Kill()}catch{}};if($script:login){$script:login.Dispose();$script:login=$null}}
function Poll {
 if($script:claudeJob -and $script:claudeJob.HasExited){$script:claudeJob.Dispose();$script:claudeJob=$null;$answer=Invoke-Setup @{operation='link_claude'};Refresh-Step;Say $answer.message $(if($script:state.claude){'ok'}else{'error'})}
 if($script:job -and $script:job.HasExited){
  $ok=$script:job.ExitCode -eq 0;$script:job.Dispose();$script:job=$null
  $null=Invoke-Setup @{operation='status'};Refresh-Step
  if($ok){Say '组件已安装' 'ok'}else{Say '组件安装失败，请检查网络后重试' 'error'}
 }
 if($script:login){
  $png=Join-Path $Root 'ilink_qrcode.png'
  if(Test-Path -LiteralPath $png){
   $stamp=[IO.File]::GetLastWriteTimeUtc($png).Ticks
   if($stamp -ne $script:qrStamp){try{$bytes=[IO.File]::ReadAllBytes($png);$old=$qr.Image;$qr.Image=[Drawing.Image]::FromStream((New-Object IO.MemoryStream(,$bytes)));if($old){$old.Dispose()};$script:qrStamp=$stamp;Say '请用微信扫描二维码，并在手机上确认登录。' 'info'}catch{}}
  }
  if($script:login.HasExited){
   $ok=$script:login.ExitCode -eq 0;$script:login.Dispose();$script:login=$null;$getQr.Enabled=$true
   if($qr.Image){$qr.Image.Dispose();$qr.Image=$null}
   $null=Invoke-Setup @{operation='status'};Refresh-Step
   if($ok){Say '微信已登录' 'ok'}else{Say '登录未完成：二维码可能已过期，请重新获取。' 'error'}
  }
 }
 if($script:step -eq 6 -and [Environment]::TickCount-$script:lastPoll -gt 2000){
  $script:lastPoll=[Environment]::TickCount
  $null=Invoke-Setup @{operation='pair_status'}
  if($script:state.config.allowed -gt 0 -and $codeLabel.Text){Say '绑定成功' 'ok'}
  Refresh-Step
 }
}
$installDeps.Add_Click({
 try{
  $info=New-Object Diagnostics.ProcessStartInfo;$info.FileName=$WeChatPython;$info.Arguments='-m pip install -r "'+(Join-Path $Root 'requirements.txt')+'"'
  $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$script:job=[Diagnostics.Process]::Start($info);Say '正在安装组件，可能需要一两分钟…' 'info'
 }catch{Say '无法运行 pip，请检查 Python 安装。' 'error'}
 Update-Navigation
})
$claudeHelp.Add_LinkClicked({Start-Process 'https://code.claude.com/docs/en/setup'})
# Official native installer (no administrator rights or Node.js); shown in its own window.
$installClaude.Add_Click({
 $command='irm https://claude.ai/install.ps1 | iex'
 if([Windows.Forms.MessageBox]::Show($window,('将运行 Claude Code 官方安装命令：'+"`r`n`r`n"+$command+"`r`n`r`n"+'会打开一个窗口显示进度，不需要管理员权限。是否继续？'),'安装 Claude Code','YesNo','Question') -ne 'Yes'){return}
 try{
  $info=New-Object Diagnostics.ProcessStartInfo;$info.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
  $info.Arguments='-NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; '+$command+'; Write-Host; Read-Host ''Done. Press Enter to close''"'
  $info.UseShellExecute=$true;$script:claudeJob=[Diagnostics.Process]::Start($info);Say '正在安装 Claude Code，请在打开的窗口中查看进度…' 'info'
 }catch{Say '无法启动安装，请按安装说明手动安装。' 'error'}
})
$loginClaude.Add_Click({$path=[string]$script:state.claude;if($path){try{Start-Process -FilePath $path -WorkingDirectory $env:USERPROFILE;Say '已打开 Claude 登录窗口，按提示完成后关闭即可。' 'info'}catch{Say '无法打开 Claude，请在终端运行 claude 登录。' 'error'}}})
$recheck.Add_Click({$null=Invoke-Setup @{operation='status'};Refresh-Step;Say '已重新检查'})
foreach($pair in @(@($browseWork,$workdir),@($browseInbox,$inbox))){
 $pair[0].Tag=$pair[1]
 $pair[0].Add_Click({$dialog=New-Object Windows.Forms.FolderBrowserDialog;$dialog.Description='选择文件夹';$dialog.ShowNewFolderButton=$true;if($this.Tag.Text){$dialog.SelectedPath=$this.Tag.Text};if($dialog.ShowDialog($window) -eq 'OK'){$this.Tag.Text=$dialog.SelectedPath};$dialog.Dispose()})
}
$openBackend.Add_Click({try{$x=Start-Hidden 'backend_editor.ps1' '' $true;$x.Dispose();Say '已打开后端配置；保存后回到这里继续。' 'info'}catch{Say '无法打开后端配置' 'error'}})
$getQr.Add_Click({
 Stop-Login;$png=Join-Path $Root 'ilink_qrcode.png';Remove-Item -LiteralPath $png -ErrorAction SilentlyContinue;$script:qrStamp=0
 if(-not $script:state.qr_library){Say '请先在“检查环境”一步安装二维码组件。' 'error';return}
 try{$script:login=Start-Hidden 'wechat_bridge.py' 'login';$getQr.Enabled=$false;Say '正在向微信申请二维码…' 'info'}catch{Say '无法启动登录，请检查 Python。' 'error'}
})
$makeCode.Add_Click({
 $answer=Invoke-Setup @{operation='pair'}
 if(-not $answer.ok){Say $answer.message 'error';return}
 $codeLabel.Text='绑定 '+$answer.code;$codeHint.Text='10 分钟内有效，只能使用一次。';Say '等待你在微信里发送绑定码…' 'info'
})
$accept.Add_CheckedChanged({Update-Navigation})
$readTerms.Add_LinkClicked({$path=Join-Path $PSScriptRoot 'DISCLAIMER.md';if(Test-Path -LiteralPath $path){Start-Process notepad.exe ('"'+$path+'"')}})
$back.Add_Click({if($script:step -gt 1){Show-Step ($script:step-1)}})
$next.Add_Click({Advance})
$window.Add_FormClosing({Stop-Login;if($script:state -and -not $script:state.completed){$null=Invoke-Setup @{operation='pair_cancel'}}})
$window.PerformAutoScale()
$timer=New-Object Windows.Forms.Timer;$timer.Interval=400;$timer.Add_Tick({Poll});$timer.Start()
Show-Step $Step
[Windows.Forms.Application]::Run($window)
$timer.Stop();$timer.Dispose();$window.Dispose()
