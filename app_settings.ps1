param([switch]$Preview,[switch]$VerifyPersistence,[switch]$Verify,[string]$ConfigRoot=$PSScriptRoot)
$ErrorActionPreference='Stop'
if($VerifyPersistence -and [IO.Path]::GetFullPath($ConfigRoot) -eq [IO.Path]::GetFullPath($PSScriptRoot)){throw 'VerifyPersistence requires isolated ConfigRoot'}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
[TrayDesign]::EnableDpi();[Windows.Forms.Application]::EnableVisualStyles()
$window=New-Object Windows.Forms.Form
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.AutoScaleMode='Dpi'
$window.Text='微信助手 · 常用应用';$window.ClientSize=New-Object Drawing.Size(760,600)
$window.MinimumSize=New-Object Drawing.Size(776,639);$window.StartPosition='CenterScreen'
$iconPath=Join-Path $PSScriptRoot 'wechat-assistant-v2.ico';if(Test-Path -LiteralPath $iconPath){$window.Icon=New-Object Drawing.Icon($iconPath)}
$window.BackColor=[TrayDesign]::PageColor;$window.ForeColor=[TrayDesign]::Heading;$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
function Label($text,$x,$y,$w,$h,$size=10){
 $l=New-Object Windows.Forms.Label;$l.Text=$text;$l.AutoEllipsis=$true;$l.SetBounds($x,$y,$w,$h);$l.Font=New-Object Drawing.Font('Microsoft YaHei UI',$size)
 $window.Controls.Add($l);return $l
}
$title=Label '常用应用与别名' 26 20 690 40 16;$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold);$title.Anchor='Top,Left,Right'
$intro=Label '勾选收藏，常用应用优先显示。选中应用后可设置简称。' 28 68 690 30;$intro.ForeColor=[TrayDesign]::MutedColor;$intro.Anchor='Top,Left,Right'
$list=New-Object Windows.Forms.ListView
$list.SetBounds(28,110,704,310);$list.View='Details';$list.CheckBoxes=$true;$list.FullRowSelect=$true
$list.MultiSelect=$false;$list.HideSelection=$false;$list.Anchor='Top,Bottom,Left,Right'
$null=$list.Columns.Add('应用',210);$null=$list.Columns.Add('别名',170);$null=$list.Columns.Add('目标 / 状态',290)
$window.Controls.Add($list)
function Size-AppColumns { $width=$list.ClientSize.Width-24;$list.Columns[0].Width=[int]($width*.30);$list.Columns[1].Width=[int]($width*.25);$list.Columns[2].Width=[Math]::Max(100,$width-$list.Columns[0].Width-$list.Columns[1].Width) }
$list.Add_SizeChanged({Size-AppColumns});$window.Add_Shown({Size-AppColumns})
$aliasLabel=Label '所选应用别名（多个用逗号分隔，每个最多 20 字）' 28 436 704 26
$aliases=New-Object Windows.Forms.TextBox;$aliases.SetBounds(28,467,704,30);$aliases.Enabled=$false;$window.Controls.Add($aliases)
$status=Label '正在读取应用…' 28 509 704 35 9
$reload=New-Object TrayButton;$reload.Text='重新读取';$reload.SetBounds(28,548,130,36);$window.Controls.Add($reload)
$save=New-Object TrayButton;$save.Text='保存';$save.Primary=$true;$save.SetBounds(596,548,136,36);$window.Controls.Add($save)
$aliasLabel.Anchor='Bottom,Left,Right';$aliases.Anchor='Bottom,Left,Right';$status.Anchor='Bottom,Left,Right';$reload.Anchor='Bottom,Left';$save.Anchor='Bottom,Right'
$script:dirty=$false;$script:data=$null;$script:selected=$null;$script:loading=$false;$script:job=$null
function Update-AppActions {
 $ready=($null -ne $script:data -and -not $script:job)
 $save.Enabled=$ready -and $script:dirty;$reload.Enabled=-not $script:job;$list.Enabled=$ready
 $aliases.Enabled=$ready -and $script:selected -and $script:selected.Tag.available
 $save.Text=if($script:job -and $script:job.action -eq 'save'){'正在保存…'}else{'保存修改'}
 $reload.Text=if($script:job -and $script:job.action -eq 'view'){'正在读取…'}else{'重新读取'}
}
function Commit-Aliases {
 if($script:selected -and -not $script:loading){
  $updated=@($aliases.Text -split '[,，]' | ForEach-Object {$_.Trim()} | Where-Object {$_})
   if(($script:selected.Tag.aliases -join ',') -cne ($updated -join ',')){$script:dirty=$true;$status.Text='有未保存的收藏或别名 · Ctrl+S 保存';$status.ForeColor=[TrayDesign]::Blue}
  $script:selected.Tag.aliases=$updated
  $script:selected.SubItems[1].Text=$script:selected.Tag.aliases -join '、'
 }
 Update-AppActions
}
function Populate($data){
 $script:loading=$true
 try{
  $script:data=$data;$script:selected=$null;$aliases.Clear();$aliases.Enabled=$false;$list.Items.Clear()
  foreach($app in $data.apps){
   $item=New-Object Windows.Forms.ListViewItem([string]$app.name);$item.Tag=$app;$item.Checked=[bool]$app.favorite
   $null=$item.SubItems.Add(($app.aliases -join '、'))
   $null=$item.SubItems.Add($(if($app.available){[string]$app.target}else{'已失效 · 保存时移除'}))
   if(-not $app.available){$item.ForeColor=[Drawing.Color]::Gray}
   $null=$list.Items.Add($item)
  }
  }finally{$script:loading=$false;$script:dirty=$false;Update-AppActions}
}
$list.Add_SelectedIndexChanged({
 if($script:loading){return};Commit-Aliases;$script:selected=$null
 if($list.SelectedItems.Count){$script:selected=$list.SelectedItems[0]}
 $script:loading=$true
 try{$aliases.Text=if($script:selected){$script:selected.Tag.aliases -join ', '}else{''};$aliases.Enabled=($null -ne $script:selected -and $script:selected.Tag.available)}finally{$script:loading=$false}
})
$aliases.Add_TextChanged({Commit-Aliases})
$list.Add_ItemChecked({if(-not $script:loading){$script:dirty=$true;$status.Text='有未保存的收藏或别名 · Ctrl+S 保存';$status.ForeColor=[TrayDesign]::Blue;Update-AppActions}})
function Request($action){
 if($script:job){return};Commit-Aliases
 $payload=@{action=$action}
 if($action -eq 'save'){
  $payload.revision=$script:data.revision;$payload.favorites=@();$payload.aliases=@{}
  foreach($item in $list.Items){
   if(-not $item.Tag.available){continue}
   if($item.Checked){$payload.favorites+=,$item.Tag.id}
   foreach($alias in $item.Tag.aliases){
    if($payload.aliases.ContainsKey($alias)){$status.Text='别名重复，请为每个应用设置不同简称';return}
    $payload.aliases[$alias]=$item.Tag.id
   }
  }
 }
 $info=New-Object Diagnostics.ProcessStartInfo
 $info.FileName='python';$info.Arguments='"'+(Join-Path $PSScriptRoot 'app_preferences.py')+'" --root "'+$ConfigRoot+'"'
 $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
 $info.StandardOutputEncoding=New-Object Text.UTF8Encoding($false);$info.StandardErrorEncoding=New-Object Text.UTF8Encoding($false);$info.EnvironmentVariables['PYTHONIOENCODING']='utf-8'
 $p=New-Object Diagnostics.Process;$p.StartInfo=$info
 try{
  [void]$p.Start();$writer=New-Object IO.StreamWriter($p.StandardInput.BaseStream,(New-Object Text.UTF8Encoding($false)))
  $writer.Write(($payload|ConvertTo-Json -Depth 8 -Compress));$writer.Dispose()
  $script:job=@{process=$p;output=$p.StandardOutput.ReadToEndAsync();error=$p.StandardError.ReadToEndAsync();action=$action}
   Update-AppActions;$status.Text=if($action -eq 'save'){'正在保存修改…'}else{'正在读取应用…'}
 }catch{$status.Text='读取或保存失败，请重试';$p.Dispose()}
}
$timer=New-Object Windows.Forms.Timer;$timer.Interval=200
$timer.Add_Tick({
 if(-not $script:job -or -not $script:job.process.HasExited){return}
 try{
  $result=$script:job.output.Result|ConvertFrom-Json
  if($result.ok){Populate $result;$status.Text=if($script:job.action -eq 'save'){'已保存，即时生效'}else{'已读取；失效应用保存时移除'}}else{$status.Text=[string]$result.error}
 }catch{$status.Text='读取失败，请重新读取'}finally{
   $script:job.process.Dispose();$script:job=$null;Update-AppActions
 }
})
$reload.Add_Click({if($script:dirty -and [Windows.Forms.MessageBox]::Show($window,'放弃未保存的修改并重新读取吗？','常用应用','YesNo','Question') -ne 'Yes'){return};Request 'view'});$save.Add_Click({Request 'save'})
$window.Add_FormClosing({param($sender,$event)
 if($script:job){$status.Text='请等待读取或保存完成后再关闭。';$event.Cancel=$true;return}
  if($script:dirty -and -not $Verify -and -not $Preview -and [Windows.Forms.MessageBox]::Show($window,'有未保存的收藏或别名，确定放弃吗？','常用应用','YesNo','Question') -ne 'Yes'){$event.Cancel=$true}
})
$window.Add_FormClosed({$timer.Stop();$timer.Dispose();if($script:job){$script:job.process.Dispose()}})
[TrayDesign]::StyleFields($window);[TrayDesign]::SaveShortcut($window,$save);Update-AppActions
if($Verify){
 # Offline visual/control test: no discovery, process execution, or persisted writes.
 Populate ([pscustomobject]@{revision=0;apps=@([pscustomobject]@{id='fake';name='示例应用';target='隔离测试';available=$true;favorite=$true;aliases=@('示例')})})
 $window.Show();[Windows.Forms.Application]::DoEvents()
 if($list.Items.Count -ne 1 -or -not $list.Items[0].Checked){throw 'UI verification failed'}
 $window.Close();Write-Output 'app_settings UI verified (isolated fake catalog)';exit
}
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.PerformAutoScale()
if($Preview -or $VerifyPersistence){
 $timer.Start();$window.Show();Request 'view';$deadline=[DateTime]::UtcNow.AddSeconds(30)
 while($script:job -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 100;[Windows.Forms.Application]::DoEvents()}
 if($script:job -or -not $script:data){throw 'App catalog preview failed'}
 if($VerifyPersistence){
   if($save.Enabled){throw 'Unchanged preferences must not enable save'}
  $before=$script:data.revision;$list.Items[0].Selected=$true;[Windows.Forms.Application]::DoEvents()
  $list.Items[0].Checked=$true;$aliases.Text='界面验证工具';$save.PerformClick()
  $deadline=[DateTime]::UtcNow.AddSeconds(30)
  while($script:job -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 100;[Windows.Forms.Application]::DoEvents()}
   if($script:job -or $script:data.revision -ne ($before+1) -or $script:dirty){throw ('App settings save failed: '+$status.Text)}
   if($save.Enabled){throw 'Saved preferences must reset save availability'}
  Write-Output 'PASS favorites UI: select, favorite, alias, atomic save and refresh in isolated root'
 }else{
  $image=New-Object Drawing.Bitmap($window.Width,$window.Height)
  $window.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)))
  $image.Save((Join-Path $PSScriptRoot '.runtime/app-settings-preview.png'));$image.Dispose()
 }
 $window.Close()
}else{$window.Add_Shown({Request 'view'});$timer.Start();[void]$window.ShowDialog()}

