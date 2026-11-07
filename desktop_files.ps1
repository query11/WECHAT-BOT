# Only explicit document-opening actions use the shell association.
function Test-DesktopDocument($path) {
 if(-not ($path -is [string]) -or $path -notmatch '^[A-Za-z]:[\\/]' -or $path.Contains('"') -or -not [IO.File]::Exists($path)){return $false}
 return [IO.Path]::GetExtension($path).ToLowerInvariant() -in @('.txt','.md','.pdf','.csv','.json','.png','.jpg','.jpeg','.bmp','.webp','.gif','.docx','.xlsx','.pptx','.doc','.xls','.ppt','.rtf','.odt','.ods','.odp')
}
function Open-DesktopDocument($path) {
 if(-not (Test-DesktopDocument $path)){throw 'Choose an existing document, spreadsheet, image or PDF.'}
 $info=New-Object Diagnostics.ProcessStartInfo;$info.FileName=$path;$info.UseShellExecute=$true
 $process=[Diagnostics.Process]::Start($info);if($process){$process.Dispose()}
}
function Show-DesktopDocumentFolder($path) {
 if(-not ($path -is [string]) -or $path -notmatch '^[A-Za-z]:[\\/]' -or $path.Contains('"') -or -not [IO.File]::Exists($path)){throw 'The local file no longer exists.'}
 $process=Start-Process explorer.exe -ArgumentList ('/select,"'+$path+'"') -PassThru
 if($process){$process.Dispose()}
}
