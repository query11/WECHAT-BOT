param([ValidateSet('','save','read')][string]$PreferenceProbe='',[string]$Root=(Split-Path -Parent $PSScriptRoot),[ValidateSet('templates','batches','results','revise','localfiles','schedules')][string]$Page='templates',[string]$TemplateId='',[ValidatePattern('^$|^[a-f0-9]{32}$')][string]$ItemId='',[switch]$Verify)
$ErrorActionPreference='Stop'
if($PreferenceProbe -and [IO.Path]::GetFullPath($Root) -eq [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))){throw 'Preference probe requires isolated Root'}
if($Verify -and [IO.Path]::GetFullPath($Root) -eq [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))){throw 'Verification requires an isolated Root'}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'ui_preferences.ps1')
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
. (Join-Path $PSScriptRoot 'desktop_files.ps1')
[TrayDesign]::EnableDpi();[Windows.Forms.Application]::EnableVisualStyles()
$window=New-Object Windows.Forms.Form;$window.Text='微信助手 · 快捷任务';$window.ClientSize=New-Object Drawing.Size(960,760)
$window.MinimumSize=New-Object Drawing.Size(976,799);$window.StartPosition='CenterScreen'
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.AutoScaleMode='Dpi';$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$window.BackColor=[TrayDesign]::PageColor;$window.ForeColor=[TrayDesign]::Heading
$icon=Join-Path $PSScriptRoot 'wechat-assistant-v2.ico';if(Test-Path -LiteralPath $icon){$window.Icon=New-Object Drawing.Icon($icon)}
$script:layout=@{}
function Label($parent,$text,$x,$y,$w,$h){$c=New-Object Windows.Forms.Label;$c.Text=$text;$c.SetBounds($x,$y,$w,$h);$c.AutoEllipsis=$true;$parent.Controls.Add($c);$script:layout[$c]=@($x,$y,$w,$h);return $c}
function Button($parent,$text,$x,$y,$w){$c=New-Object TrayButton;$c.Text=$text;$c.SetBounds($x,$y,$w,34);$parent.Controls.Add($c);$script:layout[$c]=@($x,$y,$w,34);return $c}
function Box($parent,$x,$y,$w,$h,$read=$true){$c=New-Object Windows.Forms.TextBox;$c.Multiline=$true;$c.ReadOnly=$read;$c.ScrollBars='Vertical';$c.SetBounds($x,$y,$w,$h);$c.Anchor='Top,Left,Right';$parent.Controls.Add($c);$script:layout[$c]=@($x,$y,$w,$h);return $c}
function List($parent,$x,$y,$w,$h,$columns){
 $c=New-Object Windows.Forms.ListView;$c.View='Details';$c.FullRowSelect=$true;$c.MultiSelect=$false;$c.HideSelection=$false;$c.SetBounds($x,$y,$w,$h)
 foreach($column in $columns){$null=$c.Columns.Add($column,100)};$parent.Controls.Add($c);$script:layout[$c]=@($x,$y,$w,$h);return $c
}
function Combo($parent,$x,$y,$w){$c=New-Object Windows.Forms.ComboBox;$c.DropDownStyle='DropDownList';$c.SetBounds($x,$y,$w,32);$parent.Controls.Add($c);$script:layout[$c]=@($x,$y,$w,32);return $c}
$title=Label $window '快捷任务' 26 18 600 40;$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold)
$intro=Label $window '从模板或本机文件开始，核对后运行；也可安排定时任务、查看与修改结果。' 28 60 860 26;$intro.ForeColor=[TrayDesign]::MutedColor
$ownerLabel=Label $window '微信用户' 28 97 88 32;$ownerLabel.ForeColor=[TrayDesign]::MutedColor;$ownerLabel.TextAlign='MiddleLeft'
$owner=Combo $window 120 95 590;$owner.Anchor='Top,Left,Right'
$refresh=Button $window '重新读取' 760 93 172;$refresh.Anchor='Top,Right'
$tabs=New-Object TrayTabs;$tabs.SetBounds(28,136,904,548);$tabs.Anchor='Top,Bottom,Left,Right';$window.Controls.Add($tabs)
$pages=@{}
foreach($spec in @(@('templates','运行模板'),@('batches','附件核对'),@('results','任务结果'),@('revise','继续修改'),@('localfiles','文件任务'),@('schedules','定时任务'))){$p=New-Object Windows.Forms.TabPage;$p.Text=$spec[1];$p.BackColor=[Drawing.Color]::White;$null=$tabs.TabPages.Add($p);$pages[$spec[0]]=$p}
$tabs.SelectedTab=$pages[$Page]
$feedback=Label $window '正在读取…' 28 698 904 48;$feedback.Anchor='Bottom,Left,Right';$feedback.ForeColor=[TrayDesign]::MutedColor
$tp=$pages.templates
$templates=List $tp 16 18 240 418 @('模板名称','常用')
$pin=Button $tp '固定到运行中心' 16 452 240
$definition=Box $tp 276 18 600 126
$null=Label $tp '本次输入' 276 154 590 24
$batchChoice=Combo $tp 276 180 600;$batchChoice.Anchor='Top,Left,Right'
$null=Label $tp '本次补充要求（可选）' 276 222 590 24
$body=Box $tp 276 248 600 54 $false;$body.MaxLength=8000
$review=Box $tp 276 314 600 122
$previewButton=Button $tp '核对本次任务' 276 452 184
$run=Button $tp '确认运行' 696 452 180;$run.Anchor='Top,Right';$run.Primary=$true
$bp=$pages.batches
$batches=List $bp 16 18 240 418 @('附件批次','状态')
$batchState=Label $bp '' 276 18 600 44
$batchFiles=List $bp 276 68 600 214 @('文件 / 接收记录','状态 / 说明');$batchFiles.Anchor='Top,Left,Right'
$batchGuide=Box $bp 276 294 600 142
$remove=Button $bp '移出清单' 276 452 160
$dismiss=Button $bp '已处理缺失项' 452 452 184
$seal=Button $bp '确认附件清单' 652 452 224;$seal.Anchor='Top,Right';$seal.Primary=$true
$rp=$pages.results
$results=List $rp 16 18 240 470 @('任务','状态')
$resultSummary=Box $rp 276 18 600 66
$conclusion=Label $rp '' 276 92 600 42
$files=List $rp 276 142 600 124 @('交付文件','记录时检查');$files.Anchor='Top,Left,Right'
$open=Button $rp '打开文件' 276 278 168
$folder=Button $rp '打开文件夹' 460 278 180
$revise=Button $rp '继续修改' 656 278 220;$revise.Anchor='Top,Right';$revise.Primary=$true
$reply=Box $rp 276 324 600 164
$script:data=$null;$script:job=$null;$script:loading=$false;$script:preview=$null;$script:lastResponse=$null
$script:focusId=$ItemId;$script:focusPending=[bool]$ItemId
$stateNames=@{queued='排队中';running='处理中';waiting='等待确认';recoverable='待恢复';open='收集中';ready='已确认';used='已使用';cancelled='已取消';completed='处理结束';failed='失败';uncertain='待核对';interrupted='中断'}
function Selected($list){if($list.SelectedItems.Count){return $list.SelectedItems[0].Tag};return $null}
function Invalidate-Review {$script:preview=$null;$review.Text='点击“核对本次任务”查看固定模板版本、输入清单和输出位置。';Update-Actions}
function Update-Actions {
 $ready=[bool]($script:data -and $script:data.ok -and -not $script:job)
 $owner.Enabled=$ready;$tabs.Enabled=$ready;$refresh.Enabled=-not $script:job
 $t=Selected $templates;$b=Selected $batches;$f=Selected $batchFiles
 $previewButton.Enabled=($ready -and [bool]$t);$pin.Enabled=($ready -and [bool]$t)
 $pin.Text=if($t -and $script:data.pins -contains $t.id){'取消固定'}else{'固定到运行中心'}
 $run.Enabled=($ready -and [bool]$script:preview -and -not $script:preview.launch_reason)
 $editable=($ready -and $b -and $b.state -eq 'open' -and -not $b.pending)
 $remove.Enabled=($editable -and $f -and $f.kind -eq 'file');$dismiss.Enabled=($editable -and $f -and $f.kind -eq 'failure')
 $seal.Enabled=($editable -and -not $b.failed -and @($b.files).Count -gt 0)
 Update-RevisionActions $ready
 if($localCheck){Update-LocalFileActions $ready}
 if($scheduleCheck){Update-ScheduleActions $ready}
 $file=Selected $files;$open.Enabled=($ready -and $file -and (Test-DesktopDocument ([string]$file.path)))
 $folder.Enabled=($ready -and $file -and [IO.File]::Exists([string]$file.path))
}
function Show-Template {
 $t=Selected $templates
 $definition.Text=if($t){'模板：'+$t.name+' · 第 '+$t.revision+" 版`r`n输入："+$t.definition.inputs+"`r`n步骤："+$t.definition.steps+"`r`n输出："+$t.definition.output_dir+' · '+$t.definition.output_format}else{'暂无任务模板。请从运行中心“任务模板”新建，然后回到这里重新读取。'}
 Invalidate-Review
}
function Show-Batch {
 $batchFiles.Items.Clear();$b=Selected $batches
 if(-not $b){$batchState.Text='暂无附件批次';$batchGuide.Text='在微信说“开始对话”，再说“开始收集附件”，随后发送文件。';Update-Actions;return}
 $batchState.Text=$b.name+' · '+$stateNames[[string]$b.state]+' · 已收到 '+@($b.files).Count+' 个文件 · 接收中 '+$b.pending+' 条 · 缺失 '+$b.failed+' 项'
 foreach($f in @($b.files)){$item=New-Object Windows.Forms.ListViewItem([string]$f.orig);$null=$item.SubItems.Add($(if($f.role -eq 'template'){'已收到 · 格式模板'}else{'已收到 · '+[Math]::Round($f.size/1024,1)+' KB'}));$item.Tag=@{kind='file';index=$f.index;path=$f.path};$null=$batchFiles.Items.Add($item)}
 foreach($f in @($b.failures)){$item=New-Object Windows.Forms.ListViewItem('接收失败 #'+$f.id);$null=$item.SubItems.Add([string]($f.errors -join '；'));$item.Tag=@{kind='failure';index=$f.id};$null=$batchFiles.Items.Add($item)}
 if($b.pending){$item=New-Object Windows.Forms.ListViewItem('正在接收 '+$b.pending+' 条附件消息');$null=$item.SubItems.Add('接收完成后点击重新读取');$null=$batchFiles.Items.Add($item)}
 $batchGuide.Text=if($b.state -ne 'open'){'这份清单已固定或结束，不能再移除文件。已确认且未使用的清单可在“运行模板”页选用。'}elseif($b.pending){'请等待接收结束后重新读取，核对完整清单。'}elseif($b.failed){'请在微信补传缺失的文件，重新读取并核对后，选中失败记录点击“已处理缺失项”。这表示已经处理或决定放弃缺失项，不会自动重试下载。'}else{'核对文件后点击“确认附件清单”，再到“运行模板”页选择这批附件。移出清单保留磁盘原文件。'}
 Update-Actions
}
function Show-Result {
 $r=Selected $results;$files.Items.Clear()
 $resultSummary.Text=if($r){[string]$r.summary}else{'暂无已结束的任务。'};$conclusion.Text=if($r){[string]$r.conclusion}else{''};$reply.Text=if($r){([string]$r.result_text).Replace("`r`n","`n").Replace("`n","`r`n")}else{''}
 if($r){foreach($f in @($r.files)){$item=New-Object Windows.Forms.ListViewItem([string]$f.name);$null=$item.SubItems.Add([string]$f.note);$item.Tag=$f;$null=$files.Items.Add($item)}}
 if($files.Items.Count){$files.Items[0].Selected=$true};Update-Actions
}
function Populate($data){
 $oldT=Selected $templates;$oldB=Selected $batches;$oldR=Selected $results;$oldChoice=$batchChoice.SelectedIndex
 $selectedBatch=if($oldChoice -gt 0 -and $script:readyBatches.Count -ge $oldChoice){$script:readyBatches[$oldChoice-1].id}else{''}
 $script:loading=$true
 try{
  $script:data=$data;$owner.Items.Clear();foreach($uid in @($data.users)){$null=$owner.Items.Add([string]$uid)};$owner.SelectedItem=[string]$data.uid
  $templates.Items.Clear();foreach($t in @($data.templates | Sort-Object @{Expression={if($data.pins -contains $_.id){0}else{1}}},name)){$item=New-Object Windows.Forms.ListViewItem([string]$t.name);$null=$item.SubItems.Add($(if($data.pins -contains $t.id){'已固定'}else{''}));$item.Tag=$t;$null=$templates.Items.Add($item);if(($oldT -and $oldT.id -eq $t.id) -or $t.id -eq $TemplateId){$item.Selected=$true}}
  if(-not $templates.SelectedItems.Count -and $templates.Items.Count){$templates.Items[0].Selected=$true}
  $batches.Items.Clear();foreach($b in @($data.batches)){$item=New-Object Windows.Forms.ListViewItem([string]$b.name);$null=$item.SubItems.Add([string]$stateNames[[string]$b.state]);$item.Tag=$b;$null=$batches.Items.Add($item);if(($oldB -and $oldB.id -eq $b.id) -or ($script:focusPending -and $b.id -eq $script:focusId)){$item.Selected=$true;$item.EnsureVisible()}}
  if(-not $batches.SelectedItems.Count -and $batches.Items.Count){$batches.Items[0].Selected=$true}
  $script:readyBatches=@($data.batches | Where-Object {$_.state -eq 'ready'})
  $batchChoice.Items.Clear();$null=$batchChoice.Items.Add('按模板明确指定的输入（不附加最近附件）');$batchChoice.SelectedIndex=0
  foreach($b in $script:readyBatches){$index=$batchChoice.Items.Add($b.name+' · '+@($b.files).Count+' 个文件 · '+$b.id.Substring(0,8));if($b.id -eq $selectedBatch){$batchChoice.SelectedIndex=$index}}
  $results.Items.Clear();foreach($r in @($data.results)){$item=New-Object Windows.Forms.ListViewItem([string]$r.label);$null=$item.SubItems.Add([string]$stateNames[[string]$r.state]);$item.Tag=$r;$null=$results.Items.Add($item);if(($oldR -and $oldR.id -eq $r.id) -or ($script:focusPending -and $r.id -eq $script:focusId)){$item.Selected=$true;$item.EnsureVisible()}}
  if(-not $results.SelectedItems.Count -and $results.Items.Count){$results.Items[0].Selected=$true}
  Populate-LocalTemplates $data
  Populate-Schedules $data.schedules
  $script:focusPending=$false
 }finally{$script:loading=$false}
 Show-Template;Show-Batch;Show-Result
}
. (Join-Path $PSScriptRoot 'result_revision_ui.ps1')
. (Join-Path $PSScriptRoot 'desktop_files_ui.ps1')
. (Join-Path $PSScriptRoot 'schedule_ui.ps1')
function Request($action){
 if($script:job){return}
 $payload=@{operation=$action};if($script:data){$payload.uid=$script:data.uid;$payload.source=$script:data.source}elseif($TemplateId){$payload.template_id=$TemplateId}
 if($action -eq 'view' -and $script:focusId){$payload.item_id=$script:focusId}
 if($action -in @('pin','preview')){$t=Selected $templates;if(-not $t){return};$payload.name=$t.name;$payload.expected_revision=[long]$t.revision}
 if($action -eq 'pin'){$payload.enabled=($script:data.pins -notcontains $t.id)}
 if($action -eq 'preview'){$payload.body=$body.Text;if($batchChoice.SelectedIndex -gt 0){$payload.batch_id=$script:readyBatches[$batchChoice.SelectedIndex-1].id}}
 if($action -eq 'run'){if(-not $script:preview){return};$payload.token=$script:preview.token}
 if($action -in @('seal','remove','dismiss')){$b=Selected $batches;if(-not $b){return};$payload.batch_id=$b.id;$payload.expected_version=$b.version;if($action -ne 'seal'){$payload.index=[int](Selected $batchFiles).index}}
 Add-RevisionPayload $payload $action
 Add-LocalFilePayload $payload $action
 Add-SchedulePayload $payload $action
 $info=New-Object Diagnostics.ProcessStartInfo;$info.FileName=$WeChatPython;$info.Arguments='"'+(Join-Path $WeChatApp 'workflow_desktop.py')+'" --root "'+$Root.TrimEnd('\')+'"'
 $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
 $info.StandardOutputEncoding=New-Object Text.UTF8Encoding($false);$info.StandardErrorEncoding=New-Object Text.UTF8Encoding($false);$info.EnvironmentVariables['PYTHONIOENCODING']='utf-8'
 $p=New-Object Diagnostics.Process;$p.StartInfo=$info
 try{
  [void]$p.Start();$writer=New-Object IO.StreamWriter($p.StandardInput.BaseStream,(New-Object Text.UTF8Encoding($false)));$writer.Write(($payload|ConvertTo-Json -Depth 8 -Compress));$writer.Dispose()
  $script:job=@{process=$p;output=$p.StandardOutput.ReadToEndAsync();error=$p.StandardError.ReadToEndAsync();watch=[Diagnostics.Stopwatch]::StartNew();action=$action};$feedback.Text='正在处理，请稍候…'
 }catch{$p.Dispose();$feedback.Text='操作未能确认，请重新读取后核对。';$script:preview=$null;Invalidate-Revision;Invalidate-LocalFiles;Invalidate-Schedule}
 Update-Actions
}
function Show-Preview($data){
 $script:preview=$data;$t=$data.template
 $lines=@(('模板：'+$t.name+' · 第 '+$t.revision+' 版'),('输出位置：'+$t.definition.output_dir),('格式：'+$t.definition.output_format))
 if($data.batch){$lines+='本次附件：';foreach($f in @($data.batch.files)){$lines+=([string]$f.index+'. '+$f.orig+'  ['+$f.path+']')}}else{$lines+='输入要求：'+$t.definition.inputs;$lines+='本次不附加附件，请确认模板已明确指定输入。'}
 if($data.body){$lines+='补充要求：'+$data.body};if($t.definition.checks){$lines+='检查条件：'+$t.definition.checks}
 if($data.launch_reason){$lines+=$data.launch_reason}
 $review.Text=$lines -join "`r`n";Update-Actions
}
$timer=New-Object Windows.Forms.Timer;$timer.Interval=100
$timer.Add_Tick({
 if(-not $script:job){return};$j=$script:job
 if($j.watch.ElapsedMilliseconds -gt 28000 -or ($j.process.HasExited -and $j.output.IsCompleted)){
  try{
   if(-not $j.process.HasExited){$j.process.Kill();throw 'timeout'}
   $data=ConvertFrom-Json -InputObject $j.output.GetAwaiter().GetResult();$script:lastResponse=$data
   if($data.ok){
    switch($j.action){
     'preview' {Show-Preview $data}
     'run' {$script:preview=$null;$review.Text=$data.message+"`r`n任务编号："+$data.task_id}
     'schedule_preview' {Show-SchedulePreview $data}
     {$_ -in @('schedule_save','schedule_pause','schedule_resume','schedule_delete')} {Populate-Schedules $data.schedules ([string]$data.schedule_id)}
     'files_preview' {Show-LocalPreview $data}
     'files_run' {$script:localPreview=$null;$localReview.Text=$data.message+"`r`n任务编号："+$data.task_id}
     'revision_source' {Show-RevisionSource $data.revision_source}
     'revision_preview' {Show-RevisionPreview $data}
     'revision_run' {$script:revisionPreview=$null;$revisionReview.Text=$data.message+"`r`n任务编号："+$data.task_id}
     default {Populate $data}
    }
   }
   else{$script:preview=$null;Invalidate-Revision;Invalidate-LocalFiles;Invalidate-Schedule;if($j.action -eq 'view'){$script:data=$null}}
   $feedback.Text=[string]$data.message
  }catch{$script:preview=$null;Invalidate-Revision;Invalidate-LocalFiles;Invalidate-Schedule;if($j.action -eq 'view'){$script:data=$null};$script:lastResponse=@{ok=$false};$feedback.Text='操作未能确认，请重新读取后核对；不会自动重试。'}
  finally{$j.process.Dispose();$script:job=$null;Update-Actions}
  if($script:revisionFocusPending){$script:revisionFocusPending=$false;if($data.ok -and $j.action -eq 'view'){Start-Revision $script:revisionTaskId}}
 }
})
$templates.Add_SelectedIndexChanged({if(-not $script:loading){Show-Template}})
$batches.Add_SelectedIndexChanged({if(-not $script:loading){Show-Batch}})
$results.Add_SelectedIndexChanged({if(-not $script:loading){Show-Result}})
$batchFiles.Add_SelectedIndexChanged({Update-Actions});$files.Add_SelectedIndexChanged({Update-Actions})
$batchChoice.Add_SelectedIndexChanged({if(-not $script:loading){Invalidate-Review}});$body.Add_TextChanged({if(-not $script:loading){Invalidate-Review}})
$owner.Add_SelectedIndexChanged({if(-not $script:loading -and $script:data -and $owner.SelectedItem -and $owner.SelectedItem -ne $script:data.uid){$script:focusId='';$script:focusPending=$false;Reset-Revision;Reset-LocalFiles;Reset-Schedule;$script:data.uid=[string]$owner.SelectedItem;$body.Clear();Invalidate-Review;Request 'view'}})
$refresh.Add_Click({Invalidate-Review;Invalidate-Revision;Invalidate-LocalFiles;Invalidate-Schedule;$script:revisionSource=$null;$script:revisionFocusPending=[bool]$script:revisionTaskId;Request 'view'});$pin.Add_Click({Request 'pin'});$previewButton.Add_Click({Request 'preview'});$run.Add_Click({Request 'run'})
$seal.Add_Click({if($Verify -or [Windows.Forms.MessageBox]::Show($window,'确认这批附件已收齐？确认后清单保持固定，可用于运行模板。','确认附件清单','YesNo','Question') -eq 'Yes'){Request 'seal'}})
$remove.Add_Click({if($Verify -or [Windows.Forms.MessageBox]::Show($window,'将选中的文件移出本次清单？磁盘原文件仍保留。','移出清单','YesNo','Question') -eq 'Yes'){Request 'remove'}})
$dismiss.Add_Click({if($Verify -or [Windows.Forms.MessageBox]::Show($window,'请先补传并核对缺失文件。继续表示已处理或决定放弃这条缺失项，不会自动重试下载。','已处理缺失项','YesNo','Question') -eq 'Yes'){Request 'dismiss'}})
$open.Add_Click({try{$f=Selected $files;Open-DesktopDocument ([string]$f.path);$feedback.Text='已打开文件；请以文件实际内容为准。'}catch{$feedback.Text='文件已移动、删除或不支持直接打开，请核对文件位置。'}})
$folder.Add_Click({try{$f=Selected $files;Show-DesktopDocumentFolder ([string]$f.path)}catch{$feedback.Text='文件位置不可用，请检查文件是否仍存在。'}})
$window.Add_FormClosing({param($sender,$event) if($script:job){$event.Cancel=$true;$feedback.Text='请等待当前操作完成后关闭。'}})
$window.Add_Disposed({$timer.Stop();$timer.Dispose()})
function Size-Columns {
 foreach($l in @($templates,$batches,$results,$versions,$localFiles,$scheduleList)){if(Set-RememberedColumns $l){continue};$width=$l.ClientSize.Width-24;$l.Columns[0].Width=[int]($width*.68);$l.Columns[1].Width=$width-$l.Columns[0].Width}
 foreach($l in @($batchFiles,$files,$revisionFiles)){if(Set-RememberedColumns $l){continue};$width=$l.ClientSize.Width-24;$l.Columns[0].Width=[int]($width*.52);$l.Columns[1].Width=$width-$l.Columns[0].Width}
}
function Layout-Workflow {
 if($script:layoutBusy){return};$script:layoutBusy=$true
 try{
  $g=$window.CreateGraphics();$scale=$g.DpiX/96;$g.Dispose()
  foreach($entry in @($script:layout.GetEnumerator())){
   $c=$entry.Key;if(-not ($c.Parent -is [Windows.Forms.TabPage])){continue}
   $b=$entry.Value;$x=[double]$b[0];$y=[double]$b[1];$width=[double]$b[2];$height=[double]$b[3]
   $delta=$c.Parent.ClientSize.Width/$scale-896
   if($x -ge 276){if($c -is [TrayButton]){if($c.Anchor -band [Windows.Forms.AnchorStyles]::Right){$x+=$delta}}else{$width+=$delta}}
   $c.SetBounds([int]($x*$scale),[int]($y*$scale),[int]($width*$scale),[int]($height*$scale))
  }
  Size-Columns
 }finally{$script:layoutBusy=$false}
}
$window.Add_Shown({Layout-Workflow});$window.Add_SizeChanged({Layout-Workflow});$tabs.Add_SelectedIndexChanged({Layout-Workflow})
[TrayDesign]::StyleFields($window)
# Rounded frames for page text boxes and lists. The frame takes over the control's layout entry,
# so Layout-Workflow keeps stretching the right column as before.
foreach($c in @($script:layout.Keys)){
 if(($c -is [Windows.Forms.TextBox] -or $c -is [Windows.Forms.ListView]) -and $c.Parent -is [Windows.Forms.TabPage]){
  $field=[TrayDesign]::Frame($c);$script:layout[$field]=$script:layout[$c];$null=$script:layout.Remove($c)
 }
}
$window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.PerformAutoScale();$timer.Start();Request 'view'
Register-UiPreferences $window $Root 'workflow' (-not $Verify) $tabs @{} @{templates=$templates;batches=$batches;results=$results;batchFiles=$batchFiles;files=$files;versions=$versions;revisionFiles=$revisionFiles;localFiles=$localFiles;scheduleList=$scheduleList} (-not $PSBoundParameters.ContainsKey('Page'))
Layout-Workflow
if($PreferenceProbe){. (Join-Path $WeChatHome 'tools/ui_preference_probe.ps1');Invoke-UiPreferenceProbe $window $Root $PreferenceProbe;exit}
if($Verify){
 function Wait-Request {$deadline=[DateTime]::UtcNow.AddSeconds(30);while($script:job -and [DateTime]::UtcNow -lt $deadline){[Windows.Forms.Application]::DoEvents();[Threading.Thread]::Sleep(10)};if($script:job -or -not $script:lastResponse.ok){throw ('Workflow request failed: '+$feedback.Text)}}
 try{
  $window.Show();Wait-Request;Layout-Workflow
  if($Page -eq 'schedules'){
   if($tabs.SelectedTab -ne $sp -or $scheduleSave.Enabled -or -not $script:data.schedules.enabled){throw 'Schedule entry invalid'}
   Write-Output 'PASS direct schedule entry: no automatic submission'
   return
  }
  if($Page -eq 'localfiles'){
   if($tabs.SelectedTab -ne $lp -or $localFiles.Items.Count -ne 0 -or $localDelivery.SelectedIndex -ne 0 -or $localRun.Enabled){throw 'File task entry has stale inputs or confirmation'}
   Write-Output 'PASS direct file task entry: empty draft, explicit delivery and disabled confirmation'
   return
  }
  if($Page -eq 'revise'){
   if($tabs.SelectedTab -ne $vp -or $script:revisionSource.id -ne $ItemId -or -not $revisionFiles.Items.Count){throw 'Revision entry did not load the requested task'}
   if($revisionRun.Enabled){throw 'Revision entry executed without preview'}
   Write-Output 'PASS direct revision entry: correct owner, task and files; confirmation disabled'
   return
  }
  if($ItemId -and ((Selected $batches).id -ne $ItemId -or $tabs.SelectedTab -ne $bp)){throw 'Batch entry did not select the requested batch and page'}
  if(-not $templates.Items.Count){throw 'No fixture template'}
  $tabs.SelectedTab=$tp;[Windows.Forms.Application]::DoEvents()
  $templates.Items[0].Selected=$true;$pin.PerformClick();Wait-Request
  if($script:data.pins.Count -ne 1){throw 'Pin did not persist'}
  $tabs.SelectedTab=$bp;[Windows.Forms.Application]::DoEvents()
  $item=@($batches.Items | Where-Object {$_.Tag.name -eq '待核对材料'})[0];$item.Selected=$true;[Windows.Forms.Application]::DoEvents()
  Layout-Workflow;$bitmap=New-Object Drawing.Bitmap($window.Width,$window.Height);$window.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)));$bitmap.Save((Join-Path $Root '.runtime/workflow-batches.png'));$bitmap.Dispose()
  $file=@($batchFiles.Items | Where-Object {$_.Tag.kind -eq 'file'})[0];$file.Selected=$true;$remove.PerformClick();Wait-Request
  $failure=@($batchFiles.Items | Where-Object {$_.Tag.kind -eq 'failure'})[0];$failure.Selected=$true;$dismiss.PerformClick();Wait-Request
  $seal.PerformClick();Wait-Request
  if((Selected $batches).state -ne 'ready'){throw 'Checklist was not sealed'}
  $tabs.SelectedTab=$tp;[Windows.Forms.Application]::DoEvents();$batchChoice.SelectedIndex=1;$body.Text='本次按项目整理';$previewButton.PerformClick();Wait-Request
  if($run.Right -gt $tp.ClientSize.Width -or $review.Parent.Right -gt $tp.ClientSize.Width){throw 'Preview controls extend outside page'}
  if($review.Text -notlike '*输出位置*' -or $review.Text -notlike '*本次附件*'){throw 'Preview missing fixed inputs'}
  Layout-Workflow;$bitmap=New-Object Drawing.Bitmap($window.Width,$window.Height);$window.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)));$bitmap.Save((Join-Path $Root '.runtime/workflow-preview.png'));$bitmap.Dispose()
  $run.PerformClick();Wait-Request
  if($run.Enabled -or $script:preview){throw 'Confirmation remained reusable in UI'}
  $tabs.SelectedTab=$rp;[Windows.Forms.Application]::DoEvents()
  $resultItem=@($results.Items | Where-Object {$_.Tag.label -like '*整理本周材料*'})[0];$resultItem.Selected=$true;[Windows.Forms.Application]::DoEvents()
  if(-not $results.Items.Count -or -not $files.Items.Count -or -not $open.Enabled -or -not $folder.Enabled){throw 'Result card or file actions missing'}
  if($conclusion.Text -notlike '*检查通过*'){throw 'Check conclusion missing'}
  Layout-Workflow;$bitmap=New-Object Drawing.Bitmap($window.Width,$window.Height);$window.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)));$bitmap.Save((Join-Path $Root '.runtime/workflow-results.png'));$bitmap.Dispose()
  if(-not $revise.Enabled){throw 'Revision entry unavailable for valid result'}
  $sourceId=(Selected $results).id;$revise.PerformClick();Wait-Request
  if($script:revisionSource.id -ne $sourceId -or $tabs.SelectedTab -ne $vp){throw 'Wrong revision source'}
  $revisionBody.Text='增加分项说明，保留金额合计。';$revisionCheck.PerformClick();Wait-Request
  if(-not $revisionRun.Enabled -or $revisionReview.Text -notlike '*新结果位置*'){throw 'Revision preview missing'}
  $revisionFiles.Items[0].Checked=$false
  if($revisionRun.Enabled){throw 'File selection did not invalidate preview'}
  $revisionFiles.Items[0].Checked=$true;$revisionMode.SelectedIndex=1
  $revisionCheck.PerformClick();Wait-Request
  if($script:revisionPreview.review.mode -ne 'text' -or $revisionFiles.Enabled){throw 'Text revision selection failed'}
  $revisionBody.Text+=' 末尾补充结论。'
  if($revisionRun.Enabled){throw 'Body edit did not invalidate preview'}
  $revisionMode.SelectedIndex=0;$revisionCheck.PerformClick();Wait-Request;Layout-Workflow
  if($revisionRun.Right -gt $vp.ClientSize.Width -or $revisionReview.Parent.Right -gt $vp.ClientSize.Width){throw 'Revision controls outside page'}
  $bitmap=New-Object Drawing.Bitmap($window.Width,$window.Height);$window.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)));$bitmap.Save((Join-Path $Root '.runtime/workflow-revision.png'));$bitmap.Dispose()
  $revisionRun.PerformClick();Wait-Request
  if($revisionRun.Enabled -or $script:revisionPreview){throw 'Revision confirmation remained reusable'}
  $refresh.PerformClick();Wait-Request
  if($versions.Items.Count -ne 2){throw 'Revision lineage not refreshed'}
  $queued=@($versions.Items | Where-Object {$_.Tag.state -eq 'queued'})[0];$queued.Selected=$true;[Windows.Forms.Application]::DoEvents()
  if($useVersion.Enabled){throw 'Unfinished version is editable'}
  $tabs.SelectedTab=$lp;[Windows.Forms.Application]::DoEvents()
  $dropData=New-Object Windows.Forms.DataObject
  $dropData.SetData([Windows.Forms.DataFormats]::FileDrop,[string[]]@((Join-Path $Root 'desktop-input.csv'),(Join-Path $Root 'desktop-input.csv'),$Root))
  Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System.Reflection;
