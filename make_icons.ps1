# Иконки BlackFox AI Workstation для вкладки браузера и установки на телефон.
# Рисуются System.Drawing (есть в Windows), результат — web/icons/*.png.
Add-Type -AssemblyName System.Drawing

$dir = Join-Path $PSScriptRoot "web\icons"
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

function New-Icon($size, $file, [double]$pad = 0.0) {
  $bmp = New-Object System.Drawing.Bitmap $size, $size
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

  # maskable-вариант: фон на всю площадь, рисунок внутри безопасной зоны
  $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 7, 10, 16))
  $g.FillRectangle($bg, 0, 0, $size, $size)

  $inset = [int]($size * $pad)
  $side = $size - 2 * $inset
  $rect = New-Object System.Drawing.Rectangle $inset, $inset, $side, $side
  $c1 = [System.Drawing.Color]::FromArgb(255, 59, 130, 246)
  $c2 = [System.Drawing.Color]::FromArgb(255, 29, 78, 216)
  $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush $rect, $c1, $c2, 50.0

  $radius = [int]($side * 0.24)
  $d = $radius * 2
  $path = New-Object System.Drawing.Drawing2D.GraphicsPath
  $path.AddArc($inset, $inset, $d, $d, 180, 90)
  $path.AddArc($inset + $side - $d, $inset, $d, $d, 270, 90)
  $path.AddArc($inset + $side - $d, $inset + $side - $d, $d, $d, 0, 90)
  $path.AddArc($inset, $inset + $side - $d, $d, $d, 90, 90)
  $path.CloseFigure()
  $g.FillPath($brush, $path)

  $font = New-Object System.Drawing.Font ([System.Drawing.FontFamily]::GenericSansSerif, [float]($side * 0.42), [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
  $white = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
  $fmt = New-Object System.Drawing.StringFormat
  $fmt.Alignment = [System.Drawing.StringAlignment]::Center
  $fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
  $g.DrawString("BF", $font, $white, (New-Object System.Drawing.RectangleF($inset, $inset, $side, $side)), $fmt)

  $out = Join-Path $dir $file
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
  Write-Host ("  {0}  {1}x{1}" -f $file, $size)
}

New-Icon 32  "icon-32.png"
New-Icon 48  "icon-48.png"
New-Icon 180 "icon-180.png"          # apple-touch-icon
New-Icon 192 "icon-192.png"
New-Icon 512 "icon-512.png"
New-Icon 192 "icon-192-maskable.png" 0.16
New-Icon 512 "icon-512-maskable.png" 0.16
Write-Host "Готово: $dir"
