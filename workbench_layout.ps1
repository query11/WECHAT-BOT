# Layout and read-only navigation for the task workbench. Loaded by tray_app.ps1.
function Layout-RunCenter {
    if($script:layingOut){return};$script:layingOut=$true
    try {
        $graphics=$form.CreateGraphics();$script:uiScale=$graphics.DpiX/96;$graphics.Dispose()
        $w=$form.ClientSize.Width/$script:uiScale;$h=$form.ClientSize.Height/$script:uiScale
        $left=[Math]::Floor((($w-64)*0.6)/8)*8;$right=$w-64-$left;$rx=40+$left
        $body=184;$bottom=$h-48;$taskHeight=[Math]::Min(248,208+[Math]::Max(0,($h-640)/2))
        $attentionY=$body+$taskHeight+16;$attentionHeight=$bottom-$attentionY
        # Both columns end on the same line. The right column holds three cards and two 12px gaps:
        # maintenance is fixed and the model card takes the rest (at least 156 so its status keeps
        # two lines). The entry card uses the room in between: 3x2 tiles (schedules and templates
        # join the grid), 2x2 tiles with those two as header links, or one compact row.
        $cards=$bottom-$body-24;$maintenanceHeight=104
        $rows=3;$tileHeight=[Math]::Min(64,[Math]::Floor(($cards-$maintenanceHeight-172-112)/3))
        if($tileHeight -lt 48){$rows=2;$tileHeight=[Math]::Min(64,[Math]::Floor(($cards-$maintenanceHeight-156-104)/2))}
        if($tileHeight -lt 44){$rows=0}
        $entryHeight=switch($rows){3{112+3*$tileHeight};2{104+2*$tileHeight};default{124}};$modelHeight=$cards-$entryHeight-$maintenanceHeight
        $form.SuspendLayout()
        Ui-Bounds $brand 24 16 40 40;Ui-Bounds $title 80 8 350 32
        Ui-Bounds $subtitle 80 40 ($w-312) 24;Ui-Bounds $connectionBadge ($w-208) 24 184 32
        Ui-Bounds $statusCard 24 80 ($w-48) 88
        Ui-Bounds $stateDot 16 22 12 12;Ui-Bounds $state 40 8 224 36
        Ui-Bounds $serviceGuide 280 8 ($w-528) 36
        Ui-Bounds $script:wakeButton ($w-224) 12 160 36
        Ui-Bounds $durationGlyph 16 57 16 16;Ui-Bounds $durationCaption 40 52 64 24;Ui-Bounds $detail 112 52 120 24
        Ui-Bounds $communicationGlyph 280 57 16 16;Ui-Bounds $communicationCaption 304 52 96 24;Ui-Bounds $lastTime 408 52 144 24
        foreach($c in @($eyebrow,$statusRule,$wakeHelp)){$c.Visible=$false;Ui-Bounds $c 0 0 1 1}
        # Every card header shares one rhythm: icon at (16,12), title at (44,8), header actions at y=6.
        foreach($heading in @($taskHead,$logTitle,$entryHead,$modelHead,$maintenanceHead)){Ui-Bounds $heading.Tag 16 12 20 20}
        Ui-Bounds $taskCard 24 $body $left $taskHeight
        Ui-Bounds $taskHead 44 8 ($left-284) 28
        Ui-Bounds $resultRevise ($left-224) 6 104 32
        Ui-Bounds $resultButton ($left-112) 6 96 32
        Ui-Bounds $taskGlyph (($left-32)/2) 56 32 32
        Ui-Bounds $taskRule 16 ($taskHeight-48) ($left-32) 1
        Ui-Bounds $stats 16 ($taskHeight-40) ($left-208) 32;Ui-Bounds $cancelButton ($left-168) ($taskHeight-40) 152 32
        Ui-Bounds $resultPanel 16 48 ($left-32) ($taskHeight-60)
        Ui-Bounds $resultSummary 0 0 ($left-32) 32;Ui-Bounds $resultConclusion 0 36 ($left-32) 24
        Ui-Bounds $resultFiles 0 68 ($left-32) 28;Ui-Bounds $resultNote 0 64 ($left-32) 40
        Ui-Bounds $resultOpen 0 ($taskHeight-96) 108 32;Ui-Bounds $resultFolder 120 ($taskHeight-96) 128 32
        $hasFiles=@($script:resultFileRows | Where-Object {$_}).Count -gt 0
        if($hasFiles){Ui-Bounds $resultDetail 260 ($taskHeight-96) ($left-292) 32}else{Ui-Bounds $resultDetail 0 ($taskHeight-96) 160 32}
        Ui-Bounds $logCard 24 $attentionY $left $attentionHeight
        Ui-Bounds $logTitle 44 8 ($left-292) 28
        Ui-Bounds $historyButton ($left-232) 6 104 32;Ui-Bounds $activityToggle ($left-120) 6 104 32
        Ui-Bounds $attentionList 16 48 ($left-32) ($attentionHeight-100)
        $attentionList.Columns[0].Width=[int](164*$script:uiScale);$attentionList.Columns[1].Width=[Math]::Max(80,$attentionList.ClientSize.Width-$attentionList.Columns[0].Width-24)
        Ui-Bounds $attentionEmpty 16 52 ($left-32) ([Math]::Min(64,$attentionHeight-104))
        Ui-Bounds $records 40 48 ($left-56) ($attentionHeight-100);Ui-Bounds $timeline 16 48 16 ($attentionHeight-100)
        Ui-Bounds $attentionOpen ($left-184) ($attentionHeight-44) 168 32
        Ui-Bounds $entryCard $rx $body $right $entryHeight
        Ui-Bounds $entryHead 44 8 ($right-252) 28
        Ui-Bounds $scheduleButton ($right-200) 6 88 32;Ui-Bounds $templatesButton ($right-104) 6 88 32
        Ui-Bounds $favoriteChoice 16 46 ($right-144) 28;Ui-Bounds $quickRun ($right-112) 44 96 32
        $tileOrder=if($rows -eq 3){@($fileTaskButton,$batchButton,$scheduleButton,$templatesButton,$appsButton,$memoryButton)}elseif($rows -eq 2){@($fileTaskButton,$batchButton,$appsButton,$memoryButton)}else{@()}
        if($rows){
            $tile=[Math]::Floor(($right-40)/2)
            for($i=0;$i -lt $tileOrder.Count;$i++){
                $x=if($i % 2){24+$tile}else{16};$y=84+[Math]::Floor($i/2)*($tileHeight+8)
                Ui-Bounds $tileOrder[$i] $x $y $(if($i % 2){$right-40-$tile}else{$tile}) $tileHeight
            }
        }else{
            $small=[Math]::Floor(($right-56)/4)
            Ui-Bounds $fileTaskButton 16 82 $small 32;Ui-Bounds $batchButton (24+$small) 82 $small 32
            Ui-Bounds $appsButton (32+2*$small) 82 $small 32;Ui-Bounds $memoryButton (40+3*$small) 82 ($right-56-3*$small) 32
        }
        foreach($entry in $script:entryTiles){
            $isTile=$tileOrder -contains $entry[0];$header=$entry[0] -eq $scheduleButton -or $entry[0] -eq $templatesButton
            $entry[0].IconKind=if($isTile){$entry[1]}else{''};$entry[0].Description=if($isTile){$entry[2]}else{''}
            $entry[0].Link=$header -and -not $isTile;$entry[0].Invalidate()
        }
        Ui-Bounds $modelCard $rx ($body+$entryHeight+12) $right $modelHeight
        Ui-Bounds $modelHead 44 8 ($right-156) 28
        Ui-Bounds $settingsButton ($right-104) 6 88 32
        $chip=[Math]::Floor(($right-40)/16)*8
        Ui-Bounds $backendPicker 16 44 ($right-32) 32
        Ui-Bounds $modelName 16 84 ($right-32) 24;Ui-Bounds $modelStatus 16 110 ($right-32) ($modelHeight-118)
        foreach($c in @($modelRule,$modelCaption)){$c.Visible=$false;Ui-Bounds $c 0 0 1 1}
        Ui-Bounds $maintenanceCard $rx ($bottom-$maintenanceHeight) $right $maintenanceHeight
        Ui-Bounds $maintenanceHead 44 8 ($right-60) 28
        Ui-Bounds $restartButton 16 44 $chip 32;Ui-Bounds $logButton (24+$chip) 44 ($right-40-$chip) 32
        Ui-Bounds $maintenanceGuide 16 76 ($right-32) 24
        # One status line: action feedback on the left, the standing tip right-aligned.
        Ui-Bounds $hint 24 ($h-40) ($w-480) 24;Ui-Bounds $footer ($w-444) ($h-40) 420 24
        Update-ProgressLayout
    } finally {$form.ResumeLayout($true);$script:layingOut=$false}
}

