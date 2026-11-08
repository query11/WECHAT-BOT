param([switch]$Preview,[switch]$PreviewClaude,[switch]$VerifyConnection,[switch]$VerifyApply,[switch]$Verify,[string]$ConfigRoot=$PSScriptRoot)
$ErrorActionPreference='Stop'
if($Verify -and [IO.Path]::GetFullPath($ConfigRoot) -eq [IO.Path]::GetFullPath($PSScriptRoot)){throw 'Verify requires an isolated ConfigRoot'}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
[TrayDesign]::EnableDpi();[Windows.Forms.Application]::EnableVisualStyles()
# Layout: services list card on the left, the selected service's form card on the right,
# status and actions along the bottom. Coordinates are logical pixels before PerformAutoScale.
$window=New-Object Windows.Forms.Form
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.AutoScaleMode='Dpi'
$window.Text='微信助手 · 后端配置';$window.ClientSize=New-Object Drawing.Size(880,620)
$window.MinimumSize=New-Object Drawing.Size(860,640)
$window.StartPosition='CenterScreen';$window.BackColor=[TrayDesign]::PageColor;$window.ForeColor=[TrayDesign]::Heading
$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$window.Icon=New-Object Drawing.Icon((Join-Path $PSScriptRoot 'wechat-assistant-v2.ico'))
function Add-Label($parent,$text,$x,$y,$w,$h,$size=10,$color=$null){
 $l=New-Object Windows.Forms.Label;$l.Text=$text;$l.SetBounds($x,$y,$w,$h);$l.BackColor=[Drawing.Color]::Transparent
 $l.Font=New-Object Drawing.Font('Microsoft YaHei UI',$size);$l.AutoEllipsis=$true;$l.TextAlign='MiddleLeft'
 if($color){$l.ForeColor=$color};$parent.Controls.Add($l);return $l
}
function Add-Card($x,$y,$w,$h){$c=New-Object TrayCard;$c.SetBounds($x,$y,$w,$h);$window.Controls.Add($c);return $c}
function Add-Button($parent,$text,$x,$y,$w,$h){$b=New-Object TrayButton;$b.Text=$text;$b.SetBounds($x,$y,$w,$h);$parent.Controls.Add($b);return $b}
$title=Add-Label $window '后端配置' 28 18 600 40 16;$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold)
$intro=Add-Label $window '为每个模型服务保存接口地址、模型和 Key；保存并应用后，在运行中心或托盘切换使用。' 28 58 824 26 10 ([TrayDesign]::MutedColor)
$listCard=Add-Card 24 100 256 448
$listTitle=Add-Label $listCard '模型服务' 16 12 120 28 12;$listTitle.Font=New-Object Drawing.Font('Microsoft YaHei UI',12,[Drawing.FontStyle]::Bold)
$listCount=Add-Label $listCard '' 140 12 100 28 9 ([TrayDesign]::MutedColor);$listCount.TextAlign='MiddleRight'
$list=New-Object TrayList;$list.SetBounds(8,48,240,344);$list.Font=New-Object Drawing.Font('Microsoft YaHei UI',10);$listCard.Controls.Add($list)
$addButton=Add-Button $listCard '添加服务' 16 404 224 32;$addButton.IconKind='plus'
$formCard=Add-Card 296 100 560 448
$formTitle=Add-Label $formCard '' 24 14 512 32 13;$formTitle.Font=New-Object Drawing.Font('Microsoft YaHei UI',13,[Drawing.FontStyle]::Bold)
$formHint=Add-Label $formCard '' 24 46 512 24 9 ([TrayDesign]::MutedColor)
$nameLabel=Add-Label $formCard '名称' 24 88 92 36 10 ([TrayDesign]::MutedColor)
$name=New-Object Windows.Forms.TextBox;$name.SetBounds(124,88,412,36);$name.MaxLength=24;$formCard.Controls.Add($name)
$custom=New-Object Windows.Forms.CheckBox;$custom.Text='使用 Anthropic 兼容接口（不沿用 Claude Code 原设置）';$custom.SetBounds(124,92,412,28);$formCard.Controls.Add($custom)
$null=Add-Label $formCard '接口地址' 24 136 92 36 10 ([TrayDesign]::MutedColor)
$url=New-Object Windows.Forms.TextBox;$url.SetBounds(124,136,412,36);$url.MaxLength=1000;$formCard.Controls.Add($url)
$null=Add-Label $formCard '模型' 24 184 92 36 10 ([TrayDesign]::MutedColor)
$model=New-Object Windows.Forms.ComboBox;$model.SetBounds(124,187,412,30);$model.MaxLength=160;$formCard.Controls.Add($model)
$null=Add-Label $formCard 'API Key' 24 232 92 36 10 ([TrayDesign]::MutedColor)
$key=New-Object Windows.Forms.TextBox;$key.SetBounds(124,232,412,36);$key.UseSystemPasswordChar=$true;$key.MaxLength=4096;$formCard.Controls.Add($key)
$keyState=Add-Label $formCard '正在读取配置…' 124 272 412 24 9 ([TrayDesign]::MutedColor)
$showKey=New-Object Windows.Forms.CheckBox;$showKey.Text='显示输入的 Key';$showKey.SetBounds(124,300,150,26);$formCard.Controls.Add($showKey)
$vision=New-Object Windows.Forms.CheckBox;$vision.Text='模型支持图片（允许截图识别）';$vision.SetBounds(284,300,252,26);$formCard.Controls.Add($vision)
$showKey.Add_CheckedChanged({$key.UseSystemPasswordChar=-not $showKey.Checked})
$notice=Add-Label $formCard 'Key 由 Windows 当前用户加密保存，不会回显已保存的值。地址和模型名以服务商控制台为准；连接测试会调用接口并产生少量用量。' 24 380 512 48 9 ([TrayDesign]::MutedColor)
$notice.TextAlign='TopLeft'
$status=Add-Label $window '' 28 568 400 36 9.5 ([TrayDesign]::Blue)
$delete=Add-Button $window '删除' 444 566 96 36;$delete.Danger=$true
$test=Add-Button $window '测试连接' 552 566 120 36
$save=Add-Button $window '保存并应用' 684 566 172 36;$save.Primary=$true
$addMenu=New-Object Windows.Forms.ContextMenuStrip;$addMenu.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
foreach($control in @($formTitle,$formHint,$name,$custom,$url,$model,$key,$keyState,$vision)){$control.Anchor='Top,Left,Right'}
$listCard.Anchor='Top,Bottom,Left';$list.Anchor='Top,Bottom,Left,Right';$addButton.Anchor='Bottom,Left,Right';$listCount.Anchor='Top,Right'
$formCard.Anchor='Top,Bottom,Left,Right';$notice.Anchor='Bottom,Left,Right';$intro.Anchor='Top,Left,Right'
$status.Anchor='Bottom,Left,Right';foreach($b in @($delete,$test,$save)){$b.Anchor='Bottom,Right'}
$script:data=$null;$script:job=$null;$script:dirty=$false;$script:loading=$false;$script:controlPending=$null
$script:entries=@();$script:current=$null;$script:lastIndex=-1;$script:applyHint=''

