param([ValidateSet('','save','read')][string]$PreferenceProbe='',[switch]$Preview,[switch]$Verify,[switch]$VerifyActions,[switch]$SkipClipboardVerification,[string]$Root=$PSScriptRoot,[ValidatePattern('^$|^[a-f0-9]{32}$')][string]$FocusId='')
$ErrorActionPreference='Stop'
if($PreferenceProbe -and [IO.Path]::GetFullPath($Root) -eq [IO.Path]::GetFullPath($PSScriptRoot)){throw 'Preference probe requires isolated Root'}
if($SkipClipboardVerification -and -not $Verify){throw 'SkipClipboardVerification requires Verify'}
if($VerifyActions -and -not $Verify){throw 'VerifyActions requires Verify with an isolated runtime'}
if($Verify -and [IO.Path]::GetFullPath($Root) -eq [IO.Path]::GetFullPath($PSScriptRoot)){throw 'Verify requires an isolated Root'}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'ui_preferences.ps1')
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
[TrayDesign]::EnableDpi();[Windows.Forms.Application]::EnableVisualStyles()
$window=New-Object Windows.Forms.Form
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.AutoScaleMode='Dpi'
$window.Text='微信助手 · 任务记录';$window.ClientSize=New-Object Drawing.Size(900,760)
$window.MinimumSize=New-Object Drawing.Size(916,799);$window.StartPosition='CenterScreen'
$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$window.BackColor=[TrayDesign]::PageColor
$window.ForeColor=[TrayDesign]::Heading
$iconPath=Join-Path $PSScriptRoot 'wechat-assistant-v2.ico'
if(Test-Path -LiteralPath $iconPath){$window.Icon=New-Object Drawing.Icon($iconPath)}
function Add-Label($text,$x,$y,$w,$h,$size=10){
 $l=New-Object Windows.Forms.Label;$l.Text=$text;$l.SetBounds($x,$y,$w,$h)
 $l.Font=New-Object Drawing.Font('Microsoft YaHei UI',$size);$l.AutoEllipsis=$true;$window.Controls.Add($l);return $l
}
$title=Add-Label '任务记录' 26 20 400 42 16;$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold)
$intro=Add-Label '最近 200 条任务 · 选择记录查看结果与关联文件' 28 70 640 28 10
$intro.ForeColor=[TrayDesign]::MutedColor
$sourceTip=New-Object Windows.Forms.ToolTip
$script:historyEmpty='暂无任务记录。'
$script:focusPending=[bool]$FocusId
$refresh=New-Object TrayButton;$refresh.Text='刷新';$refresh.SetBounds(758,28,116,36);$refresh.Anchor='Top,Right';$window.Controls.Add($refresh)
$list=New-Object Windows.Forms.ListView
$list.View='Details';$list.FullRowSelect=$true;$list.MultiSelect=$false;$list.HideSelection=$false
$list.SetBounds(28,148,846,160);$list.Anchor='Top,Left,Right';$list.BorderStyle='FixedSingle'
$null=$list.Columns.Add('更新时间',172);$null=$list.Columns.Add('状态',174);$null=$list.Columns.Add('任务',390)
$window.Controls.Add($list)
$searchLabel=Add-Label '搜索' 28 110 52 28 10
$search=New-Object Windows.Forms.TextBox;$search.SetBounds(80,106,512,30);$search.Anchor='Top,Left,Right';$window.Controls.Add($search)
$filter=New-Object Windows.Forms.ComboBox;$filter.DropDownStyle='DropDownList';$filter.SetBounds(608,106,266,30);$filter.Anchor='Top,Right'
$filter.Items.AddRange(@('全部任务','需要处理','等待执行','正在处理','已结束'));$filter.SelectedIndex=0;$window.Controls.Add($filter)
function Size-HistoryColumns { if(Set-RememberedColumns $list){return}; $width=$list.ClientSize.Width-24;$list.Columns[0].Width=[int]($width*.25);$list.Columns[1].Width=[int]($width*.25);$list.Columns[2].Width=[Math]::Max(100,$width-$list.Columns[0].Width-$list.Columns[1].Width) }
$list.Add_SizeChanged({Size-HistoryColumns});$window.Add_Shown({Size-HistoryColumns})
$heading=Add-Label '结果摘要' 28 320 640 26 11;$heading.Font=New-Object Drawing.Font('Microsoft YaHei UI',11,[Drawing.FontStyle]::Bold)
$summaryState=Add-Label '' 28 350 846 24 9;$summaryState.Anchor='Top,Left,Right';$summaryState.ForeColor=[TrayDesign]::MutedColor
$details=New-Object Windows.Forms.TextBox;$details.Multiline=$true;$details.ReadOnly=$true;$details.ScrollBars='Vertical'
$details.SetBounds(28,378,846,48);$details.Anchor='Top,Left,Right';$window.Controls.Add($details)
$outputList=New-Object Windows.Forms.ListView;$outputList.View='Details';$outputList.FullRowSelect=$true;$outputList.MultiSelect=$false;$outputList.HideSelection=$false
$outputList.SetBounds(28,438,650,74);$outputList.Anchor='Top,Left,Right';$null=$outputList.Columns.Add('产物文件',300);$null=$outputList.Columns.Add('检查 / 记录',310);$window.Controls.Add($outputList)
$outputList.Add_SizeChanged({if(Set-RememberedColumns $outputList){return};$width=$outputList.ClientSize.Width-24;$outputList.Columns[0].Width=[int]($width*.48);$outputList.Columns[1].Width=[Math]::Max(80,$width-$outputList.Columns[0].Width)})
$locate=New-Object TrayButton;$locate.Text='打开文件位置';$locate.SetBounds(694,438,180,36);$locate.Anchor='Top,Right';$locate.Enabled=$false;$window.Controls.Add($locate)
$outputHint=Add-Label '选择文件可定位' 694 478 180 30 9;$outputHint.Anchor='Top,Right';$outputHint.ForeColor=[TrayDesign]::MutedColor
$tabs=New-Object Windows.Forms.TabControl;$tabs.SetBounds(28,526,846,126);$tabs.Anchor='Top,Bottom,Left,Right';$window.Controls.Add($tabs)
$actionHint=Add-Label '选择任务和操作后可查看下一步。' 28 658 846 26 9;$actionHint.Anchor='Bottom,Left,Right';$actionHint.ForeColor=[TrayDesign]::MutedColor
function Detail-Tab($name){
 $page=New-Object Windows.Forms.TabPage;$page.Text=$name;$page.Padding=New-Object Windows.Forms.Padding(8);$null=$tabs.TabPages.Add($page)
 $box=New-Object Windows.Forms.TextBox;$box.Multiline=$true;$box.ReadOnly=$true;$box.ScrollBars='Vertical';$box.Dock='Fill';$page.Controls.Add($box);return $box
}
$replyDetails=Detail-Tab '完整回复';$inputDetails=Detail-Tab '输入与模板';$checkDetails=Detail-Tab '检查结果'
$status=Add-Label '' 28 736 846 22 9;$status.Anchor='Bottom,Left,Right';$status.ForeColor=[TrayDesign]::MutedColor
$operationChoice=New-Object Windows.Forms.ComboBox;$operationChoice.DropDownStyle='DropDownList';$operationChoice.SetBounds(28,692,470,32);$operationChoice.Anchor='Bottom,Left,Right';$operationChoice.Enabled=$false;$window.Controls.Add($operationChoice)
$copy=New-Object TrayButton;$copy.Text='复制微信指令';$copy.SetBounds(514,692,164,36);$copy.Anchor='Bottom,Right';$copy.Enabled=$false;$window.Controls.Add($copy)
$execute=New-Object TrayButton;$execute.Text='选择操作';$execute.Primary=$true;$execute.SetBounds(694,692,180,36);$execute.Anchor='Bottom,Right';$execute.Enabled=$false;$window.Controls.Add($execute)
$templatesButton=New-Object TrayButton;$templatesButton.Text='任务模板';$templatesButton.SetBounds(606,28,136,36);$templatesButton.Anchor='Top,Right';$window.Controls.Add($templatesButton)
$revise=New-Object TrayButton;$revise.Text='继续修改';$revise.SetBounds(454,28,136,36);$revise.Anchor='Top,Right';$revise.Enabled=$false;$window.Controls.Add($revise)
$saveTemplate=New-Object TrayButton;$saveTemplate.Text='保存为模板';$saveTemplate.SetBounds(694,316,180,32);$saveTemplate.Anchor='Top,Right';$saveTemplate.Enabled=$false;$window.Controls.Add($saveTemplate)
function Open-Templates($taskId=''){
 try{
  $start=New-Object Diagnostics.ProcessStartInfo;$start.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
  $start.Arguments='-NoProfile -STA -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'template_editor.ps1')+'" -Root "'+$Root.TrimEnd('\')+'"'
  if($taskId -match '^[a-f0-9]{32}$'){$start.Arguments+=' -TaskId '+$taskId}
  $start.UseShellExecute=$false;$start.CreateNoWindow=$true;$p=[Diagnostics.Process]::Start($start);$p.Dispose()
 }catch{$status.Text='模板窗口打开失败，请重试。'}
}
$revise.Add_Click({
 if(-not $list.SelectedItems.Count){return};$id=[string]$list.SelectedItems[0].Tag.id
 if($id -notmatch '^[a-f0-9]{32}$'){return}
 try{
  $start=New-Object Diagnostics.ProcessStartInfo;$start.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
  $start.Arguments='-NoProfile -STA -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'workflow_window.ps1')+'" -Root "'+$Root.TrimEnd('\')+'" -Page revise -ItemId '+$id
  $start.UseShellExecute=$false;$start.CreateNoWindow=$true;$p=[Diagnostics.Process]::Start($start);$p.Dispose()
 }catch{$status.Text='修改窗口打开失败，请重试。'}
})
$templatesButton.Add_Click({Open-Templates})
$saveTemplate.Add_Click({if($list.SelectedItems.Count){Open-Templates ([string]$list.SelectedItems[0].Tag.id)}})
$outputList.Add_SelectedIndexChanged({$locate.Enabled=($outputList.SelectedItems.Count -gt 0 -and [bool]$outputList.SelectedItems[0].Tag)})
$locate.Add_Click({
 if(-not $outputList.SelectedItems.Count){return};$path=[string]$outputList.SelectedItems[0].Tag
 if($path -notmatch '^[A-Za-z]:[\\/]' -or $path.Contains('"') -or -not [IO.File]::Exists($path)){$status.Text='文件不存在或不是本机文件，无法定位。';return}
 Start-Process explorer.exe -ArgumentList ('/select,"'+$path+'"') | Out-Null
})
$states=@{queued='等待执行';running='正在处理';completed='处理结束';waiting='等待确认';failed='失败';cancelled='已取消';uncertain='结果不确定';recoverable='未开始，可手动恢复';interrupted='已中断'}
$deliveryStates=@{pending='等待发送确认';accepted='微信接口已接受';unconfirmed='文字送达未确认'}
$resolutionStates=@{checked='已人工核对';abandoned='已放弃恢复';archived='已归档'}
$script:operationCommands=@()
$script:historyActions=@()
$script:directActions=@();$script:historySource=$null;$script:historyRows=@();$script:historyRead=$null;$script:historyRequest=$null
function Selected-DirectAction {
 if($operationChoice.SelectedIndex -lt 0 -or -not $list.SelectedItems.Count){return ''}
 $command=[string]$script:operationCommands[$operationChoice.SelectedIndex]
 if($command -match '^/resume [a-f0-9]{32}$'){return 'resume'}
 if($command -match '^/queue cancel [a-f0-9]{32}$'){return 'cancel_queued'}
 if($command -match '^/resolve [a-f0-9]{32} (checked|abandoned|archived)$'){return $Matches[1]}
 return ''
}
function Update-DirectAction {
 $action=Selected-DirectAction
 $actionNames=@{resume='恢复这条任务';cancel_queued='取消排队';checked='标记已核对';abandoned='放弃恢复';archived='归档记录'}
 $execute.Text=if($actionNames.ContainsKey([string]$action)){$actionNames[$action]}elseif($operationChoice.SelectedIndex -ge 0){'请在微信操作'}else{'选择操作'}
 $reason=''
 if($script:historyRequest){$reason='正在等待后台确认…';$execute.Text='正在提交…'}
 elseif($script:historyRead){$reason='正在刷新任务记录…'}
 elseif(-not $list.SelectedItems.Count){$reason='请先选择任务。'}
 elseif(-not $action){$reason=if($operationChoice.SelectedIndex -ge 0){'复制左侧微信指令后发送，即可完成此操作。'}else{'这条任务暂无可用操作。'}}
 elseif($script:directActions -notcontains $action){$reason='后台未运行或尚未加载此操作，请启动或重启后台后刷新。'}
 elseif($action -eq 'resume' -and $script:historySource.runtime_state -in @('standby','draining','stopping')){$reason='请先在微信说“开始对话”，再恢复这条任务。'}
 $execute.Enabled=($reason -eq '');$sourceTip.SetToolTip($execute,$reason)
 $actionHint.Text=if($reason){$reason}elseif($action -eq 'checked'){'请先核对实际文件或应用结果，再标记已核对。'}elseif($action -eq 'resume'){'恢复后继续使用原任务所属用户的微信通道。'}elseif($action -eq 'cancel_queued'){'只取消尚未开始的这条任务。'}elseif($action -eq 'abandoned'){'放弃恢复后，这条记录不能再恢复执行。'}else{'归档保留原始状态，文件仍然保留。'}
 if($reason -and -not $script:historyFeedback){$status.Text=$reason}
}
function Add-HistoryOperation($label,$command) {
 $null=$operationChoice.Items.Add($label);$script:operationCommands+=@([string]$command)
}
function Get-ClipboardSnapshot {
 for($attempt=0;$attempt -lt 6;$attempt++){
  try{return [Windows.Forms.Clipboard]::GetDataObject()}
  catch{if($attempt -eq 5){throw};[Threading.Thread]::Sleep(100);[Windows.Forms.Application]::DoEvents()}
 }
}
function Show-Details {
 $execute.Enabled=$false;$copy.Enabled=$false;$locate.Enabled=$false;$saveTemplate.Enabled=$false;$revise.Enabled=$false;$outputList.Items.Clear();$summaryState.Text='';$replyDetails.Clear();$inputDetails.Clear();$checkDetails.Clear()
 $operationChoice.Enabled=$false;$operationChoice.Items.Clear();$script:operationCommands=@()
 if($list.SelectedItems.Count -eq 0){$details.Text=if($list.Items.Count){'选择一条任务查看详情。'}else{$script:historyEmpty};Update-DirectAction;return}
 $r=$list.SelectedItems[0].Tag
 $summary=if([string]::IsNullOrWhiteSpace([string]$r.summary)){'该记录暂无详细结果。'}else{[string]$r.summary}
 $seen=@{}
 foreach($artifact in @($r.artifacts)){
  $path=[string]$artifact.path;$label=if($artifact.label){[string]$artifact.label}else{[string]$artifact.name}
  $item=New-Object Windows.Forms.ListViewItem($label);$null=$item.SubItems.Add([string]$artifact.reason);$item.Tag=$path;$null=$outputList.Items.Add($item)
  if($path){$seen[$path]=$true}
 }
 foreach($path in @($r.outputs)){
  if($seen.ContainsKey([string]$path)){continue}
  $item=New-Object Windows.Forms.ListViewItem([IO.Path]::GetFileName([string]$path));$null=$item.SubItems.Add('关联文件');$item.Tag=[string]$path;$null=$outputList.Items.Add($item)
 }
 if($r.revision -and $r.revision.files){
  foreach($f in @($r.revision.files)){
   $item=New-Object Windows.Forms.ListViewItem([string]$f.name);$null=$item.SubItems.Add('旧版本副本');$item.Tag=[string]$f.path;$null=$outputList.Items.Add($item)
  }
 }
 if($r.local_files -and $r.local_files.files){
  foreach($f in @($r.local_files.files)){
   $item=New-Object Windows.Forms.ListViewItem([string]$f.name);$null=$item.SubItems.Add('本机输入副本');$item.Tag=[string]$f.path;$null=$outputList.Items.Add($item)
  }
 }
 $revise.Enabled=($script:historySource.desktop_workflow -and $r.state -in @('completed','failed','cancelled','uncertain','interrupted') -and ($r.result_text -or @($r.artifacts | Where-Object {$_.status -eq 'valid'}).Count))
 if($outputList.Items.Count){$outputList.Items[0].Selected=$true;$outputHint.Text='选择文件可定位'}else{$outputHint.Text='暂无关联文件'}
 $saveTemplate.Enabled=($script:historySource.template_editor -and $r.state -notin @('queued','running','waiting','recoverable'))
 $execution=if($states.ContainsKey([string]$r.execution_state)){$states[[string]$r.execution_state]}else{'旧记录未区分'}
 $delivery=if($deliveryStates.ContainsKey([string]$r.delivery_state)){$deliveryStates[[string]$r.delivery_state]}else{'暂无文字发送回执'}
 $resolution=if($resolutionStates.ContainsKey([string]$r.resolution)){$resolutionStates[[string]$r.resolution]}else{'尚未人工结清'}
 if($r.resolved_at -gt 0){$resolution+=' · '+[DateTimeOffset]::FromUnixTimeSeconds([long]$r.resolved_at).LocalDateTime.ToString('yyyy-MM-dd HH:mm')}
 $fullResult=if([string]::IsNullOrWhiteSpace([string]$r.result_text)){'完整文字仅保留 7 天；该记录暂无可用缓存。'}else{[string]$r.result_text}
 $summaryState.Text='执行：'+$execution+'   文字：'+$delivery+'   结清：'+$resolution
 $details.Text=$summary.Replace("`r`n","`n").Replace("`n","`r`n")
 $replyDetails.Text=$fullResult.Replace("`r`n","`n").Replace("`n","`r`n")
 $inputDetails.Text='任务：'+[string]$r.label+"`r`n编号："+[string]$r.id
 if($r.local_files -and $r.local_files.output_dir){
  $inputDetails.Text+="`r`n`r`n本机文件任务`r`n本次要求："+$r.local_files.body+"`r`n结果去向："+$r.local_files.delivery+"`r`n结果目录："+$r.local_files.output_dir
  $inputDetails.Text+="`r`n确认时保存的输入：`r`n"+(@($r.local_files.files | ForEach-Object {$_.name+' · '+$_.path}) -join "`r`n")
 }elseif($r.local_files.error){$inputDetails.Text+="`r`n"+$r.local_files.error}
 if($r.revision -and $r.revision.parent_id){
  $inputDetails.Text+="`r`n`r`n本次结果修改`r`n基于："+$r.revision.source_label+"`r`n上个版本："+$r.revision.parent_id+"`r`n最初任务："+$r.revision.root_id+"`r`n本次要求："+$r.revision.instructions+"`r`n新结果目录："+$r.revision.output_dir
  $inputDetails.Text+="`r`n旧版本副本：`r`n"+(@($r.revision.files | ForEach-Object {$_.name+' · '+$_.path}) -join "`r`n")
 }elseif($r.revision.error){$inputDetails.Text+="`r`n"+$r.revision.error}
 $checkDetails.Text='没有配置结果检查。文件生成状态与内容检查结论分别记录。'
 if($r.template -and $r.template.name){
  $templateDefinition=$r.template.definition
  $inputDetails.Text+="`r`n`r`n本次任务模板（接收时固定，恢复仍使用此版本）`r`n"+[string]$r.template.name+' · 第 '+[string]$r.template.revision+" 版`r`n输入要求："+[string]$templateDefinition.inputs+"`r`n处理步骤："+[string]$templateDefinition.steps+"`r`n输出目录："+[string]$templateDefinition.output_dir+"`r`n输出格式："+[string]$templateDefinition.output_format
  if($templateDefinition.checks){$inputDetails.Text+="`r`n检查条件：`r`n"+[string]$templateDefinition.checks}
 }
 if(@($r.result_checks).Count){
  $checkLines=@();$checkStates=@{passed='通过';failed='未通过';unverified='未能检查'}
  $checkKinds=@{required_columns='必需列';column_sum='列合计';required_sections='必需章节'}
  foreach($check in @($r.result_checks)){
    $checkState=if($checkStates.ContainsKey([string]$check.status)){$checkStates[[string]$check.status]}else{'未能检查'}
    $checkKind=if($checkKinds.ContainsKey([string]$check.kind)){$checkKinds[[string]$check.kind]}else{'检查条件'}
    $condition=if($check.condition){' · '+[string]$check.condition}else{''}
    $checkLines+=($checkState+' · '+$checkKind+' · '+[string]$check.file+$condition+'：'+[string]$check.detail)
  }
  $checkDetails.Text="结果检查（独立于文件生成状态）`r`n"+($checkLines -join "`r`n")
 }
 if(@($r.inputs).Count){
  $inputLines=@()
  foreach($batchInput in @($r.inputs)){
   $inputName=if($batchInput.orig){[string]$batchInput.orig}else{[string]$batchInput.name}
   $inputRole=if($batchInput.role -eq 'template'){'模板'}else{'输入资料'}
   $inputLines+=([string]$batchInput.index+'. '+$inputName+' · '+$inputRole)
   $inputLines+=[string]$batchInput.path
  }
  $inputDetails.Text+="`r`n`r`n本次批次输入（确认时固定，恢复仍使用此清单）`r`n"+($inputLines -join "`r`n")
 }
 if(@($r.artifacts).Count){
  $artifactLines=@();$artifactNumber=0
  foreach($artifact in @($r.artifacts)){
   $artifactNumber++
   $artifactName=if($artifact.label){[string]$artifact.label}else{[string]$artifact.name}
   $artifactLines+=([string]$artifactNumber+'. '+$artifactName+' · '+[string]$artifact.reason)
   if($artifact.path){$artifactLines+=[string]$artifact.path}
  }
  $checkDetails.Text+="`r`n`r`n任务产物（记录时核验，取回前重新检查）`r`n"+($artifactLines -join "`r`n")
 }
 if([string]$r.id -match '^[a-fA-F0-9]{32}$'){
  if($r.state -eq 'queued' -and $script:directActions -contains 'cancel_queued'){Add-HistoryOperation '取消这条排队任务' ('/queue cancel '+$r.id)}
  if($r.state -eq 'recoverable' -and -not $r.resolution){Add-HistoryOperation '恢复未开始的任务' ('/resume '+$r.id)}
  if($r.state -notin @('queued','running','waiting')){
   if(@($r.artifacts).Count -and $script:historyActions -contains 'artifacts'){Add-HistoryOperation '查看任务产物清单' ('/artifacts '+$r.id)}
   if($script:historyActions -contains 'getfile'){
    $artifactNumber=0
    foreach($artifact in @($r.artifacts)){
     $artifactNumber++
     if($artifact.status -eq 'valid'){Add-HistoryOperation ('取回第 '+$artifactNumber+' 个产物（先开始对话）') ('/getfile '+$r.id+' '+$artifactNumber)}
    }
   }
   if($r.resend_available -and $script:historyActions -contains 'resend'){Add-HistoryOperation '补发未确认文字（可能重复最后一段）' ('/resend '+$r.id)}
   if(-not [string]::IsNullOrWhiteSpace([string]$r.result_text) -and $script:historyActions -contains 'result'){Add-HistoryOperation '查看保存的完整文字' ('/result '+$r.id)}
   if(-not $r.resolution -and $script:historyActions -contains 'resolve'){
    if($r.state -in @('uncertain','interrupted')){Add-HistoryOperation '我已检查实际结果，标记已核对' ('/resolve '+$r.id+' checked')}
    if($r.state -eq 'recoverable'){Add-HistoryOperation '放弃恢复这条未开始任务' ('/resolve '+$r.id+' abandoned')}
    Add-HistoryOperation '归档记录，保留原始状态' ('/resolve '+$r.id+' archived')
   }
  }
 }
 if($operationChoice.Items.Count){$operationChoice.Enabled=$true;$operationChoice.SelectedIndex=0;$copy.Enabled=$true}
 Update-DirectAction
}
function Render-History {
 $selected=if($list.SelectedItems.Count){[string]$list.SelectedItems[0].Tag.id}else{''}
 $top=if($list.TopItem){[string]$list.TopItem.Tag.id}else{''}
  $list.BeginUpdate()
  try {
   $list.Items.Clear()
   foreach($r in $script:historyRows){
    if($null -eq $r){continue}
    $query=$search.Text.Trim()
    if($query -and (([string]$r.label+' '+[string]$r.id+' '+[string]$r.summary).IndexOf($query,[StringComparison]::OrdinalIgnoreCase) -lt 0)){continue}
    $attention=(-not $r.resolution -and ($r.state -in @('recoverable','uncertain','interrupted','failed') -or $r.delivery_state -eq 'unconfirmed' -or @($r.result_checks | Where-Object {$_.status -in @('failed','unverified')}).Count -gt 0))
    if($filter.SelectedIndex -eq 1 -and -not $attention){continue}
    if($filter.SelectedIndex -eq 2 -and $r.state -ne 'queued'){continue}
    if($filter.SelectedIndex -eq 3 -and $r.state -notin @('running','waiting')){continue}
    if($filter.SelectedIndex -eq 4 -and $r.state -in @('queued','running','waiting','recoverable')){continue}
    $time=[DateTimeOffset]::FromUnixTimeSeconds([long][Math]::Floor([double]$r.updated)).LocalDateTime.ToString('yyyy-MM-dd HH:mm')
    $item=New-Object Windows.Forms.ListViewItem($time)
    $stateText=if($states.ContainsKey([string]$r.state)){$states[[string]$r.state]}else{[string]$r.state}
    if($resolutionStates.ContainsKey([string]$r.resolution)){$stateText+=' · '+$resolutionStates[[string]$r.resolution]}
    $null=$item.SubItems.Add($stateText);$null=$item.SubItems.Add([string]$r.label);$item.Tag=$r;$null=$list.Items.Add($item)
    if($r.id -eq $selected){$item.Selected=$true}
   }
   if($list.Items.Count -and $list.SelectedItems.Count -eq 0){$list.Items[0].Selected=$true}
   foreach($item in $list.Items){if([string]$item.Tag.id -eq $top){$list.TopItem=$item;break}}
  } finally {$list.EndUpdate()}
  Show-Details
}
function Refresh-History {
 if($script:historyRead){return}
 $refresh.Enabled=$false;$execute.Enabled=$false;$status.Text='正在刷新任务记录…'
 try {
  $python=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
  if(-not (Test-Path -LiteralPath $python)){$python=(Get-Command python -ErrorAction Stop).Source}
  $start=New-Object Diagnostics.ProcessStartInfo
  $start.FileName=$python;$start.Arguments='"'+(Join-Path $PSScriptRoot 'task_history.py')+'" --view --root "'+$Root.TrimEnd('\')+'"'
  if($FocusId){$start.Arguments+=' --task '+$FocusId}
  $start.UseShellExecute=$false;$start.CreateNoWindow=$true;$start.RedirectStandardOutput=$true
  $start.StandardOutputEncoding=New-Object Text.UTF8Encoding($false)
  $process=New-Object Diagnostics.Process;$process.StartInfo=$start
  $null=$process.Start()
  $script:historyRead=@{process=$process;output=$process.StandardOutput.ReadToEndAsync();watch=[Diagnostics.Stopwatch]::StartNew()}
 }catch{
  if($process){$process.Dispose()};$refresh.Enabled=$true;$script:directActions=@();$status.Text='读取失败，保留上次列表，请重试。'
  if($Verify -or $Preview){throw}
 }
}
$historyTimer=New-Object Windows.Forms.Timer;$historyTimer.Interval=100
$historyTimer.Add_Tick({
 if($script:historyRead){
  $read=$script:historyRead
  if($read.watch.ElapsedMilliseconds -gt 6000 -or ($read.process.HasExited -and $read.output.IsCompleted)){
   try {
    if(-not $read.process.HasExited -or -not $read.output.IsCompleted){$read.process.Kill();throw '读取超时'}
    if($read.process.ExitCode -ne 0){throw '读取失败'}
    $view=ConvertFrom-Json -InputObject $read.output.GetAwaiter().GetResult()
    if($view.state -notin @('ready','disabled','missing')){throw '任务库暂不可读'}
    $script:historyRows=@($view.rows);$script:historyEmpty=[string]$view.message
    $script:historySource=$view.source;$script:historyActions=@($view.source.actions);$script:directActions=@($view.source.direct_actions)
    $intro.Text=if($view.source){[string]$view.source.note}else{'无法确认任务库来源'}
    $sourceTip.SetToolTip($intro,([string]$view.source.note+"`r`n"+[string]$view.source.path))
    if($script:focusPending){$filter.SelectedIndex=0;$search.Clear()}
    Render-History;$status.Text=if($script:historyFeedback){$script:historyFeedback}else{[string]$view.message};$script:historyReadError=$null
    if($script:focusPending){
     $script:focusPending=$false;$found=$false
     foreach($item in $list.Items){$item.Selected=($item.Tag.id -eq $FocusId);if($item.Selected){$item.EnsureVisible();$found=$true}}
     Show-Details
     if(-not $found){$details.Text='这条记录已不可用，请返回运行中心刷新。';$status.Text=$details.Text}
    }
   }catch{
    $script:directActions=@();$status.Text='读取失败，保留上次列表，请重试。';$script:historyReadError=$_
   }finally{$read.process.Dispose();$script:historyRead=$null;$refresh.Enabled=$true;Update-DirectAction}
  }
 }
 if($script:historyRequest){
  $request=$script:historyRequest
  if([IO.File]::Exists($request.reply)){
   try{
    $reply=ConvertFrom-Json ([IO.File]::ReadAllText($request.reply));[IO.File]::Delete($request.reply)
    $script:historyRequest=$null;$script:historyFeedback=[string]$reply.message;Refresh-History;$status.Text=$script:historyFeedback
    $sourceTip.SetToolTip($execute,[string]$reply.message)
   }catch{$status.Text='操作回执暂不可读，请刷新任务核对。'}
  }elseif($request.watch.ElapsedMilliseconds -gt 25000){
   $script:historyRequest=$null;$status.Text='未获得操作确认，请刷新任务核对；不会自动重试。';Update-DirectAction
  }
 }
})
$historyTimer.Start()
function Wait-HistoryRead {
 while($script:historyRead){[Windows.Forms.Application]::DoEvents();[Threading.Thread]::Sleep(10)}
 if($script:historyReadError){throw $script:historyReadError}
}
$search.Add_TextChanged({Render-History});$filter.Add_SelectedIndexChanged({Render-History})
$list.Add_SelectedIndexChanged({Show-Details})
$refresh.Add_Click({Refresh-History})
$operationChoice.Add_SelectedIndexChanged({$copy.Enabled=($operationChoice.SelectedIndex -ge 0 -and $script:operationCommands.Count -gt $operationChoice.SelectedIndex);Update-DirectAction})
$execute.Add_Click({
 if(-not $execute.Enabled -or $script:historyRequest){return}
 try{
  $operation=Selected-DirectAction;$taskId=[string]$list.SelectedItems[0].Tag.id
  $runtime=Join-Path $Root '.runtime';$snapshot=ConvertFrom-Json ([IO.File]::ReadAllText((Join-Path $runtime 'status.json')))
  $now=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
  if($snapshot.pid -ne $script:historySource.pid -or [Math]::Abs($now-$snapshot.heartbeat) -gt 8 -or $snapshot.state -in @('stopped','stopping') -or $snapshot.history_direct_actions -notcontains $operation -or $snapshot.journal_source.path -ne $script:historySource.path){throw '后台或任务库已变化，请刷新后重试。'}
  $id=[Guid]::NewGuid().ToString('N');$path=Join-Path $runtime ('command-'+$id+'.json');$temp=$path+'.tmp'
  $payload=@{pid=$snapshot.pid;at=$now;action=@{kind='history';operation=$operation;task_id=$taskId;journal_path=$script:historySource.path}} | ConvertTo-Json -Depth 5 -Compress
  [IO.File]::WriteAllText($temp,$payload,(New-Object Text.UTF8Encoding($false)));[IO.File]::Move($temp,$path)
  $script:historyRequest=@{reply=(Join-Path $runtime ('reply-'+$id+'.json'));watch=[Diagnostics.Stopwatch]::StartNew()}
  $script:historyFeedback='';$execute.Enabled=$false;$status.Text='已提交操作，正在等待后台确认…'
 }catch{$status.Text='操作未提交：'+$_.Exception.Message;$script:directActions=@();Update-DirectAction}
})
$copy.Add_Click({
 if($copy.Enabled -and $operationChoice.SelectedIndex -ge 0){
  try{[Windows.Forms.Clipboard]::SetText($script:operationCommands[$operationChoice.SelectedIndex]);$status.Text='已复制；发送到微信后才会执行所选操作。补发仅处理文字，结清保留原始状态。'}
  catch{$status.Text='复制失败，请重试。'}
 }
})
[TrayDesign]::StyleFields($window)
$title.Anchor='Top,Left,Right';$intro.Anchor='Top,Left,Right';$heading.Anchor='Top,Left,Right'
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.PerformAutoScale()
Register-UiPreferences $window $Root 'task-history' (-not ($Verify -or $Preview)) $tabs @{filter=$filter} @{tasks=$list;outputs=$outputList}
Refresh-History
if($Verify -or $Preview){Wait-HistoryRead}
$window.Add_Disposed({$historyTimer.Stop();$historyTimer.Dispose();if($script:historyRead){try{if(-not $script:historyRead.process.HasExited){$script:historyRead.process.Kill()}}finally{$script:historyRead.process.Dispose();$script:historyRead=$null}}})
if($PreferenceProbe){. (Join-Path $PSScriptRoot 'tools/ui_preference_probe.ps1');Invoke-UiPreferenceProbe $window $Root $PreferenceProbe;exit}
if($Verify){
 $clipboardCaptured=$false
 try {
  $window.Show();[Windows.Forms.Application]::DoEvents()
  if(-not $window.Visible -or -not $window.IsHandleCreated){throw 'History window not visible'}
  if($FocusId -and ($list.SelectedItems.Count -ne 1 -or $list.SelectedItems[0].Tag.id -ne $FocusId)){throw 'Task entry did not select the requested receipt'}
  if(-not $details.ReadOnly){throw 'Details must be read-only'}
  $refresh.PerformClick()
  if(-not $script:historyRead -or $refresh.Enabled){throw 'Refresh must return while its background reader is running'}
  Wait-HistoryRead;[Windows.Forms.Application]::DoEvents()
  if(-not $list.Items.Count){throw 'Verify fixture must contain tasks'}
  $allCount=$list.Items.Count;$search.Text='___no_matching_task___';[Windows.Forms.Application]::DoEvents()
  if($list.Items.Count){throw 'Search failed to filter unmatched tasks'}
  $search.Clear();[Windows.Forms.Application]::DoEvents()
  if($list.Items.Count -ne $allCount){throw 'Search reset did not restore rows'}
  $filter.SelectedIndex=2;[Windows.Forms.Application]::DoEvents()
  foreach($item in $list.Items){if($item.Tag.state -ne 'queued'){throw 'Queue filter included another state'}}
  $filter.SelectedIndex=1;[Windows.Forms.Application]::DoEvents()
  foreach($item in $list.Items){if($item.Tag.resolution){throw 'Attention filter included a resolved task'}}
  $filter.SelectedIndex=0;[Windows.Forms.Application]::DoEvents()
  $list.Items[0].Selected=$true;[Windows.Forms.Application]::DoEvents();Show-Details
  if([string]::IsNullOrWhiteSpace($details.Text) -or $tabs.TabPages.Count -ne 3){throw 'Result summary or detail tabs missing'}
  if(-not $replyDetails.ReadOnly -or -not $inputDetails.ReadOnly -or -not $checkDetails.ReadOnly){throw 'Detail tabs must be read-only'}
  $recoverable=@($list.Items | Where-Object {$_.Tag.state -eq 'recoverable'})
  $copyVerified=$false
  if(-not $SkipClipboardVerification){$previousClipboard=Get-ClipboardSnapshot;$clipboardCaptured=$true}
  if($recoverable.Count){
   foreach($item in $list.Items){$item.Selected=$false};$recoverable[0].Selected=$true
   [Windows.Forms.Application]::DoEvents();Show-Details
   if(-not $copy.Enabled){throw 'Recovery copy not enabled'}
   $originalState=$script:historySource.runtime_state;$script:historySource.runtime_state='standby';Update-DirectAction
   if($execute.Enabled -or $actionHint.Text -notlike '*开始对话*'){throw 'Standby recovery must show its disabled reason'}
   $script:historySource.runtime_state=$originalState;Update-DirectAction
    if(-not $SkipClipboardVerification){
     $copy.PerformClick()
     if([Windows.Forms.Clipboard]::GetText() -ne ('/resume '+$recoverable[0].Tag.id)){throw 'Recovery command copy failed'}
     $copyVerified=$true
    }
  }
   $operationCount=0;$batchInputCount=0;$templateCount=0;$resultCheckCount=0
    foreach($item in $list.Items){
     foreach($other in $list.Items){$other.Selected=$false};$item.Selected=$true
     [Windows.Forms.Application]::DoEvents();Show-Details
     foreach($batchInput in @($item.Tag.inputs)){
      if(-not $inputDetails.Text.Contains([string]$batchInput.path)){throw 'Fixed batch input path not rendered'}
      if($inputDetails.Text -notlike '*本次批次输入*'){throw 'Batch input heading not rendered'}
      $batchInputCount++
     }
     if($item.Tag.template -and $item.Tag.template.name){
      if($inputDetails.Text -notlike '*本次任务模板*' -or -not $inputDetails.Text.Contains([string]$item.Tag.template.name)){throw 'Task template snapshot not rendered'}
      foreach($templateField in @('inputs','steps','output_dir','output_format')){
       if(-not $inputDetails.Text.Contains([string]$item.Tag.template.definition.$templateField)){throw 'Task template definition field not rendered'}
      }
      if(-not $inputDetails.Text.Contains('第 '+[string]$item.Tag.template.revision+' 版')){throw 'Task template version not rendered'}
      if($item.Tag.template.definition.checks -and -not $inputDetails.Text.Contains([string]$item.Tag.template.definition.checks)){throw 'Task template checks not rendered'}
      $templateCount++
     }
     if($item.Tag.local_files -and $item.Tag.local_files.output_dir){
      if(-not $inputDetails.Text.Contains([string]$item.Tag.local_files.delivery) -or -not $inputDetails.Text.Contains([string]$item.Tag.local_files.output_dir)){throw 'Local file task context missing'}
      foreach($f in @($item.Tag.local_files.files)){
       if(-not @($outputList.Items | Where-Object {$_.Tag -eq $f.path -and $_.SubItems[1].Text -eq '本机输入副本'}).Count){throw 'Local input copy missing from history'}
      }
     }
     if($item.Tag.revision -and $item.Tag.revision.parent_id){
      if(-not $inputDetails.Text.Contains([string]$item.Tag.revision.parent_id) -or -not $inputDetails.Text.Contains([string]$item.Tag.revision.instructions)){throw 'Revision context missing'}
      foreach($f in @($item.Tag.revision.files)){
       if(-not @($outputList.Items | Where-Object {$_.Tag -eq $f.path -and $_.SubItems[1].Text -eq '旧版本副本'}).Count){throw 'Preserved old version missing from history'}
      }
     }
     foreach($resultCheck in @($item.Tag.result_checks)){
      if($checkDetails.Text -notlike '*结果检查（独立于文件生成状态）*' -or -not $checkDetails.Text.Contains([string]$resultCheck.detail)){throw 'Result check detail not rendered'}
      if(-not $checkDetails.Text.Contains([string]$resultCheck.file)){throw 'Result check file not rendered'}
      $resultCheckCount++
     }
     foreach($path in @($item.Tag.outputs)){
      if(-not @($outputList.Items | Where-Object {[string]$_.Tag -eq [string]$path}).Count){throw 'Associated output missing from visible artifact list'}
     }
     if($item.Tag.result_text -and $replyDetails.Text.Replace("`r`n","`n") -ne ([string]$item.Tag.result_text).Replace("`r`n","`n")){throw 'Full reply lost text'}
     foreach($page in $tabs.TabPages){$tabs.SelectedTab=$page;[Windows.Forms.Application]::DoEvents();if(-not $page.Controls[0].Visible){throw 'Detail tab content not visible'}}
     for($i=0;$i -lt $operationChoice.Items.Count;$i++){
      $operationChoice.SelectedIndex=$i;[Windows.Forms.Application]::DoEvents()
      if([string]::IsNullOrWhiteSpace($operationChoice.Text) -or $operationChoice.Text -ne [string]$operationChoice.SelectedItem){throw 'History operation selection text is blank or incorrect'}
      if(-not $SkipClipboardVerification){
       $copy.PerformClick()
       if([Windows.Forms.Clipboard]::GetText() -ne $script:operationCommands[$i]){throw 'History operation copy failed'}
      }
      $operationCount++
     }
    }
   if($VerifyActions){
    function Invoke-FixtureAction($taskId,$action){
     foreach($item in $list.Items){$item.Selected=([string]$item.Tag.id -eq $taskId)}
     [Windows.Forms.Application]::DoEvents();Show-Details
     $found=$false
     for($i=0;$i -lt $operationChoice.Items.Count;$i++){
      $operationChoice.SelectedIndex=$i
      if((Selected-DirectAction) -eq $action){$found=$true;break}
     }
     Update-DirectAction
     if(-not $found -or -not $execute.Enabled){throw ('Direct action unavailable: '+$action)}
     if($execute.Text -in @('直接执行','选择操作','请在微信操作')){throw 'Action button must name the selected action'}
     $execute.PerformClick()
     $deadline=[DateTime]::UtcNow.AddSeconds(10)
     while(($script:historyRequest -or $script:historyRead) -and [DateTime]::UtcNow -lt $deadline){[Windows.Forms.Application]::DoEvents();[Threading.Thread]::Sleep(10)}
     if($script:historyRequest -or $script:historyRead){throw 'Direct action verification timed out'}
     return @($script:historyRows | Where-Object {$_.id -eq $taskId})[0]
    }
    $recover=@($script:historyRows | Where-Object {$_.state -eq 'recoverable' -and -not $_.resolution})[0]
    if(-not $recover){throw 'VerifyActions fixture needs a recoverable task'}
    $after=Invoke-FixtureAction $recover.id 'resume'
    if($after.state -ne 'queued'){throw ('Direct resume did not queue the selected task: '+$script:historyFeedback+'; '+$status.Text)}
    $after=Invoke-FixtureAction $recover.id 'cancel_queued'
    if($after.state -ne 'cancelled'){throw 'Direct cancellation did not persist'}
    $uncertain=@($script:historyRows | Where-Object {$_.state -eq 'uncertain' -and -not $_.resolution})[0]
    if(-not $uncertain){throw 'VerifyActions fixture needs an uncertain task'}
    $after=Invoke-FixtureAction $uncertain.id 'checked'
    if($after.resolution -ne 'checked' -or $after.state -ne 'uncertain'){throw 'Direct resolution lost original state'}
    $rich=@($list.Items | Where-Object {$_.Tag.template -and $_.Tag.template.name -and @($_.Tag.result_checks).Count}) | Select-Object -First 1
    if($rich){foreach($item in $list.Items){$item.Selected=$false};$rich.Selected=$true;[Windows.Forms.Application]::DoEvents();Show-Details;$tabs.SelectedIndex=2}
    $bitmap=New-Object Drawing.Bitmap($window.Width,$window.Height)
    try{$window.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)));$bitmap.Save((Join-Path $Root '.runtime/task-history-verified.png'))}finally{$bitmap.Dispose()}
   }
   Write-Output ('PASS task history: visible, read-only details, asynchronous refresh, search, filters, selection; directActions='+$VerifyActions+'; recoveryCopy='+$copyVerified+'; operations='+$operationCount+'; batchInputs='+$batchInputCount+'; templates='+$templateCount+'; resultChecks='+$resultCheckCount+'; rows='+$list.Items.Count+'; clipboardSkipped='+$SkipClipboardVerification)
 }finally{
  try {
   if($clipboardCaptured){
    if($null -ne $previousClipboard){[Windows.Forms.Clipboard]::SetDataObject($previousClipboard,$true)}else{[Windows.Forms.Clipboard]::Clear()}
   }
  }finally{$sourceTip.Dispose();$window.Dispose()}
 }
}elseif($Preview){
 try {
  $window.Show();[Windows.Forms.Application]::DoEvents()
  $image=New-Object Drawing.Bitmap($window.Width,$window.Height)
  try {
   $window.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)))
   $output=Join-Path $Root '.runtime';$null=[IO.Directory]::CreateDirectory($output)
   $image.Save((Join-Path $output 'task-history-preview.png'))
  }finally{$image.Dispose()}
 }finally{$sourceTip.Dispose();$window.Dispose()}
}else{[Windows.Forms.Application]::Run($window);$sourceTip.Dispose();$window.Dispose()}
