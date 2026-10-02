# BlackFox AI Workstation — запуск хаба кластера.
# Сервер стартует отдельным процессом (переживает закрытие этого окна),
# лог пишется в data\hub.log, интерфейс открывается по нужной схеме (https/http).
$ErrorActionPreference = "Stop"
$Dir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Port = 8765
$Log  = Join-Path $Dir "data\hub.log"

$py = @(
  "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
  (Get-Command python.exe -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $py) { Write-Host "Python не найден." -ForegroundColor Red; exit 1 }

$alive = $false
try { $alive = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) }
catch { $alive = [bool]((netstat -ano) -match ":$Port\s.*LISTENING") }

if ($alive) {
  Write-Host "[*] Хаб уже работает на порту $Port."
} else {
  Write-Host "[1/2] Запуск сервера кластера..."
  $si = ([wmiclass]"Win32_ProcessStartup").CreateInstance(); $si.ShowWindow = 0
  $cmd = "cmd.exe /c `"`"$py`" -u `"$Dir\hub_server.py`" $Port >> `"$Log`" 2>&1`""
  $r = ([wmiclass]"Win32_Process").Create($cmd, $Dir, $si)
  if ($r.ReturnValue -ne 0) { Write-Host "Не удалось запустить (код $($r.ReturnValue))." -ForegroundColor Red; exit 1 }
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 400
    try { if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { $alive = $true; break } } catch {}
  }
  if (-not $alive) {
    Write-Host "Сервер не поднялся. Последние строки лога:" -ForegroundColor Red
    if (Test-Path $Log) { Get-Content $Log -Tail 25 }
    exit 1
  }
}

# Адрес берётся из настроек: сертификат выписан на имя хаба, поэтому открывать
# нужно именно по нему — по 127.0.0.1 браузер пожалуется на несовпадение имени.
$scheme = "http"; $host_ = "127.0.0.1"
try {
  $cfg = Get-Content (Join-Path $Dir "data\config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($cfg.settings.security.tls.enabled) { $scheme = "https" }
  $tunnel = $cfg.settings.connection.tunnel_host
  if ($scheme -eq "https" -and $tunnel) { $host_ = $tunnel }
} catch {}

$url = "${scheme}://${host_}:$Port"
Write-Host "[2/2] Интерфейс: $url"
$ff = "C:\Program Files\Mozilla Firefox\firefox.exe"
if (Test-Path $ff) { Start-Process $ff -ArgumentList "-new-window", $url } else { Start-Process $url }
