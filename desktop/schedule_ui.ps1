# Schedule editor and occurrence receipts. Loaded by workflow_window.ps1.
$sp=$pages.schedules
$scheduleMode=Combo $sp 16 18 240
$null=$scheduleMode.Items.Add('计划清单');$null=$scheduleMode.Items.Add('最近触发 · 最多 50 条');$scheduleMode.SelectedIndex=0
$scheduleList=List $sp 16 62 240 322 @('计划 / 触发内容','状态');$scheduleList.ShowItemToolTips=$true
$scheduleNew=Button $sp '新建计划' 16 400 112
$scheduleToggle=Button $sp '暂停计划' 140 400 116
$scheduleDelete=Button $sp '删除计划' 16 452 112
$scheduleReceipt=Button $sp '任务记录' 140 452 116
$scheduleGuide=Label $sp '' 276 18 600 44
$scheduleKind=Combo $sp 276 70 244
$null=$scheduleKind.Items.Add('微信提醒 · 到点发消息');$null=$scheduleKind.Items.Add('自动任务 · 到点调用模型');$scheduleKind.SelectedIndex=0
$scheduleRepeat=Combo $sp 536 70 340
$null=$scheduleRepeat.Items.Add('一次性');$null=$scheduleRepeat.Items.Add('每天重复');$scheduleRepeat.SelectedIndex=0
$scheduleTimeLabel=Label $sp '执行时间（本机时区）' 276 112 600 24
$scheduleTime=New-Object Windows.Forms.DateTimePicker;$scheduleTime.Format='Custom';$scheduleTime.CustomFormat='yyyy-MM-dd HH:mm:ss';$scheduleTime.SetBounds(276,138,600,32);$sp.Controls.Add($scheduleTime);$script:layout[$scheduleTime]=@(276,138,600,32)
$null=Label $sp '提醒内容 / 自动执行要求（最多 300 字）' 276 182 600 24
$scheduleBody=Box $sp 276 208 600 88 $false;$scheduleBody.MaxLength=300
$scheduleReview=Box $sp 276 308 600 128
$scheduleCheck=Button $sp '核对计划' 276 452 184
$scheduleSave=Button $sp '确认保存计划' 696 452 180;$scheduleSave.Anchor='Top,Right';$scheduleSave.Primary=$true
$script:schedulePreview=$null;$script:scheduleEditing=$null;$script:scheduleLoading=$false
function Invalidate-Schedule {
 $script:schedulePreview=$null
 $scheduleReview.Text='确认后到点自动触发。自动任务使用触发时的模型，会消耗额度并可能修改文件。暂停仅影响后续触发，已入队的任务请到任务记录处理。'
 Update-Actions
}
function Reset-Schedule {
 $script:scheduleLoading=$true
 try{
  $scheduleList.SelectedItems.Clear();$script:scheduleEditing=$null
  $scheduleKind.SelectedIndex=0;$scheduleRepeat.SelectedIndex=0;$scheduleTime.Value=[DateTime]::Now.AddHours(1);$scheduleTime.CustomFormat='yyyy-MM-dd HH:mm:ss';$scheduleTime.ShowUpDown=$false;$scheduleBody.Clear()
 }finally{$script:scheduleLoading=$false}
 Invalidate-Schedule
}
function Populate-Schedules($data,$focus='') {
 $old=Selected $scheduleList;if(-not $focus -and $old){$focus=[string]$old.id}
 if(-not $data){$data=[pscustomobject]@{enabled=$false;message='后台尚未加载定时任务管理，请重启后台后重新读取。';items=@();history=@()}}
 if($script:data.PSObject.Properties.Name -contains 'schedules'){$script:data.schedules=$data}else{$script:data | Add-Member -NotePropertyName schedules -NotePropertyValue $data}
 $script:scheduleLoading=$true
 try{
  $scheduleList.Items.Clear()
  $rows=if($scheduleMode.SelectedIndex -eq 1){@($data.history)}else{@($data.items)}
  foreach($r in $rows){
   $item=New-Object Windows.Forms.ListViewItem([string]$r.text)
   $null=$item.SubItems.Add([string]$r.state_label);$item.Tag=$r
   $item.ToolTipText=if($scheduleMode.SelectedIndex -eq 1){$r.fired_label+' · '+$r.state_label+' · '+$r.text}else{$r.kind_label+' · '+$r.next_label+' · '+$r.text}
   $null=$scheduleList.Items.Add($item);if($r.id -eq $focus){$item.Selected=$true;$item.EnsureVisible()}
  }
 }finally{$script:scheduleLoading=$false}
 if($scheduleList.SelectedItems.Count){Show-Schedule}else{Reset-Schedule}
 Size-Columns
}
function Show-Schedule {
 $r=Selected $scheduleList;if(-not $r){Reset-Schedule;return}
 $script:scheduleLoading=$true
 try{
  $history=$scheduleMode.SelectedIndex -eq 1
  $script:scheduleEditing=if($history){$null}else{$r}
  $scheduleKind.SelectedIndex=if($r.kind -eq 'task'){1}else{0}
  $scheduleRepeat.SelectedIndex=if($r.daily){1}else{0}
  $scheduleTime.Value=[DateTimeOffset]::FromUnixTimeSeconds([long]$(if($history){$r.scheduled_at}else{$r.at})).LocalDateTime
  $scheduleTime.CustomFormat=if($r.daily){'HH:mm'}else{'yyyy-MM-dd HH:mm:ss'};$scheduleTime.ShowUpDown=[bool]$r.daily
  $scheduleBody.Text=[string]$r.text
 }finally{$script:scheduleLoading=$false}
 Invalidate-Schedule
 if($history){$scheduleReview.Text='计划时间：'+$r.scheduled_label+"`r`n触发时间："+$r.fired_label+"`r`n状态："+$r.state_label+"`r`n"+$r.detail}
 else{
  $scheduleReview.Text='下次时间：'+$r.next_label+$(if($r.paused){'（已暂停，不会触发）'}else{''})
  if($r.latest){$scheduleReview.Text+="`r`n最近触发："+$r.latest.fired_label+' · '+$r.latest.state_label+"`r`n"+$r.latest.detail}else{$scheduleReview.Text+="`r`n尚无触发记录。"}
  $scheduleReview.Text+="`r`n编辑后请重新核对再保存。暂停仅影响后续触发。"
 }
 Update-Actions
}
function Update-ScheduleActions($ready) {
 $enabled=$ready -and $script:data.schedules.enabled
 $history=$scheduleMode.SelectedIndex -eq 1;$r=Selected $scheduleList
 $scheduleNew.Enabled=$enabled;$scheduleMode.Enabled=$enabled
 $scheduleToggle.Enabled=($enabled -and -not $history -and [bool]$r);$scheduleDelete.Enabled=$scheduleToggle.Enabled
 $scheduleToggle.Text=if($r -and $r.paused){'恢复计划'}else{'暂停计划'}
 $receipt=if($history){$r}else{if($r){$r.latest}else{$null}}
 $scheduleReceipt.Enabled=($ready -and $receipt -and $receipt.task_id -match '^[a-f0-9]{32}$')
 foreach($c in @($scheduleKind,$scheduleRepeat,$scheduleTime,$scheduleBody)){$c.Enabled=$enabled -and -not $history}
 $scheduleCheck.Enabled=($enabled -and -not $history -and -not [string]::IsNullOrWhiteSpace($scheduleBody.Text))
 $scheduleSave.Enabled=($enabled -and -not $history -and [bool]$script:schedulePreview)
 $scheduleGuide.Text=if(-not $script:data.schedules.enabled){[string]$script:data.schedules.message}elseif($script:data.schedules.message){[string]$script:data.schedules.message}else{([string]@($script:data.schedules.items).Count)+' / '+$script:data.schedules.limit+" 条计划 · "+$(if($history){'最近触发记录'}elseif($script:scheduleEditing){'编辑所选计划'}else{'新建计划'})+"`r`n后台运行且电脑保持唤醒时触发；待命时也会触发。"}
 $scheduleTimeLabel.Text=if($history){'原计划时间（本机时区）'}elseif($scheduleRepeat.SelectedIndex -eq 1){'每天执行的时刻（本机时区，恢复时跳过错过的日期）'}else{'执行时间（本机时区，未来 7 天以内）'}
}
function Add-SchedulePayload($payload,$action) {
 if($action -eq 'schedule_preview'){
  $plan=@{kind=$(if($scheduleKind.SelectedIndex -eq 1){'task'}else{'remind'});text=$scheduleBody.Text;repeat=$(if($scheduleRepeat.SelectedIndex -eq 1){'daily'}else{'once'})}
  if($plan.repeat -eq 'daily'){$plan.hour=[int]$scheduleTime.Value.Hour;$plan.minute=[int]$scheduleTime.Value.Minute}else{$plan.at=([DateTimeOffset]$scheduleTime.Value.ToUniversalTime()).ToUnixTimeSeconds()}
  $payload.plan=$plan
  if($script:scheduleEditing){$payload.schedule_id=$script:scheduleEditing.id;$payload.expected_version=$script:scheduleEditing.version}
 }
 if($action -eq 'schedule_save'){$payload.token=$script:schedulePreview.token}
 if($action -in @('schedule_pause','schedule_resume','schedule_delete')){$r=Selected $scheduleList;$payload.schedule_id=$r.id;$payload.expected_version=$r.version}
}
function Show-SchedulePreview($data) {
 $script:schedulePreview=$data;$r=$data.review
 $scheduleReview.Text='微信用户：'+$data.uid+"`r`n"+$(if($r.kind -eq 'task'){'自动执行：到点交给当时选用的模型处理，会消耗额度并可能修改文件。'}else{'微信提醒：到点发送下方内容。'})+"`r`n"+$r.label+' · 下次 '+$r.next_label+$(if($r.paused){'（保存后仍保持暂停）'}else{''})+"`r`n内容："+$r.text
 Update-Actions
}
$scheduleList.Add_SelectedIndexChanged({if(-not $script:scheduleLoading -and -not $script:loading){Show-Schedule}})
$scheduleMode.Add_SelectedIndexChanged({if(-not $script:scheduleLoading -and $script:data){Populate-Schedules $script:data.schedules}})
$scheduleNew.Add_Click({$scheduleMode.SelectedIndex=0;Reset-Schedule})
$scheduleRepeat.Add_SelectedIndexChanged({if(-not $script:scheduleLoading){$scheduleTime.CustomFormat=if($scheduleRepeat.SelectedIndex -eq 1){'HH:mm'}else{'yyyy-MM-dd HH:mm:ss'};$scheduleTime.ShowUpDown=$scheduleRepeat.SelectedIndex -eq 1;Invalidate-Schedule}})
$scheduleKind.Add_SelectedIndexChanged({if(-not $script:scheduleLoading){Invalidate-Schedule}})
$scheduleTime.Add_ValueChanged({if(-not $script:scheduleLoading){Invalidate-Schedule}})
$scheduleBody.Add_TextChanged({if(-not $script:scheduleLoading){Invalidate-Schedule}})
$scheduleCheck.Add_Click({Request 'schedule_preview'});$scheduleSave.Add_Click({Request 'schedule_save'})
$scheduleToggle.Add_Click({$r=Selected $scheduleList;Request $(if($r.paused){'schedule_resume'}else{'schedule_pause'})})
$scheduleDelete.Add_Click({if($Verify -or [Windows.Forms.MessageBox]::Show($window,'删除所选计划？之后不会再触发，已有触发记录和已入队任务仍保留。','删除计划','YesNo','Question') -eq 'Yes'){Request 'schedule_delete'}})
$scheduleReceipt.Add_Click({
 $r=Selected $scheduleList;if($scheduleMode.SelectedIndex -eq 0){$r=$r.latest}
 if(-not $r -or $r.task_id -notmatch '^[a-f0-9]{32}$'){return}
 $info=New-Object Diagnostics.ProcessStartInfo;$info.FileName='powershell.exe';$info.Arguments='-NoProfile -STA -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'task_history.ps1')+'" -Root "'+$Root.TrimEnd('\')+'" -FocusId '+$r.task_id;$info.UseShellExecute=$false;$info.CreateNoWindow=$true
 try{$p=[Diagnostics.Process]::Start($info);$p.Dispose()}catch{$feedback.Text='任务记录窗口未能打开，请从运行中心查看。'}
})
Reset-Schedule
