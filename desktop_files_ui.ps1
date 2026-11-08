# Local file selection and reviewed submission. Loaded by workflow_window.ps1.
$lp=$pages.localfiles
$null=Label $lp '1  选择材料' 16 18 240 28
$dropZone=Label $lp "把文件拖到这里`r`n也可以点击下方「选择文件」`r`n最多 12 个 · 单个 20 MB" 16 54 240 90
$dropZone.BackColor=[TrayDesign]::BlueSoft;$dropZone.TextAlign='MiddleCenter';$dropZone.AllowDrop=$true
$localAdd=Button $lp '选择文件' 16 156 112
$localRemove=Button $lp '移出清单' 140 156 116
$localFiles=List $lp 16 200 240 236 @('文件','大小');$localFiles.ShowItemToolTips=$true;$localFiles.AllowDrop=$true
$localCount=Label $lp '尚未选择文件' 16 452 240 34
$null=Label $lp '2  填写任务与结果去向' 276 18 600 28
$localTemplate=Combo $lp 276 54 600;$localTemplate.Anchor='Top,Left,Right'
$localDefinition=Box $lp 276 94 600 74
$localBodyLabel=Label $lp '任务要求' 276 178 600 24
$localBody=Box $lp 276 204 600 70 $false;$localBody.MaxLength=8000
$localDelivery=Combo $lp 276 286 600;$localDelivery.Anchor='Top,Left,Right'
$null=$localDelivery.Items.Add('文件留在本机，摘要发微信')
$null=$localDelivery.Items.Add('文件留在本机，同时发送到微信');$localDelivery.SelectedIndex=0
$localReview=Box $lp 276 332 600 104
$localCheck=Button $lp '3  核对并预览' 276 452 184
$localRun=Button $lp '确认创建任务' 696 452 180;$localRun.Anchor='Top,Right';$localRun.Primary=$true
$script:localPreview=$null;$script:localTemplates=@()
function Invalidate-LocalFiles {
 $script:localPreview=$null
 $localReview.Text='先核对文件清单与任务要求。确认后保存输入副本，新结果另存到独立目录；摘要发往上方选中的微信用户。'
 Update-Actions
}
function Reset-LocalFiles {
 $localFiles.Items.Clear();$localBody.Clear();$localDelivery.SelectedIndex=0
 if($localTemplate.Items.Count){$localTemplate.SelectedIndex=0}
 Invalidate-LocalFiles
}
function Populate-LocalTemplates($data){
 $old=if($localTemplate.SelectedIndex -gt 0){$script:localTemplates[$localTemplate.SelectedIndex-1].id}else{''}
 $script:localTemplates=@($data.templates)
 $localTemplate.Items.Clear();$null=$localTemplate.Items.Add('自定义任务 · 直接填写要求');$localTemplate.SelectedIndex=0
 foreach($t in $script:localTemplates){$index=$localTemplate.Items.Add($t.name+' · 第 '+$t.revision+' 版');if($t.id -eq $old){$localTemplate.SelectedIndex=$index}}
 Show-LocalTemplate
}
function Show-LocalTemplate {
 $t=if($localTemplate.SelectedIndex -gt 0){$script:localTemplates[$localTemplate.SelectedIndex-1]}else{$null}
 $localBodyLabel.Text=if($t){'本次补充要求（可选）'}else{'任务要求'}
 $localDefinition.Text=if($t){'输入要求：'+$t.definition.inputs+"`r`n步骤："+$t.definition.steps+"`r`n输出格式："+$t.definition.output_format+"`r`n本次使用独立结果目录，核对时显示完整路径。"}else{'例如：合并这些报表，按项目汇总金额，并列出缺失数据。结果保存在本机独立目录，可从任务结果页打开。'}
 Invalidate-LocalFiles
}
function Add-LocalPaths($paths){
 if($script:job){return}
 $rejected=0;$duplicates=0;$added=0
 foreach($path in @($paths)){
  if(@($localFiles.Items | Where-Object {[string]$_.Tag.path -eq [string]$path}).Count){$duplicates++;continue}
  if($localFiles.Items.Count -ge 12){$rejected++;continue}
  if([string]$path -notmatch '^[A-Za-z]:[\\/]' -or [string]$path -match '(?:^|[\\/])\.\.(?:[\\/]|$)'){$rejected++;continue}
  try{
   $drive=New-Object IO.DriveInfo([IO.Path]::GetPathRoot([string]$path))
   if($drive.DriveType -eq 'Network' -or -not [IO.File]::Exists([string]$path)){$rejected++;continue}
   $info=New-Object IO.FileInfo([string]$path)
   $total=0L;foreach($item in $localFiles.Items){$total+=[long]$item.Tag.size}
   if($info.Length -le 0 -or $info.Length -gt 20MB -or $total+$info.Length -gt 100MB){$rejected++;continue}
   $size=if($info.Length -lt 1024){[string]$info.Length+' B'}else{[Math]::Round($info.Length/1024,1).ToString()+' KB'}
   $item=New-Object Windows.Forms.ListViewItem($info.Name);$null=$item.SubItems.Add($size);$item.Tag=@{path=$info.FullName;size=$info.Length};$item.ToolTipText=$info.FullName;$null=$localFiles.Items.Add($item);$added++
  }catch{$rejected++}
 }
 Invalidate-LocalFiles;Size-Columns
 $feedback.Text='已添加 '+$added+' 个文件。'
 if($duplicates){$feedback.Text+=' 已忽略 '+$duplicates+' 个重复文件。'}
 if($rejected){$feedback.Text+=' '+$rejected+' 项未添加；请选择本机非空文件，最多 12 个、单个 20 MB、合计 100 MB。'}
}
function Receive-LocalDrop($event){
 if(-not $script:job -and $event.Data.GetDataPresent([Windows.Forms.DataFormats]::FileDrop)){
  Add-LocalPaths @($event.Data.GetData([Windows.Forms.DataFormats]::FileDrop))
 }
}
foreach($target in @($dropZone,$localFiles)){
 $target.Add_DragEnter({param($sender,$event) if(-not $script:job -and $event.Data.GetDataPresent([Windows.Forms.DataFormats]::FileDrop)){$event.Effect=[Windows.Forms.DragDropEffects]::Copy}else{$event.Effect=[Windows.Forms.DragDropEffects]::None}})
 $target.Add_DragDrop({param($sender,$event) Receive-LocalDrop $event})
}
function Update-LocalFileActions($ready){
 $localAdd.Enabled=$ready;$localRemove.Enabled=($ready -and $localFiles.SelectedItems.Count -gt 0)
 $localCheck.Enabled=($ready -and $localFiles.Items.Count -gt 0 -and ($localTemplate.SelectedIndex -gt 0 -or -not [string]::IsNullOrWhiteSpace($localBody.Text)))
 $localRun.Enabled=($ready -and $script:localPreview -and -not $script:localPreview.launch_reason)
 $total=0L;foreach($item in $localFiles.Items){$total+=[long]$item.Tag.size}
 $totalText=if($total -lt 1KB){[string]$total+' B'}elseif($total -lt 1MB){[Math]::Round($total/1KB,1).ToString()+' KB'}else{[Math]::Round($total/1MB,2).ToString()+' MB'}
 $localCount.Text=if($localFiles.Items.Count){[string]$localFiles.Items.Count+' 个文件 · '+$totalText}else{'尚未选择文件'}
}
function Add-LocalFilePayload($payload,$action){
 if($action -eq 'files_preview'){
  $payload.paths=@($localFiles.Items | ForEach-Object {[string]$_.Tag.path});$payload.body=$localBody.Text
  $payload.delivery=if($localDelivery.SelectedIndex -eq 1){'files'}else{'summary'}
  if($localTemplate.SelectedIndex -gt 0){$t=$script:localTemplates[$localTemplate.SelectedIndex-1];$payload.name=$t.name;$payload.expected_revision=[long]$t.revision}
 }
 if($action -eq 'files_run'){$payload.token=$script:localPreview.token}
}
function Show-LocalPreview($data){
 $script:localPreview=$data;$r=$data.review
 $lines=@(('微信用户：'+$data.uid),('结果去向：'+$r.delivery_label),('新结果位置：'+$r.output_dir))
 if($r.template){$lines+=('模板：'+$r.template.name+' · 第 '+$r.template.revision+' 版');$lines+='处理步骤：'+$r.template.definition.steps;$lines+='输出格式：'+$r.template.definition.output_format;if($r.template.definition.checks){$lines+='检查条件：'+$r.template.definition.checks}}
 if($r.body){$lines+='本次要求：'+$r.body}
 $lines+='本次文件：';foreach($f in @($r.files)){$lines+=($f.name+' · '+$f.path)}
 if($r.delivery -eq 'files'){$lines+='微信文件交付沿用当前配置：单次最多 '+$r.send_limit+' 个，大小上限 '+$(if($r.send_max_mb){[string]$r.send_max_mb+' MB'}else{'按既有文件校验限制'})+'。超限会明确提示未发送项。'}
 if($data.launch_reason){$lines+=$data.launch_reason}
 $localReview.Text=$lines -join "`r`n";Update-Actions
}
$localAdd.Add_Click({
 $dialog=New-Object Windows.Forms.OpenFileDialog;$dialog.Title='选择本次任务的文件';$dialog.Multiselect=$true;$dialog.CheckFileExists=$true;$dialog.Filter='所有文件 (*.*)|*.*'
 try{if($dialog.ShowDialog($window) -eq 'OK'){Add-LocalPaths $dialog.FileNames}}finally{$dialog.Dispose()}
})
$localRemove.Add_Click({foreach($item in @($localFiles.SelectedItems)){$localFiles.Items.Remove($item)};Invalidate-LocalFiles})
$localFiles.Add_SelectedIndexChanged({Update-Actions})
$localTemplate.Add_SelectedIndexChanged({if(-not $script:loading){Show-LocalTemplate}})
$localBody.Add_TextChanged({if(-not $script:loading){Invalidate-LocalFiles}})
$localDelivery.Add_SelectedIndexChanged({if(-not $script:loading){Invalidate-LocalFiles}})
$localCheck.Add_Click({Request 'files_preview'});$localRun.Add_Click({Request 'files_run'})
Invalidate-LocalFiles
