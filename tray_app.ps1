param([ValidateSet('','save','read')][string]$PreferenceProbe='', [string]$PreferenceRoot='', [switch]$Show, [switch]$Preview, [switch]$Verify, [switch]$VerifyBackend, [switch]$VerifyWindows, [switch]$VerifyUX, [string]$PreviewState='standby')
$ErrorActionPreference = 'Stop'
if($PreferenceProbe -and (-not $PreferenceRoot -or [IO.Path]::GetFullPath($PreferenceRoot) -eq [IO.Path]::GetFullPath($PSScriptRoot))){throw 'Preference probe requires isolated Root'}
if (-not $PreferenceProbe -and -not $Preview -and -not $Verify -and -not $VerifyBackend -and -not $VerifyWindows -and -not $VerifyUX) {
    . (Join-Path $PSScriptRoot 'process_launcher.ps1')
    if ([WeChatIndependentProcess]::IsCurrentProcessInJob() -and ([WeChatIndependentProcess]::CurrentJobFlags() -band 0x2000)) {
        $independentArgs='-NoProfile -STA -ExecutionPolicy Bypass -File "'+$PSCommandPath+'"'
        if($Show){$independentArgs+=' -Show'}
        $independentUi=Start-WeChatIndependentProcess (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') $independentArgs $PSScriptRoot
        $independentUi.Dispose()
        exit
    }
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'ui_preferences.ps1')
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
. (Join-Path $PSScriptRoot 'desktop_files.ps1')
[TrayDesign]::EnableDpi()
[System.Windows.Forms.Application]::EnableVisualStyles()
$root = if($PreferenceProbe){$PreferenceRoot}else{Split-Path -Parent $MyInvocation.MyCommand.Path}
$runtime = Join-Path $root '.runtime'
[IO.Directory]::CreateDirectory($runtime) | Out-Null
$mutex = New-Object Threading.Mutex($false, 'Local.WeChatAssistantTray')
if (-not $PreferenceProbe -and -not $Preview -and -not $Verify -and -not $VerifyBackend -and -not $VerifyWindows -and -not $VerifyUX -and -not $mutex.WaitOne(0)) {
    Set-Content -LiteralPath (Join-Path $runtime 'show-window') -Value 'show'
    exit
}
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class TrayNative {
    [DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr hIcon);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool ShowWindowAsync(IntPtr hWnd, int command);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
    public static void ActivateWindow(IntPtr hWnd) {
        if (IsIconic(hWnd)) ShowWindowAsync(hWnd, 9);
        SetForegroundWindow(hWnd);
    }
}
'@
$script:utilityWindows=@{}
function Report-UtilityFailure($label,$message) {
    Set-Feedback ('无法打开'+$label+'：'+$message) 'error'
    try {
        [IO.File]::AppendAllText((Join-Path $runtime 'ui-launch-error.log'),
            ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss')+' '+$label+' '+$message+"`r`n"),[Text.Encoding]::UTF8)
    } catch {}
    if(-not $form.Visible){$tray.ShowBalloonTip(4000,'微信助手',$hint.Text,[Windows.Forms.ToolTipIcon]::Error)}
}
function Open-UtilityWindow($file,$label,$extraArguments='') {
    try {
        $windowKey=$file+$extraArguments
        $existing=$script:utilityWindows[$windowKey]
        if($existing -and -not $existing.process.HasExited){
            $existing.process.Refresh()
            if($existing.process.MainWindowHandle -ne [IntPtr]::Zero){
                [TrayNative]::ActivateWindow($existing.process.MainWindowHandle)
                Set-Feedback ($label+'已打开')
            }else{Set-Feedback ('正在打开'+$label+'，请稍候…')}
            return
        }
        if($existing){$existing.process.Dispose();$script:utilityWindows.Remove($windowKey)}
        $path=Join-Path $root $file
        if(-not [IO.File]::Exists($path)){throw '找不到窗口程序文件。'}
        $start=New-Object Diagnostics.ProcessStartInfo
        $start.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $start.Arguments='-NoProfile -STA -ExecutionPolicy Bypass -File "'+$path+'" '+$extraArguments
        $start.WorkingDirectory=$root;$start.UseShellExecute=$false
        # Hide only the console; WindowStyle=Hidden also hides the first WinForms window.
        $start.CreateNoWindow=$true
        $process=New-Object Diagnostics.Process;$process.StartInfo=$start
        try{[void]$process.Start()}catch{$process.Dispose();throw}
        $script:utilityWindows[$windowKey]=@{process=$process;label=$label;ready=$false}
        Set-Feedback ('正在打开'+$label+'，请稍候…')
    }catch{Report-UtilityFailure $label $_.Exception.Message}
}
function Open-Workflow($page='templates',$templateId='',$itemId='') {
    if($page -notin @('templates','batches','results','revise','localfiles','schedules')){return}
    $arguments='-Page '+$page
    if($templateId -match '^[a-f0-9]{32}$'){$arguments+=' -TemplateId '+$templateId}
    if($itemId -match '^[a-f0-9]{32}$'){$arguments+=' -ItemId '+$itemId}
    Open-UtilityWindow 'workflow_window.ps1' '快捷任务' $arguments
}
function Open-TaskReceipt($ident) {
    if($ident -match '^[a-f0-9]{32}$'){Open-UtilityWindow 'task_history.ps1' '任务记录' ('-FocusId '+$ident)}
}
function Refresh-UtilityWindows {
    foreach($file in @($script:utilityWindows.Keys)){
        $entry=$script:utilityWindows[$file];$process=$entry.process
        if($process.HasExited){
            if($process.ExitCode -ne 0){Report-UtilityFailure $entry.label ('窗口异常退出（退出码 '+$process.ExitCode+'）。')}
            $process.Dispose();$script:utilityWindows.Remove($file)
        }elseif(-not $entry.ready){
            $process.Refresh();$handle=$process.MainWindowHandle
            if($handle -ne [IntPtr]::Zero -and [TrayNative]::IsWindowVisible($handle)){
                $entry.ready=$true;[TrayNative]::ActivateWindow($handle);Set-Feedback ($entry.label+'已打开')
            }
        }
    }
}
$form = New-Object Windows.Forms.Form
$form.Text = '微信助手 · 运行中心'
$form.AutoScaleDimensions = New-Object Drawing.SizeF(96,96)
$form.AutoScaleMode = 'Dpi'
$form.ClientSize = New-Object Drawing.Size(960,720)
$form.MinimumSize = New-Object Drawing.Size(896,679)
$form.StartPosition = 'CenterScreen'
$form.BackColor = [TrayDesign]::PageColor
$form.ForeColor = [TrayDesign]::TextColor
$form.Font = New-Object Drawing.Font('Microsoft YaHei UI',10.5)
$muted = [TrayDesign]::MutedColor
$accent = [TrayDesign]::Blue
function Label($text,$x,$y,$width,$height,$size=10.5,$parent=$form) {
    $l = New-Object Windows.Forms.Label
    $l.AutoEllipsis=$true; $l.Text=$text; $l.Location=New-Object Drawing.Point($x,$y); $l.Size=New-Object Drawing.Size($width,$height)
    $l.Font=New-Object Drawing.Font('Microsoft YaHei UI',$size)
    $l.BackColor=[Drawing.Color]::Transparent;$l.TextAlign='MiddleLeft'
    $parent.Controls.Add($l); return $l
}
function Card($x,$y,$w,$h) {
    $p=New-Object TrayCard; $p.Location=New-Object Drawing.Point($x,$y);$p.Size=New-Object Drawing.Size($w,$h)
    $p.Anchor='Top,Left';$form.Controls.Add($p);return $p
}
function Divider($parent,$x,$y,$width) {
    $line=New-Object Windows.Forms.Panel
    $line.SetBounds($x,$y,$width,1)
    $line.BackColor=[TrayDesign]::DividerColor
    $parent.Controls.Add($line);return $line
}
function Glyph($parent,$kind,$x,$y,$size,$color=$accent) {
    $p=New-Object Windows.Forms.PictureBox;$p.SetBounds($x,$y,$size,$size)
    $p.SizeMode='Zoom';$p.BackColor=[Drawing.Color]::Transparent
    $p.Image=[TrayDesign]::Glyph($kind,$color,64);$parent.Controls.Add($p);return $p
}
function Card-Heading($parent,$text,$kind) {
    $icon=Glyph $parent $kind 24 24 20
    $label=Label $text 44 8 240 28 12 $parent
    # 12pt keeps card titles calmer than the content they introduce.
    $label.Font=New-Object Drawing.Font('Microsoft YaHei UI',12,[Drawing.FontStyle]::Bold)
    $label.Tag=$icon
    return $label
}
$brand=New-Object Windows.Forms.PictureBox
$brand.Location=New-Object Drawing.Point(24,24);$brand.Size=New-Object Drawing.Size(40,40)
$brand.SizeMode='Zoom';$brand.Image=[TrayDesign]::Symbol([TrayDesign]::BrandBlue,96,'standby');$form.Controls.Add($brand)
$title=Label '微信助手' 80 16 350 32 16
# 16pt keeps the brand below the 18pt assistant state, which is the main reading point.
$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold);$title.ForeColor=[TrayDesign]::Heading
$subtitle=Label '运行中心 · 让微信连接你的电脑' 80 48 430 24 10.5
$subtitle.ForeColor=$muted
$connectionBadge=New-Object TrayBadge;$connectionBadge.Text='正在连接微信'
$connectionBadge.SetBounds(752,32,184,32);$form.Controls.Add($connectionBadge)
$statusCard=Card 24 104 536 232
$eyebrow=Label '助手状态' 24 24 210 24 10.5 $statusCard;$eyebrow.ForeColor=$muted
$backendTip=New-Object Windows.Forms.ToolTip
$backendTip.ShowAlways=$true;$backendTip.AutoPopDelay=12000
$stateDot=New-Object Windows.Forms.Panel;$stateDot.SetBounds(24,72,12,12);$stateDot.BackColor=[Drawing.Color]::Transparent
$script:statusColor=[TrayDesign]::Standby
$stateDot.Add_Paint({param($sender,$e)
    $e.Graphics.SmoothingMode='AntiAlias';$brush=New-Object Drawing.SolidBrush($script:statusColor)
    try{$e.Graphics.FillEllipse($brush,0,0,$sender.Width-1,$sender.Height-1)}finally{$brush.Dispose()}
});$statusCard.Controls.Add($stateDot)
$state=Label '正在读取状态…' 48 56 288 48 23 $statusCard
$state.Font=New-Object Drawing.Font('Microsoft YaHei UI',18,[Drawing.FontStyle]::Bold)
$serviceGuide=Label '正在读取后台状态，请稍候。' 24 104 312 48 9.75 $statusCard;$serviceGuide.ForeColor=$muted
$script:wakeButton=New-Object TrayButton;$script:wakeButton.Text='唤醒助手';$script:wakeButton.Primary=$true;$script:wakeButton.IconKind='send'
$script:wakeButton.SetBounds(352,64,160,40);$script:wakeButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5)
$script:wakeButton.Add_Click({Request-PrimaryAction});$statusCard.Controls.Add($script:wakeButton)
$wakeHelp=Label '在微信发送“开始对话”也可唤醒' 352 112 160 40 9.75 $statusCard;$wakeHelp.ForeColor=$muted
$statusRule=Divider $statusCard 24 168 488
$durationGlyph=Glyph $statusCard 'clock' 24 192 16 $muted
$durationCaption=Label '运行时长' 48 184 64 32 9.75 $statusCard;$durationCaption.ForeColor=$muted
$detail=Label '—' 120 184 104 32 10.5 $statusCard;$detail.Font=New-Object Drawing.Font('Segoe UI',10.5,[Drawing.FontStyle]::Bold)
$communicationGlyph=Glyph $statusCard 'chat' 256 192 16 $muted
$communicationCaption=Label '最近微信通信' 280 184 88 32 9.75 $statusCard;$communicationCaption.ForeColor=$muted
$lastTime=Label '尚未成功通信' 376 184 136 32 10.5 $statusCard
$lastTime.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5,[Drawing.FontStyle]::Bold)
$modelCard=Card 576 104 360 304
$modelHead=Card-Heading $modelCard '当前模型' 'cube'
$deepseekButton=New-Object TrayButton
$deepseekButton.Text='DeepSeek';$deepseekButton.SetBounds(24,64,152,40);$deepseekButton.Quiet=$true;$deepseekButton.ToggleChip=$true
$deepseekButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5)
$deepseekButton.AccessibleName='切换到 DeepSeek 官方后端'
$deepseekButton.Add_Click({Request-Backend 'deepseek'});$modelCard.Controls.Add($deepseekButton)
$claudeButton=New-Object TrayButton
$claudeButton.Text='Claude';$claudeButton.SetBounds(184,64,152,40);$claudeButton.Quiet=$true;$claudeButton.ToggleChip=$true
$claudeButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5)
$claudeButton.AccessibleName='切换到 Claude 后端'
$claudeButton.Add_Click({Request-Backend 'claude'});$modelCard.Controls.Add($claudeButton)
$backendTip.SetToolTip($deepseekButton,'切换后立即生效；两套对话分别保留。任务处理中暂不可切换。')
$backendTip.SetToolTip($claudeButton,'使用已保存的 Claude 配置；两套对话分别保留。任务处理中暂不可切换。')
$modelRule=Divider $modelCard 24 120 312
$modelCaption=Label '当前模型' 24 128 312 24 9.75 $modelCard;$modelCaption.ForeColor=$muted
$modelName=Label '正在读取…' 24 152 312 32 12 $modelCard;$modelName.Font=New-Object Drawing.Font('Microsoft YaHei UI',12,[Drawing.FontStyle]::Bold)
$modelStatus=Label '尚无模型调用记录' 24 184 312 40 9.75 $modelCard;$modelStatus.ForeColor=$muted;$modelStatus.TextAlign='TopLeft'
$backendTip.SetToolTip($modelStatus,'根据实际模型任务更新，不会自动发送收费请求。显示上次调用结果，不代表持续探测在线。')
$settingsButton=New-Object TrayButton;$settingsButton.Text='后端配置';$settingsButton.IconKind='';$settingsButton.Navigate=$false;$settingsButton.Link=$true
$settingsButton.SetBounds(24,232,312,48);$settingsButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$modelCard.Controls.Add($settingsButton)
$settingsButton.Add_Click({Open-UtilityWindow 'backend_editor.ps1' '后端配置'})
$taskCard=Card 24 352 536 216
$taskHead=Card-Heading $taskCard '当前任务' 'task'
$taskGlyph=Glyph $taskCard 'empty-task' 252 56 32 $muted
$task=Label '暂无执行中的任务' 24 96 488 24 10.5 $taskCard;$task.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5,[Drawing.FontStyle]::Bold);$task.TextAlign='MiddleCenter'
$taskGuide=Label '任务执行时可在这里查看进度。' 24 128 488 24 9.75 $taskCard;$taskGuide.ForeColor=$muted;$taskGuide.TextAlign='MiddleCenter'
$progressText=Label '' 24 52 488 32 9.75 $taskCard;$progressText.ForeColor=$accent;$progressText.TextAlign='MiddleCenter';$progressText.Visible=$false
$elapsedText=Label '' 24 136 488 24 9.75 $taskCard;$elapsedText.ForeColor=$muted;$elapsedText.TextAlign='MiddleCenter';$elapsedText.Visible=$false
$stats=Label '等待任务 0  ·  附件处理 0' 24 176 300 32 9.75 $taskCard;$stats.ForeColor=$muted
$taskRule=Divider $taskCard 24 160 488
$cancelButton=New-Object TrayButton;$cancelButton.Text='停止当前任务';$cancelButton.IconKind='stop';$cancelButton.Danger=$true
$cancelButton.SetBounds(360,176,152,32);$cancelButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$cancelButton.Enabled=$false
$cancelButton.Add_Click({Send-Control 'cancel'});$taskCard.Controls.Add($cancelButton)
$entryCard=Card 576 424 360 192
$entryHead=Card-Heading $entryCard '常用入口' 'apps'
$appsButton=New-Object TrayButton;$appsButton.Text='常用应用';$appsButton.IconKind='apps';$appsButton.Navigate=$true;$appsButton.Description='收藏与应用简称'
$appsButton.SetBounds(24,64,312,48);$appsButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5);$entryCard.Controls.Add($appsButton)
$appsButton.Add_Click({Open-UtilityWindow 'app_settings.ps1' '常用应用'})
$memoryButton=New-Object TrayButton;$memoryButton.Text='固定记忆';$memoryButton.IconKind='memory';$memoryButton.Navigate=$true;$memoryButton.Description='工作目录与回复偏好'
$memoryButton.SetBounds(24,120,312,48);$memoryButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5);$entryCard.Controls.Add($memoryButton)
$memoryButton.Add_Click({Open-UtilityWindow 'memory_editor.ps1' '固定记忆'})
$templatesButton=New-Object TrayButton;$templatesButton.Text='任务模板';$templatesButton.Quiet=$true;$templatesButton.Link=$true;$templatesButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$entryCard.Controls.Add($templatesButton)
$scheduleButton=New-Object TrayButton;$scheduleButton.Text='定时任务';$scheduleButton.Quiet=$true;$scheduleButton.Link=$true;$scheduleButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$entryCard.Controls.Add($scheduleButton);$scheduleButton.Add_Click({Open-Workflow 'schedules'})
$templatesButton.Add_Click({Open-UtilityWindow 'template_editor.ps1' '任务模板'})
$favoriteChoice=New-Object Windows.Forms.ComboBox;$favoriteChoice.DropDownStyle='DropDownList';$favoriteChoice.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$entryCard.Controls.Add($favoriteChoice)
$quickRun=New-Object TrayButton;$quickRun.Text='运行模板';$quickRun.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$entryCard.Controls.Add($quickRun)
$quickRun.Add_Click({$id='';if($favoriteChoice.SelectedIndex -ge 0 -and $favoriteChoice.SelectedIndex -lt $script:favorites.Count){$id=[string]$script:favorites[$favoriteChoice.SelectedIndex].id};Open-Workflow 'templates' $id})
$fileTaskButton=New-Object TrayButton;$fileTaskButton.Text='文件任务';$fileTaskButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$entryCard.Controls.Add($fileTaskButton);$fileTaskButton.Add_Click({Open-Workflow 'localfiles'})
$batchButton=New-Object TrayButton;$batchButton.Text='附件核对';$batchButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$entryCard.Controls.Add($batchButton);$batchButton.Add_Click({Open-Workflow 'batches'})
$appsButton.Navigate=$false;$memoryButton.Navigate=$false
# Icon and description used when the entry card has room for 2x2 tiles (see Layout-RunCenter).
$script:entryTiles=@(@($fileTaskButton,'folder','处理本机文件'),@($batchButton,'clip','核对微信附件'),@($scheduleButton,'clock','提醒与自动任务'),@($templatesButton,'log','保存常用要求'),@($appsButton,'apps','收藏与应用简称'),@($memoryButton,'bookmark','工作目录与偏好'))
$resultRevise=New-Object TrayButton;$resultRevise.Text='继续修改';$resultRevise.Quiet=$true;$resultRevise.Link=$true;$resultRevise.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$taskCard.Controls.Add($resultRevise);$resultRevise.Add_Click({if($script:resultCard){Open-Workflow 'revise' '' ([string]$script:resultCard.id)}})
$resultButton=New-Object TrayButton;$resultButton.Text='全部结果';$resultButton.Quiet=$true;$resultButton.Link=$true;$resultButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$taskCard.Controls.Add($resultButton);$resultButton.Add_Click({Open-Workflow 'results'})
$resultPanel=New-Object Windows.Forms.Panel;$resultPanel.BackColor=[Drawing.Color]::Transparent;$taskCard.Controls.Add($resultPanel);$resultPanel.Visible=$false
$resultSummary=Label '' 0 0 400 32 10.5 $resultPanel;$resultSummary.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5,[Drawing.FontStyle]::Bold)
$resultConclusion=Label '' 0 36 400 24 9.75 $resultPanel;$resultConclusion.ForeColor=$muted
$resultFiles=New-Object Windows.Forms.ComboBox;$resultFiles.DropDownStyle='DropDownList';$resultFiles.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$resultPanel.Controls.Add($resultFiles)
$resultOpen=New-Object TrayButton;$resultOpen.Text='打开文件';$resultOpen.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$resultPanel.Controls.Add($resultOpen)
$resultFolder=New-Object TrayButton;$resultFolder.Text='打开文件夹';$resultFolder.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$resultPanel.Controls.Add($resultFolder)
$resultDetail=New-Object TrayButton;$resultDetail.Text='查看完整回复';$resultDetail.Primary=$true;$resultDetail.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$resultPanel.Controls.Add($resultDetail)
$resultDetail.Add_Click({Open-TaskReceipt ([string]$script:resultCard.id)})
$resultNote=Label '' 0 64 400 40 9.75 $resultPanel;$resultNote.ForeColor=$muted
$resultOpen.Add_Click({try{Open-DesktopDocument ([string]$script:resultFileRows[$resultFiles.SelectedIndex].path);Set-Feedback '已打开文件，请核对实际内容。'}catch{Set-Feedback '文件不可用或不支持直接打开，请检查文件位置。' 'error'}})
$resultFolder.Add_Click({try{Show-DesktopDocumentFolder ([string]$script:resultFileRows[$resultFiles.SelectedIndex].path)}catch{Set-Feedback '文件已移动或删除，请检查任务结果。' 'error'}})
$resultFiles.Add_SelectedIndexChanged({Update-ResultFileActions})
$script:favorites=@();$script:resultFileRows=@();$script:favoriteSignature='';$script:resultSignature=''
$logCard=Card 24 584 536 184
$logTitle=Card-Heading $logCard '最近动态' 'activity'
$historyButton=New-Object TrayButton;$historyButton.Text='全部记录';$historyButton.IconKind='';$historyButton.Navigate=$false;$historyButton.Quiet=$true;$historyButton.Link=$true
$historyButton.SetBounds(368,16,144,32);$historyButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75)
$historyButton.Add_Click({Open-UtilityWindow 'task_history.ps1' '任务记录'});$logCard.Controls.Add($historyButton)
$records=New-Object Windows.Forms.TextBox
$records.Location=New-Object Drawing.Point(48,56);$records.Size=New-Object Drawing.Size(464,104)
$records.Multiline=$true;$records.ReadOnly=$true;$records.ScrollBars='Vertical';$records.BorderStyle='None'
$records.BackColor=$logCard.Surface;$records.ForeColor=[TrayDesign]::TextColor
$records.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75)
$records.TabStop=$true;$records.AccessibleName='最近运行动态';$records.HideSelection=$true;$logCard.Controls.Add($records)
$timeline=New-Object TrayTimelineMarks;$timeline.Source=$records;$timeline.SetBounds(24,56,16,104);$logCard.Controls.Add($timeline)
$attentionList=New-Object Windows.Forms.ListView;$attentionList.View='Details';$attentionList.FullRowSelect=$true;$attentionList.MultiSelect=$false;$attentionList.HideSelection=$false;$attentionList.BorderStyle='None'
$attentionList.BackColor=$logCard.Surface;$attentionList.ForeColor=[TrayDesign]::TextColor;$attentionList.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75)
$attentionList.ShowItemToolTips=$true;$attentionList.AccessibleName='待处理事项';$null=$attentionList.Columns.Add('待处理原因',160);$null=$attentionList.Columns.Add('任务 / 附件',300);$logCard.Controls.Add($attentionList)
$attentionEmpty=Label '正在读取待处理事项…' 16 56 400 48 10 $logCard;$attentionEmpty.ForeColor=$muted
$attentionOpen=New-Object TrayButton;$attentionOpen.Text='查看所选事项';$attentionOpen.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$attentionOpen.Enabled=$false;$logCard.Controls.Add($attentionOpen)
$activityToggle=New-Object TrayButton;$activityToggle.Text='最近动态';$activityToggle.Quiet=$true;$activityToggle.Link=$true;$activityToggle.Font=New-Object Drawing.Font('Microsoft YaHei UI',9.75);$logCard.Controls.Add($activityToggle)
$script:showActivity=$false;$script:attentionSignature='';$script:attentionData=$null;$script:resultCard=$null
$activityToggle.Add_Click({$script:showActivity=-not $script:showActivity;Update-AttentionDisplay})
$attentionList.Add_SelectedIndexChanged({Update-AttentionAction})
$attentionList.Add_DoubleClick({Open-AttentionItem})
$attentionList.Add_KeyDown({param($sender,$e) if($e.KeyCode -eq 'Enter'){$e.Handled=$true;$e.SuppressKeyPress=$true;Open-AttentionItem}})
$attentionOpen.Add_Click({Open-AttentionItem})
$maintenanceCard=Card 576 632 360 136
$maintenanceHead=Card-Heading $maintenanceCard '服务维护' 'shield'
$restartButton=New-Object TrayButton;$restartButton.Text='重启服务';$restartButton.IconKind='restart';$restartButton.SetBounds(24,56,152,40)
$restartButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5);$restartButton.Add_Click({Request-Restart});$maintenanceCard.Controls.Add($restartButton)
$logButton=New-Object TrayButton;$logButton.Text='打开日志';$logButton.IconKind='log';$logButton.SetBounds(184,56,152,40)
$logButton.Font=New-Object Drawing.Font('Microsoft YaHei UI',10.5);$logButton.Add_Click({Start-Process notepad.exe -ArgumentList ('"'+(Join-Path $root 'bridge.log')+'"')});$maintenanceCard.Controls.Add($logButton)
$maintenanceGuide=Label '空闲时可重启服务' 24 104 312 24 9.75 $maintenanceCard;$maintenanceGuide.ForeColor=$muted
$hint=Label '准备就绪' 24 776 912 24 9.75;$hint.ForeColor=$muted
$footer=Label '关闭窗口后仍在托盘运行 · 在微信发送“开始对话”也可唤醒' 24 800 912 24 9.75;$footer.ForeColor=$muted;$footer.TextAlign='MiddleRight'
[TrayDesign]::EnableDepth($form)

