# Ярлык «BlackFox Hub» на рабочем столе: запускает хаб (если не запущен) и открывает интерфейс.
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# --- .ico для ярлыка (ярлыки Windows не принимают png) -----------------------
$ico = Join-Path $Dir "web\icons\blackfox.ico"
$src = Join-Path $Dir "web\icons\icon-512.png"
if (-not (Test-Path $src)) { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir "make_icons.ps1") | Out-Null }
$sizes = 16, 32, 48, 64, 128, 256
$pngs = @()
$big = [System.Drawing.Image]::FromFile($src)
foreach ($s in $sizes) {
  $bmp = New-Object System.Drawing.Bitmap $s, $s
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.DrawImage($big, 0, 0, $s, $s); $g.Dispose()
  $ms = New-Object System.IO.MemoryStream
  $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
  $pngs += ,@{ size = $s; bytes = $ms.ToArray() }
}
$big.Dispose()
# ICO с PNG-кадрами: заголовок + каталог + данные
$fs = [System.IO.File]::Create($ico)
$bw = New-Object System.IO.BinaryWriter $fs
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$pngs.Count)
$offset = 6 + 16 * $pngs.Count
foreach ($p in $pngs) {
  $dim = if ($p.size -ge 256) { 0 } else { $p.size }
  $bw.Write([byte]$dim); $bw.Write([byte]$dim); $bw.Write([byte]0); $bw.Write([byte]0)
  $bw.Write([uint16]1); $bw.Write([uint16]32)
  $bw.Write([uint32]$p.bytes.Length); $bw.Write([uint32]$offset)
  $offset += $p.bytes.Length
}
foreach ($p in $pngs) { $bw.Write($p.bytes) }
$bw.Close(); $fs.Close()

# --- ярлык на рабочем столе ------------------------------------------------
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "BlackFox Hub.lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$sc.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$Dir\launch_hub.ps1`""
$sc.WorkingDirectory = $Dir
$sc.IconLocation = "$ico,0"
$sc.Description = "BlackFox AI Workstation — запустить хаб кластера и открыть интерфейс"
$sc.WindowStyle = 7
$sc.Save()
Write-Host "Ярлык создан: $lnk"
Write-Host "Иконка:       $ico"
