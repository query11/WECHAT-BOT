param([ValidateSet('','save','read')][string]$PreferenceProbe='',[string]$Root=$PSScriptRoot,[string]$TaskId='',[switch]$Verify,[switch]$Preview)
$ErrorActionPreference='Stop'
if($PreferenceProbe -and [IO.Path]::GetFullPath($Root) -eq [IO.Path]::GetFullPath($PSScriptRoot)){throw 'Preference probe requires isolated Root'}
if(($Verify -or $Preview) -and [IO.Path]::GetFullPath($Root) -eq [IO.Path]::GetFullPath($PSScriptRoot)){throw 'Verification requires an isolated Root'}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'ui_preferences.ps1')
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
[TrayDesign]::EnableDpi();[Windows.Forms.Application]::EnableVisualStyles()
$window=New-Object Windows.Forms.Form
$window.Text='微信助手 · 任务模板';$window.ClientSize=New-Object Drawing.Size(900,720)
$window.MinimumSize=New-Object Drawing.Size(916,759);$window.StartPosition='CenterScreen'
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.AutoScaleMode='Dpi'
$window.BackColor=[TrayDesign]::PageColor;$window.ForeColor=[TrayDesign]::Heading
$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$icon=Join-Path $PSScriptRoot 'wechat-assistant-v2.ico';if(Test-Path -LiteralPath $icon){$window.Icon=New-Object Drawing.Icon($icon)}
function Label($text,$x,$y,$w,$h,$size=10){
 $c=New-Object Windows.Forms.Label;$c.Text=$text;$c.SetBounds($x,$y,$w,$h);$c.AutoEllipsis=$true
 $c.Font=New-Object Drawing.Font('Microsoft YaHei UI',$size);$window.Controls.Add($c);return $c
}
function Button($text,$x,$y,$w){$c=New-Object TrayButton;$c.Text=$text;$c.SetBounds($x,$y,$w,36);$window.Controls.Add($c);return $c}
$title=Label '任务模板' 24 18 600 38 16;$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold)
$intro=Label '把常用要求保存下来，下次在微信直接按模板处理。' 26 62 840 26 10;$intro.ForeColor=[TrayDesign]::MutedColor
$ownerLabel=Label '微信用户' 26 103 90 28
$owner=New-Object Windows.Forms.ComboBox;$owner.DropDownStyle='DropDownList';$owner.SetBounds(122,99,516,30);$owner.Anchor='Top,Left,Right';$window.Controls.Add($owner)
$reload=Button '重新读取' 720 97 154;$reload.Anchor='Top,Right'
$list=New-Object Windows.Forms.ListView;$list.View='Details';$list.FullRowSelect=$true;$list.MultiSelect=$false;$list.HideSelection=$false
$list.SetBounds(26,149,240,475);$list.Anchor='Top,Bottom,Left';$null=$list.Columns.Add('模板名称',158);$null=$list.Columns.Add('版本',58);$window.Controls.Add($list)
function Size-TemplateColumns {if(Set-RememberedColumns $list){return};$width=$list.ClientSize.Width-24;$list.Columns[0].Width=[int]($width*.65);$list.Columns[1].Width=[Math]::Max(70,$width-$list.Columns[0].Width)}
$list.Add_SizeChanged({Size-TemplateColumns});$window.Add_Shown({Size-TemplateColumns})
$new=Button '新建' 26 636 112;$new.Anchor='Bottom,Left'
$duplicate=Button '复制模板' 150 636 116;$duplicate.Anchor='Bottom,Left'
$fields=@{}
$labels=@{}
foreach($spec in @(@('name','模板名称',149,30,40),@('inputs','输入要求',211,62,2000),@('steps','处理步骤',305,108,8000),@('output_dir','输出目录',445,30,2000),@('output_format','输出格式',507,30,1000),@('checks','检查条件（可选）',569,55,4000))){
 $key=[string]$spec[0];$y=[int]$spec[2]
 $label=Label $spec[1] 290 ($y-25) 584 24 10;$label.Anchor='Top,Left,Right';$labels[$key]=$label
 $box=New-Object Windows.Forms.TextBox;$box.SetBounds(290,$y,584,[int]$spec[3]);$box.MaxLength=[int]$spec[4];$box.Anchor='Top,Left,Right'
 if([int]$spec[3] -gt 30){$box.Multiline=$true;$box.ScrollBars='Vertical';$box.AcceptsReturn=$true}
 $window.Controls.Add($box);$fields[$key]=$box
}
$help=New-Object Windows.Forms.ToolTip;$help.SetToolTip($fields['output_dir'],'填写“工作目录”或工作目录内的子目录，例如 工作目录/周报。')
$help.SetToolTip($fields['checks'],"每行一条规则，例如：`r`n必需列 | 汇总.csv | - | 项目,金额`r`n列合计 | 汇总.csv | - | 金额 | 40")
$checksHelp=New-Object Windows.Forms.LinkLabel;$checksHelp.Text='规则示例';$checksHelp.SetBounds(788,544,86,24);$checksHelp.Anchor='Top,Right';$window.Controls.Add($checksHelp)
$checksHelp.Add_LinkClicked({[void][Windows.Forms.MessageBox]::Show($window,"每行一条，字段之间用 | 分隔：`r`n`r`n必需列 | 汇总.csv | - | 项目,金额`r`n列合计 | 汇总.csv | - | 金额 | 40`r`n必需章节 | 报告.md | 结论,建议`r`n`r`nXLSX 的第三项填写工作表名称，CSV 填 -。",'检查条件示例','OK','Information')})
$delete=Button '删除模板' 290 636 128;$delete.Anchor='Bottom,Left';$delete.Danger=$true
$save=Button '保存新模板' 704 636 170;$save.Anchor='Bottom,Right';$save.Primary=$true
$feedback=Label '正在读取模板…' 26 680 848 30 9;$feedback.Anchor='Bottom,Left,Right';$feedback.ForeColor=[TrayDesign]::MutedColor
$script:data=$null;$script:job=$null;$script:loading=$false;$script:dirty=$false;$script:current=$null;$script:users=@();$script:feedbackError=$false
function Update-Actions {
 $ready=($script:data -and $script:data.ok -and -not $script:job)
 foreach($box in $fields.Values){$box.Enabled=[bool]$ready}
 $owner.Enabled=[bool]$ready;$list.Enabled=[bool]$ready;$new.Enabled=[bool]$ready;$duplicate.Enabled=[bool]($ready -and $script:current)
 $delete.Enabled=[bool]($ready -and $script:current);$save.Enabled=[bool]($ready -and $script:dirty)
 $reload.Enabled=-not $script:job
 $fields['name'].ReadOnly=[bool]$script:current
 $save.Text=if($script:job -and $script:job.action -eq 'save'){'正在保存…'}elseif($script:current){'保存修改'}else{'保存新模板'}
 $help.SetToolTip($fields['name'],$(if($script:current){'修改现有模板保留名称；需要另一个名称时点击“复制模板”。'}else{'名称为 1 至 40 个字，不含空格。'}))
}
function Discard-Changes {
 if(-not $script:dirty -or $Verify -or $Preview){return $true}
 return [Windows.Forms.MessageBox]::Show($window,'有未保存的模板内容，放弃修改吗？','任务模板','YesNo','Question') -eq 'Yes'
}
function Fill-Editor($template,$draft=$null){
 $script:loading=$true
 try {
  $script:current=$template
  $definition=if($draft){$draft.definition}elseif($template){$template.definition}else{$null}
  $fields['name'].Text=if($draft){[string]$draft.name}elseif($template){[string]$template.name}else{''}
  foreach($key in @('inputs','steps','output_dir','output_format','checks')){$fields[$key].Text=if($definition){[string]$definition.$key}elseif($key -eq 'output_dir'){'工作目录'}else{''}}
  $script:dirty=[bool]$draft
 }finally{$script:loading=$false;Update-Actions}
}
function Populate($data,$selectName=''){
 $script:loading=$true
 try{
  $script:data=$data;$owner.Items.Clear();$script:users=@($data.users)
  foreach($uid in $script:users){$null=$owner.Items.Add([string]$uid)}
  $owner.SelectedIndex=[Array]::IndexOf($script:users,[string]$data.uid)
  $list.Items.Clear()
  foreach($template in @($data.templates)){
   $item=New-Object Windows.Forms.ListViewItem([string]$template.name);$null=$item.SubItems.Add('第 '+$template.revision+' 版');$item.Tag=$template;$null=$list.Items.Add($item)
   if($template.name -eq $selectName){$item.Selected=$true}
  }
 }finally{$script:loading=$false}
 $chosen=@($data.templates | Where-Object {$_.name -eq $selectName}) | Select-Object -First 1
 Fill-Editor $chosen
 if($data.draft){Fill-Editor $null $data.draft}
}
function Request($action){
 if($script:job){return}
 $payload=@{operation=$action}
 if($script:data){$payload.uid=$script:data.uid;$payload.source=$script:data.source}
 if($action -eq 'seed'){$payload.task_id=$TaskId}
 if($action -in @('save','delete')){
  $payload.name=$fields['name'].Text.Trim();$payload.expected_revision=if($script:current){[long]$script:current.revision}else{0}
  if($action -eq 'save'){
   $payload.definition=@{}
   foreach($key in @('inputs','steps','output_dir','output_format','checks')){$payload.definition[$key]=$fields[$key].Text.Trim()}
  }
 }
 $info=New-Object Diagnostics.ProcessStartInfo;$info.FileName='python';$info.Arguments='"'+(Join-Path $PSScriptRoot 'template_desktop.py')+'" --root "'+$Root.TrimEnd('\')+'"'
 $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
 $info.StandardOutputEncoding=New-Object Text.UTF8Encoding($false);$info.StandardErrorEncoding=New-Object Text.UTF8Encoding($false);$info.EnvironmentVariables['PYTHONIOENCODING']='utf-8'
 $process=New-Object Diagnostics.Process;$process.StartInfo=$info
 try{
  [void]$process.Start();$writer=New-Object IO.StreamWriter($process.StandardInput.BaseStream,(New-Object Text.UTF8Encoding($false)))
  $writer.Write(($payload|ConvertTo-Json -Depth 8 -Compress));$writer.Dispose()
  $script:job=@{process=$process;output=$process.StandardOutput.ReadToEndAsync();error=$process.StandardError.ReadToEndAsync();watch=[Diagnostics.Stopwatch]::StartNew();action=$action;name=$payload.name}
  $feedback.Text=if($action -eq 'save'){'正在保存模板…'}elseif($action -eq 'delete'){'正在删除模板…'}else{'正在读取模板…'}
 }catch{$process.Dispose();$feedback.Text='操作未能确认，请重新读取后核对。'}
 Update-Actions
}
$timer=New-Object Windows.Forms.Timer;$timer.Interval=100
$timer.Add_Tick({
 if(-not $script:job){return};$job=$script:job
 if($job.watch.ElapsedMilliseconds -gt 28000 -or ($job.process.HasExited -and $job.output.IsCompleted)){
  try{
   if(-not $job.process.HasExited){$job.process.Kill();throw 'timeout'}
   $data=ConvertFrom-Json -InputObject $job.output.GetAwaiter().GetResult()
   $script:lastResponse=$data
   if($data.ok){$select=if($data.template){$data.template.name}elseif($job.action -eq 'view' -and $script:current){$script:current.name}else{''};Populate $data $select}
   elseif($job.action -in @('view','seed')){$script:data=$null}
   $feedback.Text=[string]$data.message;$script:feedbackError=-not $data.ok
  }catch{if($job.action -in @('view','seed')){$script:data=$null};$script:feedbackError=$true;$feedback.Text='操作未能确认，请重新读取后核对；不会自动重试。'}
  finally{$job.process.Dispose();$script:job=$null;Update-Actions}
 }
})
foreach($box in $fields.Values){$box.Add_TextChanged({if(-not $script:loading){$script:dirty=$true;$feedback.Text='有未保存的模板内容 · Ctrl+S 保存';Update-Actions}})}
$new.Add_Click({if(Discard-Changes){$script:loading=$true;foreach($item in $list.Items){$item.Selected=$false};$script:loading=$false;Fill-Editor $null;$fields['name'].Focus()}})
$duplicate.Add_Click({if($script:current -and (Discard-Changes)){$draft=@{name=([string]$script:current.name).Substring(0,[Math]::Min(36,$script:current.name.Length))+'副本';definition=$script:current.definition};Fill-Editor $null $draft;$feedback.Text='已复制为草稿，请修改名称后保存。'}})
$list.Add_SelectedIndexChanged({
 if($script:loading -or -not $list.SelectedItems.Count){return}
 $item=$list.SelectedItems[0]
 if($script:current -and $item.Tag.id -eq $script:current.id){return}
 if(Discard-Changes){Fill-Editor $item.Tag;$feedback.Text='已读取第 '+$item.Tag.revision+' 版。'}
 else{$script:loading=$true;foreach($row in $list.Items){$row.Selected=($script:current -and $row.Tag.id -eq $script:current.id)};$script:loading=$false}
})
$owner.Add_SelectedIndexChanged({
 if($script:loading -or $owner.SelectedIndex -lt 0){return}
 $uid=[string]$script:users[$owner.SelectedIndex]
 if($uid -eq $script:data.uid){return}
 if(Discard-Changes){$script:data.uid=$uid;Fill-Editor $null;Request 'view'}else{$script:loading=$true;$owner.SelectedIndex=[Array]::IndexOf($script:users,[string]$script:data.uid);$script:loading=$false}
})
$reload.Add_Click({if(Discard-Changes){Request 'view'}})
$save.Add_Click({Request 'save'})
$delete.Add_Click({if($script:current -and ($Verify -or [Windows.Forms.MessageBox]::Show($window,('删除模板“'+$script:current.name+'”？已经排队的任务不受影响。'),'删除模板','YesNo','Question') -eq 'Yes')){Request 'delete'}})
$window.KeyPreview=$true;$window.Add_KeyDown({param($sender,$event) if($event.Control -and $event.KeyCode -eq 'S' -and $save.Enabled){$save.PerformClick();$event.SuppressKeyPress=$true}})
$window.Add_FormClosing({param($sender,$event) if($script:job){$feedback.Text='请等待当前操作完成后关闭。';$event.Cancel=$true}elseif(-not (Discard-Changes)){$event.Cancel=$true}})
$window.Add_Disposed({$timer.Stop();$timer.Dispose();$help.Dispose()})
[TrayDesign]::StyleFields($window);$null=[TrayDesign]::Frame($list);foreach($box in @($fields.Values)){$null=[TrayDesign]::Frame($box)};$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.PerformAutoScale();$timer.Start()
Register-UiPreferences $window $Root 'template-editor' (-not ($Verify -or $Preview)) $null @{} @{templates=$list}
function Wait-Request {
 $deadline=[DateTime]::UtcNow.AddSeconds(30)
 while($script:job -and [DateTime]::UtcNow -lt $deadline){[Windows.Forms.Application]::DoEvents();[Threading.Thread]::Sleep(10)}
 if($script:job -or $script:feedbackError){throw ('Template request failed: '+$feedback.Text)}
}
Request $(if($TaskId){'seed'}else{'view'})
if($PreferenceProbe){. (Join-Path $PSScriptRoot 'tools/ui_preference_probe.ps1');Invoke-UiPreferenceProbe $window $Root $PreferenceProbe;exit}
if($Verify -or $Preview){
 try{
  $window.Show();Wait-Request
  if($TaskId -and (-not $script:dirty -or -not $fields['steps'].Text)){throw 'Task draft did not populate editable form'}
  if($Verify){
   if(-not $window.Visible -or -not $window.IsHandleCreated){throw 'Template window not visible'}
   foreach($label in @($title,$intro,$ownerLabel)+@($labels.Values)){
    if(-not $label.Visible -or $label.Height -lt $label.Font.Height){throw 'Template label is invisible or clipped'}
   }
   $new.PerformClick();$fields['name'].Text='界面验收模板';$fields['inputs'].Text='本次已确认的报表';$fields['steps'].Text='核对输入并生成汇总';$fields['output_format'].Text='TXT';$save.PerformClick();Wait-Request
   if($script:current.revision -ne 1){throw 'Template create failed'}
   $fields['steps'].Text='逐项核对输入并生成汇总';$save.PerformClick();Wait-Request
   if($script:current.revision -ne 2){throw 'Template edit failed'}
   $duplicate.PerformClick();$save.PerformClick();Wait-Request
   if($script:current.name -ne '界面验收模板副本' -or $script:current.revision -ne 1){throw 'Template copy failed'}
   $delete.PerformClick();Wait-Request
   if(@($script:data.templates | Where-Object {$_.name -eq '界面验收模板副本'}).Count){throw 'Template delete failed'}
   $item=@($list.Items | Where-Object {$_.Tag.name -eq '界面验收模板'})[0];$item.Selected=$true;[Windows.Forms.Application]::DoEvents()
   if(-not $fields['name'].ReadOnly -or $save.Text -ne '保存修改'){throw 'Template edit state incorrect'}
   Write-Output 'PASS template window: visible, create, edit, copy, delete, version update, Chinese form fields'
  }
  $bitmap=New-Object Drawing.Bitmap($window.Width,$window.Height)
  try{$window.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)));$bitmap.Save((Join-Path $Root '.runtime/template-editor-verified.png'))}finally{$bitmap.Dispose()}
 }finally{$window.Dispose()}
}else{[Windows.Forms.Application]::Run($window);$window.Dispose()}
