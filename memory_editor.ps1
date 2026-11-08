param([switch]$Verify, [switch]$Preview)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'tray_theme.cs') -ReferencedAssemblies System.Windows.Forms,System.Drawing
[TrayDesign]::EnableDpi()
[Windows.Forms.Application]::EnableVisualStyles()
Add-Type 'public static class MemoryAtomic { public static void Replace(string source, string target) { System.IO.File.Replace(source, target, null); } }'
$projectDir=$PSScriptRoot
if($Verify){
    $memoryPath=Join-Path $projectDir ('.runtime\memory-editor-test-'+[Guid]::NewGuid().ToString('N')+'.md')
    [IO.File]::WriteAllText($memoryPath,'test memory',(New-Object Text.UTF8Encoding($false)))
}elseif($Preview){$memoryPath=Join-Path $projectDir '.runtime\memory-preview-sample.md'}else{
    try {
        $config=Get-Content -LiteralPath (Join-Path $projectDir 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if(-not $config.bot_memory_file){throw '尚未配置 bot_memory_file。'}
        $memoryPath=if([IO.Path]::IsPathRooted($config.bot_memory_file)){$config.bot_memory_file}else{Join-Path $projectDir $config.bot_memory_file}
    }catch{[Windows.Forms.MessageBox]::Show('无法打开固定记忆，请检查配置文件。','固定记忆')|Out-Null;exit 1}
}
function Read-Memory {
    if(Test-Path -LiteralPath $memoryPath){return [IO.File]::ReadAllText($memoryPath,[Text.Encoding]::UTF8)}
    return ''
}
$script:original=if($Preview){"- 常用工作目录：D:\工作资料`n- 默认输出目录：D:\工作资料\输出`n- 回复偏好：先说明结果，再简短解释。"}else{Read-Memory}
$window=New-Object Windows.Forms.Form
$window.AutoScaleMode='Dpi'
$window.Text='微信助手 · 固定记忆';$window.ClientSize=New-Object Drawing.Size(760,600)
$appIcon=Join-Path $projectDir 'wechat-assistant-v2.ico'
if(Test-Path -LiteralPath $appIcon){$window.Icon=New-Object Drawing.Icon($appIcon)}
$window.MinimumSize=New-Object Drawing.Size(650,540);$window.StartPosition='CenterScreen'
$window.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$window.BackColor=[TrayDesign]::PageColor;$window.ForeColor=[TrayDesign]::Heading
$title=New-Object Windows.Forms.Label
$title.Text='固定记忆';$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16,[Drawing.FontStyle]::Bold)
$title.SetBounds(28,18,600,40);$title.BackColor=[Drawing.Color]::Transparent;$window.Controls.Add($title)
$tip=New-Object Windows.Forms.Label
$tip.Text='每次处理任务都会读取。可记录桌面、常用工作目录、输出目录和偏好；不要填写密码或密钥。'
$tip.SetBounds(28,58,704,44);$tip.Anchor='Top,Left,Right';$tip.ForeColor=[TrayDesign]::MutedColor;$tip.BackColor=[Drawing.Color]::Transparent;$window.Controls.Add($tip)
# One card holds the directory toolbar and the editor; status and save sit below it.
$card=New-Object TrayCard;$card.SetBounds(24,108,712,420);$card.Anchor='Top,Bottom,Left,Right';$window.Controls.Add($card)
$kind=New-Object Windows.Forms.ComboBox
$kind.DropDownStyle='DropDownList';$kind.Items.AddRange(@('桌面位置','常用工作目录','默认输出目录'))
$kind.SelectedIndex=1;$kind.SetBounds(16,15,170,30);$card.Controls.Add($kind)
$add=New-Object TrayButton;$add.Text='选择目录并添加';$add.IconKind='folder';$add.SetBounds(196,12,180,36);$card.Controls.Add($add)
$clear=New-Object TrayButton;$clear.Text='清空内容';$clear.Danger=$true;$clear.SetBounds(576,12,120,36);$clear.Anchor='Top,Right';$card.Controls.Add($clear)
$editor=New-Object Windows.Forms.TextBox
$editor.Multiline=$true;$editor.AcceptsReturn=$true;$editor.AcceptsTab=$true;$editor.ScrollBars='Vertical'
$editor.SetBounds(16,60,680,344);$editor.Anchor='Top,Bottom,Left,Right';$editor.Text=$script:original.Replace("`r`n","`n").Replace("`n","`r`n")
$script:initialEditor=$editor.Text
$card.Controls.Add($editor)
$status=New-Object Windows.Forms.Label;$status.SetBounds(28,546,560,36);$status.Anchor='Bottom,Left,Right';$status.TextAlign='MiddleLeft';$status.ForeColor=[TrayDesign]::MutedColor;$window.Controls.Add($status)
$save=New-Object TrayButton;$save.Text='保存记忆';$save.Primary=$true;$save.SetBounds(600,546,136,36);$save.Anchor='Bottom,Right';$save.Enabled=$false;$window.Controls.Add($save)
$editor.Add_TextChanged({$save.Enabled=($editor.Text -cne $script:initialEditor);if($save.Enabled){$status.Text='有未保存的修改 · Ctrl+S 保存';$status.ForeColor=[TrayDesign]::Blue}else{$status.Text='暂无未保存的修改';$status.ForeColor=[TrayDesign]::MutedColor}})
[TrayDesign]::StyleFields($window);$null=[TrayDesign]::Frame($editor);[TrayDesign]::SaveShortcut($window,$save)
function Save-Memory {
    if($editor.Text.Length -gt 12000){$status.Text='内容超过 12000 字符，请精简后保存。';return $false}
    if((Read-Memory) -cne $script:original){$status.Text='文件已被其他程序修改。请先保留当前草稿，再重新打开，避免覆盖。';return $false}
    $temp=$memoryPath+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    try{
        $content=$editor.Text.Replace("`r`n","`n")
        [IO.File]::WriteAllText($temp,$content,(New-Object Text.UTF8Encoding($false)))
        if(Test-Path -LiteralPath $memoryPath){[MemoryAtomic]::Replace($temp,$memoryPath)}else{[IO.File]::Move($temp,$memoryPath)}
        $script:original=$content;$script:initialEditor=$editor.Text;$save.Enabled=$false;$status.ForeColor=[TrayDesign]::SuccessText;$status.Text='已保存。下一次处理任务生效，当前正在执行的任务不受影响。'
        return $true
    }catch{if($Verify){throw};$status.Text='保存失败，原记忆未被替换。请检查路径和文件权限。';return $false}
    finally{if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp}}
}
$save.Add_Click({$null=Save-Memory})
$clear.Add_Click({if([Windows.Forms.MessageBox]::Show($window,'清空后点击保存才会删除固定记忆内容，确定清空编辑区吗？','清空记忆','YesNo','Question') -eq 'Yes'){$editor.Clear();$status.Text='已清空编辑区，尚未保存。'}})
$add.Add_Click({
    $dialog=New-Object Windows.Forms.FolderBrowserDialog;$dialog.Description='选择要记住的目录'
    if($dialog.ShowDialog($window) -eq 'OK'){$editor.AppendText("`r`n- "+$kind.SelectedItem+'：'+$dialog.SelectedPath+"`r`n")}
    $dialog.Dispose()
})
$window.Add_FormClosing({param($sender,$e)
    if(-not $Verify -and $editor.Text -cne $script:initialEditor){
        if([Windows.Forms.MessageBox]::Show($window,'有尚未保存的内容，确定放弃并关闭吗？','未保存的记忆','YesNo','Question') -ne 'Yes'){$e.Cancel=$true}
    }
})
if($Verify){
    try{
        $editor.Text='updated memory';if(-not (Save-Memory)){throw $status.Text}
        if((Read-Memory) -cne 'updated memory'){throw 'Saved content mismatch'}
        [IO.File]::WriteAllText($memoryPath,'external update');$editor.Text='conflicting edit'
        if(Save-Memory){throw 'Conflict was not blocked'}
        $script:original=Read-Memory;$editor.Clear();if(-not (Save-Memory)){throw $status.Text}
        if((Read-Memory) -ne ''){throw 'Clear failed'}
        Write-Output 'PASS memory editor: save, external-change conflict, clear'
    }finally{$window.Dispose();Remove-Item -LiteralPath $memoryPath}
}else{
    $window.AutoScaleDimensions=New-Object Drawing.SizeF(96,96);$window.PerformAutoScale()
    if($Preview){
        $window.Show();[Windows.Forms.Application]::DoEvents()
        $image=New-Object Drawing.Bitmap($window.Width,$window.Height)
        $window.DrawToBitmap($image,(New-Object Drawing.Rectangle(0,0,$window.Width,$window.Height)))
        $image.Save((Join-Path $projectDir '.runtime/memory-editor-preview.png'));$image.Dispose();$window.Dispose()
    }else{[Windows.Forms.Application]::Run($window);$window.Dispose()}
}
