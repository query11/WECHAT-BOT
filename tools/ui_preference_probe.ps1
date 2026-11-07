function Invoke-UiPreferenceProbe($form,$root,$mode) {
 $form.Show();[Windows.Forms.Application]::DoEvents()
 $s=$script:uiPreferenceState;if(-not $s){throw 'Preferences not registered'}
 $proofPath=$s.path+'.probe.json'
 if($mode -eq 'save'){
  $s.timer.Stop()
  $form.WindowState='Normal';$form.ClientSize=New-Object Drawing.Size(($form.ClientSize.Width+32),($form.ClientSize.Height+24))
  if($s.tabs){$s.tabs.SelectedIndex=$s.tabs.TabPages.Count-1}
  foreach($choice in $s.choices.Values){$choice.SelectedIndex=$choice.Items.Count-1}
  foreach($entry in $s.lists.Values){
   if($entry.control.Parent -is [Windows.Forms.TabPage]){$s.tabs.SelectedTab=$entry.control.Parent;[Windows.Forms.Application]::DoEvents()}
   $entry.userChanging=$true;$entry.control.Columns[0].Width=[int]($entry.control.Columns[0].Width+28)
  }
  if($s.tabs){$s.tabs.SelectedIndex=$s.tabs.TabPages.Count-1}
  [Windows.Forms.Application]::DoEvents();Queue-UiPreferenceSave;Save-UiPreferences
  if(-not [IO.File]::Exists($s.path)){throw 'Preference file missing'}
  $record=Read-UiPreferences $s.path
  if($s.lists.Count -and $record.columns.psobject.Properties.Count -eq 0){throw 'User column widths were not captured'}
  [IO.File]::WriteAllText($proofPath,($record|ConvertTo-Json -Depth 6),(New-Object Text.UTF8Encoding($false)))
 }else{
  $expected=Read-UiPreferences $proofPath;$scale=Get-UiScale $form
  if(-not $expected){throw 'Expected saved preferences missing'}
  if([Math]::Abs($form.ClientSize.Width/$scale-$expected.width) -gt 2 -or [Math]::Abs($form.ClientSize.Height/$scale-$expected.height) -gt 2){throw 'Window dimensions were not restored'}
  if($s.tabs -and $s.tabs.SelectedIndex -ne $expected.tab){throw 'Last tab was not restored'}
  foreach($key in $s.choices.Keys){if($s.choices[$key].SelectedIndex -ne $expected.choices.$key){throw 'Filter choice was not restored'}}
  foreach($key in $s.lists.Keys){
   $entry=$s.lists[$key]
   if($entry.control.Parent -is [Windows.Forms.TabPage]){$s.tabs.SelectedTab=$entry.control.Parent;[Windows.Forms.Application]::DoEvents()}
   for($i=0;$i -lt $entry.control.Columns.Count;$i++){
    if([Math]::Abs($entry.control.Columns[$i].Width/$scale-$expected.columns.$key[$i]) -gt 2){throw ('Column width was not restored: '+$key)}
   }
  }
 }
 Write-Output ('PASS preference '+$mode+': '+[IO.Path]::GetFileName($s.path))
 $form.Dispose()
}
