<#
  Автозапуск хаба BlackFox AI Workstation при входе в систему.

    .\autostart_hub.ps1 -Install   — включить (задача в планировщике, окна нет)
    .\autostart_hub.ps1 -Remove    — выключить
    .\autostart_hub.ps1 -Status    — показать состояние

  Задача выполняется только когда вы вошли в систему, права администратора
  и пароль учётной записи не требуются.
#>
param([switch]$Install, [switch]$Remove, [switch]$Status)

$ErrorActionPreference = "Stop"
$Dir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Name = "BlackFox AI Workstation Hub"
$Port = 8765

function Get-Py {
  @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    (Get-Command python.exe -ErrorAction SilentlyContinue).Source) |
    Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

if ($Install) {
  $py = Get-Py
  if (-not $py) { throw "Python не найден." }
  $log = Join-Path $Dir "data\hub.log"
  $arg = "/c `"`"$py`" -u `"$Dir\hub_server.py`" $Port >> `"$log`" 2>&1`""
  $action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arg -WorkingDirectory $Dir
  $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  Write-Host "[+] Автозапуск включён: «$Name» стартует при входе в систему." -ForegroundColor Green
  Write-Host "    Отключить:  .\autostart_hub.ps1 -Remove"
}
elseif ($Remove) {
  if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Host "[-] Автозапуск отключён, задача удалена." -ForegroundColor Yellow
  } else { Write-Host "[*] Автозапуск и так не настроен." }
}
else {
  $t = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  if ($t) { Write-Host "[*] Автозапуск: включён (состояние задачи — $($t.State))." }
  else    { Write-Host "[*] Автозапуск: выключен. Включить: .\autostart_hub.ps1 -Install" }
  $live = $false
  try { $live = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) } catch {}
  Write-Host ("[*] Хаб сейчас: " + $(if ($live) { "работает на порту $Port" } else { "не запущен" }))
}
