param([switch]$Preview,[switch]$PreviewClaude,[switch]$VerifyConnection,[switch]$VerifyApply,[switch]$Verify,[string]$ConfigRoot=$PSScriptRoot)
$ErrorActionPreference='Stop'
if($Verify -and [IO.Path]::GetFullPath($ConfigRoot) -eq [IO.Path]::GetFullPath($PSScriptRoot)){throw 'Verify requires an isolated ConfigRoot'}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
[TrayDesign]::EnableDpi();[Windows.Forms.Application]::EnableVisualStyles()
$window=New-Object Windows.Forms.Form
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.AutoScaleMode='Dpi'
$window.Text='微信助手 · 后端配置';$window.ClientSize=New-Object Drawing.Size(680,590)
$window.MinimumSize=New-Object Drawing.Size(696,629)
$window.StartPosition='CenterScreen';$window.BackColor=[Drawing.ColorTranslator]::FromHtml('#F3F5F9');$window.ForeColor=[Drawing.ColorTranslator]::FromHtml('#202D40')
$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$window.Icon=New-Object Drawing.Icon((Join-Path $PSScriptRoot 'wechat-assistant-v2.ico'))
function Add-Label($text,$x,$y,$w,$h,$size=10){
 $l=New-Object Windows.Forms.Label;$l.Text=$text;$l.SetBounds($x,$y,$w,$h)
 $l.Font=New-Object Drawing.Font('Microsoft YaHei UI',$size);$l.AutoEllipsis=$true;$window.Controls.Add($l);return $l
}
$title=Add-Label '后端配置' 26 22 620 40 16;$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold)
$intro=Add-Label '分别保存两套配置；不会自动切换当前后端。' 28 68 620 28 10
$intro.ForeColor=[Drawing.Color]::SlateGray
$null=Add-Label '选择后端' 28 116 135 30
$provider=New-Object Windows.Forms.ComboBox;$provider.DropDownStyle='DropDownList'
$provider.Items.AddRange(@('DeepSeek 官方','Claude'));$provider.SetBounds(176,112,474,32);$window.Controls.Add($provider)
$custom=New-Object Windows.Forms.CheckBox;$custom.Text='使用自定义 Claude API（Anthropic 兼容）'
$custom.SetBounds(176,156,474,28);$window.Controls.Add($custom)
$null=Add-Label '接口地址' 28 202 135 30
$url=New-Object Windows.Forms.TextBox;$url.SetBounds(176,198,474,30);$url.MaxLength=1000;$window.Controls.Add($url)
$null=Add-Label '模型名称' 28 247 135 30
$model=New-Object Windows.Forms.ComboBox;$model.SetBounds(176,243,474,32);$window.Controls.Add($model)
$null=Add-Label 'API Key' 28 296 135 30
$key=New-Object Windows.Forms.TextBox;$key.SetBounds(176,292,474,30);$key.UseSystemPasswordChar=$true;$key.MaxLength=4096;$window.Controls.Add($key)
$keyState=Add-Label '正在读取配置…' 176 330 474 44 9;$keyState.ForeColor=[Drawing.Color]::SlateGray
$showKey=New-Object Windows.Forms.CheckBox;$showKey.Text='显示本次输入的 Key';$showKey.SetBounds(176,378,474,28);$window.Controls.Add($showKey)
$showKey.Add_CheckedChanged({$key.UseSystemPasswordChar=-not $showKey.Checked})
$notice=Add-Label 'Key 留空会保留已有值。连接测试会调用所选接口，产生少量用量。' 28 426 622 44 9
$notice.ForeColor=[Drawing.Color]::SlateGray
$status=Add-Label '' 28 474 622 50 9;$status.ForeColor=[Drawing.ColorTranslator]::FromHtml('#2563EB')
$test=New-Object TrayButton;$test.Text='测试连接';$test.SetBounds(28,538,138,36);$window.Controls.Add($test)
$save=New-Object TrayButton;$save.Text='保存并应用';$save.Primary=$true;$save.SetBounds(484,538,166,36);$window.Controls.Add($save)
$script:data=$null;$script:job=$null;$script:dirty=$false;$script:loading=$false;$script:controlPending=$null
function Set-EnabledState {
 $ready=($null -ne $script:data) -and (-not $script:job) -and (-not $script:controlPending)
 $isDeep=$provider.SelectedIndex -eq 0;$editing=$isDeep -or $custom.Checked
 $provider.Enabled=$ready;$custom.Enabled=$ready -and (-not $isDeep)
 $url.Enabled=$ready -and (-not $isDeep) -and $editing;$url.ReadOnly=$isDeep
 $model.Enabled=$ready -and $editing;$key.Enabled=$ready -and $editing;$showKey.Enabled=$key.Enabled
 $test.Enabled=$ready -and $editing;$save.Enabled=$ready
 $test.Text=if($script:job -and $script:job.operation -eq 'test'){'正在测试…'}else{'测试连接'}
 $save.Text=if($script:job -and $script:job.operation -eq 'save'){'正在保存…'}elseif($script:controlPending){'正在应用…'}else{'保存并应用'}
}
function Load-Profile {
 if(-not $script:data){return}
 $script:loading=$true
 try{
  $isDeep=$provider.SelectedIndex -eq 0;$p=if($isDeep){$script:data.deepseek}else{$script:data.claude}
  $custom.Visible=-not $isDeep;$custom.Checked=[bool]$p.enabled
  $url.Text=[string]$p.base_url;$model.Items.Clear()
  if($isDeep){$model.DropDownStyle='DropDownList';$model.Items.AddRange(@('deepseek-flash','deepseek-v4-pro'));$model.SelectedItem=[string]$p.model}
  else{$model.DropDownStyle='DropDown';$model.Text=[string]$p.model}
  $key.Clear();$showKey.Checked=$false
  $keyState.Text=if($p.configured){'已配置 Key；留空保留，输入新值则替换。'}else{'尚未配置 Key，请填写。'}
  if(-not $p.configured -and $p.encrypted){$keyState.Text='已有加密 Key，但当前账户无法解密，请重新输入。'}
  if($p.configured -and $p.encrypted){$keyState.Text='已配置 Key，受 Windows 用户加密保护；留空保留。'}
  if($isDeep -and $p.env_override){$keyState.Text='当前 Key 来自环境变量；请先移除变量，再在此更换。'}
  if(-not $isDeep -and -not $p.enabled){$keyState.Text='当前沿用原 Claude 设置，不读取或显示其中的密钥。'}
  $script:dirty=$false
  $intro.Text='分别保存两套配置；不会自动切换当前后端。';$intro.ForeColor=[Drawing.Color]::SlateGray
 }finally{$script:loading=$false;Set-EnabledState}
}
function Mark-ProfileDirty {
 if(-not $script:loading){
  $script:dirty=$true;$intro.Text='有未保存的配置 · Ctrl+S 保存并应用';$intro.ForeColor=[Drawing.ColorTranslator]::FromHtml('#2563EB')
 }
}
function Start-ProfileRequest($operation){
 if($script:job){return}
 $payload=@{operation=$operation}
 if($operation -ne 'view'){
  $payload.provider=if($provider.SelectedIndex -eq 0){'deepseek'}else{'claude'}
  $payload.enabled=[bool]$custom.Checked;$payload.model=$model.Text;$payload.base_url=$url.Text
  $payload.api_key=$key.Text;$payload.revision=$script:data.revision
 }
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
  $status.Text=if($operation -eq 'test'){'正在测试连接，请稍候…'}elseif($operation -eq 'save'){'正在保存配置…'}else{'正在读取配置…'}
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
    elseif($j.operation -eq 'view'){$script:data=$answer;if($provider.SelectedIndex -lt 0){$provider.SelectedIndex=0};Load-Profile;$status.Text=''}
    elseif($j.operation -eq 'save'){$script:data=$answer;Load-Profile;$status.Text='配置已保存';if(-not $Verify){Request-Apply}}
    else{$status.Text=$answer.message}
   }catch{if($Preview){throw};$status.Text='配置服务未正常返回；请检查文件与网络。'}
   finally{$j.process.Dispose();$script:job=$null;Set-EnabledState}
  }
 }
 if($script:controlPending){
  $pending=$script:controlPending;$reply=Join-Path $ConfigRoot ('.runtime/reply-'+$pending.id+'.json')
  if(Test-Path -LiteralPath $reply){
   try{$answer=Get-Content -LiteralPath $reply -Raw -Encoding UTF8|ConvertFrom-Json;$status.Text=$answer.message;Remove-Item -LiteralPath $reply}catch{$status.Text='已保存，但未能读取后台确认。'}
   $script:controlPending=$null;Set-EnabledState
  }elseif([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()-$pending.at -gt 25){$status.Text='已保存，后台未确认应用；请检查运行中心。';$script:controlPending=$null;Set-EnabledState}
 }
}
$provider.Add_SelectedIndexChanged({
 if($script:loading){return}
 if($script:dirty -and -not $Verify -and -not $Preview){
  if([Windows.Forms.MessageBox]::Show($window,'当前后端有未保存的输入，放弃并切换配置页吗？','未保存的配置','YesNo','Question') -ne 'Yes'){
   $script:loading=$true;$provider.SelectedIndex=$script:lastProvider;$script:loading=$false;return
  }
 }
 $script:lastProvider=$provider.SelectedIndex;Load-Profile
})
$custom.Add_CheckedChanged({Mark-ProfileDirty;Set-EnabledState})
foreach($control in @($url,$model,$key)){$control.Add_TextChanged({Mark-ProfileDirty})}
$test.Add_Click({Start-ProfileRequest 'test'});$save.Add_Click({Start-ProfileRequest 'save'})
$window.Add_FormClosing({param($sender,$e)
 if($script:job -or $script:controlPending){$status.Text='请等待当前操作完成后关闭。';$e.Cancel=$true;return}
 if($script:dirty -and -not $Preview -and -not $Verify){if([Windows.Forms.MessageBox]::Show($window,'有未保存的配置，确定放弃并关闭吗？','后端配置','YesNo','Question') -ne 'Yes'){$e.Cancel=$true}}
})
foreach($control in @($provider,$custom,$url,$model,$key,$keyState,$showKey)){$control.Anchor='Top,Left,Right'}
$title.Anchor='Top,Left,Right';$intro.Anchor='Top,Left,Right';$notice.Anchor='Bottom,Left,Right';$status.Anchor='Bottom,Left,Right';$test.Anchor='Bottom,Left';$save.Anchor='Bottom,Right'
[TrayDesign]::StyleFields($window);[TrayDesign]::SaveShortcut($window,$save)
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.PerformAutoScale()
$timer=New-Object Windows.Forms.Timer;$timer.Interval=150;$timer.Add_Tick({Poll-Profile});$timer.Start()
Set-EnabledState;Start-ProfileRequest 'view'
if($Preview -or $Verify -or $VerifyConnection -or $VerifyApply){
 $window.Show();$deadline=[DateTime]::UtcNow.AddSeconds(10)
 while($script:job -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 100;Poll-Profile;[Windows.Forms.Application]::DoEvents()}
 if(-not $script:data){throw 'Profile view failed'}
 if($PreviewClaude){$provider.SelectedIndex=1;[Windows.Forms.Application]::DoEvents()}
 if($key.Text -ne '' -or -not $key.UseSystemPasswordChar){throw 'Saved key must never be prefilled'}
 if($save.Bounds.Bottom -gt $window.ClientSize.Height -or $key.Bounds.Right -gt $window.ClientSize.Width){throw 'Profile fields are clipped'}
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
  $key.Text='sk-ui-isolated-placeholder-12345678';$save.PerformClick()
  $deadline=[DateTime]::UtcNow.AddSeconds(10)
  while($script:job -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 100;Poll-Profile;[Windows.Forms.Application]::DoEvents()}
  if(-not $script:data.deepseek.configured -or $key.Text -ne '' -or $script:dirty){throw 'Profile UI save failed'}
  Write-Output 'PASS profile UI: masked empty field, stdin save, refreshed configured status and cleared key'
 }
}else{[Windows.Forms.Application]::Run($window)}
$timer.Stop();$timer.Dispose();$key.Clear();$window.Dispose()
