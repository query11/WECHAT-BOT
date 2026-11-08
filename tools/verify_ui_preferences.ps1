param([string]$Output)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot '..\desktop\ui_preferences.ps1')
$fixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('ui-preferences-'+[Guid]::NewGuid().ToString('N'))
$path=Join-Path $fixtureRoot '.runtime\ui-preferences\bounds.json'
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))|Out-Null
function Fixture-Form {
 $form=New-Object Windows.Forms.Form;$form.ClientSize=New-Object Drawing.Size(640,480);$form.MinimumSize=New-Object Drawing.Size(400,300)
 return $form
}
try{
 [IO.File]::WriteAllText($path,'not json')
 $form=Fixture-Form;Register-UiPreferences $form $fixtureRoot 'bounds';$form.Show();[Windows.Forms.Application]::DoEvents()
 if($form.ClientSize.Width -ne 640){throw 'Malformed preferences changed dimensions'}
 $form.Dispose()
 [IO.File]::WriteAllText($path,'{"version":1,"width":8000,"height":8000,"maximized":false,"tab":999}')
 $form=Fixture-Form;Register-UiPreferences $form $fixtureRoot 'bounds';$form.Show();[Windows.Forms.Application]::DoEvents()
 $area=[Windows.Forms.Screen]::FromHandle($form.Handle).WorkingArea
 if($form.Width -gt $area.Width -or $form.Height -gt $area.Height){throw 'Oversized dimensions not clamped to screen'}
 $form.Dispose()
 [IO.File]::WriteAllText($path,'{"version":1,"width":640,"height":480,"maximized":false,"tab":1}')
 $form=Fixture-Form;$tabs=New-Object Windows.Forms.TabControl;$tabs.Dock='Fill';$form.Controls.Add($tabs)
 $null=$tabs.TabPages.Add('first');$null=$tabs.TabPages.Add('second');$tabs.SelectedIndex=0
 Register-UiPreferences $form $fixtureRoot 'bounds' $true $tabs @{} @{} $false
 if($tabs.SelectedIndex -ne 0){throw 'Explicit entry page was overridden'}
 $form.Show();[Windows.Forms.Application]::DoEvents();Save-UiPreferences
 $saved=Read-UiPreferences $path
 if(-not $saved -or $saved.version -ne 1){throw 'Atomic replacement did not leave valid JSON'}
 $form.Dispose()
 $result=@{ok=$true;corrupt_fallback=$true;screen_clamp=$true;explicit_page=$true;atomic_save=$true;root=$fixtureRoot}
 if($Output){[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Output)))|Out-Null;[IO.File]::WriteAllText($Output,($result|ConvertTo-Json),(New-Object Text.UTF8Encoding($false)))}
 $result|ConvertTo-Json
}finally{if($form -and -not $form.IsDisposed){$form.Dispose()}}