using System.Windows.Forms;
public static class FileDropProbe {
 public static bool Drop(Control control, IDataObject data) {
  var args = new DragEventArgs(data, 0, 0, 0, DragDropEffects.Copy, DragDropEffects.None);
  var flags = BindingFlags.Instance | BindingFlags.NonPublic;
  typeof(Control).GetMethod("OnDragEnter", flags).Invoke(control, new object[] { args });
  if (args.Effect != DragDropEffects.Copy) return false;
  typeof(Control).GetMethod("OnDragDrop", flags).Invoke(control, new object[] { args });
  return true;
 }
}
'@
  if(-not [FileDropProbe]::Drop($dropZone,$dropData)){throw 'File drop not accepted'}
  if($localFiles.Items.Count -ne 1 -or $localCheck.Enabled){throw 'Drop deduplication, directory rejection or task requirements failed'}
  $localTemplate.SelectedIndex=1;$localBody.Text='按项目汇总，并列出缺失项。';$localDelivery.SelectedIndex=1
  $localCheck.PerformClick();Wait-Request
  if(-not $localRun.Enabled -or $localReview.Text -notlike '*同时发送到微信*' -or $localReview.Text -notlike '*新结果位置*'){throw 'File preview missing delivery or output'}
  $localBody.Text+=' 保留原始数据。'
  if($localRun.Enabled){throw 'File task text edit did not invalidate preview'}
  $localCheck.PerformClick();Wait-Request;$localDelivery.SelectedIndex=0
  if($localRun.Enabled){throw 'Delivery edit did not invalidate preview'}
  $localCheck.PerformClick();Wait-Request
  $localFiles.Items[0].Selected=$true;$localRemove.PerformClick()
  if($localFiles.Items.Count -or $localRun.Enabled){throw 'Removal did not invalidate preview'}
  Add-LocalPaths @((Join-Path $Root 'desktop-input.csv'))
  $localCheck.PerformClick();Wait-Request;Layout-Workflow
  if($localRun.Right -gt $lp.ClientSize.Width -or $localReview.Parent.Right -gt $lp.ClientSize.Width){throw 'File task controls outside page'}
  $bitmap=New-Object Drawing.Bitmap($window.Width,$window.Height);$window.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)));$bitmap.Save((Join-Path $Root '.runtime/workflow-files.png'));$bitmap.Dispose()
  $localRun.PerformClick();Wait-Request
  if($localRun.Enabled -or $script:localPreview){throw 'File confirmation remained reusable'}
  $localTemplate.SelectedIndex=0;$localBody.Text='列出材料中的项目和金额';$localCheck.PerformClick();Wait-Request
  if($script:localPreview.review.template){throw 'Custom task reused a template'}
  $localRun.PerformClick();Wait-Request
  $tabs.SelectedTab=$sp;[Windows.Forms.Application]::DoEvents();$scheduleNew.PerformClick()
  $scheduleBody.Text='每天整理指定材料并输出汇总';$scheduleKind.SelectedIndex=1;$scheduleRepeat.SelectedIndex=1
  $scheduleTime.Value=[DateTime]::Today.AddDays(1).AddHours(9)
  $before=@($script:data.schedules.items).Count;$scheduleCheck.PerformClick();Wait-Request
  if(-not $scheduleSave.Enabled -or $scheduleReview.Text -notlike '*自动执行*' -or $scheduleReview.Text -notlike '*微信用户*'){throw 'Schedule preview missing recipient or automatic execution'}
  if(@($script:data.schedules.items).Count -ne $before){throw 'Schedule preview created a plan'}
  $scheduleBody.Text+=' 保留原始文件。'
  if($scheduleSave.Enabled){throw 'Schedule content edit did not invalidate preview'}
  $scheduleCheck.PerformClick();Wait-Request;$scheduleTime.Value=$scheduleTime.Value.AddMinutes(1)
  if($scheduleSave.Enabled){throw 'Schedule time edit did not invalidate preview'}
  $scheduleCheck.PerformClick();Wait-Request;Layout-Workflow
  if($scheduleSave.Right -gt $sp.ClientSize.Width -or $scheduleReview.Parent.Right -gt $sp.ClientSize.Width -or $scheduleRepeat.Right -gt $sp.ClientSize.Width){throw 'Schedule controls outside page'}
  $bitmap=New-Object Drawing.Bitmap($window.Width,$window.Height);$window.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)));$bitmap.Save((Join-Path $Root '.runtime/workflow-schedules.png'));$bitmap.Dispose()
  $scheduleSave.PerformClick();Wait-Request
  if($scheduleSave.Enabled -or @($script:data.schedules.items).Count -ne $before+1 -or -not (Selected $scheduleList).daily){throw 'Schedule creation did not persist exactly one daily plan'}
  $scheduleToggle.PerformClick();Wait-Request
  if(-not (Selected $scheduleList).paused -or $scheduleToggle.Text -ne '恢复计划'){throw 'Pause not reflected'}
  $scheduleBody.Text='编辑后保留暂停';$scheduleCheck.PerformClick();Wait-Request
  if($scheduleReview.Text -notlike '*仍保持暂停*'){throw 'Paused edit preview unclear'}
  $scheduleSave.PerformClick();Wait-Request
  if(-not (Selected $scheduleList).paused -or (Selected $scheduleList).text -ne '编辑后保留暂停'){throw 'Edit unpaused or failed'}
  $scheduleToggle.PerformClick();Wait-Request
  if((Selected $scheduleList).paused){throw 'Resume failed'}
  $scheduleDelete.PerformClick();Wait-Request
  if(@($script:data.schedules.items).Count -ne $before){throw 'Delete failed'}
  $scheduleNew.PerformClick();$scheduleBody.Text='一次性提醒';$scheduleTime.Value=[DateTime]::Now.AddHours(2)
  $scheduleCheck.PerformClick();Wait-Request
  if($script:schedulePreview.review.daily -or $script:schedulePreview.review.kind -ne 'remind'){throw 'Once reminder fields incorrect'}
  $scheduleSave.PerformClick();Wait-Request;$scheduleDelete.PerformClick();Wait-Request
  $scheduleMode.SelectedIndex=1;[Windows.Forms.Application]::DoEvents();$scheduleList.Items[0].Selected=$true;[Windows.Forms.Application]::DoEvents()
  if(-not $scheduleReceipt.Enabled -or $scheduleSave.Enabled -or $scheduleBody.Enabled -or $scheduleReview.Text -notlike '*处理结束*'){throw 'Schedule history receipt link or read-only state missing'}
  $scheduleMode.SelectedIndex=0;$scheduleNew.PerformClick();$scheduleBody.Text='切换用户前的草稿';$scheduleCheck.PerformClick();Wait-Request
  $owner.SelectedItem='other';Wait-Request
  if($localFiles.Items.Count -or $localBody.Text -or $script:localPreview){throw 'Owner switch retained local draft'}
  if($scheduleBody.Text -or $script:schedulePreview -or $script:scheduleEditing -or $scheduleList.Items.Count){throw 'Owner switch retained schedule draft'}
  $savedSchedules=$script:data.schedules;$script:data.PSObject.Properties.Remove('schedules');Populate-Schedules $null
  if($scheduleNew.Enabled -or $scheduleGuide.Text -notlike '*重启后台*' -or -not $tabs.Enabled){throw 'Old backend schedule capability handling failed'}
  Populate-Schedules $savedSchedules
  Write-Output 'PASS schedule editor: preview, invalidation, daily/once creation, edit, pause/resume, delete, history receipt and owner isolation'
  Write-Output 'PASS workflow window: pins, checklist, template run, results, file/text revision preview, invalidation, one confirmation, version lineage; no document launched'
  Write-Output 'PASS local file task: drop event, duplicates, folder rejection, removal, template/custom preview, edits invalidate, delivery, once-only submission, owner switch'
 }finally{$window.Dispose()}
}else{[void]$window.ShowDialog();$window.Dispose()}
