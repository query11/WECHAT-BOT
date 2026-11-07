# Local presentation preferences only; never persists task text or execution state.
$script:uiPreferenceState=$null
function Get-UiScale($form) {
 $g=$form.CreateGraphics();try{return $g.DpiX/96.0}finally{$g.Dispose()}
}
function Read-UiPreferences($path) {
 try{
  if(-not [IO.File]::Exists($path) -or (Get-Item -LiteralPath $path).Length -gt 32768){return $null}
  $value=[IO.File]::ReadAllText($path,[Text.Encoding]::UTF8)|ConvertFrom-Json
  if($value.version -ne 1){return $null};return $value
 }catch{return $null}
}
function Test-UiNumber($value,$minimum,$maximum) {
 return (($value -is [int] -or $value -is [long] -or $value -is [double] -or $value -is [decimal]) -and [double]$value -ge $minimum -and [double]$value -le $maximum)
}
function Save-UiPreferences {
 $s=$script:uiPreferenceState
 if(-not $s -or $s.restoring -or $s.form.IsDisposed -or -not $s.normalSize){return}
 $mutex=$null;$locked=$false;$temp=$null
 try{
  $record=@{version=1;width=$s.normalSize[0];height=$s.normalSize[1];maximized=($s.form.WindowState -eq 'Maximized');columns=@{};choices=@{}}
  if($s.tabs){$record.tab=$s.tabs.SelectedIndex}
  foreach($key in $s.choices.Keys){$record.choices[$key]=$s.choices[$key].SelectedIndex}
  foreach($key in $s.lists.Keys){$entry=$s.lists[$key];if($entry.widths){$record.columns[$key]=@($entry.widths)}}
  $directory=[IO.Path]::GetDirectoryName($s.path);[IO.Directory]::CreateDirectory($directory)|Out-Null
  $sha=[Security.Cryptography.SHA256]::Create();try{$hash=([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($s.path.ToLowerInvariant())))).Replace('-','')}finally{$sha.Dispose()}
  $mutex=New-Object Threading.Mutex($false,('Local.WeChatUiPreferences'+$hash))
  try{$locked=$mutex.WaitOne(100)}catch [Threading.AbandonedMutexException]{$locked=$true}
  if(-not $locked){return}
  $temp=$s.path+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
  [IO.File]::WriteAllText($temp,($record|ConvertTo-Json -Depth 6),(New-Object Text.UTF8Encoding($false)))
  if([IO.File]::Exists($s.path)){[IO.File]::Replace($temp,$s.path,$null)}else{[IO.File]::Move($temp,$s.path)}
  $temp=$null
 }catch{
  # Preference failures must never block task controls or closing a window.
 }finally{
  if($temp -and [IO.File]::Exists($temp)){try{[IO.File]::Delete($temp)}catch{}}
  if($locked){$mutex.ReleaseMutex()};if($mutex){$mutex.Dispose()}
 }
}
function Queue-UiPreferenceSave {
 $s=$script:uiPreferenceState;if(-not $s -or $s.restoring){return}
 if($s.form.WindowState -eq 'Normal'){
  $scale=Get-UiScale $s.form
  $s.normalSize=@([Math]::Round($s.form.ClientSize.Width/$scale),[Math]::Round($s.form.ClientSize.Height/$scale))
 }
 $s.timer.Stop();$s.timer.Start()
}
function Set-RememberedColumns($list) {
 $s=$script:uiPreferenceState;if(-not $s){return $false}
 $entry=@($s.lists.Values | Where-Object {$_.control -eq $list}) | Select-Object -First 1
 if(-not $entry -or -not $entry.widths){return $false}
 $entry.applying=$true
 try{
  $scale=Get-UiScale $s.form
  for($i=0;$i -lt $list.Columns.Count;$i++){$list.Columns[$i].Width=[int]([Math]::Max(36,[double]$entry.widths[$i])*$scale)}
 }finally{$entry.applying=$false}
 return $true
}
function Register-UiPreferences($form,$root,$key,$enabled=$true,$tabs=$null,$choices=@{},$lists=@{},$restoreTab=$true) {
 if(-not $enabled){return}
 if($key -notmatch '^[a-z-]+$'){throw 'Invalid UI preference key'}
 $path=Join-Path $root ('.runtime\ui-preferences\'+$key+'.json')
 $saved=Read-UiPreferences $path
 $timer=New-Object Windows.Forms.Timer;$timer.Interval=700
 $script:uiPreferenceState=@{form=$form;path=$path;restoring=$true;normalSize=$null;timer=$timer;tabs=$tabs;choices=$choices;lists=@{}}
 $s=$script:uiPreferenceState
 foreach($name in $lists.Keys){
  $list=$lists[$name];$entry=@{control=$list;widths=$null;applying=$false;userChanging=$false};$s.lists[$name]=$entry
  $widths=@($saved.columns.$name)
  if($widths.Count -eq $list.Columns.Count -and -not @($widths|Where-Object {-not (Test-UiNumber $_ 36 2000)}).Count){$entry.widths=$widths}
  $list.Add_ColumnWidthChanging({param($sender,$event) $entry.userChanging=$true}.GetNewClosure())
  $list.Add_ColumnWidthChanged({param($sender,$event)
   if($entry.userChanging -and -not $entry.applying){
    $entry.userChanging=$false;$scale=Get-UiScale $form;$entry.widths=@($sender.Columns|ForEach-Object {[Math]::Max(36,[Math]::Round($_.Width/$scale))});Queue-UiPreferenceSave
   }
  }.GetNewClosure())
 }
 if($saved){
  $scale=Get-UiScale $form;$area=[Windows.Forms.Screen]::FromHandle($form.Handle).WorkingArea
  $frameW=$form.Width-$form.ClientSize.Width;$frameH=$form.Height-$form.ClientSize.Height
  if((Test-UiNumber $saved.width 320 8000) -and (Test-UiNumber $saved.height 240 8000)){
   $w=[Math]::Min([double]$saved.width*$scale,$area.Width-$frameW);$h=[Math]::Min([double]$saved.height*$scale,$area.Height-$frameH)
   $form.ClientSize=New-Object Drawing.Size([int]$w,[int]$h)
  }
  if($tabs -and $restoreTab -and (Test-UiNumber $saved.tab 0 ($tabs.TabPages.Count-1))){$tabs.SelectedIndex=[int]$saved.tab}
  foreach($name in $choices.Keys){$c=$choices[$name];$index=$saved.choices.$name;if(Test-UiNumber $index 0 ($c.Items.Count-1)){$c.SelectedIndex=[int]$index}}
  if($saved.maximized -is [bool] -and $saved.maximized){$form.WindowState='Maximized'}
 }
 $s.restoring=$false
 # Restore saved column widths after each window's initial layout handlers.
 $form.Add_Shown({foreach($entry in $script:uiPreferenceState.lists.Values){[void](Set-RememberedColumns $entry.control)};Queue-UiPreferenceSave})
 $form.Add_ResizeEnd({Queue-UiPreferenceSave});$form.Add_ClientSizeChanged({Queue-UiPreferenceSave})
 $form.Add_VisibleChanged({Queue-UiPreferenceSave})
 $form.Add_FormClosing({Save-UiPreferences})
 if($tabs){$tabs.Add_SelectedIndexChanged({Queue-UiPreferenceSave})}
 foreach($c in $choices.Values){$c.Add_SelectedIndexChanged({Queue-UiPreferenceSave})}
 $timer.Add_Tick({$script:uiPreferenceState.timer.Stop();Save-UiPreferences})
 $form.Add_Disposed({$script:uiPreferenceState.timer.Stop();$script:uiPreferenceState.timer.Dispose()})
 $scale=Get-UiScale $form
 $s.normalSize=if($saved -and (Test-UiNumber $saved.width 320 8000) -and (Test-UiNumber $saved.height 240 8000)){@([Math]::Round([Math]::Min([double]$saved.width,$form.ClientSize.Width/$scale)),[Math]::Round([Math]::Min([double]$saved.height,$form.ClientSize.Height/$scale)))}else{@([Math]::Round($form.ClientSize.Width/$scale),[Math]::Round($form.ClientSize.Height/$scale))}
}