# Reflow the existing cards on the same 8px grid; dimensions below are logical pixels.
function Ui-Bounds($control,$x,$y,$w,$h) {
    $control.SetBounds([int][Math]::Round($x*$script:uiScale),[int][Math]::Round($y*$script:uiScale),[int][Math]::Round($w*$script:uiScale),[int][Math]::Round($h*$script:uiScale))
}
. (Join-Path $PSScriptRoot 'workbench_layout.ps1')
function Format-TaskDuration($seconds) {
    $span=[TimeSpan]::FromSeconds([Math]::Max(0,[double]$seconds))
    return ('{0:00}:{1:00}:{2:00}' -f [int][Math]::Floor($span.TotalHours),$span.Minutes,$span.Seconds)
}
function Show-TaskProgress($s,$alive) {
    $p=$s.task_progress
    $show=[bool]($alive -and $p -and (Has-ActiveTask $s) -and $script:lastState -ne 'error-stale')
    $progressText.Visible=$show;$elapsedText.Visible=$show
    if($show){
        $labels=@($p.steps | ForEach-Object {[string]$_.label})
        $progressText.Text=if($p.stopping){'正在停止 · '+$p.label}else{'当前阶段 · '+$p.label}
        $elapsedText.Text='已用时 '+(Format-TaskDuration $p.elapsed_seconds)+'   ·   当前阶段 '+(Format-TaskDuration $p.phase_seconds)
        $backendTip.SetToolTip($progressText,('实际经过的阶段：'+($labels -join ' → ')+"`r`n仅显示后台实际进入的阶段，不估算百分比或剩余时间。"))
        $taskGlyph.Visible=$false
    }
    Update-ProgressLayout
}
$script:snapshot=$null; $script:pending=$null; $script:mode=''; $script:closing=$false
$script:lastState=''; $script:startAt=0
$script:lastFault=$false;$script:connectionFeedback=$false
$connectionBadge.TabStop=$false
$statusCard.TabIndex=0;$taskCard.TabIndex=1;$logCard.TabIndex=2;$modelCard.TabIndex=3;$entryCard.TabIndex=4;$maintenanceCard.TabIndex=5
$deepseekButton.TabIndex=0;$claudeButton.TabIndex=1;$settingsButton.TabIndex=2;$historyButton.TabIndex=0;$records.TabIndex=1;$appsButton.TabIndex=0;$memoryButton.TabIndex=1;$templatesButton.TabIndex=2;$restartButton.TabIndex=0;$logButton.TabIndex=1
function Set-Feedback($text,$tone='info') {
    $script:connectionFeedback=$false
    $hint.Text=$text
    $hint.ForeColor=switch($tone){'error'{[TrayDesign]::DangerText};'success'{[TrayDesign]::SuccessText};default{$accent}}
    $backendTip.SetToolTip($hint,$text)
}
function Has-ActiveTask($s) {
    return [bool]($s -and (($s.task -and $s.task -ne '暂无执行中的任务') -or $s.stage))
}
function Refresh-Actions($s,$alive) {
    $starting=$script:startAt -and [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()-$script:startAt -lt 8 -and (-not $alive -or $s.started -lt $script:startAt)
    $waiting=[bool]($script:pending -or $script:mode -or $starting)
    $ready=[bool]($alive -and -not $waiting -and $script:lastState -notin @('stopped','error-stale','stopping'))
    $hasTask=$alive -and (Has-ActiveTask $s)
    $busy=$hasTask -or ($s -and ($s.queued -gt 0 -or $s.downloads -gt 0))
    $script:wakeButton.Enabled=$false;$script:wakeButton.Tag=''
    switch($script:lastState){
        'standby' {$script:wakeButton.Text='唤醒助手';$script:wakeButton.Tag='wake';$script:wakeButton.Enabled=$ready}
        'working' {$script:wakeButton.Text='进入待机';$script:wakeButton.Tag='standby';$script:wakeButton.Enabled=$ready}
        'draining' {$script:wakeButton.Text='继续接收任务';$script:wakeButton.Tag='wake';$script:wakeButton.Enabled=$ready}
        'stopping' {$script:wakeButton.Text='正在停止…'}
        default {
            if(-not $alive -or $script:lastState -in @('stopped','error-stale')){
                $script:wakeButton.Text='启动服务';$script:wakeButton.Tag='start';$script:wakeButton.Enabled=-not $waiting
            }else{$script:wakeButton.Text='等待微信连接…'}
        }
    }
    if($script:pending){$script:wakeButton.Enabled=$false}
    if($starting){$script:wakeButton.Text='正在启动…';$script:wakeButton.Tag='';$script:wakeButton.Enabled=$false}
    $cancelling=$s -and $s.stage -eq '正在停止当前任务'
    $cancelButton.Enabled=[bool]($ready -and $hasTask -and -not $cancelling)
    $cancelButton.Text=if($cancelling){'正在停止…'}else{'停止当前任务'}
    $restartButton.Enabled=[bool]($ready -and -not $busy)
    $maintenanceGuide.Text=if($busy){'任务或附件处理中，暂不能重启'}elseif(-not $alive){'请从助手状态卡启动服务'}elseif($s -and $s.code_identity -and [string]$s.code_identity.state -eq 'changed'){'代码已更新，重启后生效'}else{'空闲时可重启服务'}
    # 提交号和"为什么这么判"放进已有的诊断提示文字里，不新建面板、不改布局。
    $codeDetail='版本状态未知：还没有后台状态可读。'
    if($s -and $s.code_identity){
        switch([string]$s.code_identity.state){
            'changed'{$codeDetail='运行源码已变化，重启后加载新代码。'}
            'same'{$codeDetail='运行源码与启动时一致。'}
            default{$codeDetail='版本状态未知：'+[string]$s.code_identity.detail}
        }
        if($s.code_identity.commit){$codeDetail=$codeDetail+' 启动时提交 '+[string]$s.code_identity.commit}
    }elseif($s){$codeDetail='版本状态未知：后台状态里没有代码身份字段（可能是旧版本后台）。'}
    $backendTip.SetToolTip($restartButton,($maintenanceGuide.Text+"`r`n"+$codeDetail))
    $backendTip.SetToolTip($maintenanceGuide,($maintenanceGuide.Text+"`r`n"+$codeDetail))
    $backendTip.SetToolTip($cancelButton,($(if($hasTask){'请求停止当前任务；已完成的文件修改会保留。'}else{'当前没有执行中的任务。'})))
    $wakeHelp.Text=switch($script:lastState){'working'{'待机会暂停新任务，已有任务继续收尾'};'draining'{'已有任务继续收尾，可恢复接收新任务'};default{'在微信发送“开始对话”也可唤醒'}}
    if($wakeMenu){$wakeMenu.Enabled=$ready -and $script:lastState -in @('standby','draining')}
    if($standbyMenu){$standbyMenu.Enabled=$ready -and $script:lastState -eq 'working'}
    # Only the action that applies to the current state is listed in the tray menu.
    if($wakeMenu -and $standbyMenu){$standbyMenu.Available=($script:lastState -eq 'working');$wakeMenu.Available=-not $standbyMenu.Available}
    if($restartMenu){$restartMenu.Enabled=$restartButton.Enabled;$restartMenu.ToolTipText=$maintenanceGuide.Text}
    if($exitMenu){$exitMenu.Enabled=-not $waiting -and -not $busy;$exitMenu.ToolTipText=if($busy){'请等待任务与附件完成后再退出。'}else{'停止后台并退出；之后微信将无法唤醒助手。'}}
}
function Request-PrimaryAction {
    switch([string]$script:wakeButton.Tag){'start'{Start-ServiceProcess};'wake'{Send-Control 'wake'};'standby'{Send-Control 'standby'}}
}
function Start-ServiceProcess {
    $script:startAt=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    try {
        Start-Process powershell.exe -WindowStyle Hidden -WorkingDirectory $root -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $root 'toggle.ps1')+'" -On') | Out-Null
        Refresh-Actions $script:snapshot $false;Set-Feedback '正在启动后台，请稍候…'
    }catch{$script:startAt=0;Set-Feedback '无法启动后台，请检查程序文件与日志。' 'error'}
}
function Send-Control($action) {
    if ($script:pending) { Set-Feedback '上一项操作尚未完成，请稍候'; return }
    if (-not $script:snapshot -or $script:lastState -in @('stopped','error-stale')) { Set-Feedback '后台未就绪，请先启动服务' 'error'; return }
    $id=[Guid]::NewGuid().ToString('N')
    $payload=@{pid=$script:snapshot.pid;at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();action=$action} | ConvertTo-Json -Compress
    $temp=Join-Path $runtime ('command-'+$id+'.tmp')
    [IO.File]::WriteAllText($temp,$payload,(New-Object Text.UTF8Encoding($false)))
    [IO.File]::Move($temp,(Join-Path $runtime ('command-'+$id+'.json')))
    $script:pending=$id; $script:pendingAt=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    Set-Feedback '操作已提交，等待后台确认…'
    Refresh-Actions $script:snapshot $true
    if($timer){$timer.Interval=500}
}
function Request-Backend($backend) {
    if ($script:pending) { Set-Feedback '上一项操作尚未完成，请稍候'; return }
    if (-not $script:snapshot -or -not $script:snapshot.backend_switch_available) {
        Set-Feedback '请等待任务和附件处理结束，再切换模型后端'; return
    }
    if ([string]$script:snapshot.backend -ieq $backend) { Set-Feedback '当前已经使用这个后端'; return }
    Send-Control ('backend:'+$backend)
}
function Refresh-Backend($s,$alive) {
    $ready=$alive -and $s -and $s.backend_switch_available -and (-not $script:pending) -and $script:lastState -notin @('stopped','error-stale','stopping')
    $current=if($alive -and $s -and $script:lastState -notin @('stopped','error-stale')){[string]$s.backend}else{''}
    $deepseekButton.Enabled=[bool]$ready;$claudeButton.Enabled=[bool]$ready
    $deepseekButton.Primary=($current -ieq 'deepseek');$claudeButton.Primary=($current -ieq 'claude')
    $deepseekButton.Invalidate();$claudeButton.Invalidate()
    if($deepseekMenu){$deepseekMenu.Checked=$deepseekButton.Primary;$deepseekMenu.Enabled=[bool]$ready}
    if($claudeMenu){$claudeMenu.Checked=$claudeButton.Primary;$claudeMenu.Enabled=[bool]$ready}
}
function Request-Exit {
    $alive=$script:snapshot -and $script:snapshot.pid -gt 0 -and (Get-Process -Id $script:snapshot.pid -ErrorAction SilentlyContinue)
    if($alive -and ((Has-ActiveTask $script:snapshot) -or $script:snapshot.queued -gt 0 -or $script:snapshot.downloads -gt 0)){Set-Feedback '任务或附件尚未完成，请完成后再退出。' 'error';return}
    if ([Windows.Forms.MessageBox]::Show($form,'退出后后台服务会停止，微信将无法唤醒助手。确定退出吗？','退出微信助手','YesNo','Question') -ne 'Yes') { return }
    if (-not $alive) { $script:closing=$true; $form.Close(); return }
    $script:mode='exit'; Send-Control 'shutdown'
}
function Request-Restart {
    if (-not $script:snapshot -or $script:snapshot.pid -le 0 -or -not (Get-Process -Id $script:snapshot.pid -ErrorAction SilentlyContinue)) { Start-ServiceProcess; return }
    if((Has-ActiveTask $script:snapshot) -or $script:snapshot.queued -gt 0 -or $script:snapshot.downloads -gt 0){Set-Feedback '任务或附件尚未完成，请完成后再重启。' 'error';return}
    $script:mode='restart'; Send-Control 'shutdown'
}
$tray=New-Object Windows.Forms.NotifyIcon
$tray.Text='微信助手 · 正在连接'; $tray.Visible=$true
$menu=New-Object Windows.Forms.ContextMenuStrip
$menu.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$menu.ShowItemToolTips=$true
$menu.BackColor=[TrayDesign]::CardColor
$menuScale=1;try{$screen=[Drawing.Graphics]::FromHwnd([IntPtr]::Zero);$menuScale=$screen.DpiX/96;$screen.Dispose()}catch{}
function Add-MenuItem($items,$text,$kind,$action) {
    $item=New-Object Windows.Forms.ToolStripMenuItem($text)
    if($kind){$item.Image=[TrayDesign]::Glyph($kind,[TrayDesign]::MutedColor,[int](36*$menuScale))}
    if($action){$item.Add_Click($action)}
    $null=$items.Add($item);return $item
}
function Add-MenuSeparator($items) {$null=$items.Add((New-Object Windows.Forms.ToolStripSeparator))}
# Status line, then: open/wake, task entries, settings, service. Labels match the run center buttons.
$menuHeader=New-Object Windows.Forms.ToolStripLabel('正在连接微信')
$menuHeader.Font=New-Object Drawing.Font('Microsoft YaHei UI',10,[Drawing.FontStyle]::Bold);$null=$menu.Items.Add($menuHeader)
Add-MenuSeparator $menu.Items
$openMenu=Add-MenuItem $menu.Items '打开运行中心' '' { $form.Show(); $form.WindowState='Normal'; $form.Activate() }
$openMenu.Font=New-Object Drawing.Font($menu.Font,[Drawing.FontStyle]::Bold);$openMenu.Image=[TrayDesign]::Symbol([TrayDesign]::BrandBlue,64,'standby')
$wakeMenu=Add-MenuItem $menu.Items '唤醒助手' 'send' { Send-Control 'wake' }
$standbyMenu=Add-MenuItem $menu.Items '进入待机' 'power' { Send-Control 'standby' }
Add-MenuSeparator $menu.Items
$quickMenu=Add-MenuItem $menu.Items '快捷任务' 'task' $null
$null=Add-MenuItem $quickMenu.DropDownItems '运行模板' 'task' {Open-Workflow 'templates'}
$null=Add-MenuItem $quickMenu.DropDownItems '文件任务' 'folder' {Open-Workflow 'localfiles'}
$null=Add-MenuItem $quickMenu.DropDownItems '附件核对' 'clip' {Open-Workflow 'batches'}
$null=Add-MenuItem $quickMenu.DropDownItems '定时任务' 'clock' {Open-Workflow 'schedules'}
$null=Add-MenuItem $menu.Items '任务结果' 'check' {Open-Workflow 'results'}
$null=Add-MenuItem $menu.Items '任务记录' 'history' {Open-UtilityWindow 'task_history.ps1' '任务记录'}
Add-MenuSeparator $menu.Items
$settingsMenu=Add-MenuItem $menu.Items '设置' 'settings' $null
$backendMenu=Add-MenuItem $settingsMenu.DropDownItems '模型后端' 'cube' $null
$deepseekMenu=Add-MenuItem $backendMenu.DropDownItems 'DeepSeek 官方' '' {Request-Backend 'deepseek'}
$claudeMenu=Add-MenuItem $backendMenu.DropDownItems 'Claude' '' {Request-Backend 'claude'}
$null=Add-MenuItem $settingsMenu.DropDownItems '后端配置' 'settings' {Open-UtilityWindow 'backend_editor.ps1' '后端配置'}
Add-MenuSeparator $settingsMenu.DropDownItems
$null=Add-MenuItem $settingsMenu.DropDownItems '任务模板' 'log' {Open-UtilityWindow 'template_editor.ps1' '任务模板'}
$null=Add-MenuItem $settingsMenu.DropDownItems '常用应用' 'apps' {Open-UtilityWindow 'app_settings.ps1' '常用应用'}
$null=Add-MenuItem $settingsMenu.DropDownItems '固定记忆' 'bookmark' {Open-UtilityWindow 'memory_editor.ps1' '固定记忆'}
Add-MenuSeparator $menu.Items
$restartMenu=Add-MenuItem $menu.Items '重启服务' 'restart' { Request-Restart }
$exitMenu=Add-MenuItem $menu.Items '退出程序' 'power' { Request-Exit }
[TrayMenuStyle]::Apply($menu,$menuScale)
function Find-MenuItem($items,$text) {
    foreach($item in $items){
        if($item.Text -eq $text){return $item}
        if($item -is [Windows.Forms.ToolStripMenuItem] -and $item.HasDropDownItems){$found=Find-MenuItem $item.DropDownItems $text;if($found){return $found}}
    }
}
$tray.ContextMenuStrip=$menu
$tray.Add_DoubleClick({$form.Show();$form.WindowState='Normal';$form.Activate()})
function Set-Indicator($key,$caption) {
    if ($script:lastState -eq $key) {return}
    $script:lastState=$key
    $c=[TrayDesign]::MutedColor; $textColor=[TrayDesign]::TextColor
    switch ($key) {
      'standby' {$c=[TrayDesign]::Standby;$textColor=[TrayDesign]::StandbyText}
      'working' {$c=[TrayDesign]::Success;$textColor=[TrayDesign]::SuccessText}
      {$_ -in @('reconnecting','stopping','draining')} {$c=[TrayDesign]::Warning}
      {$_ -in @('error','error-stale')} {$c=[TrayDesign]::Danger;$textColor=[TrayDesign]::DangerText}
    }
    # The status card border carries a faint tint of the indicator color.
    $border=[TrayDesign]::Mix([TrayDesign]::CardColor,$c,0.25)
    $script:statusColor=$c;$stateDot.Invalidate()
    $statusCard.Surface=[TrayDesign]::CardColor
    $statusCard.Border=$border
    $statusRule.BackColor=$statusCard.Border;$statusCard.Invalidate($true)
    $bitmap=[TrayDesign]::Symbol($c,64,$key)
    $oldHeader=$menuHeader.Image;$menuHeader.Image=[TrayDesign]::Symbol($c,64,$key);if($oldHeader){$oldHeader.Dispose()}
    $handle=$bitmap.GetHicon();$borrowed=[Drawing.Icon]::FromHandle($handle);$icon=$borrowed.Clone()
    $old=$tray.Icon;$tray.Icon=$icon;$form.Icon=$icon
    if($old){$old.Dispose()};$borrowed.Dispose();[TrayNative]::DestroyIcon($handle)|Out-Null
    $bitmap.Dispose()
    $tray.Text=('微信助手 · '+$caption);$state.ForeColor=$textColor
}
function Show-Snapshot($s,$alive,$now) {
    $key='stopped';$caption='后台未启动'
    if($s){$key=$s.state;$caption=$s.message}
    if($s -and $key -notin @('stopped','error') -and (-not $alive -or $now-$s.heartbeat -gt 8)){$key='error-stale';$caption='后台异常退出或状态更新中断'}
    if($script:startAt -and $now-$script:startAt -lt 8 -and (-not $s -or $s.started -lt $script:startAt)){$key='reconnecting';$caption='正在启动后台'}
    if($s -and $s.pid -eq 0 -and $key -eq 'error'){$caption='后台启动失败'}
    Set-Indicator $key $caption
    $state.Text=switch($key){'error-stale'{'后台已中断'};'error'{if($s.pid -eq 0){'服务启动失败'}else{'连接需要检查'}};default{$caption}}
    $serviceGuide.Text=switch($key){
      'standby'{'唤醒后即可接收微信任务，已有提醒继续通知。'}
      'working'{'可以在微信发任务；进入待机后，已有任务继续收尾。'}
      'draining'{'已暂停接收新任务，已有任务完成后自动待机。'}
      'stopping'{'正在结束当前通信，请稍候。'}
      'stopped'{'启动服务后，会自动连接微信。'}
      'error-stale'{'后台未响应，请启动服务；详细情况可查看日志。'}
      default{if($s.error){[string]$s.error}else{'正在连接微信，请稍候。'}}
    }
    $backendTip.SetToolTip($serviceGuide,$serviceGuide.Text)
    $connectionBadge.Text=switch($key){'standby'{'微信已连接'};'working'{'微信已连接'};'draining'{'微信已连接'};'stopped'{'服务未启动'};'stopping'{'正在停止服务'};'error-stale'{'后台未响应'};'error'{'连接需要检查'};default{'正在连接微信'}}
    # The badge draws its dot in this color and darkens the text to a readable shade.
    $connectionBadge.StatusColor=switch($key){'standby'{[TrayDesign]::Success};'working'{[TrayDesign]::Success};'draining'{[TrayDesign]::Warning};'reconnecting'{[TrayDesign]::Warning};'error'{[TrayDesign]::Danger};'error-stale'{[TrayDesign]::Danger};default{[TrayDesign]::MutedColor}}
    $connectionBadge.Invalidate()
    $menuHeader.Text=$state.Text+' · '+$connectionBadge.Text
    if($s){
      $last='尚未成功通信';if($s.last_success -gt 0){$last=[DateTimeOffset]::FromUnixTimeSeconds([long]$s.last_success).LocalDateTime.ToString('HH:mm:ss')}
      $duration=[TimeSpan]::FromSeconds([Math]::Max(0,$now-$s.started))
      $hours=[int][Math]::Floor($duration.TotalHours)
      $detail.Text=if($alive){('{0:00}:{1:00}:{2:00}' -f $hours,$duration.Minutes,$duration.Seconds)}else{'—'}
      $lastTime.Text=$last
      $connected=$alive -and $key -in @('standby','working','draining') -and $s.last_success -gt 0 -and -not $s.error
      if($connected -and $script:lastFault){Set-Feedback '微信连接已恢复正常。' 'success';$script:connectionFeedback=$true}
      elseif(-not $connected -and $script:connectionFeedback){Set-Feedback '微信连接尚未恢复，正在等待重试。';$script:connectionFeedback=$true}
      if($connected){$script:lastFault=$false}elseif($s.error){$script:lastFault=$true}
      $modelName.Text=if($s.model_name){[string]$s.model_name}else{'尚未读取'}
      $backendTip.SetToolTip($modelName,$modelName.Text)
      $mh=$s.model_health
      if($mh){
        $modelStatus.Text=[string]$mh.label
        if($mh.last_checked -gt 0 -and $mh.state -ne 'running'){$modelStatus.Text+="`r`n"+[DateTimeOffset]::FromUnixTimeSeconds([long]$mh.last_checked).LocalDateTime.ToString('MM-dd HH:mm')}
        $backendTip.SetToolTip($modelStatus,$modelStatus.Text+"`r`n"+[string]$mh.detail+"`r`n根据实际模型任务更新，不自动发送收费请求。")
        $modelStatus.ForeColor=switch($mh.state){'available'{[TrayDesign]::SuccessText};'error'{[TrayDesign]::DangerText};'running'{[TrayDesign]::WarningText};default{$muted}}
      }else{$modelStatus.Text='尚无模型调用记录';$modelStatus.ForeColor=$muted}
      $task.Text=if($alive){[string]$s.task}else{'后台未运行'}
      $taskGuide.Text=if(-not $alive){'启动服务后，任务状态会显示在这里。'}elseif($s.stage){[string]$s.stage}elseif(Has-ActiveTask $s){'任务执行中，状态由后台报告。'}else{'任务执行时可在这里查看进度。'}
      $backendTip.SetToolTip($task,$task.Text);$backendTip.SetToolTip($taskGuide,$taskGuide.Text)
      $stats.Text=if($alive){"等待任务 $($s.queued)  ·  附件处理 $($s.downloads)"}else{'等待任务 —  ·  附件处理 —'}
      $eventText=($s.events -join "`r`n`r`n")
      if($records.Text -ne $eventText -and -not $records.Focused){$records.Text=$eventText;$records.SelectionStart=$records.Text.Length;$records.SelectionLength=0;$records.ScrollToCaret()}
    }else{$detail.Text='—';$lastTime.Text='尚未成功通信';$modelName.Text='尚未读取';$task.Text='后台未运行';$taskGuide.Text='启动服务后，任务状态会显示在这里。';$stats.Text='等待任务 —  ·  附件处理 —'}
    Refresh-Backend $s $alive;Refresh-Actions $s $alive
    Update-WorkflowPanels $s ($alive -and $now-$s.heartbeat -le 8)
    Show-TaskProgress $s ($alive -and $now-$s.heartbeat -le 8)
}
function Update-ResultFileActions {
    $selected=$resultFiles.SelectedIndex
    $file=if($selected -ge 0 -and $selected -lt $script:resultFileRows.Count){$script:resultFileRows[$selected]}else{$null}
    $resultOpen.Enabled=($file -and (Test-DesktopDocument ([string]$file.path)))
    $resultFolder.Enabled=($file -and [IO.File]::Exists([string]$file.path))
}
function Update-WorkflowPanels($s,$alive) {
    $data=if($alive){$s.desktop_overview}else{$null}
    Update-Attention $data $alive
    $signature=if($data){$data.favorites|ConvertTo-Json -Depth 4 -Compress}else{''}
    if($signature -ne $script:favoriteSignature -or -not $favoriteChoice.Items.Count){
        $oldId=if($favoriteChoice.SelectedIndex -ge 0 -and $favoriteChoice.SelectedIndex -lt $script:favorites.Count){$script:favorites[$favoriteChoice.SelectedIndex].id}else{''}
        $script:favoriteSignature=$signature;$script:favorites=@($data.favorites | Where-Object {$_});$favoriteChoice.Items.Clear()
        foreach($item in $script:favorites){$label=[string]$item.name;if(@($script:favorites | Where-Object {$_.name -eq $item.name}).Count -gt 1){$label+=' · '+$item.uid};$index=$favoriteChoice.Items.Add($label);if($item.id -eq $oldId){$favoriteChoice.SelectedIndex=$index}}
        if(-not $favoriteChoice.Items.Count){$null=$favoriteChoice.Items.Add('尚未固定模板')};if($favoriteChoice.SelectedIndex -lt 0){$favoriteChoice.SelectedIndex=0}
    }
    $backendTip.SetToolTip($favoriteChoice,'最多固定 5 个常用模板；点击运行模板进入核对窗口。'+[string]$data.message)
    $quickRun.Text=if($script:favorites.Count){'运行模板'}else{'选择模板'}
    $card=$data.result;$visible=($alive -and $card -and -not (Has-ActiveTask $s) -and -not $s.queued -and -not $s.downloads)
    $script:resultCard=$card
    $resultPanel.Visible=[bool]$visible;$resultButton.Visible=[bool]$card
    $resultRevise.Visible=[bool]$visible;$resultRevise.Enabled=($visible -and $card.can_revise)
    foreach($control in @($taskGlyph,$task,$taskGuide,$taskRule,$stats,$cancelButton)){$control.Visible=-not $visible}
    $taskHead.Text=if($visible){'最近结果'}else{'当前任务'}
    if($visible){
        $resultSummary.Text=[string]$card.summary;$resultConclusion.Text=[string]$card.conclusion
        $resultConclusion.ForeColor=switch([string]$card.tone){'error'{[TrayDesign]::DangerText};'warning'{[TrayDesign]::WarningText};default{$muted}}
        $resultDetail.Text=if($card.action_label){[string]$card.action_label}else{'查看完整回复'}
        $resultDetail.Enabled=([string]$card.id -match '^[a-f0-9]{32}$')
        $backendTip.SetToolTip($resultSummary,$resultSummary.Text);$backendTip.SetToolTip($resultConclusion,$resultConclusion.Text)
        $signature=($card|ConvertTo-Json -Depth 6 -Compress)
        if($signature -ne $script:resultSignature){
            $script:resultSignature=$signature;$script:resultFileRows=@($card.files);$resultFiles.Items.Clear()
            foreach($file in $script:resultFileRows){$null=$resultFiles.Items.Add([string]$file.name)}
            if($resultFiles.Items.Count){$resultFiles.SelectedIndex=0}
            $hasFiles=$script:resultFileRows.Count -gt 0
            foreach($control in @($resultFiles,$resultOpen,$resultFolder)){$control.Visible=$hasFiles}
            $resultNote.Visible=-not $hasFiles
            $resultNote.Text=if($card.state -eq 'failed'){'查看任务记录中的错误说明，核对已执行的部分。'}else{'本次没有关联文件，可查看完整文字回复。'}
            Layout-RunCenter
        }
        Update-ResultFileActions
    }
}
function Refresh-State {
  if($PreferenceProbe -or $Preview -or $VerifyWindows -or $VerifyUX){return}
  try {
    if($timer){$timer.Interval=if($form.Visible -or $script:pending -or $script:mode){1000}else{5000}}
    $now=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $showFile=Join-Path $runtime 'show-window'
    if(Test-Path -LiteralPath $showFile){Remove-Item -LiteralPath $showFile;$form.Show();$form.Activate()}
    $file=Join-Path $runtime 'status.json'
    if(Test-Path -LiteralPath $file){$script:snapshot=Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json}else{$script:snapshot=$null}
    $s=$script:snapshot
    $alive=$s -and $s.pid -gt 0 -and (Get-Process -Id $s.pid -ErrorAction SilentlyContinue)
    Show-Snapshot $s $alive $now
    if($script:pending){
      $reply=Join-Path $runtime ('reply-'+$script:pending+'.json')
      if(Test-Path -LiteralPath $reply){
        $answer=Get-Content -LiteralPath $reply -Raw -Encoding UTF8 | ConvertFrom-Json
        Set-Feedback $answer.message;Remove-Item -LiteralPath $reply;$script:pending=$null
        if($script:mode -and $answer.message -ne '正在停止后台'){$script:mode=''}
      }elseif($now-$script:pendingAt -gt 25){Set-Feedback '后台未确认操作，请检查状态和日志' 'error';$script:pending=$null;$script:mode=''}
    }
    Refresh-Backend $s $alive;Refresh-Actions $s $alive
    if($script:mode -and -not $script:pending -and -not $alive){
      $next=$script:mode;$script:mode=''
      if($next -eq 'restart'){Start-ServiceProcess}else{$script:closing=$true;$form.Close()}
    }
  } catch { Set-Feedback ('状态读取失败：'+$_.Exception.Message) 'error';Set-Indicator 'error' '状态读取失败' }
}
$form.Add_FormClosing({param($sender,$e) if(-not $script:closing){$e.Cancel=$true;$form.Hide()}})
$form.AutoScaleDimensions=New-Object Drawing.SizeF(96,96)
$form.PerformAutoScale()
# Fit the first window to its monitor while retaining the normal desktop size.
$initialGraphics=$form.CreateGraphics();$initialScale=$initialGraphics.DpiX/96;$initialGraphics.Dispose()
$initialArea=[Windows.Forms.Screen]::FromHandle($form.Handle).WorkingArea
$frameWidth=$form.Width-$form.ClientSize.Width;$frameHeight=$form.Height-$form.ClientSize.Height
$form.MinimumSize=New-Object Drawing.Size(([int][Math]::Ceiling(880*$initialScale)+$frameWidth),([int][Math]::Ceiling(640*$initialScale)+$frameHeight))
$initialWidth=[Math]::Max([int][Math]::Ceiling(880*$initialScale),[Math]::Min([int][Math]::Round(960*$initialScale),$initialArea.Width-$frameWidth))
$initialHeight=[Math]::Max([int][Math]::Ceiling(640*$initialScale),[Math]::Min([int][Math]::Round(720*$initialScale),$initialArea.Height-$frameHeight))
if($Preview -or $VerifyUX){$initialWidth=[int][Math]::Round(960*$initialScale);$initialHeight=[int][Math]::Round(824*$initialScale)}
$form.ClientSize=New-Object Drawing.Size($initialWidth,$initialHeight)
$form.StartPosition='Manual'
$form.Location=New-Object Drawing.Point(($initialArea.Left+[Math]::Max(0,[int](($initialArea.Width-$form.Width)/2))),($initialArea.Top+[Math]::Max(0,[int](($initialArea.Height-$form.Height)/2))))
$form.Add_ClientSizeChanged({Layout-RunCenter})
$form.Add_DpiChanged({Layout-RunCenter})
Layout-RunCenter
Register-UiPreferences $form $root 'run-center' (-not ($Preview -or $Verify -or $VerifyBackend -or $VerifyWindows -or $VerifyUX))
Layout-RunCenter
$timer=New-Object Windows.Forms.Timer;$timer.Interval=5000;$timer.Add_Tick({Refresh-State;Refresh-UtilityWindows});$timer.Start()
$form.Add_VisibleChanged({if($timer){$timer.Interval=if($form.Visible){1000}else{5000};Refresh-State}})
Refresh-State
if($PreferenceProbe){. (Join-Path $PSScriptRoot 'tools/ui_preference_probe.ps1');Invoke-UiPreferenceProbe $form $root $PreferenceProbe;$tray.Visible=$false;$tray.Dispose();exit}
if($VerifyWindows){
 function Wait-UtilityVisible($panel) {
  $entry=$script:utilityWindows[$panel.file]
  if(-not $entry){throw ('Entry did not start '+$panel.label)}
  $deadline=[DateTime]::UtcNow.AddSeconds(15)
  while(-not $entry.ready -and -not $entry.process.HasExited -and [DateTime]::UtcNow -lt $deadline){
   Start-Sleep -Milliseconds 100;[Windows.Forms.Application]::DoEvents();Refresh-UtilityWindows
  }
  if(-not $entry.ready -or $entry.process.HasExited -or $entry.process.MainWindowTitle -ne $panel.title){throw ('Window not visible: '+$panel.label+' '+$hint.Text)}
  return $entry
 }
 function Close-VerifiedWindow($entry) {
  $deadline=[DateTime]::UtcNow.AddSeconds(10)
  while(-not $entry.process.HasExited -and [DateTime]::UtcNow -lt $deadline){
   [void]$entry.process.CloseMainWindow();Start-Sleep -Milliseconds 200;[Windows.Forms.Application]::DoEvents()
  }
  if(-not $entry.process.HasExited){throw ('Read-only window did not close: '+$entry.label)}
  Refresh-UtilityWindows
 }
 try {
  # Exercise the actual launch handlers without backend commands or configuration writes.
  $timer.Stop();$form.Show();[Windows.Forms.Application]::DoEvents()
  $panels=@(
   @{file='backend_editor.ps1';label='后端配置';title='微信助手 · 后端配置';button=$settingsButton},
   @{file='app_settings.ps1';label='常用应用';title='微信助手 · 常用应用';button=$appsButton},
   @{file='task_history.ps1';label='任务记录';title='微信助手 · 任务记录';button=$historyButton},
   @{file='memory_editor.ps1';label='固定记忆';title='微信助手 · 固定记忆';button=$memoryButton},
   @{file='template_editor.ps1';label='任务模板';title='微信助手 · 任务模板';button=$templatesButton},
   @{file='workflow_window.ps1-Page templates';label='运行模板';title='微信助手 · 快捷任务';button=$quickRun},
   @{file='workflow_window.ps1-Page batches';label='附件核对';title='微信助手 · 快捷任务';button=$batchButton},
   @{file='workflow_window.ps1-Page schedules';label='定时任务';title='微信助手 · 快捷任务';button=$scheduleButton},
   @{file='workflow_window.ps1-Page localfiles';label='文件任务';title='微信助手 · 快捷任务';button=$fileTaskButton}
  )
  foreach($panel in $panels){
   $panel.button.PerformClick();$entry=Wait-UtilityVisible $panel
   $form.Activate();$panel.button.PerformClick()
   if($script:utilityWindows[$panel.file].process.Id -ne $entry.process.Id){throw 'Repeated click created another process'}
   $form.Hide();[Windows.Forms.Application]::DoEvents()
   $menuItem=Find-MenuItem $menu.Items $panel.label
   $menuItem.PerformClick()
   if($script:utilityWindows[$panel.file].process.Id -ne $entry.process.Id -or -not [TrayNative]::IsWindowVisible($entry.process.MainWindowHandle)){throw 'Hidden tray entry failed'}
   Write-Output ('PASS button, visible window, repeated click and hidden tray entry: '+$panel.label)
   Close-VerifiedWindow $entry
   $menuItem.PerformClick();$entry=Wait-UtilityVisible $panel
   Write-Output ('PASS new window from hidden tray: '+$panel.label)
   Close-VerifiedWindow $entry
   $form.Show();[Windows.Forms.Application]::DoEvents()
  }
 }finally{
  foreach($entry in @($script:utilityWindows.Values)){
   if(-not $entry.process.HasExited){
    [void]$entry.process.CloseMainWindow()
    if(-not $entry.process.WaitForExit(3000)){$entry.process.Kill()}
   }
   $entry.process.Dispose()
  }
  $script:utilityWindows.Clear();$script:closing=$true;$timer.Stop();$tray.Visible=$false;$form.Close()
 }
}elseif($Verify -or $VerifyBackend){
 try {
  $form.Show();[Windows.Forms.Application]::DoEvents()
  $form.Close();[Windows.Forms.Application]::DoEvents()
  if($form.Visible -or $form.IsDisposed){throw 'Close-to-tray verification failed'}
  Refresh-State
  if($timer.Interval -ne 5000){throw 'Hidden refresh interval must be 5000 ms'}
  Write-Output 'PASS hidden refresh interval: 5000 ms'
  if($script:snapshot.queued -or $script:snapshot.downloads -or $script:lastState -ne 'standby'){throw 'Verify requires an idle standby backend'}
  $form.Show();[Windows.Forms.Application]::DoEvents()
  foreach($action in @('wake','standby')){
   if($script:wakeButton.Tag -ne $action){throw ('Primary button has wrong action: '+$script:wakeButton.Tag)}
   $script:wakeButton.PerformClick()
   $deadline=[DateTime]::UtcNow.AddSeconds(8)
   while($script:pending -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 150;Refresh-State;[Windows.Forms.Application]::DoEvents()}
   Start-Sleep -Milliseconds 700;Refresh-State
   $expected=if($action -eq 'wake'){'working'}else{'standby'}
   if($script:pending -or $script:lastState -ne $expected){throw ('Control verification failed: '+$action+' '+$hint.Text)}
   Write-Output ('PASS UI control: '+$action+' -> '+$script:lastState)
  }
  Write-Output 'PASS close-to-tray: form hidden and not disposed'
  if($VerifyBackend){
   $originalBackend=[string]$script:snapshot.backend
   $originalPid=$script:snapshot.pid
   try {
    $form.Show();[Windows.Forms.Application]::DoEvents();Refresh-State
    foreach($target in @('claude','deepseek')){
     if($target -eq 'claude'){$claudeButton.PerformClick()}else{$deepseekButton.PerformClick()}
     $deadline=[DateTime]::UtcNow.AddSeconds(10)
     while(($script:pending -or [string]$script:snapshot.backend -ine $target) -and [DateTime]::UtcNow -lt $deadline){
      Start-Sleep -Milliseconds 150;Refresh-State;[Windows.Forms.Application]::DoEvents()
     }
     Refresh-State
     $saved=Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
     if($script:pending -or [string]$script:snapshot.backend -ine $target -or $saved.backend -ne $target -or $script:snapshot.pid -ne $originalPid){throw ('Backend UI switch did not apply: '+$hint.Text)}
     if(($target -eq 'claude' -and (-not $claudeButton.Primary -or -not $claudeMenu.Checked)) -or ($target -eq 'deepseek' -and (-not $deepseekButton.Primary -or -not $deepseekMenu.Checked))){throw 'Backend selection did not match actual state'}
     Write-Output ('PASS backend button, tray selection, saved config and same PID: '+$target)
    }
   } finally {
    if([string]$script:snapshot.backend -ine $originalBackend){
     Request-Backend $originalBackend.ToLowerInvariant()
     $deadline=[DateTime]::UtcNow.AddSeconds(10)
     while($script:pending -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 150;Refresh-State}
     if([string]$script:snapshot.backend -ine $originalBackend){throw 'Original backend was not restored'}
    }
   }
  }

 } finally { $script:closing=$true;$timer.Stop();$tray.Visible=$false;$form.Dispose() }
}elseif($Preview -or $VerifyUX){
 $timer.Stop();$form.Show();[Windows.Forms.Application]::DoEvents()
 function New-SampleSnapshot($sampleState,$now){
  $key=if($sampleState -eq 'busy'){'working'}elseif($sampleState -eq 'codechanged'){'standby'}else{$sampleState}
  $sample=[pscustomobject]@{pid=999999;heartbeat=$now;started=$now-8316;last_success=$now-2;state=$key;message='在线待机';backend='deepseek';backend_switch_available=$true;code_identity=[pscustomobject]@{state='same';message='';commit='abc1234';detail='运行源码与启动时一致'};model_name='deepseek-flash';model_health=[pscustomobject]@{state='unknown';label='尚无模型调用记录';last_checked=0;detail=''};task='暂无执行中的任务';stage='';queued=0;downloads=0;error='';events=@('16:20:30  微信连接正常，等待新消息','16:18:12  助手服务已启动','16:17:03  检测到微信已登录')}
  if($key -eq 'working'){$sample.message='助手已唤醒'}
  if($sampleState -in @('busy','draining')){$sample.task='示例任务：整理工作资料';$sample.stage='正在处理资料，请稍候';$sample.queued=2;$sample.downloads=1;$sample.backend_switch_available=$false}
  if($key -eq 'draining'){$sample.message='正在收尾';$sample.backend_switch_available=$false}
  if($key -in @('reconnecting','error')){$sample.message='正在连接微信';$sample.error='暂时无法连接微信，后台正在重试。请检查本机网络。'}
  if($key -eq 'stopped'){$sample.pid=0;$sample.message='服务未启动';$sample.backend_switch_available=$false}
  if($sampleState -eq 'codechanged'){$sample.code_identity=[pscustomobject]@{state='changed';message='代码已更新，重启后生效';commit='abc1234';detail='运行源码与启动时不一致'}}
  return $sample
 }
 $samples=if($VerifyUX){@('standby','working','busy','draining','reconnecting','error','stopped')}else{@($PreviewState)}
 $reviewDir=Join-Path $runtime 'workbench-20261107';[void][IO.Directory]::CreateDirectory($reviewDir)
 foreach($sampleState in $samples){
  $now=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();$script:snapshot=New-SampleSnapshot $sampleState $now
  Show-Snapshot $script:snapshot ($sampleState -ne 'stopped') $now;[Windows.Forms.Application]::DoEvents()
  if($VerifyUX){
   $expected=switch($sampleState){'standby'{'wake'};'working'{'standby'};'busy'{'standby'};'draining'{'wake'};'stopped'{'start'};default{''}}
   if([string]$script:wakeButton.Tag -ne $expected){throw ('Wrong primary action: '+$sampleState)}
   if($cancelButton.Enabled -ne ($sampleState -in @('busy','draining'))){throw ('Wrong cancel availability: '+$sampleState)}
   if($sampleState -in @('busy','draining') -and ($restartButton.Enabled -or $deepseekButton.Enabled)){throw 'Busy maintenance/model switch must be disabled'}
   if($sampleState -in @('reconnecting','error') -and $script:wakeButton.Enabled){throw 'Wake must wait for connection'}
   if($sampleState -eq 'standby'){$standbyIndicatorColor=$script:statusColor;$standbyTextColor=$state.ForeColor;$connectedBadgeColor=$connectionBadge.StatusColor}
   if($sampleState -eq 'working'){
    if($script:statusColor -eq $standbyIndicatorColor -or $state.ForeColor -eq $standbyTextColor){throw 'Standby and awake must have distinct indicator and title colors'}
    if($connectionBadge.StatusColor -ne $connectedBadgeColor){throw 'Standby and awake must retain the same connected WeChat badge'}
   }
   $buttonTextWidth=[Windows.Forms.TextRenderer]::MeasureText($script:wakeButton.Text,$script:wakeButton.Font).Width
   $graphics=$form.CreateGraphics();$scale=$graphics.DpiX/96;$graphics.Dispose()
   if($buttonTextWidth -gt $script:wakeButton.Width-52*$scale){throw ('Primary action text clipped: '+$script:wakeButton.Text)}
   Write-Output ('PASS UI state and action availability: '+$sampleState)
  }
  $image=New-Object Drawing.Bitmap($form.Width,$form.Height)
  $form.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)))
  $image.Save((Join-Path $reviewDir ('tray-'+$sampleState+'.png')))
  if(-not $VerifyUX -and $sampleState -eq 'standby'){$image.Save((Join-Path $runtime 'tray-preview.png'))}
  $image.Dispose()
 }
 if($VerifyUX){
  $now=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();$script:snapshot=New-SampleSnapshot 'standby' $now
  Show-Snapshot $script:snapshot $true $now;Set-Feedback '设置窗口已打开'
  Show-Snapshot $script:snapshot $true $now
  if($hint.Text -ne '设置窗口已打开'){throw 'Refresh overwrote action feedback'}
  $script:pending='isolated-ui-test';Refresh-Actions $script:snapshot $true;Refresh-Backend $script:snapshot $true
  if($script:wakeButton.Enabled -or $cancelButton.Enabled -or $restartButton.Enabled -or $deepseekButton.Enabled){throw 'Pending operation allowed another control request'}
  $script:pending=$null
  Write-Output 'PASS persistent action feedback and pending-operation lock'
  $script:snapshot=New-SampleSnapshot 'error' $now;Show-Snapshot $script:snapshot $true $now
  $script:snapshot=New-SampleSnapshot 'reconnecting' $now;$script:snapshot.error='';$script:snapshot.last_success=0
  Show-Snapshot $script:snapshot $true $now
  if($hint.Text -eq '微信连接已恢复正常。'){throw 'Restart without a successful poll falsely reported recovery'}
  $script:snapshot=New-SampleSnapshot 'standby' $now;Show-Snapshot $script:snapshot $true $now
  if($hint.Text -ne '微信连接已恢复正常。'){throw 'A successful reconnection did not report recovery'}
  $script:snapshot=New-SampleSnapshot 'error' $now;Show-Snapshot $script:snapshot $true $now
  if($hint.Text -eq '微信连接已恢复正常。'){throw 'Recovery feedback remained after a new connection failure'}
  Set-Feedback '设置窗口已打开';Show-Snapshot $script:snapshot $true $now
  if($hint.Text -ne '设置窗口已打开'){throw 'Connection refresh overwrote normal action feedback'}
  $script:snapshot=New-SampleSnapshot 'standby' $now;Show-Snapshot $script:snapshot $true $now
  Write-Output 'PASS recovery requires successful communication and clears on a new outage'
  $script:snapshot=New-SampleSnapshot 'codechanged' $now;Show-Snapshot $script:snapshot $true $now
  if($maintenanceGuide.Text -ne '代码已更新，重启后生效'){throw ('Code update hint missing: '+$maintenanceGuide.Text)}
  $hintWidth=[Windows.Forms.TextRenderer]::MeasureText($maintenanceGuide.Text,$maintenanceGuide.Font).Width
  if($hintWidth -gt $maintenanceGuide.ClientSize.Width){throw ('Code update hint is clipped: '+$hintWidth)}
  if($backendTip.GetToolTip($restartButton) -notlike '*abc1234*'){throw 'Diagnostic text lost the loaded revision'}
  if($state.Text -notlike '*在线待机*' -or $task.Text -notlike '*暂无执行中的任务*'){throw ('Code update hint hid the connection or task state: '+$state.Text+' / '+$task.Text)}
  Write-Output 'PASS code update hint is visible, unclipped and keeps the loaded revision'
  $legacy=New-SampleSnapshot 'standby' $now;$legacy.PSObject.Properties.Remove('code_identity')
  $script:snapshot=$legacy;Show-Snapshot $script:snapshot $true $now
  if($maintenanceGuide.Text -ne '空闲时可重启服务'){throw ('Old status without code identity must keep the previous text: '+$maintenanceGuide.Text)}
  if($backendTip.GetToolTip($restartButton) -notlike '*版本状态未知*'){throw 'Missing code identity must read as unknown, never as up to date'}
  Write-Output 'PASS an older backend without the new field still refreshes and reads as unknown'
  foreach($control in @($title,$state,$serviceGuide,$hint,$durationCaption,$communicationCaption,$maintenanceGuide)){
   if($control.Height -lt $control.Font.Height){throw ('Text clipped vertically: '+$control.Text)}
  }
  $script:snapshot.downloads=1;$script:snapshot.backend_switch_available=$false;Show-Snapshot $script:snapshot $true $now
  if($cancelButton.Enabled -or $restartButton.Enabled){throw 'Queued-only attachment has no active cancellation target and must prevent restart'}
  $script:snapshot=New-SampleSnapshot 'stopped' $now;$script:startAt=$now;Show-Snapshot $script:snapshot $false $now
  if($script:wakeButton.Enabled -or $exitMenu.Enabled){throw 'Starting service allowed duplicate launch or premature exit'}
  $script:startAt=0;$script:snapshot=New-SampleSnapshot 'standby' $now;Show-Snapshot $script:snapshot $true $now
  $graphics=$form.CreateGraphics();$dpiScale=$graphics.DpiX/96;$graphics.Dispose()
  foreach($logicalWidth in @(880,1040)){
   foreach($logicalHeight in @(640,656,800,816,824)){
   $sizeLabel=if($logicalHeight -eq 824){'width-'+$logicalWidth}else{'width-'+$logicalWidth+'-height-'+$logicalHeight}
   $form.ClientSize=New-Object Drawing.Size([int]($logicalWidth*$dpiScale),[int]($logicalHeight*$dpiScale));[Windows.Forms.Application]::DoEvents()
   $script:snapshot=New-SampleSnapshot 'standby' $now;Show-Snapshot $script:snapshot $true $now
   foreach($control in @($statusCard,$taskCard,$logCard,$modelCard,$entryCard,$maintenanceCard,$hint,$footer)){
    if($control.Bounds.Right -gt $form.ClientSize.Width -or $control.Bounds.Bottom -gt $form.ClientSize.Height){throw ('Control outside resized window: '+$control.GetType().Name)}
   }
   if($taskCard.Bounds.Right -ge $entryCard.Bounds.Left -or $task.Bounds.Right -gt $taskCard.ClientSize.Width -or $state.Bounds.Right -gt $script:wakeButton.Bounds.Left){throw 'Column or text overlap after resize'}
   if($statusCard.Bottom -gt $taskCard.Top -or $taskCard.Bottom -gt $logCard.Top -or $entryCard.Bottom -gt $modelCard.Top -or $modelCard.Bottom -gt $maintenanceCard.Top -or $logCard.Bottom -gt $hint.Top -or $maintenanceCard.Bottom -gt $hint.Top){throw ('Cards or footer overlap at '+$sizeLabel)}
   if($title.Bottom -gt $subtitle.Top -or $subtitle.Bottom -gt $statusCard.Top -or $brand.Bottom -gt $statusCard.Top -or $connectionBadge.Bottom -gt $statusCard.Top -or $subtitle.Right -ge $connectionBadge.Left){throw ('Header or subtitle overlaps at '+$sizeLabel)}
   foreach($label in @($subtitle,$durationCaption,$communicationCaption,$maintenanceGuide,$hint,$footer)){
    if($label.Height -lt $label.Font.Height){throw ('Auxiliary text clipped vertically at '+$sizeLabel+': '+$label.Text)}
   }
   $lastTime.Text='尚未成功通信'
   foreach($label in @($durationCaption,$communicationCaption,$lastTime)){
    if([Windows.Forms.TextRenderer]::MeasureText($label.Text,$label.Font).Width -gt $label.Width){throw ('Metric label clipped: '+$label.Text)}
   }
   Show-Snapshot $script:snapshot $true $now
   if(@($fileTaskButton,$batchButton,$appsButton,$memoryButton | ForEach-Object {$_.Height} | Select-Object -Unique).Count -ne 1){throw 'Quick action heights must match'}
   $image=New-Object Drawing.Bitmap($form.Width,$form.Height);$form.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)))
   $image.Save((Join-Path $reviewDir ('tray-'+$sizeLabel+'.png')));$image.Dispose()
   foreach($scenario in @('busy','error')){
    $script:snapshot=New-SampleSnapshot $scenario $now
    $script:snapshot.task='整理项目资料，核对本轮附件并生成结果报告（较长任务名称示例）'
    $script:snapshot.stage='正在核对已接收的文件并准备结果，请稍候。'
    $script:snapshot.queued=128;$script:snapshot.downloads=12
    $script:snapshot.model_name='provider/model-name-with-a-long-version-suffix'
    $script:snapshot.model_health=[pscustomobject]@{state='available';label='上次模型调用正常';last_checked=$now-300;last_success=$now-300;detail='示例数据'}
    Show-Snapshot $script:snapshot $true $now;[Windows.Forms.Application]::DoEvents()
    foreach($panel in @($statusCard,$taskCard,$logCard,$modelCard,$entryCard,$maintenanceCard)){
     foreach($child in $panel.Controls | Where-Object {$_.Visible}){
      if($child.Left -lt 0 -or $child.Top -lt 0 -or $child.Right -gt $panel.ClientSize.Width -or $child.Bottom -gt $panel.ClientSize.Height){throw ('Child outside panel: '+$child.Text)}
     }
    }
    if($stats.Right -ge $cancelButton.Left -or $taskGuide.Bottom -ge $taskRule.Top){throw 'Task footer overlaps task copy'}
    if($modelStatus.Height -lt 2*$modelStatus.Font.Height -or $modelName.Bottom -gt $modelStatus.Top){throw 'Two-line model status is clipped or overlaps settings'}
    if($backendTip.GetToolTip($task) -ne $script:snapshot.task -or $backendTip.GetToolTip($modelName) -ne $script:snapshot.model_name){throw 'Long copy lost its full tooltip'}
    $image=New-Object Drawing.Bitmap($form.Width,$form.Height);$form.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)))
    $image.Save((Join-Path $reviewDir ('tray-'+$sizeLabel+'-'+$scenario+'.png')));$image.Dispose()
   }
   Write-Output ('PASS card, header, subtitle, metric and long-copy boundaries: '+$logicalWidth+'x'+$logicalHeight)
   }
  }
  Write-Output 'PASS long task/model copy, two-line model status and all card children at both widths'
  $workflowDir=Join-Path $runtime 'workbench-20261107';[IO.Directory]::CreateDirectory($workflowDir)|Out-Null
  $samplePath=Join-Path $workflowDir 'ui-sample.txt';[IO.File]::WriteAllText($samplePath,'isolated UI document',(New-Object Text.UTF8Encoding($false)))
  foreach($size in @(@(880,640),@(960,824))){
   $form.ClientSize=New-Object Drawing.Size([int]($size[0]*$dpiScale),[int]($size[1]*$dpiScale));[Windows.Forms.Application]::DoEvents()
   $script:snapshot=New-SampleSnapshot 'standby' $now
   $sampleCard=[pscustomobject]@{id=('b'*32);state='completed';tone='neutral';action_label='查看完整回复';summary='已生成本周汇总表；金额合计 40。';conclusion='处理结束 · 已配置的结果检查通过';files=@([pscustomobject]@{path=$samplePath;name='汇总说明.txt'})}
   $script:snapshot | Add-Member -NotePropertyName desktop_overview -NotePropertyValue ([pscustomobject]@{favorites=@([pscustomobject]@{id=('a'*32);uid='示例用户';name='周报';revision=1});result=$sampleCard;message=''})
   Show-Snapshot $script:snapshot $true $now;[Windows.Forms.Application]::DoEvents()
   if(-not $resultPanel.Visible -or $task.Visible -or -not $resultOpen.Enabled -or $favoriteChoice.Items[0] -ne '周报'){throw 'Dashboard result or favorite template missing'}
   foreach($child in $resultPanel.Controls){if($child.Right -gt $resultPanel.ClientSize.Width -or $child.Bottom -gt $resultPanel.ClientSize.Height){throw 'Result card controls overflow'}}
   if($taskHead.Right -gt $resultRevise.Left -or $resultRevise.Right -gt $resultButton.Left){throw 'Result heading overlaps action'}
   $image=New-Object Drawing.Bitmap($form.Width,$form.Height);$form.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)));$image.Save((Join-Path $workflowDir ('dashboard-'+$size[0]+'x'+$size[1]+'.png')));$image.Dispose()
   $sampleCard.files[0].path=$samplePath+'.exe';Show-Snapshot $script:snapshot $true $now
   if($resultOpen.Enabled -or $resultFolder.Enabled){throw 'Missing result file must be disabled'}
   $sampleCard.files=@();$sampleCard.state='failed';$sampleCard.tone='error';$sampleCard.action_label='查看失败原因'
   $sampleCard.summary='汇总报表未完成：缺少本月数据文件。';$sampleCard.conclusion='执行失败 · 未配置结果检查'
   $attention=[pscustomobject]@{total=2;items=@(
     [pscustomobject]@{id=('c'*32);uid='示例用户';kind='task';title='整理上周的会议记录';detail='可手动恢复';action_label='查看任务记录'},
     [pscustomobject]@{id=('d'*32);uid='示例用户';kind='batch';title='本月报表 · 3 个文件';detail='附件清单待确认';action_label='核对附件'}
   )}
   $script:snapshot.desktop_overview | Add-Member -NotePropertyName attention -NotePropertyValue $attention
   Show-Snapshot $script:snapshot $true $now;[Windows.Forms.Application]::DoEvents()
   if($resultFiles.Visible -or $resultOpen.Visible -or $resultFolder.Visible -or -not $resultDetail.Enabled -or $resultDetail.Text -ne '查看失败原因'){throw 'Text-only failure shows file controls or loses detail action'}
   if($resultConclusion.ForeColor -ne [TrayDesign]::DangerText){throw 'Failure is not visually distinguished'}
   if($attentionList.Items.Count -ne 2 -or -not $attentionList.Visible){throw 'Pending work is missing from the workbench'}
   $attentionList.Items[1].Selected=$true;Show-Snapshot $script:snapshot $true $now
   if($attentionOpen.Text -ne '核对附件' -or $attentionList.SelectedItems[0].Tag.id -ne ('d'*32)){throw 'Attention selection or target was lost on refresh'}
   $activityToggle.PerformClick();if(-not $records.Visible -or $attentionList.Visible){throw 'Activity toggle failed'}
   $activityToggle.PerformClick();if($records.Visible -or -not $attentionList.Visible){throw 'Return to attention failed'}
   $image=New-Object Drawing.Bitmap($form.Width,$form.Height);$form.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)));$image.Save((Join-Path $workflowDir ('workbench-'+$size[0]+'x'+$size[1]+'.png')));$image.Dispose()
   $sampleCard.state='completed';$sampleCard.tone='neutral';$sampleCard.action_label='查看完整回复'
   Show-Snapshot $script:snapshot $true $now
   if($resultDetail.Text -ne '查看完整回复' -or -not $resultNote.Visible){throw 'Text-only completed result lacks reply action'}
   $script:snapshot.desktop_overview.attention=[pscustomobject]@{total=0;items=@()};Show-Snapshot $script:snapshot $true $now
   if(-not $attentionEmpty.Visible -or $attentionOpen.Enabled -or $attentionList.Items.Count){throw 'Resolved attention items linger'}
   $script:snapshot.desktop_overview.attention=$null;$script:snapshot.desktop_overview.message='任务记录暂不可读，请稍后刷新';Show-Snapshot $script:snapshot $true $now
   if($attentionEmpty.Text -notlike '*暂不可读*'){throw 'Unreadable attention was shown as empty'}
   $script:snapshot.queued=1;Show-Snapshot $script:snapshot $true $now
   if($resultPanel.Visible -or -not $task.Visible){throw 'Result card hid queued task status'}
  }
  Write-Output 'PASS favorite templates, result cards, document availability and active-task priority'
  foreach($size in @(@(880,640),@(960,824))){
   $form.ClientSize=New-Object Drawing.Size([int]($size[0]*$dpiScale),[int]($size[1]*$dpiScale));[Windows.Forms.Application]::DoEvents()
   $script:snapshot=New-SampleSnapshot 'busy' $now
   $script:snapshot | Add-Member -NotePropertyName task_progress -NotePropertyValue ([pscustomobject]@{phase='model';label='模型处理';elapsed_seconds=125;phase_seconds=118;stopping=$false;steps=@([pscustomobject]@{label='准备任务'},[pscustomobject]@{label='核对输入'},[pscustomobject]@{label='模型处理'})})
   Show-Snapshot $script:snapshot $true $now;[Windows.Forms.Application]::DoEvents()
   if(-not $progressText.Visible -or -not $elapsedText.Visible -or $taskGlyph.Visible -or $elapsedText.Text -notlike '*00:02:05*'){throw 'Observed progress not visible'}
   if($progressText.Bottom -gt $task.Top -or $taskGuide.Bottom -gt $elapsedText.Top -or $elapsedText.Bottom -gt $taskRule.Top){throw 'Progress text overlaps'}
   $progressDir=Join-Path $runtime 'workbench-20261107';[IO.Directory]::CreateDirectory($progressDir)|Out-Null
   $image=New-Object Drawing.Bitmap($form.Width,$form.Height);$form.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)));$image.Save((Join-Path $progressDir ('progress-'+$size[0]+'x'+$size[1]+'.png')));$image.Dispose()
   $script:snapshot.task_progress.stopping=$true;$script:snapshot.stage='正在停止当前任务';Show-Snapshot $script:snapshot $true $now
   if($progressText.Text -notlike '*正在停止*'){throw 'Cancellation must retain actual phase'}
   $script:snapshot=New-SampleSnapshot 'standby' $now;Show-Snapshot $script:snapshot $true $now
   if($progressText.Visible -or $elapsedText.Visible){throw 'Idle view retains old progress'}
  }
  Write-Output 'PASS real phase, elapsed times, cancellation, idle clearing and two layout sizes'
  Write-Output 'PASS queued-only attachment, duplicate startup prevention and narrow/wide layouts'
  $script:snapshot=New-SampleSnapshot 'standby' $now;Show-Snapshot $script:snapshot $true $now
  $activityToggle.PerformClick();[Windows.Forms.Application]::DoEvents()
  [void]$records.Focus();$records.Select(0,3);$oldText=$records.Text;$script:snapshot.events+=,'示例新动态'
  Show-Snapshot $script:snapshot $true $now
  if($records.Text -ne $oldText){throw 'Refresh interrupted reading or copying events'}
  [void]$script:wakeButton.Focus();Show-Snapshot $script:snapshot $true $now
  if($records.Text -notlike '*示例新动态*'){throw 'Event refresh did not resume after reading'}
  Write-Output 'PASS event reading/copying is preserved and refresh resumes on focus change'
 }
 $script:closing=$true;$form.Close()
}else{
 if($Show){$form.Show()}else{$form.Add_Shown({$form.Hide()})}
 [Windows.Forms.Application]::Run($form)
}
foreach($entry in @($script:utilityWindows.Values)){$entry.process.Dispose()}
$backendTip.Dispose();$timer.Dispose();$tray.Visible=$false;$tray.Dispose();$form.Dispose();$mutex.Dispose()
