# Result revision controls and preview state. Dot-sourced by workflow_window.ps1.
$vp=$pages.revise
$versions=List $vp 16 18 240 418 @('版本记录','状态')
$versions.ShowItemToolTips=$true
$useVersion=Button $vp '以此版本继续修改' 16 452 240
$revisionTitle=Label $vp '先在“任务结果”中选择一条结果，再点击“继续修改”。' 276 18 600 44
$revisionMode=Combo $vp 276 72 600;$revisionMode.Anchor='Top,Left,Right'
$revisionFiles=List $vp 276 112 600 100 @('选择需要修改的文件','大小');$revisionFiles.CheckBoxes=$true
$null=Label $vp '这次希望怎么改？' 276 222 600 24
$revisionBody=Box $vp 276 248 600 66 $false;$revisionBody.MaxLength=8000
$revisionReview=Box $vp 276 326 600 110
$revisionCheck=Button $vp '核对本次修改' 276 452 184
$revisionRun=Button $vp '确认修改' 696 452 180;$revisionRun.Anchor='Top,Right';$revisionRun.Primary=$true
$script:revisionSource=$null;$script:revisionPreview=$null;$script:revisionTaskId='';$script:revisionModes=@()
$script:revisionFocusPending=($Page -eq 'revise' -and [bool]$ItemId)
if($script:revisionFocusPending){$script:revisionTaskId=$ItemId}
function Invalidate-Revision {
 $script:revisionPreview=$null
 $revisionReview.Text='核对后将保存旧版本副本，新结果另存到独立目录。修改要求或所选文件变化后，需要重新核对。'
 Update-Actions
}
function Reset-Revision {
 $script:revisionSource=$null;$script:revisionTaskId='';$script:revisionFocusPending=$false
 $versions.Items.Clear();$revisionFiles.Items.Clear();$revisionMode.Items.Clear();$script:revisionModes=@()
 $revisionBody.Clear();$revisionTitle.Text='先在“任务结果”中选择一条结果，再点击“继续修改”。';Invalidate-Revision
}
function Start-Revision($id){
 if($script:job -or $id -notmatch '^[a-f0-9]{32}$'){return}
 $script:revisionTaskId=$id;$script:revisionSource=$null;Invalidate-Revision;$tabs.SelectedTab=$vp;Request 'revision_source'
}
function Show-RevisionSource($source){
 $script:loading=$true
 try{
  $script:revisionSource=$source;$script:revisionTaskId=$source.id
  $revisionTitle.Text='基于：'+$source.label+' · '+$source.id.Substring(0,8)
  $versions.Items.Clear()
  foreach($v in @($source.versions)){
   $name=if($v.id -eq $source.id){'当前 · '+$v.label}else{$v.label}
   $item=New-Object Windows.Forms.ListViewItem($name);$null=$item.SubItems.Add([string]$stateNames[[string]$v.state]);$item.ToolTipText=$v.label+"`r`n"+$v.id;$item.Tag=$v;$null=$versions.Items.Add($item)
   if($v.id -eq $source.id){$item.Selected=$true;$item.EnsureVisible()}
  }
  $revisionFiles.Items.Clear()
  foreach($f in @($source.files)){
   $sizeText=if($f.size -lt 1024){[string]$f.size+' B'}else{[Math]::Round($f.size/1024,1).ToString()+' KB'}
   $item=New-Object Windows.Forms.ListViewItem([string]$f.name);$null=$item.SubItems.Add($sizeText);$item.Tag=$f;$item.Checked=$true;$null=$revisionFiles.Items.Add($item)
  }
  $revisionMode.Items.Clear();$script:revisionModes=@()
  if(@($source.files).Count){$script:revisionModes+='files';$null=$revisionMode.Items.Add('修改选中的结果文件')}
  if($source.has_text){$script:revisionModes+='text';$null=$revisionMode.Items.Add('修改完整文字回复')}
  if($revisionMode.Items.Count){$revisionMode.SelectedIndex=0}else{$revisionTitle.Text+=' · 暂无可修改内容'}
 }finally{$script:loading=$false}
 Invalidate-Revision
 if(-not $source.has_text -and $source.text_reason){$revisionReview.Text+="`r`n"+$source.text_reason}
 Size-Columns
}
function Update-RevisionActions($ready){
 $selectedResult=Selected $results;$revise.Enabled=($ready -and $selectedResult -and $selectedResult.can_revise)
 $v=Selected $versions;$useVersion.Enabled=($ready -and $v -and $v.state -in @('completed','failed','cancelled','uncertain','interrupted'))
 $hasMode=($revisionMode.SelectedIndex -ge 0 -and $script:revisionSource)
 $revisionFiles.Enabled=($ready -and $hasMode -and $script:revisionModes[$revisionMode.SelectedIndex] -eq 'files')
 $revisionCheck.Enabled=($ready -and $hasMode -and -not [string]::IsNullOrWhiteSpace($revisionBody.Text))
 $revisionRun.Enabled=($ready -and $script:revisionPreview -and -not $script:revisionPreview.launch_reason)
}
function Add-RevisionPayload($payload,$action){
 if($action -in @('revision_source','revision_preview')){$payload.task_id=$script:revisionTaskId}
 if($action -eq 'revision_preview'){
  $payload.mode=$script:revisionModes[$revisionMode.SelectedIndex];$payload.body=$revisionBody.Text
  $payload.indexes=@($revisionFiles.CheckedItems | ForEach-Object {[int]$_.Tag.index})
 }
 if($action -eq 'revision_run'){$payload.token=$script:revisionPreview.token}
}
function Show-RevisionPreview($data){
 $script:revisionPreview=$data;$r=$data.review
 $lines=@(('基于：'+$r.source_label),('修改对象：'+$(if($r.mode -eq 'text'){'完整文字回复'}else{@($r.files) -join '、'})),('本次要求：'+$r.instructions),('新结果位置：'+$r.output_dir),'确认后保存旧版本副本，再加入任务队列。')
 if($data.launch_reason){$lines+=$data.launch_reason}
 $revisionReview.Text=$lines -join "`r`n";Update-Actions
}
$revise.Add_Click({$r=Selected $results;if($r){Start-Revision ([string]$r.id)}})
$useVersion.Add_Click({$v=Selected $versions;if($v){Start-Revision ([string]$v.id)}})
$versions.Add_SelectedIndexChanged({Update-Actions})
$revisionMode.Add_SelectedIndexChanged({if(-not $script:loading){Invalidate-Revision}})
$revisionFiles.Add_ItemChecked({if(-not $script:loading){Invalidate-Revision}})
$revisionBody.Add_TextChanged({if(-not $script:loading){Invalidate-Revision}})
$revisionCheck.Add_Click({Request 'revision_preview'})
$revisionRun.Add_Click({Request 'revision_run'})
Invalidate-Revision