function Find-Preset($id){foreach($p in @($script:data.presets)){if($p.id -eq $id){return $p}};return $null}
# Entries are keyed by backend id; profile ids can never be claude or deepseek.
function Entry-Key($entry){if(-not $entry){return ''};return [string]$entry.id}
function Set-EnabledState {
 $ready=($null -ne $script:data) -and (-not $script:job) -and (-not $script:controlPending)
 $kind=if($script:current){$script:current.kind}else{''}
 $editing=$kind -and ($kind -ne 'claude' -or $custom.Checked)
 $list.Enabled=$ready;$addButton.Enabled=$ready -and @($script:data.profiles).Count -lt [int]$script:data.max_profiles
 $custom.Enabled=$ready;$name.Enabled=$ready;$vision.Enabled=$ready
 $url.Enabled=$ready -and $editing -and $kind -ne 'deepseek'
 $model.Enabled=$ready -and $editing;$key.Enabled=$ready -and $editing;$showKey.Enabled=$key.Enabled
 $test.Enabled=$ready -and $editing;$save.Enabled=$ready -and [bool]$kind
 $delete.Visible=($kind -eq 'profile');$delete.Enabled=$ready -and $kind -eq 'profile' -and $script:current.id -ne [string]$script:data.active_backend
 $test.Text=if($script:job -and $script:job.operation -eq 'test'){'正在测试…'}else{'测试连接'}
 $save.Text=if($script:job -and $script:job.operation -eq 'save'){'正在保存…'}elseif($script:controlPending){'正在应用…'}else{'保存并应用'}
}
function Show-Dirty {
 if($script:dirty){$intro.Text='有未保存的配置 · Ctrl+S 保存并应用';$intro.ForeColor=[TrayDesign]::Blue}
 else{$intro.Text='为每个模型服务保存接口地址、模型和 Key；保存并应用后，在运行中心或托盘切换使用。';$intro.ForeColor=[TrayDesign]::MutedColor}
}
function Set-Models($suggestions,$value,$fixed){
 $model.Items.Clear();$model.DropDownStyle=if($fixed){'DropDownList'}else{'DropDown'}
 foreach($m in @($suggestions)){if($m){$null=$model.Items.Add([string]$m)}}
 if($fixed){$model.SelectedItem=[string]$value}else{$model.Text=[string]$value}
}
function Describe-Key($p,$isNew){
 if($isNew){return '请填写该服务的 API Key。'}
 if($p.env_override){return '当前 Key 来自环境变量；请先移除变量，再在此更换。'}
 if($p.configured -and $p.encrypted){return '已配置 Key，受 Windows 用户加密保护；留空保留。'}
 if($p.configured){return '已配置 Key；留空保留，输入新值则替换。'}
 if($p.encrypted){return '已有加密 Key，但当前账户无法解密，请重新输入。'}
 return '尚未配置 Key，请填写。'
}
function Load-Entry($entry){
 $script:loading=$true
 try{
  $script:current=$entry;$kind=$entry.kind;$isProfile=$kind -in @('profile','new')
  $nameLabel.Visible=$isProfile;$name.Parent.Visible=$isProfile;$custom.Visible=($kind -eq 'claude');$vision.Visible=$isProfile
  $key.Clear();$showKey.Checked=$false;$vision.Checked=$false;$name.Text=''
  switch($kind){
   'deepseek'{
    $p=$script:data.deepseek;$formTitle.Text='DeepSeek';$formHint.Text='DeepSeek 官方接口 · 地址固定，模型从官方型号中选择'
    $url.Text=[string]$p.base_url;Set-Models @('deepseek-flash','deepseek-v4-pro') $p.model $true;$keyState.Text=Describe-Key $p $false
   }
   'claude'{
    $p=$script:data.claude;$formTitle.Text='Claude';$formHint.Text='默认沿用 Claude Code 自己的登录与设置；也可改用 Anthropic 兼容接口'
    $custom.Checked=[bool]$p.enabled;$url.Text=[string]$p.base_url;Set-Models @() $p.model $false
    $keyState.Text=if($p.enabled){Describe-Key $p $false}else{'当前沿用原 Claude 设置，不读取或显示其中的密钥。'}
   }
   default{
    $p=if($kind -eq 'profile'){$entry.data}else{$null};$presetId=if($p){[string]$p.preset}else{[string]$entry.preset}
    $preset=Find-Preset $presetId
    $displayName=if($p){[string]$p.name}elseif($preset){[string]$preset.name}else{''}
    $formTitle.Text=if($kind -eq 'new'){'添加 '+$(if($preset){$preset.name}else{'自定义接口'})}else{$displayName}
    $formHint.Text=if($preset){$preset.vendor+' · Anthropic 兼容接口，地址与模型可按控制台修改'}else{'自定义 Anthropic 兼容接口 · 填写服务商提供的地址与模型'}
    $name.Text=$displayName
    $url.Text=if($p){[string]$p.base_url}elseif($preset){[string]$preset.base_url}else{'https://'}
    $suggest=if($preset){@($preset.models)}else{@()}
    Set-Models $suggest $(if($p){$p.model}elseif($suggest.Count){$suggest[0]}else{''}) $false
    $vision.Checked=[bool]($p -and $p.vision);$keyState.Text=Describe-Key $p ($kind -eq 'new')
   }
  }
  $script:dirty=($kind -eq 'new');Show-Dirty
 }finally{$script:loading=$false;Set-EnabledState}
}
function Fill-List($selectKey){
 $script:loading=$true
 try{
  $d=$script:data;$active=[string]$d.active_backend;$entries=New-Object Collections.ArrayList
  $null=$entries.Add(@{kind='claude';title='Claude';detail=$(if($d.claude.enabled){'兼容接口 · '+$d.claude.model}else{'沿用 Claude Code 原设置'});configured=$true;id='claude'})
  $null=$entries.Add(@{kind='deepseek';title='DeepSeek';detail=([string]$d.deepseek.model+' · 官方接口');configured=[bool]$d.deepseek.configured;id='deepseek'})
  foreach($p in @($d.profiles)){if($p){$null=$entries.Add(@{kind='profile';title=[string]$p.name;detail=[string]$p.model;configured=[bool]$p.configured;id=[string]$p.id;data=$p})}}
  $list.BeginUpdate();$list.Items.Clear();$script:entries=@($entries)
  foreach($e in $script:entries){
   $item=New-Object TrayListItem;$item.Title=$e.title;$item.Detail=$e.detail;$item.Tag=$e
   if($e.id -eq $active){$item.Badge='使用中';$item.BadgeColor=[TrayDesign]::Success}
   elseif(-not $e.configured){$item.Badge='未配置';$item.BadgeColor=[TrayDesign]::Warning}
   $null=$list.Items.Add($item)
  }
  $list.EndUpdate()
  $listCount.Text=('自定义 {0}/{1}' -f @($d.profiles).Count,$d.max_profiles)
  $index=0;for($i=0;$i -lt $script:entries.Count;$i++){if((Entry-Key $script:entries[$i]) -eq $selectKey){$index=$i}}
  $list.SelectedIndex=$index;$script:lastIndex=$index
 }finally{$script:loading=$false}
 Load-Entry $script:entries[$index]
}
function Confirm-Discard {
 if(-not $script:dirty -or $Verify -or $Preview){return $true}
 return [Windows.Forms.MessageBox]::Show($window,'当前服务有未保存的输入，放弃并切换吗？','未保存的配置','YesNo','Question') -eq 'Yes'
}
function Begin-Draft($presetId){
 if($script:job -or -not (Confirm-Discard)){return}
 $script:loading=$true;$list.ClearSelected();$script:lastIndex=-1;$script:loading=$false
 Load-Entry @{kind='new';preset=$presetId};$name.Focus()
}
function Start-ProfileRequest($operation){
 if($script:job){return}
 $payload=@{operation=$operation}
 if($operation -ne 'view'){$payload.revision=$script:data.revision}
 if($operation -in @('save','test')){
  $kind=$script:current.kind
  $payload.provider=if($kind -in @('profile','new')){'profile'}else{$kind}
  $payload.enabled=[bool]$custom.Checked;$payload.model=$model.Text;$payload.base_url=$url.Text;$payload.api_key=$key.Text
  $payload.name=$name.Text;$payload.vision=[bool]$vision.Checked
  $payload.profile_id=if($kind -eq 'profile'){$script:current.id}else{''}
  $payload.preset=if($kind -eq 'new'){[string]$script:current.preset}elseif($kind -eq 'profile'){[string]$script:current.data.preset}else{''}
 }elseif($operation -eq 'delete'){$payload.profile_id=$script:current.id}
 try{
  $info=New-Object Diagnostics.ProcessStartInfo
  $info.FileName='python';$info.Arguments='"'+(Join-Path $PSScriptRoot 'backend_profile.py')+'" --root "'+$ConfigRoot+'"'
  $info.UseShellExecute=$false;$info.CreateNoWindow=$true
  $info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
  $info.StandardOutputEncoding=New-Object Text.UTF8Encoding($false)
  $info.StandardErrorEncoding=New-Object Text.UTF8Encoding($false)
  $info.EnvironmentVariables['PYTHONIOENCODING']='utf-8'
  $process=New-Object Diagnostics.Process;$process.StartInfo=$info;[void]$process.Start()
  $writer=New-Object IO.StreamWriter($process.StandardInput.BaseStream,(New-Object Text.UTF8Encoding($false)))
  $writer.Write(($payload|ConvertTo-Json -Compress));$writer.Dispose()
  $script:job=@{process=$process;output=$process.StandardOutput.ReadToEndAsync();error=$process.StandardError.ReadToEndAsync();operation=$operation;started=[DateTime]::UtcNow}
  $status.Text=switch($operation){'test'{'正在测试连接，请稍候…'};'save'{'正在保存配置…'};'delete'{'正在删除…'};default{'正在读取配置…'}}
 }catch{if($Preview){throw};$status.Text='无法启动配置服务，请检查 Python 和程序文件。'}
 finally{$payload=$null;Set-EnabledState}
}
function Request-Apply {
 $runtime=Join-Path $ConfigRoot '.runtime'
 try{
  $s=Get-Content -LiteralPath (Join-Path $runtime 'status.json') -Raw -Encoding UTF8|ConvertFrom-Json
  $now=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  if(-not $s.backend_switch_available -or $now-$s.heartbeat -gt 8 -or $s.state -in @('stopped','stopping')){
   $status.Text='已保存。后台未就绪或正在处理任务；空闲后再次点“保存并应用”。';return
  }
  $id=[Guid]::NewGuid().ToString('N');$tmp=Join-Path $runtime ('command-'+$id+'.tmp')
  $content=@{pid=$s.pid;at=$now;action='profile:reload'}|ConvertTo-Json -Compress
  [IO.File]::WriteAllText($tmp,$content,(New-Object Text.UTF8Encoding($false)))
  [IO.File]::Move($tmp,(Join-Path $runtime ('command-'+$id+'.json')))
  $script:controlPending=@{id=$id;at=$now};$status.Text='已保存，正在等待后台应用…'
 }catch{$status.Text='已保存。后台启动或重读配置后生效。'}
 Set-EnabledState
}
function Poll-Profile {
 if($script:job){
  $j=$script:job
  if(-not $j.process.HasExited -and ([DateTime]::UtcNow-$j.started).TotalSeconds -gt 35){
   $j.process.Kill();$status.Text='操作超时，请检查网络或本机配置。'
  }
  if($j.process.HasExited){
   try{
    $answer=$j.output.GetAwaiter().GetResult()|ConvertFrom-Json
    if(-not $answer.ok){if($Preview){throw 'Profile view failed'};$status.Text=$answer.message}
    elseif($j.operation -eq 'view'){$script:data=$answer;Fill-List $(if($Verify -or $VerifyConnection -or $VerifyApply){'deepseek'}else{[string]$answer.active_backend});$status.Text=''}
    elseif($j.operation -eq 'save'){
     $script:data=$answer;$saved=[string]$answer.saved_id
     Fill-List $saved
     $script:applyHint=if($saved -notin @('claude','deepseek') -and $saved -ne [string]$answer.active_backend){'可在运行中心或托盘切换到「'+$script:current.title+'」。'}else{''}
     $status.Text='配置已保存';if(-not $Verify){Request-Apply}
    }
    elseif($j.operation -eq 'delete'){$script:data=$answer;Fill-List ([string]$answer.active_backend);$status.Text=$answer.message}
    else{$status.Text=$answer.message}
   }catch{if($Preview){throw};$status.Text='配置服务未正常返回；请检查文件与网络。'}
   finally{$j.process.Dispose();$script:job=$null;Set-EnabledState}
  }
 }
 if($script:controlPending){
  $pending=$script:controlPending;$reply=Join-Path $ConfigRoot ('.runtime/reply-'+$pending.id+'.json')
  if(Test-Path -LiteralPath $reply){
   try{$answer=Get-Content -LiteralPath $reply -Raw -Encoding UTF8|ConvertFrom-Json;$status.Text=[string]$answer.message+$(if($script:applyHint){'；'+$script:applyHint}else{''});Remove-Item -LiteralPath $reply}catch{$status.Text='已保存，但未能读取后台确认。'}
   $script:controlPending=$null;Set-EnabledState
  }elseif([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()-$pending.at -gt 25){$status.Text='已保存，后台未确认应用；请检查运行中心。';$script:controlPending=$null;Set-EnabledState}
 }
}
function Mark-Dirty {if(-not $script:loading){$script:dirty=$true;Show-Dirty}}
$list.Add_SelectedIndexChanged({
 if($script:loading -or $list.SelectedIndex -lt 0){return}
 if(-not (Confirm-Discard)){$script:loading=$true;if($script:lastIndex -ge 0){$list.SelectedIndex=$script:lastIndex}else{$list.ClearSelected()};$script:loading=$false;return}
 $script:lastIndex=$list.SelectedIndex;Load-Entry $list.SelectedItem.Tag
})
$addButton.Add_Click({
 if(-not $script:data){return}
 $addMenu.Items.Clear()
 foreach($p in @($script:data.presets)){$item=$addMenu.Items.Add([string]$p.name+'　'+[string]$p.vendor);$item.Tag=[string]$p.id;$item.Image=[TrayDesign]::Glyph('cube',[TrayDesign]::MutedColor,36);$item.Add_Click({Begin-Draft $this.Tag})}
 $null=$addMenu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
 $item=$addMenu.Items.Add('自定义接口…');$item.Tag='custom';$item.Image=[TrayDesign]::Glyph('settings',[TrayDesign]::MutedColor,36);$item.Add_Click({Begin-Draft 'custom'})
 $g=$window.CreateGraphics();$scale=$g.DpiX/96;$g.Dispose();[TrayMenuStyle]::Apply($addMenu,$scale)
 $addMenu.Show($addButton,(New-Object Drawing.Point(0,0)),[Windows.Forms.ToolStripDropDownDirection]::AboveRight)
})
$delete.Add_Click({
 if($script:current.kind -ne 'profile'){return}
 if($Verify -or [Windows.Forms.MessageBox]::Show($window,('删除“'+$script:current.title+'”及其保存的 Key？此操作不可撤销。'),'删除服务','YesNo','Warning') -eq 'Yes'){Start-ProfileRequest 'delete'}
})
$custom.Add_CheckedChanged({Mark-Dirty;Set-EnabledState})
foreach($control in @($name,$url,$model,$key)){$control.Add_TextChanged({Mark-Dirty})}
$vision.Add_CheckedChanged({Mark-Dirty})
$test.Add_Click({Start-ProfileRequest 'test'});$save.Add_Click({Start-ProfileRequest 'save'})
$window.Add_FormClosing({param($sender,$e)
 if($script:job -or $script:controlPending){$status.Text='请等待当前操作完成后关闭。';$e.Cancel=$true;return}
 if($script:dirty -and -not $Preview -and -not $Verify){if([Windows.Forms.MessageBox]::Show($window,'有未保存的配置，确定放弃并关闭吗？','后端配置','YesNo','Question') -ne 'Yes'){$e.Cancel=$true}}
})
[TrayDesign]::StyleFields($window)
foreach($box in @($name,$url,$key)){$field=[TrayDesign]::Frame($box);$field.Height=36}
[TrayDesign]::SaveShortcut($window,$save)
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.PerformAutoScale()
$list.ItemHeight=[Math]::Min(255,[int]($list.Font.Height*2.7))
$timer=New-Object Windows.Forms.Timer;$timer.Interval=150;$timer.Add_Tick({Poll-Profile});$timer.Start()
Set-EnabledState;Start-ProfileRequest 'view'
if($Preview -or $Verify -or $VerifyConnection -or $VerifyApply){
 $window.Show();$deadline=[DateTime]::UtcNow.AddSeconds(10)
 while($script:job -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 100;Poll-Profile;[Windows.Forms.Application]::DoEvents()}
 if(-not $script:data){throw 'Profile view failed'}
 if($PreviewClaude){$list.SelectedIndex=0;[Windows.Forms.Application]::DoEvents()}
 if($key.Text -ne '' -or -not $key.UseSystemPasswordChar){throw 'Saved key must never be prefilled'}
 foreach($c in @($save,$test,$delete,$formCard,$listCard)){if($c.Bounds.Bottom -gt $window.ClientSize.Height -or $c.Bounds.Right -gt $window.ClientSize.Width){throw 'Profile layout is clipped'}}
 if($VerifyApply){
  $save.PerformClick();$deadline=[DateTime]::UtcNow.AddSeconds(30)
  while(($script:job -or $script:controlPending) -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 100;Poll-Profile;[Windows.Forms.Application]::DoEvents()}
  if($status.Text -notlike '*已保存并应用*'){throw ('Apply button failed: '+$status.Text)}
  Write-Output 'PASS save and apply button with existing key retained, backend acknowledged'
 }elseif($VerifyConnection){
  $test.PerformClick();$deadline=[DateTime]::UtcNow.AddSeconds(30)
  while($script:job -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 100;Poll-Profile;[Windows.Forms.Application]::DoEvents()}
  if($status.Text -notlike '*连接测试通过*'){throw ('Connection button failed: '+$status.Text)}
  Write-Output 'PASS real connection button using stored key; no configuration saved'
 }elseif($Preview){
  $image=New-Object Drawing.Bitmap($window.Width,$window.Height);$window.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)))
  $image.Save((Join-Path $PSScriptRoot '.runtime/backend-editor-preview.png'));$image.Dispose()
 }else{
  function Wait-Job {$deadline=[DateTime]::UtcNow.AddSeconds(10);while($script:job -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 100;Poll-Profile;[Windows.Forms.Application]::DoEvents()}}
  $key.Text='sk-ui-isolated-placeholder-12345678';$save.PerformClick();Wait-Job
  if(-not $script:data.deepseek.configured -or $key.Text -ne '' -or $script:dirty){throw 'Profile UI save failed'}
  Write-Output 'PASS profile UI: masked empty field, stdin save, refreshed configured status and cleared key'
  Begin-Draft 'kimi';$key.Text='kimi-ui-isolated-placeholder-12345678';$save.PerformClick();Wait-Job
  $saved=@($script:data.profiles | Where-Object {$_.id -eq 'kimi'})
  if($saved.Count -ne 1 -or -not $saved[0].configured -or $script:current.id -ne 'kimi' -or $key.Text -ne ''){throw ('Preset profile save failed: '+$status.Text)}
  Write-Output 'PASS preset profile: draft from menu entry, saved with encrypted key and selected in the list'
  $delete.PerformClick();Wait-Job
  if(@($script:data.profiles).Count -ne 0){throw ('Profile delete failed: '+$status.Text)}
  Write-Output 'PASS profile delete: entry and key removed, list refreshed'
 }
}else{[Windows.Forms.Application]::Run($window)}
$timer.Stop();$timer.Dispose();$key.Clear();$window.Dispose()