function Update-ProgressLayout {
    if(-not $script:uiScale){return}
    $left=$taskCard.Width/$script:uiScale;$height=$taskCard.Height/$script:uiScale
    $top=if($height -lt 232){44}else{56}
    Ui-Bounds $progressText 16 $top ($left-32) 24
    Ui-Bounds $task 16 ($top+28) ($left-32) 24
    Ui-Bounds $taskGuide 16 ($top+56) ($left-32) 28
    Ui-Bounds $elapsedText 16 ($top+88) ($left-32) 24
    if(-not $progressText.Visible){Ui-Bounds $task 16 92 ($left-32) 24;Ui-Bounds $taskGuide 16 120 ($left-32) 28}
}

function Update-AttentionAction {
    $item=if($attentionList.SelectedItems.Count){$attentionList.SelectedItems[0].Tag}else{$null}
    $attentionOpen.Enabled=([bool]$item -and -not $script:showActivity)
    $attentionOpen.Text=if($item){[string]$item.action_label}else{'查看所选事项'}
}

function Open-AttentionItem {
    if($script:showActivity -or -not $attentionList.SelectedItems.Count){return}
    $item=$attentionList.SelectedItems[0].Tag
    if($item.kind -eq 'batch'){Open-Workflow 'batches' '' ([string]$item.id)}else{Open-TaskReceipt ([string]$item.id)}
}

function Update-AttentionDisplay {
    $events=$script:showActivity;$available=$null -ne $script:attentionData
    $logTitle.Text=if($events){'最近动态'}elseif($available){'待处理事项 · '+$script:attentionData.total}else{'待处理事项'}
    $activityToggle.Text=if($events){'返回待处理'}else{'最近动态'}
    $records.Visible=$events;$timeline.Visible=$events
    $attentionList.Visible=(-not $events -and $available -and $attentionList.Items.Count -gt 0)
    $attentionEmpty.Visible=(-not $events -and -not $attentionList.Visible)
    $attentionOpen.Visible=-not $events
    $backendTip.SetToolTip($logTitle, $(if($available -and $script:attentionData.total -gt 100){'当前显示最近 100 项；处理后较早事项会继续显示。'}else{'汇总未结清的任务与待核对附件，查看不会自动执行。'}))
    Update-AttentionAction
}

function Update-Attention($data,$alive) {
    $script:attentionData=if($alive){$data.attention}else{$null}
    $signature=if($script:attentionData){$script:attentionData|ConvertTo-Json -Depth 5 -Compress}else{''}
    if($signature -ne $script:attentionSignature){
        $selected=if($attentionList.SelectedItems.Count){$attentionList.SelectedItems[0].Tag.id}else{''}
        $top=if($attentionList.TopItem){$attentionList.TopItem.Tag.id}else{''}
        $script:attentionSignature=$signature;$attentionList.BeginUpdate()
        try {
            $attentionList.Items.Clear()
            foreach($entry in @($script:attentionData.items | Where-Object {$_})){
                $item=New-Object Windows.Forms.ListViewItem([string]$entry.detail)
                $null=$item.SubItems.Add([string]$entry.title);$item.Tag=$entry;$item.ToolTipText=$entry.title+' · '+$entry.detail
                $null=$attentionList.Items.Add($item);if($entry.id -eq $selected){$item.Selected=$true}
            }
            if(-not $attentionList.SelectedItems.Count -and $attentionList.Items.Count){$attentionList.Items[0].Selected=$true}
            foreach($item in $attentionList.Items){if($item.Tag.id -eq $top){$attentionList.TopItem=$item;break}}
        } finally {$attentionList.EndUpdate()}
    }
    $attentionEmpty.Text=if(-not $alive){'启动服务后显示待处理事项。'}elseif($data.message){[string]$data.message}elseif($null -eq $script:attentionData){'后台尚未提供待处理清单，重启服务后可查看。'}else{'暂无待处理事项。需要确认或核对时会显示在这里。'}
    Update-AttentionDisplay
}
