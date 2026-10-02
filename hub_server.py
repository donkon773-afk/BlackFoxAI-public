"""
BlackFox AI Workstation — hub server (v3).

Serves the web UI and orchestrates a cluster of OpenAI-compatible LLM nodes
(LM Studio / llama.cpp / anything with /v1/chat/completions).  Stdlib only.

  * chat threads with owners, multi-turn history, per-thread execution plan
  * execution plans: single node, all nodes, pipeline, hybrid (stages of
    parallel nodes run sequentially, results flow to the next stage)
  * skills: fast / deep reasoning / web search / fact check / code / custom
  * "no roles" mode and an always-on "answer in the user's language" rule
  * streaming responses, cancellation
  * per-node telemetry (host GPUs via nvidia-smi, remote via node_agent.py)
  * connected clients with device details (browser + Tailscale peer info)
  * admin (hub machine): node system/network/location info, whitelisted
    management commands (incl. natural-language -> command via an LLM),
    model catalog, HuggingFace search and downloads to nodes
  * node add/probe with hardware-based recommendations

Run:  python hub_server.py [port]
"""

import base64
import hashlib
import codecs
import collections
import concurrent.futures
import copy
import html
import http.server
import json
import os
import platform
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

# Вывод всегда в UTF-8: при перенаправлении в файл или в консоли с cp866/cp1252
# кириллица в сообщениях иначе роняла сервер (UnicodeEncodeError) ещё до запуска.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import hub_files
import node_agent as agentlib  # shared: telemetry sampling, sysinfo, downloads, management actions, request signing
from security_store import SecurityStore, new_token

# Приложение (blackfox_core) хранит данные в профиле пользователя и подсовывает
# папки через окружение; при обычном запуске — рядом со скриптом, как раньше.
WEB_DIR = Path(os.environ.get("BLACKFOX_WEB") or (BASE_DIR / "web"))
DATA_DIR = Path(os.environ.get("BLACKFOX_DATA") or (BASE_DIR / "data"))
CONFIG_FILE = DATA_DIR / "config.json"
THREADS_FILE = DATA_DIR / "threads.json"
DATA_DIR.mkdir(exist_ok=True)

IS_WINDOWS = platform.system() == "Windows"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def now_hms():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_NODES = {
    # Узлы-заглушки: замените адрес, модель и роль в Настройки → Узлы или добавьте свои.
    "node_1": {
        "name": "Узел 1", "tag": "NODE-1", "role": "",
        "hardware": "", "location": "Заглушка: локальный LLM-сервер",
        "endpoint": "http://127.0.0.1:1234/v1/chat/completions",
        "model": "", "enabled": True, "color": "#818cf8", "avatar": "🖥️",
        "telemetry": {"type": "auto"},
        "system_prompt": "",
    },
    "node_2": {
        "name": "Узел 2", "tag": "NODE-2", "role": "",
        "hardware": "", "location": "Заглушка: второй LLM-сервер",
        "endpoint": "http://127.0.0.1:1235/v1/chat/completions",
        "model": "", "enabled": True, "color": "#38bdf8", "avatar": "🖥️",
        "telemetry": {"type": "auto"},
        "system_prompt": "",
    },
}

NODE_ORDER = ["node_1", "node_2"]

BUILTIN_SKILLS = [
    {"id": "default", "icon": "💬", "name": "Обычный", "desc": "Параметры по умолчанию, роль узла как есть.", "builtin": True,
     "params": {}, "addendum": "", "web": False},
    {"id": "fast", "icon": "⚡", "name": "Быстрый ответ", "desc": "Короткий ответ по делу, минимум токенов, без рассуждений.", "builtin": True,
     "params": {"temperature": 0.2, "max_tokens": 700, "reasoning_effort": "none"},
     "addendum": "Отвечай кратко и по существу, без вступлений и повторов вопроса. Не более нескольких абзацев или короткого списка.", "web": False},
    {"id": "deep", "icon": "🧠", "name": "Глубокое рассуждение", "desc": "Пошаговый разбор, проверка альтернатив, самопроверка. Дольше и дороже.", "builtin": True,
     "params": {"temperature": 0.5, "max_tokens": 6000, "reasoning_effort": "high"},
     "addendum": "Рассуждай пошагово: сначала кратко переформулируй задачу и перечисли допущения, рассмотри 2–3 альтернативных подхода, выбери лучший и обоснуй. В конце дай итоговый ответ и отдельно проверь его на ошибки и упущения.", "web": False},
    {"id": "web", "icon": "🌐", "name": "Поиск в интернете", "desc": "Хаб ищет в интернете, узел отвечает по найденным источникам со ссылками.", "builtin": True,
     "params": {"temperature": 0.3}, "web": True, "web_results": 5, "web_pages": 3,
     "addendum": "Тебе переданы результаты поиска в интернете. Опирайся на них, ссылайся на источники в тексте в формате [номер]. Если источники противоречат друг другу или данных недостаточно — прямо скажи об этом. Не выдумывай ссылки."},
    {"id": "verify", "icon": "✅", "name": "Сверка фактов", "desc": "Проверить утверждения запроса по интернет-источникам.", "builtin": True,
     "params": {"temperature": 0.2}, "web": True, "web_results": 6, "web_pages": 4,
     "addendum": "Проверь каждое утверждение из сообщения пользователя по переданным источникам. Для каждого укажи: подтверждается / опровергается / не удалось проверить, с цитатой и ссылкой [номер]. В конце — краткий итог."},
    {"id": "code", "icon": "💻", "name": "Код", "desc": "Максимум кода, минимум слов; низкая температура.", "builtin": True,
     "params": {"temperature": 0.1, "max_tokens": 4096},
     "addendum": "Отвечай преимущественно кодом: полным, запускаемым, с комментариями в коде. Пояснения — только необходимые, после кода.", "web": False},
    {"id": "critic", "icon": "🧐", "name": "Критик", "desc": "Искать ошибки, слабые места и риски; не хвалить.", "builtin": True,
     "params": {"temperature": 0.3},
     "addendum": "Твоя задача — критика. Найди ошибки, слабые места, риски, недостающие проверки и спорные решения. Не хвали и не пересказывай; каждое замечание — конкретно, с тем, как исправить.", "web": False},
    {"id": "brief", "icon": "📝", "name": "Резюме", "desc": "Сжать до ключевых тезисов.", "builtin": True,
     "params": {"temperature": 0.2, "max_tokens": 800},
     "addendum": "Сожми ответ до ключевых тезисов в виде короткого списка. Без воды.", "web": False},
]

DEFAULT_SETTINGS = {
    "llm": {
        "max_tokens": 2048, "temperature": 0.3, "top_p": 0.95, "reasoning_effort": "none",
        "timeout_s": 180, "stream": True, "history_turns": 8, "pipeline_context_chars": 1500,
        "force_language": True,
    },
    "telemetry": {
        "interval_s": 1.5, "agent_port": 8766, "agent_token": "", "llm_ping_interval_s": 10,
        "speedtest_interval_s": 30, "speedtest_bytes": 2000000, "history_len": 60,
    },
    "connection": {"tunnel_host": "", "port": 8765},
    "admin": {"enabled": True, "extra_ips": [], "command_node": ""},
    "security": {
        "enabled": False,            # требовать вход по паролю
        "restrict_network": True,    # пускать только из приватных сетей (Tailscale/LAN/localhost)
        "allow_cidrs": [],           # дополнительные разрешённые сети
        "bind": "0.0.0.0",           # 0.0.0.0 | tailscale | 127.0.0.1
        "session_hours": 72,
        "lockout_fails": 8,          # неудачных попыток до блокировки IP
        "lockout_minutes": 15,
        "sign_agents": True,         # подписывать команды узлам (HMAC)
        "tls": {"enabled": False, "cert": "", "key": ""},
    },
    "council": {"rounds": 1, "draft_chars": 3500, "critique_chars": 1500, "critique_max_tokens": 700, "moderator": ""},
    # приложенные файлы: лимиты текста, который уходит в промпт
    "files": {"max_mb": 25, "per_file_chars": 40000, "total_chars": 120000, "history_chars": 6000},
    # «Агент ПК»: выполнение команд LLM на узлах (только администратор, только узлы с exec_enabled)
    "tools": {"approve": "confirm", "max_steps": 8, "timeout_s": 120, "max_output_chars": 12000, "history_chars": 3000},
    "pipeline": [
        {"node": "node_1", "label": "Шаг 1: Черновик", "prompt": "Подготовь ответ на задачу."},
        {"node": "node_2", "label": "Шаг 2: Проверка", "prompt": "Проверь ответ предыдущего шага, исправь ошибки и дай итоговый ответ."},
    ],
    "skills": copy.deepcopy(BUILTIN_SKILLS),
}

# Role library used by the "add node" recommendations
ROLE_LIBRARY = [
    {"id": "coder", "name": "Основной разработчик", "tag": "DEV", "avatar": "💻", "min_class": 2,
     "prompt": "Ты — старший инженер-разработчик. Пишешь чистый, производительный и безопасный код с обработкой ошибок и комментариями, проектируешь API и структуры данных. Всегда даёшь готовый к запуску код."},
    {"id": "architect", "name": "Архитектор / аналитик", "tag": "ARCH", "avatar": "🏛️", "min_class": 3,
     "prompt": "Ты — системный архитектор. Разбираешь требования, проектируешь структуру решения, компоненты, интерфейсы, данные и модель безопасности; сравниваешь альтернативы и обосновываешь выбор. Отвечаешь структурированно."},
    {"id": "reviewer", "name": "Ревьюер / критик", "tag": "REVIEW", "avatar": "🧐", "min_class": 1,
     "prompt": "Ты — строгий ревьюер. Находишь ошибки, слабые места, риски, неполные проверки и предлагаешь конкретные исправления. Не хвалишь и не пересказываешь."},
    {"id": "auditor", "name": "Быстрый аудитор / линтер", "tag": "AUDIT", "avatar": "🛡️", "min_class": 0,
     "prompt": "Ты — быстрый аудитор. Проверяешь текст или код по чек-листу (ошибки, уязвимости, утечки секретов, несоответствие требованиям) и выдаёшь короткий вердикт списком."},
    {"id": "redteam", "name": "Red Team / тестировщик", "tag": "TEST", "avatar": "🎯", "min_class": 1,
     "prompt": "Ты — тестировщик и специалист по атакам. Ищешь способы сломать решение: граничные случаи, гонки, инъекции, обходы; пишешь тесты и PoC-проверки."},
    {"id": "researcher", "name": "Исследователь (с поиском)", "tag": "RESEARCH", "avatar": "🌐", "min_class": 1,
     "prompt": "Ты — исследователь. Собираешь и сопоставляешь факты, отделяешь проверенное от предположений, ссылаешься на источники, честно отмечаешь неопределённость."},
    {"id": "writer", "name": "Редактор / документация", "tag": "DOCS", "avatar": "📝", "min_class": 0,
     "prompt": "Ты — технический писатель и редактор. Пишешь ясные документы, README, инструкции и резюме; структурируешь текст, убираешь лишнее, сохраняешь точность."},
    {"id": "translator", "name": "Переводчик", "tag": "LANG", "avatar": "🈯", "min_class": 0,
     "prompt": "Ты — профессиональный переводчик. Переводишь точно, сохраняя смысл, термины и форматирование; при неоднозначности даёшь варианты."},
    {"id": "router", "name": "Классификатор / маршрутизатор", "tag": "ROUTER", "avatar": "🔀", "min_class": 0,
     "prompt": "Ты — быстрый классификатор. Определяешь тип запроса, извлекаешь ключевые сущности и отвечаешь в строго заданном коротком формате."},
    {"id": "generalist", "name": "Универсальный ассистент", "tag": "GEN", "avatar": "🤖", "min_class": 1,
     "prompt": "Ты — полезный, точный и честный ассистент. Отвечаешь по существу, структурированно, признаёшь границы своих знаний."},
]

GPU_BANDWIDTH = [  # (substring, GB/s) — longest / most specific first
    ("5090", 1792), ("5080", 960), ("5070 ti", 896), ("5070", 672), ("5060 ti", 448), ("5060", 448),
    ("4090", 1008), ("4080 super", 736), ("4080", 717), ("4070 ti super", 672), ("4070 ti", 504), ("4070 super", 504), ("4070", 504),
    ("4060 ti", 288), ("4060", 272), ("3090", 936), ("3080 ti", 912), ("3080", 760), ("3070 ti", 608), ("3070", 448),
    ("3060 ti", 448), ("3060", 360), ("3050", 224), ("2080 ti", 616), ("2080", 448), ("2070", 448), ("2060", 336),
    ("1080 ti", 484), ("1080", 320), ("1070", 256), ("1060", 192), ("1660", 192), ("1650", 128),
    ("a100", 1555), ("h100", 3350), ("a6000", 768), ("a4000", 448), ("l40", 864),
    ("m4 max", 546), ("m4 pro", 273), ("m4", 120), ("m3 ultra", 800), ("m3 max", 400), ("m3 pro", 150), ("m3", 100),
    ("m2 ultra", 800), ("m2 max", 400), ("m2 pro", 200), ("m2", 100), ("m1 ultra", 800), ("m1 max", 400), ("m1 pro", 200), ("m1", 68),
    ("apple", 150), ("radeon rx 7900", 800), ("rx 7800", 624), ("rx 7600", 288), ("rx 6800", 512), ("rx 6700", 384), ("rx 6600", 256),
]

QUANT_BPW = [("iq4_xs", 4.25), ("q2_k", 3.35), ("q3_k_s", 3.5), ("q3_k_m", 3.9), ("q3_k_l", 4.3), ("q4_k_s", 4.58), ("q4_k_m", 4.85),
             ("q4_0", 4.55), ("q4_1", 5.0), ("q5_k_s", 5.5), ("q5_k_m", 5.7), ("q5_0", 5.5), ("q6_k", 6.6), ("q8_0", 8.5),
             ("mxfp4", 4.25), ("bf16", 16), ("f16", 16), ("f32", 32), ("8bit", 8.5), ("6bit", 6.5), ("4bit", 4.5), ("3bit", 3.5), ("2bit", 2.5)]


# --- One-command agent installers, served by the hub with its own address baked in ---

INSTALL_PS1 = r'''# BlackFox node agent — установка на узел (Windows)
# Запуск:  irm %HUB%/install-agent.ps1 | iex
$Hub = "%HUB%"; $Port = %PORT%; $Token = "%TOKEN%"; $Secret = "%SECRET%"; $Pin = "%PIN%"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Скачиваем встроенным curl.exe: при самоподписанном сертификате он проверяет открытый
# ключ по закреплённому отпечатку (--pinnedpubkey). Это надёжнее колбэка проверки
# сертификата в PowerShell, который в фоновом потоке TLS срабатывает не всегда.
function Get-HubFile($Url, $OutFile) {
  $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
  if ($curl -and $Pin) {
    & $curl -sS -k --pinnedpubkey "sha256//$Pin" -o $OutFile $Url
    if ($LASTEXITCODE -ne 0) { throw "curl не смог скачать $Url (код $LASTEXITCODE); 60/90 = сертификат хаба не совпал с ожидаемым" }
  } elseif ($curl) {
    & $curl -sS -o $OutFile $Url
    if ($LASTEXITCODE -ne 0) { throw "curl не смог скачать $Url (код $LASTEXITCODE)" }
  } else {
    Invoke-WebRequest -UseBasicParsing $Url -OutFile $OutFile
  }
}
$Dir = Join-Path $env:LOCALAPPDATA "BlackFoxAgent"
Write-Host "[1/5] Каталог $Dir"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

Write-Host "[2/5] Загрузка агента с $Hub"
try { Get-HubFile "$Hub/node_agent.py" (Join-Path $Dir "node_agent.py") }
catch { Write-Host "Не удалось скачать агент: $_" -ForegroundColor Red; Write-Host "Проверьте, что хаб доступен: $Hub"; exit 1 }

Write-Host "[3/5] Поиск Python"
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) {
  Write-Host "Python не найден. Установите: winget install -e --id Python.Python.3.12" -ForegroundColor Yellow
  Write-Host "затем повторите эту команду."; exit 1
}
Write-Host "    $py"

Write-Host "[4/5] Автозапуск при входе в систему"
$cmd = "`"$py`" `"$Dir\node_agent.py`" $Port"
if ($Secret) { $cmd += " --secret $Secret" }   # подписанные команды от хаба
elseif ($Token) { $cmd += " --token $Token" }
$vbsCmd = $cmd.Replace('"', '""')
$vbs = Join-Path $Dir "start-agent.vbs"
"Set s = CreateObject(""WScript.Shell"")`r`ns.Run ""$vbsCmd"", 0, False" | Out-File -Encoding ASCII $vbs
$startup = [Environment]::GetFolderPath('Startup')
Copy-Item $vbs (Join-Path $startup "BlackFoxAgent.vbs") -Force
try { New-NetFirewallRule -DisplayName "BlackFox node agent $Port" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow -ErrorAction Stop | Out-Null; Write-Host "    правило брандмауэра добавлено" }
catch { Write-Host "    правило брандмауэра не добавлено (нужен запуск от администратора) — если хаб не видит агент, добавьте порт $Port вручную" -ForegroundColor Yellow }

Write-Host "[5/5] Запуск"
Get-CimInstance Win32_Process -Filter "Name like '%python%'" | Where-Object { $_.CommandLine -like "*node_agent.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Process wscript.exe -ArgumentList "`"$vbs`""
Start-Sleep 2
try {
  $null = Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:$Port/telemetry" -TimeoutSec 2
} catch {
  $pArgs = "`"$Dir\node_agent.py`" $Port"
  if ($Token) { $pArgs += " --token $Token" }
  Start-Process $py -ArgumentList $pArgs -WindowStyle Hidden
  Start-Sleep 2
}
try {
  $r = Invoke-RestMethod "http://127.0.0.1:$Port/telemetry" -TimeoutSec 5
  Write-Host ""
  Write-Host "ГОТОВО. Агент работает: $($r.hostname) / $($r.platform)" -ForegroundColor Green
  Write-Host "GPU: $((($r.gpus | ForEach-Object { $_.name }) -join ', '))"
  $ts = (Get-Command tailscale.exe -ErrorAction SilentlyContinue).Source
  if (-not $ts -and (Test-Path "C:\Program Files\Tailscale\tailscale.exe")) { $ts = "C:\Program Files\Tailscale\tailscale.exe" }
  if ($ts) { $ip = (& $ts ip -4 2>$null | Select-Object -First 1); if ($ip) { Write-Host "Адрес для хаба: http://$ip`:$Port" -ForegroundColor Cyan } }
} catch { Write-Host "Агент не ответил на 127.0.0.1:$Port — проверьте $Dir\node_agent.py" -ForegroundColor Red }
'''

INSTALL_SH = r'''#!/bin/sh
# BlackFox node agent — установка на узел (macOS / Linux)
# Запуск:  curl -fsSL %HUB%/install-agent.sh | sh
HUB="%HUB%"; PORT=%PORT%; TOKEN="%TOKEN%"; SECRET="%SECRET%"; PIN="%PIN%"
CURL="curl -fsSL"
[ -n "$PIN" ] && CURL="curl -fsSL -k --pinnedpubkey sha256//$PIN"   # самоподписанный сертификат: проверяем по ключу
DIR="$HOME/.blackfox-agent"
echo "[1/5] Каталог $DIR"
mkdir -p "$DIR" || exit 1

echo "[2/5] Загрузка агента с $HUB"
$CURL "$HUB/node_agent.py" -o "$DIR/node_agent.py" || { echo "Не удалось скачать агент. Проверьте доступность $HUB"; exit 1; }

echo "[3/5] Поиск Python"
PY=$(command -v python3 || command -v python) || { echo "Python 3 не найден. macOS: xcode-select --install  |  Linux: sudo apt install python3"; exit 1; }
echo "    $PY"

ARGS="$DIR/node_agent.py $PORT"
if [ -n "$SECRET" ]; then ARGS="$ARGS --secret $SECRET"
elif [ -n "$TOKEN" ]; then ARGS="$ARGS --token $TOKEN"; fi

if [ "$(uname)" = "Darwin" ]; then
  echo "[4/5] Автозапуск через launchd"
  PLIST="$HOME/Library/LaunchAgents/com.blackfox.agent.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  ARGV="<string>$PY</string><string>$DIR/node_agent.py</string><string>$PORT</string>"
  if [ -n "$SECRET" ]; then ARGV="$ARGV<string>--secret</string><string>$SECRET</string>"
  elif [ -n "$TOKEN" ]; then ARGV="$ARGV<string>--token</string><string>$TOKEN</string>"; fi
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.blackfox.agent</string>
  <key>ProgramArguments</key><array>$ARGV</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/agent.log</string>
  <key>StandardErrorPath</key><string>$DIR/agent.log</string>
</dict></plist>
PLISTEOF
  pkill -f node_agent.py 2>/dev/null
  launchctl unload -w "$PLIST" 2>/dev/null
  launchctl load -w "$PLIST" 2>/dev/null || { echo "launchctl не смог загрузить сервис, запускаю вручную"; nohup $PY $ARGS >"$DIR/agent.log" 2>&1 & }
else
  echo "[4/5] Автозапуск через systemd --user"
  pkill -f node_agent.py 2>/dev/null
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/blackfox-agent.service" <<UNITEOF
[Unit]
Description=BlackFox node agent
[Service]
ExecStart=$PY $ARGS
Restart=always
[Install]
WantedBy=default.target
UNITEOF
  systemctl --user daemon-reload 2>/dev/null
  systemctl --user enable --now blackfox-agent 2>/dev/null || { echo "systemd недоступен, запускаю вручную"; nohup $PY $ARGS >"$DIR/agent.log" 2>&1 & }
fi

echo "[5/5] Запуск"
sleep 2
if ! curl -fsS "http://127.0.0.1:$PORT/telemetry" >/dev/null 2>&1; then
  nohup $PY $ARGS >"$DIR/agent.log" 2>&1 &
  sleep 2
fi
if curl -fsS "http://127.0.0.1:$PORT/telemetry" >/dev/null 2>&1; then
  echo ""
  echo "ГОТОВО. Агент работает на порту $PORT."
  curl -fsS "http://127.0.0.1:$PORT/telemetry" | head -c 400; echo
  TS=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
  IP=$("$TS" ip -4 2>/dev/null | head -1)
  [ -n "$IP" ] && echo "Адрес для хаба: http://$IP:$PORT"
else
  echo "Агент не ответил на 127.0.0.1:$PORT — последние строки $DIR/agent.log:"
  tail -n 15 "$DIR/agent.log" 2>/dev/null
fi
'''


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ---------------------------------------------------------------------------
# Config store
# ---------------------------------------------------------------------------

class ConfigStore:
    RUNTIME = ("status", "latency_ms", "last_error", "kind")

    def __init__(self):
        self.lock = threading.RLock()
        self.nodes = copy.deepcopy(DEFAULT_NODES)
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.load()
        for cfg in self.nodes.values():
            cfg.setdefault("status", "unknown")
            cfg.setdefault("latency_ms", 0)
            cfg.setdefault("last_error", "")
            cfg.setdefault("kind", "")
            cfg.setdefault("telemetry", {"type": "auto"})

    def load(self):
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"[!] ВНИМАНИЕ: data/config.json не читается ({e}).")
            print("[!] Работаю на настройках по умолчанию; сохранение перезапишет файл.")
            return
        with self.lock:
            saved_nodes = data.get("nodes") or {}
            if saved_nodes:
                merged = {}
                for k, v in saved_nodes.items():
                    merged[k] = deep_merge(DEFAULT_NODES.get(k, DEFAULT_NODES["node_1"]), v)
                    if k not in DEFAULT_NODES:
                        merged[k] = deep_merge({"telemetry": {"type": "auto"}, "system_prompt": "", "color": "#64748b", "avatar": "💻", "enabled": True}, v)
                self.nodes = merged
            s = data.get("settings") or {}
            self.settings = deep_merge(self.settings, s)
            if "pipeline" in s:
                self.settings["pipeline"] = s["pipeline"]
            if "skills" in s and isinstance(s["skills"], list):
                self.settings["skills"] = self._merge_skills(s["skills"])
            self.settings.pop("presets", None)

    def _merge_skills(self, saved):
        """Keep built-ins (allow param edits), append customs."""
        by_id = {s["id"]: s for s in saved}
        out = []
        for b in BUILTIN_SKILLS:
            s = copy.deepcopy(b)
            if b["id"] in by_id:
                for f in ("params", "addendum", "web", "web_results", "web_pages", "name", "desc", "icon", "hidden"):
                    if f in by_id[b["id"]]:
                        s[f] = by_id[b["id"]][f]
            out.append(s)
        for s in saved:
            if s.get("id") and not s.get("builtin") and s["id"] not in {b["id"] for b in BUILTIN_SKILLS}:
                out.append(s)
        return out

    def save(self):
        with self.lock:
            for k in ("agent_secret_set", "admin_password_set", "user_password_set"):
                (self.settings.get("security") or {}).pop(k, None)
            nodes_out = {k: {kk: vv for kk, vv in cfg.items() if kk not in self.RUNTIME} for k, cfg in self.nodes.items()}
            payload = {"nodes": nodes_out, "node_order": list(self.nodes.keys()), "settings": self.settings, "saved_at": now_iso()}
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)

    def node_public(self, key):
        cfg = self.nodes[key]
        return {
            "key": key, "name": cfg["name"], "tag": cfg.get("tag", "NODE"), "role": cfg.get("role", ""),
            "hardware": cfg.get("hardware", ""), "location": cfg.get("location", ""), "endpoint": cfg["endpoint"],
            "model": cfg.get("model", ""), "enabled": cfg.get("enabled", True), "status": cfg.get("status", "unknown"),
            "color": cfg.get("color", "#64748b"), "avatar": cfg.get("avatar", "💻"), "latency_ms": cfg.get("latency_ms", 0),
            "last_error": cfg.get("last_error", ""), "telemetry": cfg.get("telemetry", {"type": "auto"}),
            "telemetry_resolved": resolve_telemetry(cfg), "system_prompt": cfg.get("system_prompt", ""),
            "timeout_s": cfg.get("timeout_s"), "kind": cfg.get("kind", ""), "is_local": is_local_endpoint(cfg["endpoint"]),
            "agent_url": agent_url_for(cfg), "launch": cfg.get("launch") or {},
            "exec_enabled": bool(cfg.get("exec_enabled")), "vision": bool(cfg.get("vision")),
        }

    def nodes_public(self):
        with self.lock:
            keys = [k for k in NODE_ORDER if k in self.nodes] + [k for k in self.nodes if k not in NODE_ORDER]
            return {k: self.node_public(k) for k in keys}

    def update_node(self, key, body):
        editable = ("name", "tag", "role", "hardware", "location", "endpoint", "model", "enabled", "color", "avatar", "system_prompt", "timeout_s",
                    "exec_enabled", "vision")
        with self.lock:
            cfg = self.nodes[key]
            for f in editable:
                if f in body:
                    val = body[f]
                    if isinstance(val, str):
                        val = val.strip()
                    if f in ("enabled", "exec_enabled", "vision"):
                        val = bool(val)
                    if f == "timeout_s":
                        try:
                            val = int(val) if val not in (None, "") else None
                        except Exception:
                            val = None
                    cfg[f] = val
            if "telemetry" in body and isinstance(body["telemetry"], dict):
                t = body["telemetry"]
                cfg["telemetry"] = {"type": t.get("type", "auto") or "auto", "url": (t.get("url") or "").strip(), "gpu_match": (t.get("gpu_match") or "").strip()}
            if "launch" in body and isinstance(body["launch"], dict):
                cfg["launch"] = {k: body["launch"][k] for k in ("model_path", "port", "ctx", "ngl", "device", "alias", "parallel") if k in body["launch"]}
            self.save()

    def add_node(self, body):
        with self.lock:
            base = re.sub(r"[^a-z0-9_]+", "_", (body.get("key") or body.get("name") or "node").lower()).strip("_") or "node"
            key = base
            i = 2
            while key in self.nodes:
                key = f"{base}_{i}"
                i += 1
            cfg = {"name": body.get("name") or key, "tag": body.get("tag") or key.upper()[:10], "role": body.get("role") or "Универсальный ассистент",
                   "hardware": body.get("hardware") or "", "location": body.get("location") or "", "endpoint": (body.get("endpoint") or "").strip(),
                   "model": (body.get("model") or "").strip(), "enabled": True, "color": body.get("color") or "#a78bfa", "avatar": body.get("avatar") or "🖥️",
                   "telemetry": body.get("telemetry") or {"type": "auto"}, "system_prompt": body.get("system_prompt") or ROLE_LIBRARY[-1]["prompt"],
                   "status": "unknown", "latency_ms": 0, "last_error": "", "kind": ""}
            self.nodes[key] = cfg
            self.save()
            return key

    def delete_node(self, key):
        with self.lock:
            if key in self.nodes and len(self.nodes) > 1:
                del self.nodes[key]
                self.settings["pipeline"] = [s for s in self.settings.get("pipeline", []) if s.get("node") != key]
                self.save()
                return True
        return False

    def reset_node_prompt(self, key):
        with self.lock:
            if key in DEFAULT_NODES:
                for f in ("system_prompt", "role", "tag"):
                    self.nodes[key][f] = DEFAULT_NODES[key][f]
                self.save()

    def update_settings(self, body):
        with self.lock:
            for section in ("llm", "telemetry", "connection", "admin", "council", "security"):
                if section in body and isinstance(body[section], dict):
                    self.settings[section] = deep_merge(self.settings[section], body[section])
            if "pipeline" in body and isinstance(body["pipeline"], list):
                self.settings["pipeline"] = [{"node": s.get("node", ""), "label": s.get("label", ""), "prompt": s.get("prompt", "")} for s in body["pipeline"] if s.get("node")]
            if "skills" in body and isinstance(body["skills"], list):
                cleaned = []
                for s in body["skills"]:
                    if not s.get("id"):
                        s["id"] = "s_" + uuid.uuid4().hex[:6]
                    cleaned.append({"id": s["id"], "icon": s.get("icon", "✨"), "name": s.get("name", ""), "desc": s.get("desc", ""),
                                    "builtin": bool(s.get("builtin")), "params": s.get("params") or {}, "addendum": s.get("addendum", ""),
                                    "web": bool(s.get("web")), "web_results": int(s.get("web_results") or 5), "web_pages": int(s.get("web_pages") or 3),
                                    "hidden": bool(s.get("hidden"))})
                self.settings["skills"] = self._merge_skills(cleaned)
            self.save()

    def reset_settings(self, section=None):
        with self.lock:
            if section and section in DEFAULT_SETTINGS:
                self.settings[section] = copy.deepcopy(DEFAULT_SETTINGS[section])
            else:
                self.settings = copy.deepcopy(DEFAULT_SETTINGS)
            self.save()

    def llm(self):
        with self.lock:
            return copy.deepcopy(self.settings["llm"])

    def skill(self, sid):
        with self.lock:
            for s in self.settings.get("skills", []):
                if s["id"] == (sid or "default"):
                    return copy.deepcopy(s)
        return copy.deepcopy(BUILTIN_SKILLS[0])


# ---------------------------------------------------------------------------
# Endpoint / telemetry helpers
# ---------------------------------------------------------------------------

HOST_IPS_CACHE = {"ts": 0, "ips": set()}


def host_ips():
    if time.time() - HOST_IPS_CACHE["ts"] > 120:
        ips = set(agentlib.local_ips())
        try:
            ts = agentlib.tailscale_status()
            for ip in (ts.get("self") or {}).get("ips") or []:
                ips.add(ip)
        except Exception:
            pass
        HOST_IPS_CACHE.update({"ts": time.time(), "ips": ips})
    return HOST_IPS_CACHE["ips"]


def endpoint_host(endpoint):
    try:
        return urllib.parse.urlparse(endpoint).hostname or ""
    except Exception:
        return ""


def is_local_endpoint(endpoint):
    h = endpoint_host(endpoint)
    return h in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or h in host_ips()


def agent_url_for(cfg):
    tel = cfg.get("telemetry") or {}
    if tel.get("type") == "agent" and tel.get("url"):
        return tel["url"].rstrip("/")
    if tel.get("type") == "local":
        return ""
    if is_local_endpoint(cfg["endpoint"]):
        return ""
    port = config.settings["telemetry"].get("agent_port", 8766)
    return f"http://{endpoint_host(cfg['endpoint'])}:{port}"


def resolve_telemetry(cfg):
    """auto -> local (hub machine) or agent (remote host:agent_port)."""
    tel = dict(cfg.get("telemetry") or {"type": "auto"})
    t = tel.get("type", "auto")
    if t == "local":
        return {"type": "local", "gpu_match": tel.get("gpu_match", "")}
    if t == "agent" and tel.get("url"):
        return {"type": "agent", "url": tel["url"].rstrip("/")}
    if t == "none":
        return {"type": "none"}
    if is_local_endpoint(cfg["endpoint"]):
        return {"type": "local", "gpu_match": tel.get("gpu_match", "")}
    return {"type": "agent", "url": agent_url_for(cfg), "auto": True}


config = ConfigStore()
security = SecurityStore(DATA_DIR)


# ---------------------------------------------------------------------------
# Security: network gate, roles, sessions, secret redaction
# ---------------------------------------------------------------------------

AUTH_FREE = {"/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/node/pair"}
# Кроме собственного адреса хаба, браузерные запросы принимаются только от оболочки приложения
# и прокси ядра на этой машине (тот же список, что CORS control API в blackfox_core.py).
APP_ORIGIN_RE = re.compile(r"^(tauri://localhost|https?://tauri\.localhost|https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?)$", re.I)
SECRET_KEYS = {"agent_token"}


def scheme_is_https():
    tls = (config.settings.get("security") or {}).get("tls") or {}
    return bool(tls.get("enabled") and tls.get("cert") and tls.get("key"))


def sec():
    return config.settings.get("security") or {}


def network_allowed(ip):
    """First gate: is this address allowed to talk to the hub at all?"""
    s = sec()
    if not s.get("restrict_network", True):
        return True
    if agentlib._is_private(ip):
        return True
    extra = agentlib.parse_cidrs(s.get("allow_cidrs") or [])
    return bool(extra) and agentlib.ip_in(ip, extra)


def client_role(handler):
    """-> 'admin' | 'user' | None.  With auth off, admin is decided by IP as before."""
    if not sec().get("enabled"):
        return "admin" if is_admin_ip(handler.client_address[0]) else "user"
    token = handler.headers.get("X-BF-Auth") or ""
    sess = security.session(token)
    if not sess:
        return None
    return sess.get("role")


def redact_settings(settings, role):
    """Never hand secrets to a non-admin client."""
    out = copy.deepcopy(settings)
    if role != "admin":
        tel = out.get("telemetry") or {}
        for k in SECRET_KEYS:
            if tel.get(k):
                tel[k] = "***"
        out.pop("admin", None)
        s = out.get("security") or {}
        s.pop("allow_cidrs", None)
    else:
        tel = out.get("telemetry") or {}
        if tel.get("agent_token"):
            tel["agent_token"] = "***"   # even admins read it only via the security API
    sc = out.setdefault("security", {})
    if role == "admin" and scheme_is_https():
        pn = cert_pins()
        sc["tls_pins"] = {"sha256": pn.get("sha256", ""), "spki": pn.get("spki", ""), "self_signed": pn.get("self_signed", False)}
    sc["agent_secret_set"] = bool(security.agent_secret())
    sc["admin_password_set"] = security.has_password("admin")
    sc["user_password_set"] = security.has_password("user")
    return out


def agent_auth_headers(method, path, body=b""):
    """Signed (or legacy token) headers for a hub -> agent control request."""
    secret = security.agent_secret()
    if secret and sec().get("sign_agents", True):
        return agentlib.sign_headers(secret, method, path, body)
    tok = config.settings["telemetry"].get("agent_token") or ""
    return {"X-BF-Token": tok} if tok else {}



def node_base_url(endpoint):
    m = re.match(r"^(https?://[^/]+)", endpoint.strip())
    return m.group(1) if m else endpoint.rstrip("/")


def tcp_reachable(url, timeout=3.0):
    try:
        p = urllib.parse.urlparse(url)
        host, port = p.hostname, p.port or (443 if p.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except Exception as e:
        return str(e) or e.__class__.__name__


def http_get_json(url, timeout=3.0, headers=None):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "BlackFoxHub/3"} | (headers or {}))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url, body, timeout=10.0, headers=None, raw=None):
    req = urllib.request.Request(url, data=(raw if raw is not None else json.dumps(body).encode("utf-8")), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "BlackFoxHub/3"} | (headers or {}))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def agent_headers(method="GET", path="/", body=b""):
    return agent_auth_headers(method, path, body)


def ping_node(node_key):
    cfg = config.nodes.get(node_key)
    if not cfg:
        return {"status": "error", "error": "Unknown node"}
    url = node_base_url(cfg["endpoint"]) + "/v1/models"
    t0 = time.time()
    try:
        http_get_json(url, timeout=5.0)
        lat = max(1, int((time.time() - t0) * 1000))
        cfg.update(status="online", latency_ms=lat, last_error="")
        return {"status": "online", "latency_ms": lat}
    except Exception as e:
        cfg.update(status="offline", last_error=str(e)[:200])
        return {"status": "offline", "error": str(e)}


_models_cache = {}
_models_cache_lock = threading.Lock()


def probe_models(endpoint):
    """Returns (models list, kind, error) for any OpenAI-compatible endpoint."""
    base = node_base_url(endpoint)
    models, kind = {}, "openai"
    data = http_get_json(base + "/v1/models", timeout=5.0)
    for m in data.get("data", []):
        mid = m.get("id")
        if not mid:
            continue
        entry = {"id": mid, "loaded": None, "type": "", "quant": "", "ctx": None, "arch": "", "size_bytes": None, "params_b": None}
        meta = m.get("meta") or {}
        if meta:
            kind = "llamacpp"
            entry.update(ctx=meta.get("n_ctx"), quant=meta.get("ftype") or "", loaded=True, size_bytes=meta.get("size"),
                         params_b=round(meta["n_params"] / 1e9, 2) if meta.get("n_params") else None)
        models[mid] = entry
    try:
        v0 = http_get_json(base + "/api/v0/models", timeout=5.0)
        kind = "lmstudio"
        for m in v0.get("data", []):
            mid = m.get("id")
            if not mid:
                continue
            e = models.setdefault(mid, {"id": mid, "loaded": None, "type": "", "quant": "", "ctx": None, "arch": "", "size_bytes": None, "params_b": None})
            e.update(loaded=(m.get("state") == "loaded"), type=m.get("type", ""), quant=m.get("quantization", "") or "",
                     ctx=m.get("max_context_length"), arch=m.get("arch", ""))
    except Exception:
        pass
    for e in models.values():
        if e["params_b"] is None:
            e["params_b"] = parse_params_b(e["id"])
        if not e["quant"]:
            e["quant"] = parse_quant(e["id"]) or ""
    ordered = sorted(models.values(), key=lambda e: (e["type"] == "embeddings", not e["loaded"], e["id"]))
    return ordered, kind


def list_node_models(node_key, force=False):
    cfg = config.nodes.get(node_key)
    if not cfg:
        return {"models": [], "error": "Unknown node"}
    with _models_cache_lock:
        cached = _models_cache.get(node_key)
        if cached and not force and time.time() - cached["ts"] < 15:
            return dict(cached["data"], current=cfg["model"])
    result = {"models": [], "current": cfg["model"], "kind": cfg.get("kind", "")}
    try:
        models, kind = probe_models(cfg["endpoint"])
        result["models"], result["kind"] = models, kind
        cfg["kind"] = kind
        cfg["status"] = "online"
    except Exception as e:
        result["error"] = str(e)
        cfg["status"] = "offline"
        cfg["last_error"] = str(e)[:200]
    with _models_cache_lock:
        _models_cache[node_key] = {"ts": time.time(), "data": {"models": result["models"], "error": result.get("error"), "kind": result["kind"]}}
    return result


def parse_params_b(model_id):
    m = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*[bB](?![a-zA-Z0-9])", model_id)
    if m:
        v = float(m.group(1))
        return v if 0.1 <= v <= 2000 else None
    m = re.search(r"(?<![\d.])(\d{3,4})\s*[mM](?![a-zA-Z0-9])", model_id)
    if m:
        return round(float(m.group(1)) / 1000, 2)
    return None


def parse_quant(s):
    m = re.search(r"(iq\d_[a-z]+|q\d_k_[sml]|q\d_k|q\d_\d|mxfp4|bf16|f16|f32|\d+bit)", s.lower())
    return m.group(1).upper() if m else ""


def quant_bpw(q):
    q = (q or "").lower().replace("-", "_")
    for k, v in QUANT_BPW:
        if k in q:
            return v
    return 4.85


def gpu_bandwidth(name):
    n = (name or "").lower()
    for k, v in GPU_BANDWIDTH:
        if k in n:
            return v
    return 300 if n else 60


def estimate_model(params_b, bpw, ctx, vram_gb, ram_gb, bandwidth, unified=False, size_bytes=None):
    """Rough memory / speed estimate for a model on given hardware."""
    if not params_b:
        return None
    weights = size_bytes / 1024 ** 3 if size_bytes else params_b * bpw / 8
    kv = 0.012 * params_b * (ctx / 1000.0)
    total = weights + kv + 0.6
    budget = (ram_gb * 0.75) if unified else vram_gb
    if unified:
        budget = ram_gb * 0.75
    fit = "ok" if total <= budget * 0.9 else "tight" if total <= budget else "offload" if total <= budget + ram_gb * 0.8 else "no"
    if fit in ("ok", "tight"):
        tok_s = bandwidth / max(0.5, weights + kv * 0.3) * 0.7
    elif fit == "offload":
        f = max(0.05, min(0.95, budget * 0.9 / total))
        eff = 1 / (f / bandwidth + (1 - f) / 45.0)
        tok_s = eff / max(0.5, weights) * 0.7
    else:
        tok_s = 0
    quality = params_b * (0.65 if bpw < 3.6 else 0.8 if bpw < 4.4 else 0.9 if bpw < 5.4 else 0.96 if bpw < 7 else 1.0)
    return {"weights_gb": round(weights, 2), "kv_gb": round(kv, 2), "total_gb": round(total, 2), "budget_gb": round(budget, 1),
            "fit": fit, "est_tok_s": round(tok_s, 1), "quality": round(quality, 1)}


# ---------------------------------------------------------------------------
# Recommendations for a node (hardware -> profile / roles / models)
# ---------------------------------------------------------------------------

def recommend_node(hw, models=None, kind=""):
    gpus = hw.get("gpus") or []
    ram_gb = float((hw.get("ram") or {}).get("total_gb") or 0)
    platform_s = (hw.get("platform") or "")
    unified = any(g.get("unified") for g in gpus) or ("darwin" in platform_s.lower() and not any("nvidia" in (g.get("name") or "").lower() for g in gpus))
    gpu = max(gpus, key=lambda g: g.get("mem_total_mb", 0)) if gpus else None
    vram_gb = round((gpu.get("mem_total_mb", 0) / 1024) if gpu else 0, 1)
    if unified:
        vram_gb = round(ram_gb * 0.75, 1)
    bw = gpu_bandwidth((gpu or {}).get("name") or (hw.get("cpu") or {}).get("name") or "")
    if not gpu and not unified:
        bw = 50
    budget = vram_gb if (gpu or unified) else ram_gb * 0.6
    if budget < 4.5:
        cls, profile = 0, "Лёгкий вспомогательный узел"
        classes = ["1–3B (Q4_K_M…Q8_0, 1–3 ГБ)", "эмбеддинги (nomic, bge)"]
        stage = "последний этап (быстрая проверка, классификация) или предобработка"
        settings = {"ctx": 4096, "max_tokens": 1024, "parallel": 1, "timeout_s": 120}
        notes = ["Модели крупнее 3–4B будут выгружаться в ОЗУ и работать очень медленно.", "Хорош для линтинга, чек-листов, извлечения полей, маршрутизации запросов, эмбеддингов."]
    elif budget < 9.5:
        cls, profile = 1, "Компактный рабочий узел"
        classes = ["7–9B Q4_K_M (5–6 ГБ) с контекстом 8–16k", "3–4B Q8_0 для скорости", "12–14B только IQ3/Q3 (потеря качества)"]
        stage = "проверка, ревью, тесты, перевод, быстрые ответы; второй/третий этап конвейера"
        settings = {"ctx": 8192, "max_tokens": 2048, "parallel": 1, "timeout_s": 180}
        notes = ["9B Q4 занимает ~6 ГБ + KV-кэш: контекст выше 16k уже не поместится.", "Не запускайте две модели одновременно."]
    elif budget < 14:
        cls, profile = 2, "Основной рабочий узел"
        classes = ["9B Q6_K/Q8_0 (7–10 ГБ) — лучшее качество/скорость", "12–14B Q4_K_M (8–9 ГБ)", "20B MoE (gpt-oss MXFP4 ~12 ГБ) — впритык"]
        stage = "основная генерация (код, анализ); первый или центральный этап"
        settings = {"ctx": 16384, "max_tokens": 3000, "parallel": 2, "timeout_s": 240}
        notes = ["Держите один основной чат-модель + при необходимости маленькую вспомогательную (2B) на другом порту.", "27B даже в Q4 не помещается целиком — только с выгрузкой на CPU (2–5 tok/s)."]
    elif budget < 26:
        cls, profile = 3, "Мощный узел рассуждений"
        classes = ["27–32B Q4_K_M (16–20 ГБ)", "14B Q8_0 (15 ГБ) для точности", "9B с длинным контекстом 64k+"]
        stage = "архитектура, сложный анализ, глубокое рассуждение; первый этап"
        settings = {"ctx": 32768, "max_tokens": 4096, "parallel": 2, "timeout_s": 300}
        notes = ["Хороший кандидат на роль «архитектор / глубокое рассуждение»."]
    else:
        cls, profile = 4, "Флагманский узел (большая память)"
        classes = ["27–32B Q8_0 или 70B Q4 (35–45 ГБ)", "MoE 100B+ при 96 ГБ+", "длинный контекст 128k"]
        stage = "архитектура, длинные документы, финальная проверка качества"
        settings = {"ctx": 65536, "max_tokens": 6000, "parallel": 2, "timeout_s": 600}
        notes = ["Объём памяти позволяет крупные модели, но скорость ограничена пропускной способностью памяти — смотрите оценку tok/s."]
    if unified:
        notes.append(f"Unified memory: под модель доступно ~75% ОЗУ ({vram_gb} ГБ); скорость ≈ {bw} ГБ/с шины памяти — ниже, чем у дискретных GPU.")
    if not gpu and not unified:
        notes.append("GPU не обнаружен: работа только на CPU, реально лишь 1–4B модели с 3–10 tok/s.")
    roles = [r for r in ROLE_LIBRARY if r["min_class"] <= cls]
    pref = {0: ["auditor", "router", "translator", "writer"], 1: ["reviewer", "redteam", "translator", "generalist", "researcher"],
            2: ["coder", "generalist", "reviewer", "researcher"], 3: ["architect", "coder", "researcher"], 4: ["architect", "coder", "researcher"]}[cls]
    roles.sort(key=lambda r: pref.index(r["id"]) if r["id"] in pref else 99)
    fits = []
    for m in (models or []):
        if m.get("type") == "embeddings":
            continue
        est = estimate_model(m.get("params_b"), quant_bpw(m.get("quant")), settings["ctx"], vram_gb, ram_gb, bw, unified, m.get("size_bytes"))
        if est:
            fits.append({"id": m["id"], "params_b": m.get("params_b"), "quant": m.get("quant"), **est})
    fits.sort(key=lambda f: ({"ok": 0, "tight": 1, "offload": 2, "no": 3}[f["fit"]], -f["quality"]))
    best = next((f for f in fits if f["fit"] in ("ok", "tight")), None)
    return {"class": cls, "profile": profile, "vram_gb": vram_gb, "ram_gb": ram_gb, "unified": unified, "gpu": (gpu or {}).get("name", ""),
            "bandwidth_gbs": bw, "model_classes": classes, "stage": stage, "settings": settings, "notes": notes,
            "roles": [{"id": r["id"], "name": r["name"], "tag": r["tag"], "avatar": r["avatar"], "prompt": r["prompt"]} for r in roles[:5]],
            "fits": fits, "best_model": best["id"] if best else None, "kind": kind}


# ---------------------------------------------------------------------------
# Language rule / text helpers
# ---------------------------------------------------------------------------

def detect_language(text):
    t = text or ""
    cyr = len(re.findall(r"[А-Яа-яЁё]", t))
    lat = len(re.findall(r"[A-Za-z]", t))
    if cyr and re.search(r"[ІіЇїЄєҐґ]", t):
        return "uk", "украинский"
    if cyr > lat:
        return "ru", "русский"
    if re.search(r"[一-鿿]", t):
        return "zh", "китайский"
    if re.search(r"[぀-ヿ]", t):
        return "ja", "японский"
    if re.search(r"[가-힯]", t):
        return "ko", "корейский"
    if re.search(r"[؀-ۿ]", t):
        return "ar", "арабский"
    if lat:
        return "en", "English (or another Latin-script language)"
    return "", ""


def language_rule(prompt):
    code, name = detect_language(prompt)
    if code in ("ru", "uk"):
        return (f"ПРАВИЛО ЯЗЫКА: отвечай строго на языке сообщения пользователя — {name}. Не переключайся на английский или другой язык "
                "ни в тексте, ни в заголовках, даже если код, термины или предыдущие сообщения на другом языке. Код и имена оставляй как есть.")
    if code:
        return (f"LANGUAGE RULE: always answer in the same language as the user's message (detected: {name}). "
                "Never switch to another language in prose or headings, even if the context contains other languages. Keep code and identifiers as they are.")
    return "LANGUAGE RULE: answer in the same language as the user's message."


def strip_think(text):
    if not text:
        return "", ""
    reasoning = ""
    m = re.search(r"<think>([\s\S]*?)</think>", text, flags=re.IGNORECASE)
    if m:
        reasoning = m.group(1).strip()
        text = re.sub(r"<think>[\s\S]*?</think>\s*", "", text, count=1, flags=re.IGNORECASE)
    elif text.lstrip().lower().startswith("<think>"):
        reasoning = text.lstrip()[7:].strip()
        text = ""
    return text.strip(), reasoning


# ---------------------------------------------------------------------------
# LLM query (streaming)
# ---------------------------------------------------------------------------

def query_llm(node_key, messages, llm_opts, on_progress=None, cancel_event=None):
    cfg = config.nodes.get(node_key)
    if not cfg:
        return {"error": f"Unknown node: {node_key}", "node_key": node_key}
    if not cfg.get("enabled", True):
        return {"error": f"Узел {cfg['name']} отключён", "node_key": node_key, "node_name": cfg["name"]}
    endpoint = cfg["endpoint"]
    model = cfg.get("model") or "default"
    stream = bool(llm_opts.get("stream", True))
    timeout = int(cfg.get("timeout_s") or llm_opts.get("timeout_s") or 180)
    payload = {"model": model, "messages": messages, "temperature": float(llm_opts.get("temperature", 0.3)),
               "max_tokens": int(llm_opts.get("max_tokens", 2048)), "stream": stream}
    if llm_opts.get("top_p") not in (None, ""):
        payload["top_p"] = float(llm_opts["top_p"])
    re_eff = llm_opts.get("reasoning_effort", "none")
    if re_eff and re_eff != "off":
        payload["reasoning_effort"] = re_eff
    meta = {"model": model, "node_key": node_key, "node_name": cfg["name"], "node_tag": cfg.get("tag", ""), "node_role": cfg.get("role", ""),
            "node_color": cfg.get("color", "#64748b"), "node_avatar": cfg.get("avatar", "💻"), "node_hardware": cfg.get("hardware", "")}
    t0 = time.time()
    first_token_ms = None
    raw_content, raw_reasoning = "", ""
    tokens = 0
    try:
        reach_err = tcp_reachable(endpoint, timeout=3.0)
        if reach_err:
            raise ConnectionError(f"узел недоступен: {reach_err}")
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if stream:
                for raw in resp:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content") or ""
                    rpiece = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if piece or rpiece:
                        if first_token_ms is None:
                            first_token_ms = int((time.time() - t0) * 1000)
                        tokens += 1
                        raw_content += piece
                        raw_reasoning += rpiece
                        if on_progress:
                            c, r = strip_think(raw_content)
                            on_progress(c, (raw_reasoning + ("\n" + r if r else "")).strip())
                    usage = obj.get("usage")
                    if usage and usage.get("completion_tokens"):
                        tokens = usage["completion_tokens"]
            else:
                data = json.loads(resp.read().decode("utf-8"))
                choice = data["choices"][0]["message"]
                raw_content = choice.get("content") or ""
                raw_reasoning = choice.get("reasoning_content") or choice.get("reasoning") or ""
                tokens = (data.get("usage") or {}).get("completion_tokens", 0)
        lat = max(1, int((time.time() - t0) * 1000))
        content, r2 = strip_think(raw_content)
        reasoning = (raw_reasoning + ("\n" + r2 if r2 else "")).strip()
        cfg.update(status="online", latency_ms=lat, last_error="")
        out = dict(meta)
        out.update({"content": content, "reasoning": reasoning, "latency_ms": lat, "first_token_ms": first_token_ms, "tokens": tokens,
                    "tok_per_s": round(tokens / max(0.001, (lat - (first_token_ms or 0)) / 1000), 1) if tokens else 0,
                    "cancelled": bool(cancel_event is not None and cancel_event.is_set())})
        return out
    except Exception as e:
        lat = max(1, int((time.time() - t0) * 1000))
        err = str(e)
        if isinstance(e, urllib.error.HTTPError):
            try:
                err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:400]}"
            except Exception:
                err = f"HTTP {e.code}"
        cfg.update(status="offline", last_error=err[:200])
        out = dict(meta)
        out.update({"error": err, "latency_ms": lat, "content": strip_think(raw_content)[0]})
        return out


def quick_llm(node_key, system, user, max_tokens=300, temperature=0.0, timeout_s=60):
    """Small non-streaming helper call (query generation, command parsing)."""
    opts = config.llm()
    opts.update({"stream": False, "max_tokens": max_tokens, "temperature": temperature, "timeout_s": timeout_s, "reasoning_effort": "none"})
    return query_llm(node_key, [{"role": "system", "content": system}, {"role": "user", "content": user}], opts)


def extract_json(text):
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text).strip()
    for pat in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        m = re.search(pat, t)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Web search (DuckDuckGo HTML) + page fetch
# ---------------------------------------------------------------------------

def _fetch(url, timeout=8, max_bytes=1_500_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(max_bytes)
        ctype = r.headers.get("Content-Type", "")
        m = re.search(r"charset=([\w-]+)", ctype)
        enc = m.group(1) if m else "utf-8"
        try:
            return raw.decode(enc, errors="replace"), ctype
        except Exception:
            return raw.decode("utf-8", errors="replace"), ctype


def strip_tags(s):
    s = re.sub(r"<(script|style|noscript|svg|header|footer|nav)[\s\S]*?</\1>", " ", s, flags=re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def ddg_search(query, n=6):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    page, _ = _fetch(url, timeout=10)
    results = []
    for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.S):
        href, title = m.group(1), strip_tags(m.group(2))
        u = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query).get("uddg", [""])[0] or html.unescape(href)
        if u.startswith("//"):
            u = "https:" + u
        results.append({"title": title, "url": u, "snippet": ""})
    snippets = [strip_tags(s) for s in re.findall(r'<a class="result__snippet"[^>]*>(.*?)</a>', page, flags=re.S)]
    for i, s in enumerate(snippets[:len(results)]):
        results[i]["snippet"] = s
    seen, out = set(), []
    for r in results:
        if r["url"] in seen or "duckduckgo.com" in r["url"]:
            continue
        seen.add(r["url"])
        out.append(r)
        if len(out) >= n:
            break
    return out


def fetch_page_text(url, max_chars=6000):
    try:
        page, ctype = _fetch(url, timeout=8)
        if "html" not in ctype and "text" not in ctype and "json" not in ctype:
            return ""
        title = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.S | re.I)
        body = re.search(r"<body[^>]*>([\s\S]*)</body>", page, flags=re.I)
        text = strip_tags(body.group(1) if body else page)
        return text[:max_chars]
    except Exception:
        return ""


def build_search_context(prompt, node_key, n_results=5, n_pages=3, status_cb=None):
    """Generates queries with the node's LLM, searches, fetches top pages. Returns (context_text, sources)."""
    queries = []
    try:
        if status_cb:
            status_cb("Формулирую поисковые запросы…")
        r = quick_llm(node_key, "Ты формулируешь поисковые запросы. Верни только JSON-массив из 1–3 коротких запросов (строк) на языке вопроса, без пояснений.",
                      f"Вопрос пользователя:\n{prompt[:1500]}", max_tokens=120, timeout_s=min(90, int(config.llm().get("timeout_s") or 180)))
        q = extract_json(r.get("content", ""))
        if isinstance(q, list):
            queries = [str(x)[:150] for x in q if str(x).strip()][:3]
    except Exception:
        pass
    if not queries:
        queries = [re.sub(r"\s+", " ", prompt)[:150]]
    if status_cb:
        status_cb("Поиск в интернете: " + "; ".join(queries)[:80])
    results, seen = [], set()
    for q in queries:
        try:
            for r in ddg_search(q, n=n_results):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    results.append(r)
        except Exception:
            continue
        if len(results) >= n_results:
            break
    results = results[:n_results]
    if status_cb:
        status_cb(f"Читаю {min(n_pages, len(results))} страниц…")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        texts = list(ex.map(lambda r: fetch_page_text(r["url"], 5000), results[:n_pages]))
    sources, blocks = [], []
    for i, r in enumerate(results, 1):
        text = texts[i - 1] if i - 1 < len(texts) else ""
        sources.append({"n": i, "title": r["title"], "url": r["url"], "snippet": r["snippet"], "fetched": bool(text)})
        block = f"[{i}] {r['title']}\nURL: {r['url']}\nФрагмент: {r['snippet']}"
        if text:
            block += f"\nТекст страницы: {text}"
        blocks.append(block)
    ctx = "=== РЕЗУЛЬТАТЫ ПОИСКА В ИНТЕРНЕТЕ (запросы: " + "; ".join(queries) + ") ===\n\n" + "\n\n".join(blocks) + "\n=== КОНЕЦ РЕЗУЛЬТАТОВ ==="
    return ctx, sources, queries


# ---------------------------------------------------------------------------
# Thread store
# ---------------------------------------------------------------------------

class ThreadStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.threads = {}
        self.order = []
        self._dirty = False
        self.load()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def load(self):
        if not THREADS_FILE.exists():
            return
        try:
            data = json.loads(THREADS_FILE.read_text(encoding="utf-8-sig"))
            for t in data.get("threads", []):
                for m in t.get("messages", []):
                    if m.get("status") == "streaming":
                        m["status"] = "error"
                        m["error"] = m.get("error") or "Сервер был перезапущен во время генерации"
                t.setdefault("owner_id", "")
                t.setdefault("owner_name", "")
                t.setdefault("plan", None)
                t.setdefault("no_roles", False)
                t.setdefault("skill", "default")
                t.setdefault("council", None)
                self.threads[t["id"]] = t
            self.order = [t["id"] for t in data.get("threads", []) if t["id"] in self.threads]
        except Exception as e:
            print(f"[!] threads.json unreadable: {e}")

    def _flush_loop(self):
        while True:
            time.sleep(2.0)
            if self._dirty:
                try:
                    self.save()
                except Exception as e:
                    print(f"[!] threads save failed: {e}")

    def mark(self):
        self._dirty = True

    def save(self):
        with self.lock:
            payload = {"threads": [self.threads[i] for i in self.order if i in self.threads], "saved_at": now_iso()}
            self._dirty = False
        tmp = THREADS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, THREADS_FILE)

    def create(self, title="Новый чат", mode="single", node="node_1", owner_id="", owner_name=""):
        with self.lock:
            tid = uuid.uuid4().hex[:10]
            t = {"id": tid, "title": title, "created_at": now_iso(), "updated_at": now_iso(), "mode": mode, "node": node,
                 "pinned": False, "messages": [], "owner_id": owner_id, "owner_name": owner_name, "plan": None, "no_roles": False, "skill": "default", "council": None}
            self.threads[tid] = t
            self.order.insert(0, tid)
            self.mark()
            return t

    def get(self, tid):
        with self.lock:
            return self.threads.get(tid)

    def summaries(self):
        with self.lock:
            out = []
            for tid in self.order:
                t = self.threads.get(tid)
                if not t:
                    continue
                msgs = t["messages"]
                last = msgs[-1] if msgs else None
                out.append({"id": tid, "title": t["title"], "created_at": t["created_at"], "updated_at": t["updated_at"],
                            "mode": t.get("mode", "single"), "node": t.get("node", ""), "pinned": t.get("pinned", False),
                            "owner_id": t.get("owner_id", ""), "owner_name": t.get("owner_name", ""), "skill": t.get("skill", "default"),
                            "no_roles": t.get("no_roles", False), "count": len(msgs),
                            "busy": any(m.get("status") == "streaming" for m in msgs[-8:]),
                            "preview": (last.get("content") or last.get("error") or "")[:80] if last else ""})
            out.sort(key=lambda s: (not s["pinned"],))
            return out

    def rename(self, tid, title):
        with self.lock:
            t = self.threads.get(tid)
            if t:
                t["title"] = title.strip()[:120] or t["title"]
                t["updated_at"] = now_iso()
                self.mark()

    def set_meta(self, tid, **kw):
        with self.lock:
            t = self.threads.get(tid)
            if t:
                for k, v in kw.items():
                    if k in ("mode", "node", "pinned", "plan", "no_roles", "skill", "owner_id", "owner_name", "council"):
                        t[k] = v
                self.mark()

    def delete(self, tid):
        with self.lock:
            self.threads.pop(tid, None)
            if tid in self.order:
                self.order.remove(tid)
            self.mark()

    def clear(self, tid):
        with self.lock:
            t = self.threads.get(tid)
            if t:
                t["messages"] = []
                t["updated_at"] = now_iso()
                self.mark()

    def add_message(self, tid, msg):
        with self.lock:
            t = self.threads.get(tid)
            if not t:
                return None
            msg.setdefault("id", uuid.uuid4().hex[:10])
            msg.setdefault("ts", now_iso())
            msg.setdefault("time", now_hms())
            t["messages"].append(msg)
            t["updated_at"] = now_iso()
            if msg["role"] == "user" and t["title"] in ("Новый чат", "") and len([m for m in t["messages"] if m["role"] == "user"]) == 1:
                t["title"] = re.sub(r"\s+", " ", msg["content"]).strip()[:48] or t["title"]
            if tid in self.order:
                self.order.remove(tid)
            self.order.insert(0, tid)
            self.mark()
            return msg

    def update_message(self, tid, mid, **fields):
        with self.lock:
            t = self.threads.get(tid)
            if not t:
                return
            for m in t["messages"]:
                if m["id"] == mid:
                    m.update(fields)
                    break
            t["updated_at"] = now_iso()
            self.mark()

    def delete_message(self, tid, mid):
        with self.lock:
            t = self.threads.get(tid)
            if t:
                t["messages"] = [m for m in t["messages"] if m["id"] != mid]
                self.mark()

    def history_for(self, tid, node_key, turns, exclude_task=None):
        """Prior conversation of the branch (all nodes' replies; other nodes' replies are labelled)."""
        with self.lock:
            t = self.threads.get(tid)
            if not t or turns <= 0:
                return []
            out = []
            for m in t["messages"]:
                if m.get("status") == "streaming" or (exclude_task and m.get("task_id") == exclude_task and m["role"] == "assistant"):
                    continue
                if m["role"] == "user":
                    content = m["content"]
                    if m.get("files"):
                        hc = int(config.settings.get("files", {}).get("history_chars", 6000))
                        content += "\n\n" + hub_files.files_block(FILES, m["files"], per_file_chars=hc, total_chars=hc * 3)
                    out.append({"role": "user", "content": content})
                elif m["role"] == "tool":
                    if m.get("status") == "done":
                        tc = int(config.settings.get("tools", {}).get("history_chars", 3000))
                        out.append({"role": "user", "content": tool_result_text(m, tc)})
                elif m["role"] == "assistant" and m.get("content") and not m.get("error"):
                    if m.get("node_key") == node_key or not m.get("node_key"):
                        out.append({"role": "assistant", "content": m["content"]})
                    else:
                        out.append({"role": "assistant", "content": f"[{m.get('node_name', 'другой узел')}]: {m['content']}"})
            user_idx = [i for i, m in enumerate(out) if m["role"] == "user"]
            if len(user_idx) > turns:
                out = out[user_idx[-turns]:]
            collapsed = []
            for m in out:
                if collapsed and collapsed[-1]["role"] == m["role"]:
                    collapsed[-1]["content"] += "\n\n" + m["content"]
                else:
                    collapsed.append(dict(m))
            return collapsed


threads = ThreadStore()
FILES = hub_files.FileStore(DATA_DIR / "files")


# ---------------------------------------------------------------------------
# Execution plans
# ---------------------------------------------------------------------------

def normalize_plan(plan):
    """-> {"stages": [{"name", "nodes": [{"key", "instruction"}], "instruction", "context": all|last|none}]}"""
    stages = []
    for i, st in enumerate((plan or {}).get("stages") or []):
        nodes = []
        for n in st.get("nodes") or []:
            if isinstance(n, str):
                n = {"key": n}
            if n.get("key") in config.nodes:
                nodes.append({"key": n["key"], "instruction": (n.get("instruction") or "").strip()})
        if nodes:
            stages.append({"name": (st.get("name") or f"Этап {i + 1}").strip(), "nodes": nodes,
                           "instruction": (st.get("instruction") or "").strip(), "context": st.get("context") or "all"})
    return {"stages": stages}


def plan_for(mode, target_node, thread):
    if mode == "single":
        nk = target_node if target_node in config.nodes else next(iter(config.nodes))
        return {"stages": [{"name": "Ответ", "nodes": [{"key": nk, "instruction": ""}], "instruction": "", "context": "none"}]}
    if mode == "all":
        enabled = [k for k in config.nodes_public() if config.nodes[k].get("enabled", True)]
        return {"stages": [{"name": "Все узлы", "nodes": [{"key": k, "instruction": ""} for k in enabled], "instruction": "", "context": "none"}]}
    if mode == "pipeline":
        stages = [{"name": s.get("label") or f"Шаг {i + 1}", "nodes": [{"key": s["node"], "instruction": ""}], "instruction": s.get("prompt", ""), "context": "all"}
                  for i, s in enumerate(config.settings.get("pipeline") or []) if s.get("node") in config.nodes]
        return {"stages": stages}
    if mode == "hybrid":
        p = normalize_plan(thread.get("plan"))
        if p["stages"]:
            return p
        return plan_for("all", target_node, thread)
    return plan_for("single", target_node, thread)


TOOL_BLOCK_RE = re.compile(r"```run:(powershell|pwsh|cmd|bash|sh)(?:@([\w.\-]+))?[ \t]*\r?\n(.*?)```", re.S | re.I)


def parse_tool_calls(text):
    """Блоки ```run:powershell@node_key … ``` в ответе модели → [{shell, node, command}]."""
    out = []
    for m in TOOL_BLOCK_RE.finditer(text or ""):
        shell = m.group(1).lower()
        shell = {"pwsh": "powershell", "sh": "bash"}.get(shell, shell)
        cmd = m.group(3).strip()
        if cmd:
            out.append({"shell": shell, "node": (m.group(2) or "").strip(), "command": cmd})
    return out


def tools_system_prompt():
    nodes = [(k, c) for k, c in config.nodes.items() if c.get("exec_enabled") and c.get("enabled", True)]
    lst = "\n".join(f"- {k} — {c['name']}" + (f" ({c.get('hardware')})" if c.get("hardware") else "") for k, c in nodes) or "- (нет узлов с разрешённым выполнением)"
    return ("РЕЖИМ «АГЕНТ ПК». Ты можешь выполнять команды на компьютерах кластера. Доступные узлы (ключ — имя):\n" + lst +
            "\n\nЧтобы выполнить команду, выведи ровно такой блок и ЗАКОНЧИ ответ (результат придёт следующим сообщением):\n"
            "```run:powershell@<ключ_узла>\n<команда PowerShell>\n```\n"
            "Допустимы также run:cmd и run:bash (для Linux/macOS-узлов). Не больше трёх блоков за ход. "
            "Сначала собирай информацию неразрушающими командами (Get-*, dir, cat, Test-Path), опасные действия (удаление, "
            "остановка служб, перезагрузка, изменение реестра) выполняй только если пользователь прямо об этом попросил, и объясняй, что делаешь. "
            "Команды должны быть неинтерактивными и завершаться сами. Когда задача решена, дай итоговый ответ без блоков run.")


def tool_result_text(m, limit=12000):
    out = (m.get("output") or "")[-limit:]
    head = f"РЕЗУЛЬТАТ КОМАНДЫ на узле {m.get('node_name') or m.get('node_key')} [{m.get('shell')}], код выхода {m.get('exit_code')}" + \
           (" (превышено время)" if m.get("timed_out") else "") + (" — отклонена" if m.get("denied") else "")
    tail = ("\nАдминистратор отклонил эту команду. Не повторяй её и не пытайся обойти отказ — объясни, что хотел сделать, "
            "и предложи другой путь или спроси пользователя.") if m.get("denied") else ""
    return head + ":\n```\n" + out + "\n```" + tail


class TaskRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}
        self.cancel_flags = {}
        self.counter = 0
        self.legacy_tasks = collections.deque(maxlen=50)
        self.pending = {}        # call_id → {event, decision, by}: команды «Агента ПК», ждущие подтверждения

    def is_busy(self):
        with self.lock:
            return bool(self.active)

    def active_info(self):
        with self.lock:
            return list(self.active.values())

    def thread_busy(self, tid):
        with self.lock:
            return any(a["thread_id"] == tid for a in self.active.values())

    def cancel(self, task_id=None, thread_id=None):
        with self.lock:
            for k, ev in self.cancel_flags.items():
                a = self.active.get(k)
                if not a:
                    continue
                if (task_id and k == task_id) or (thread_id and a["thread_id"] == thread_id) or (not task_id and not thread_id):
                    ev.set()
                    for p in self.pending.values():
                        if p.get("thread_id") == a["thread_id"]:
                            p["event"].set()

    def start(self, thread_id, mode, target_node, prompt, overrides=None, client=None):
        overrides = overrides or {}
        thread = threads.get(thread_id)
        skill = config.skill(overrides.get("skill") or thread.get("skill") or "default")
        llm_opts = config.llm()
        for k, v in (skill.get("params") or {}).items():
            if v not in (None, ""):
                llm_opts[k] = v
        for k in ("max_tokens", "temperature", "top_p", "reasoning_effort", "stream", "timeout_s", "history_turns"):
            if k in overrides and overrides[k] not in (None, ""):
                llm_opts[k] = overrides[k]
        no_roles = bool(overrides.get("no_roles", thread.get("no_roles", False)))
        llm_opts["_files"] = overrides.get("files") or []
        llm_opts["_tools"] = bool(overrides.get("tools")) and mode == "single"
        council = None
        if mode == "council":
            c = dict(thread.get("council") or {})
            if isinstance(overrides.get("council"), dict):
                c.update(overrides["council"])
            parts = [k for k in (c.get("nodes") or []) if k in config.nodes and config.nodes[k].get("enabled", True)]
            if not parts:
                parts = [k for k in config.nodes_public() if config.nodes[k].get("enabled", True)]
            if len(parts) < 2:
                return None, "Для консилиума нужны минимум два включённых узла"
            mod = c.get("moderator") or config.settings.get("council", {}).get("moderator") or target_node
            if mod not in config.nodes or not config.nodes[mod].get("enabled", True):
                mod = parts[0]
            council = {"nodes": parts, "moderator": mod, "rounds": int(c.get("rounds", config.settings.get("council", {}).get("rounds", 1)))}
            plan = {"stages": [{"name": "Консилиум", "nodes": [{"key": k} for k in parts]}]}
        else:
            plan = plan_for(mode, target_node, thread)
        if not plan["stages"]:
            return None, "План пуст: нет включённых узлов"
        with self.lock:
            self.counter += 1
            task_id = self.counter
            ev = threading.Event()
            self.cancel_flags[task_id] = ev
            self.active[task_id] = {"task_id": task_id, "thread_id": thread_id, "mode": mode, "target_node": target_node,
                                    "status": "Инициализация…", "started": now_iso(), "skill": skill["id"], "stages": len(plan["stages"]), "stage": 0}
        meta = {"mode": mode, "node": target_node, "skill": skill["id"], "no_roles": no_roles}
        if mode == "hybrid":
            meta["plan"] = plan
        if council:
            meta["council"] = council
        threads.set_meta(thread_id, **meta)
        if client and not thread.get("owner_id"):
            threads.set_meta(thread_id, owner_id=client.get("id", ""), owner_name=client.get("name", ""))
        threads.add_message(thread_id, {"role": "user", "content": prompt, "mode": mode, "node_key": target_node, "task_id": task_id,
                                        "skill": skill["id"], "author": (client or {}).get("name", ""), "author_id": (client or {}).get("id", ""),
                                        **({"files": llm_opts["_files"]} if llm_opts["_files"] else {}), **({"tools": True} if llm_opts["_tools"] else {})})
        legacy = {"id": task_id, "time": now_hms(), "mode": mode, "prompt": prompt, "status": "processing", "responses": []}
        self.legacy_tasks.append(legacy)
        if council:
            threading.Thread(target=self._council_worker, args=(task_id, thread_id, council, prompt, llm_opts, skill, no_roles, ev, legacy), daemon=True).start()
        else:
            threading.Thread(target=self._worker, args=(task_id, thread_id, plan, prompt, llm_opts, skill, no_roles, ev, legacy), daemon=True).start()
        return task_id, None

    # ---- council: drafts -> mutual critique -> single synthesized answer
    def _council_worker(self, task_id, thread_id, council, prompt, llm_opts, skill, no_roles, cancel_ev, legacy):
        cset = config.settings.get("council") or {}
        draft_chars = int(cset.get("draft_chars", 3500))
        crit_chars = int(cset.get("critique_chars", 1500))
        parts, mod, rounds = council["nodes"], council["moderator"], max(0, min(3, council.get("rounds", 1)))
        mcfg = config.nodes[mod]
        names = {k: config.nodes[k]["name"] for k in parts}
        msg = threads.add_message(thread_id, {"role": "assistant", "content": "", "reasoning": "", "status": "streaming", "task_id": task_id, "council": True,
                                              "node_key": mod, "node_name": "Консилиум", "node_tag": "COUNCIL", "node_role": f"модератор: {mcfg['name']}",
                                              "node_color": "#f59e0b", "node_avatar": "🤝", "model": mcfg.get("model"), "discussion": [], "phase": "drafts",
                                              "participants": [{"key": k, "name": names[k], "avatar": config.nodes[k].get("avatar"), "color": config.nodes[k].get("color")} for k in parts]})
        mid = msg["id"]
        discussion = []
        lock = threading.Lock()

        def push():
            threads.update_message(thread_id, mid, discussion=copy.deepcopy(discussion))

        def entry(kind, nk, rnd):
            e = {"kind": kind, "round": rnd, "node_key": nk, "node_name": names.get(nk, nk), "avatar": config.nodes[nk].get("avatar"), "color": config.nodes[nk].get("color"),
                 "content": "", "status": "streaming", "latency_ms": None}
            with lock:
                discussion.append(e)
            return e

        def run(nk, messages, e, opts):
            last = [0.0]

            def prog(c, r):
                if time.time() - last[0] > 0.3:
                    e["content"] = c
                    push()
                    last[0] = time.time()
            r = query_llm(nk, messages, opts, on_progress=prog, cancel_event=cancel_ev)
            e["content"] = r.get("content") or ""
            e["status"] = "error" if "error" in r else "done"
            e["error"] = r.get("error", "")
            e["latency_ms"] = r.get("latency_ms")
            e["model"] = r.get("model")
            push()
            return r

        responses = []
        try:
            search_ctx, sources = "", []
            if skill.get("web"):
                try:
                    search_ctx, sources, _ = build_search_context(prompt, parts[0], int(skill.get("web_results") or 5), int(skill.get("web_pages") or 3),
                                                                  status_cb=lambda s: self._set_status(task_id, s))
                    threads.update_message(thread_id, mid, sources=sources)
                except Exception:
                    pass
            user_base = prompt + ("\n\n" + search_ctx if search_ctx else "")
            # --- round 0: independent drafts (with branch history)
            self._set_status(task_id, f"Консилиум · черновики ({len(parts)} узлов)…", stage=1, stages=2 + rounds)
            turns = int(llm_opts.get("history_turns", 8))

            def draft(nk):
                e = entry("draft", nk, 0)
                system = self._system_for(nk, prompt, skill, no_roles)
                hist = threads.history_for(thread_id, nk, turns, exclude_task=task_id)
                if hist and hist[-1]["role"] == "user":
                    hist[-1] = {"role": "user", "content": user_base}
                else:
                    hist.append({"role": "user", "content": user_base})
                return run(nk, ([{"role": "system", "content": system}] if system else []) + hist, e, llm_opts)

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(parts)) as ex:
                drafts = dict(zip(parts, ex.map(draft, parts)))
            responses += list(drafts.values())
            # --- critique rounds
            for rnd in range(1, rounds + 1):
                if cancel_ev.is_set():
                    break
                self._set_status(task_id, f"Консилиум · обсуждение, раунд {rnd}/{rounds}…", stage=1 + rnd)
                snapshot = [e for e in discussion if e["round"] == rnd - 1 and e.get("content")]
                if len(snapshot) < 2:
                    break

                def critique(nk, rnd=rnd, snapshot=snapshot):
                    e = entry("critique", nk, rnd)
                    others = "\n\n".join(f"--- {'ТВОЙ ОТВЕТ' if s['node_key'] == nk else 'Участник ' + s['node_name']} ---\n{s['content'][:draft_chars]}" for s in snapshot)
                    system = self._system_for(nk, prompt, skill, no_roles)
                    user = (f"Вопрос пользователя:\n{prompt}\n\nОтветы участников консилиума:\n{others}\n\n"
                            "Ты участвуешь в обсуждении. Коротко и конкретно (без полного ответа заново): "
                            "1) с чем в чужих ответах ты не согласен и почему; 2) какие важные моменты упущены или ошибочны; "
                            "3) что из ответов стоит взять за основу итога. Отвечай на языке вопроса.")
                    opts = dict(llm_opts, max_tokens=int(cset.get("critique_max_tokens", 700)))
                    return run(nk, ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}], e, opts)

                with concurrent.futures.ThreadPoolExecutor(max_workers=len(parts)) as ex:
                    list(ex.map(critique, parts))
            # --- synthesis by the moderator (streams into the visible message)
            if not cancel_ev.is_set():
                self._set_status(task_id, f"Консилиум · итоговый ответ ({mcfg['name']})…", stage=2 + rounds)
                threads.update_message(thread_id, mid, phase="synthesis")
                drafts_txt = "\n\n".join(f"--- Черновик: {e['node_name']} ---\n{e['content'][:draft_chars]}" for e in discussion if e["kind"] == "draft" and e.get("content"))
                crit_txt = "\n\n".join(f"--- Замечания ({e['node_name']}, раунд {e['round']}) ---\n{e['content'][:crit_chars]}" for e in discussion if e["kind"] == "critique" and e.get("content"))
                system_parts = []
                if not no_roles and mcfg.get("system_prompt"):
                    system_parts.append(mcfg["system_prompt"])
                system_parts.append("Ты — модератор консилиума из нескольких моделей. Твоя задача — выдать ОДИН окончательный ответ пользователю: объединить сильные стороны черновиков, "
                                    "исправить ошибки, учтённые в замечаниях, убрать противоречия. Не упоминай процесс обсуждения, участников и черновики — пиши так, будто это единственный ответ системы.")
                if skill.get("addendum"):
                    system_parts.append(skill["addendum"])
                if config.settings["llm"].get("force_language", True):
                    system_parts.append(language_rule(prompt))
                user = f"Вопрос пользователя:\n{prompt}\n\n{('Результаты поиска:' + chr(10) + search_ctx + chr(10) + chr(10)) if search_ctx else ''}ЧЕРНОВИКИ УЧАСТНИКОВ:\n{drafts_txt}"
                if crit_txt:
                    user += f"\n\nЗАМЕЧАНИЯ УЧАСТНИКОВ:\n{crit_txt}"
                user += "\n\nСоставь окончательный ответ."
                last = [0.0]

                def prog(c, r):
                    if time.time() - last[0] > 0.15:
                        threads.update_message(thread_id, mid, content=c, reasoning=r)
                        last[0] = time.time()
                res = query_llm(mod, [{"role": "system", "content": "\n\n".join(system_parts)}, {"role": "user", "content": user}], llm_opts, on_progress=prog, cancel_event=cancel_ev)
                responses.append(res)
                if "error" in res:
                    threads.update_message(thread_id, mid, status="error", error=res["error"], content=res.get("content", ""), latency_ms=res.get("latency_ms"), phase="done")
                else:
                    threads.update_message(thread_id, mid, status="cancelled" if res.get("cancelled") else "done", content=res["content"], reasoning=res.get("reasoning", ""),
                                           latency_ms=res["latency_ms"], first_token_ms=res.get("first_token_ms"), tokens=res.get("tokens"), tok_per_s=res.get("tok_per_s"),
                                           model=res["model"], phase="done")
            else:
                threads.update_message(thread_id, mid, status="cancelled", phase="done")
        except Exception as e:
            threads.update_message(thread_id, mid, status="error", error=f"Сбой консилиума: {e}", phase="done")
        finally:
            legacy["status"] = "completed" if any("content" in r for r in responses) else "failed"
            legacy["responses"] = responses
            with self.lock:
                self.active.pop(task_id, None)
                self.cancel_flags.pop(task_id, None)

    def _set_status(self, task_id, text, **kw):
        with self.lock:
            if task_id in self.active:
                self.active[task_id]["status"] = text
                self.active[task_id].update(kw)

    def _system_for(self, node_key, prompt, skill, no_roles):
        parts = []
        if not no_roles:
            sp = (config.nodes[node_key].get("system_prompt") or "").strip()
            if sp:
                parts.append(sp)
        if skill.get("addendum"):
            parts.append(skill["addendum"].strip())
        if config.settings["llm"].get("force_language", True):
            parts.append(language_rule(prompt))
        return "\n\n".join(parts) if parts else None

    def _run_one(self, thread_id, task_id, node_key, messages, llm_opts, cancel_ev, extra=None):
        cfg = config.nodes[node_key]
        msg = threads.add_message(thread_id, {"role": "assistant", "content": "", "reasoning": "", "status": "streaming", "task_id": task_id,
                                              "node_key": node_key, "node_name": cfg["name"], "node_tag": cfg.get("tag", ""), "node_role": cfg.get("role", ""),
                                              "node_color": cfg.get("color"), "node_avatar": cfg.get("avatar"), "model": cfg.get("model"), **(extra or {})})
        mid = msg["id"]
        last_push = [0.0]

        def on_progress(content, reasoning):
            now = time.time()
            if now - last_push[0] > 0.15:
                threads.update_message(thread_id, mid, content=content, reasoning=reasoning)
                last_push[0] = now

        res = query_llm(node_key, messages, llm_opts, on_progress=on_progress, cancel_event=cancel_ev)
        if "error" in res:
            threads.update_message(thread_id, mid, status="error", error=res["error"], content=res.get("content", ""), latency_ms=res.get("latency_ms"))
        else:
            threads.update_message(thread_id, mid, status="cancelled" if res.get("cancelled") else "done", content=res["content"],
                                   reasoning=res.get("reasoning", ""), latency_ms=res["latency_ms"], first_token_ms=res.get("first_token_ms"),
                                   tokens=res.get("tokens"), tok_per_s=res.get("tok_per_s"), model=res["model"])
        return res

    # ---- «Агент ПК»: модель просит выполнить команду на узле → (подтверждение) → выполнение → ответ модели
    def approve(self, call_id, ok, by=""):
        with self.lock:
            p = self.pending.get(call_id)
            if not p:
                return False
            p["decision"] = bool(ok)
            p["by"] = by
            p["event"].set()
        return True

    def _tool_loop(self, thread_id, task_id, node_key, messages, res, llm_opts, cancel_ev):
        ts = config.settings.get("tools", {})
        max_steps = int(ts.get("max_steps", 8))
        for step in range(max_steps):
            calls = parse_tool_calls(res.get("content") or "")
            if not calls or cancel_ev.is_set():
                return res
            results = []
            for c in calls[:3]:
                results.append(self._run_tool_call(thread_id, task_id, c, ts, cancel_ev))
                if cancel_ev.is_set():
                    return res
            messages.append({"role": "assistant", "content": res.get("content") or ""})
            messages.append({"role": "user", "content": "\n\n".join(tool_result_text(r, int(ts.get("max_output_chars", 12000))) for r in results) +
                             "\n\nПродолжай: проанализируй результат и либо выполни следующий шаг, либо дай итоговый ответ."})
            self._set_status(task_id, f"Агент ПК: шаг {step + 2}/{max_steps}…")
            res = self._run_one(thread_id, task_id, node_key, messages, llm_opts, cancel_ev)
            if res.get("error"):
                return res
        return res

    def _tool_msg(self, thread_id, mid):
        t = threads.get(thread_id) or {"messages": []}
        return next((m for m in t["messages"] if m["id"] == mid), {"id": mid})

    def _run_tool_call(self, thread_id, task_id, call, ts, cancel_ev):
        nk = call["node"] or ""
        cfg = config.nodes.get(nk)
        msg = threads.add_message(thread_id, {"role": "tool", "status": "pending", "task_id": task_id, "node_key": nk,
                                              "node_name": cfg["name"] if cfg else nk, "shell": call["shell"], "command": call["command"]})
        mid = msg["id"]
        if not cfg:
            threads.update_message(thread_id, mid, status="done", ok=False, output=f"неизвестный узел: {nk}", exit_code=-1)
            return self._tool_msg(thread_id, mid)
        if not cfg.get("exec_enabled"):
            threads.update_message(thread_id, mid, status="done", ok=False, exit_code=-1,
                                   output=f"на узле «{cfg['name']}» выполнение команд выключено (Настройки → Узлы → «Агент ПК»)")
            return self._tool_msg(thread_id, mid)
        if ts.get("approve", "confirm") != "auto":
            ev = threading.Event()
            with self.lock:
                self.pending[mid] = {"event": ev, "decision": None, "thread_id": thread_id}
            self._set_status(task_id, f"Агент ПК: ждёт подтверждения команды на «{cfg['name']}»")
            while not ev.is_set():
                if cancel_ev.is_set():
                    break
                ev.wait(0.5)
            with self.lock:
                p = self.pending.pop(mid, {})
            if not p.get("decision"):
                threads.update_message(thread_id, mid, status="done", ok=False, exit_code=-1, denied=True,
                                       output="команда отклонена администратором" if p.get("decision") is False else "команда не выполнена (задача остановлена)")
                security.audit("tool_denied", "", f"{nk}: {call['command'][:200]}", p.get("by", ""))
                return self._tool_msg(thread_id, mid)
        threads.update_message(thread_id, mid, status="running")
        self._set_status(task_id, f"Агент ПК: выполняю на «{cfg['name']}»…")
        security.audit("tool_exec", "", f"{nk} [{call['shell']}]: {call['command'][:300]}", "admin")
        t0 = time.time()
        r = node_exec(nk, "shell", {"shell": call["shell"], "command": call["command"], "timeout": int(ts.get("timeout_s", 120))},
                      timeout=int(ts.get("timeout_s", 120)) + 15)
        out = r.get("output") if isinstance(r.get("output"), dict) else {}
        text = (out.get("stdout") or "")
        if out.get("stderr"):
            text += ("\n[stderr]\n" + out["stderr"]) if text else out["stderr"]
        if not r.get("ok") and not out:
            text = r.get("error") or "ошибка выполнения"
        threads.update_message(thread_id, mid, status="done", ok=bool(r.get("ok")), exit_code=out.get("exit_code", -1 if not r.get("ok") else 0),
                               output=text[-int(ts.get("max_output_chars", 12000)):], duration_ms=out.get("duration_ms") or int((time.time() - t0) * 1000),
                               timed_out=bool(out.get("timed_out")))
        return self._tool_msg(thread_id, mid)

    def _worker(self, task_id, thread_id, plan, prompt, llm_opts, skill, no_roles, cancel_ev, legacy):
        responses = []
        try:
            turns = int(llm_opts.get("history_turns", 8))
            ctx_chars = int(llm_opts.get("pipeline_context_chars", 1500))
            stages = plan["stages"]
            multi = len(stages) > 1
            search_ctx, sources = "", []
            if skill.get("web"):
                first_node = stages[0]["nodes"][0]["key"]
                try:
                    search_ctx, sources, queries = build_search_context(prompt, first_node, int(skill.get("web_results") or 5), int(skill.get("web_pages") or 3),
                                                                        status_cb=lambda s: self._set_status(task_id, s))
                except Exception as e:
                    search_ctx, sources = "", [{"n": 0, "title": "Поиск не удался", "url": "", "snippet": str(e), "fetched": False}]
            prev_results = []   # [{"stage": name, "node": name, "content": str}]
            for si, stage in enumerate(stages):
                if cancel_ev.is_set():
                    break
                names = ", ".join(config.nodes[n["key"]]["name"] for n in stage["nodes"])
                self._set_status(task_id, f"{stage['name']}: {names}…" if multi else f"Генерация: {names}…", stage=si + 1)

                def run_node(n, si=si, stage=stage):
                    nk = n["key"]
                    if not config.nodes[nk].get("enabled", True):
                        return {"error": "узел отключён", "node_key": nk, "node_name": config.nodes[nk]["name"]}
                    system = self._system_for(nk, prompt, skill, no_roles)
                    instr = "\n\n".join(x for x in (stage.get("instruction"), n.get("instruction")) if x)
                    user_parts = []
                    if instr:
                        user_parts.append(instr)
                    user_parts.append(prompt)
                    if si == 0 and llm_opts.get("_files"):
                        fs = config.settings.get("files", {})
                        blk = hub_files.files_block(FILES, llm_opts["_files"], int(fs.get("per_file_chars", 40000)), int(fs.get("total_chars", 120000)))
                        if blk:
                            user_parts.append(blk)
                    if search_ctx:
                        user_parts.append(search_ctx)
                    if si > 0 and stage.get("context", "all") != "none" and prev_results:
                        use = prev_results if stage.get("context") == "all" else [r for r in prev_results if r["stage_index"] == si - 1]
                        blocks = [f"--- {r['stage']} · {r['node']} ---\n{r['content'][:ctx_chars]}" for r in use]
                        user_parts.append("=== РЕЗУЛЬТАТЫ ПРЕДЫДУЩИХ ЭТАПОВ ===\n" + "\n\n".join(blocks))
                    user_content = "\n\n".join(user_parts)
                    messages = ([{"role": "system", "content": system}] if system else [])
                    if si == 0:
                        hist = threads.history_for(thread_id, nk, turns, exclude_task=task_id)
                        if hist and hist[-1]["role"] == "user":
                            hist[-1] = {"role": "user", "content": user_content}
                        else:
                            hist.append({"role": "user", "content": user_content})
                        messages += hist
                    else:
                        messages.append({"role": "user", "content": user_content})
                    if si == 0 and llm_opts.get("_files") and config.nodes[nk].get("vision"):
                        imgs = [FILES.image_data_url(f["id"]) for f in llm_opts["_files"] if f.get("kind") == "image"]
                        imgs = [u for u in imgs if u]
                        if imgs:
                            last = messages[-1]
                            last["content"] = [{"type": "text", "text": last["content"]}] + [{"type": "image_url", "image_url": {"url": u}} for u in imgs]
                    if llm_opts.get("_tools") and not multi:
                        if messages and messages[0]["role"] == "system":
                            messages[0] = {"role": "system", "content": messages[0]["content"] + "\n\n" + tools_system_prompt()}
                        else:
                            messages.insert(0, {"role": "system", "content": tools_system_prompt()})
                    extra = {"step_label": stage["name"], "step_index": si + 1} if multi else {}
                    if sources:
                        extra["sources"] = sources
                    res = self._run_one(thread_id, task_id, nk, messages, llm_opts, cancel_ev, extra=extra)
                    if llm_opts.get("_tools") and not multi and not res.get("error"):
                        res = self._tool_loop(thread_id, task_id, nk, messages, res, llm_opts, cancel_ev)
                    return res

                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(stage["nodes"]))) as ex:
                    futs = {ex.submit(run_node, n): n for n in stage["nodes"]}
                    stage_results = []
                    for f in futs:  # keep node order
                        try:
                            r = f.result()
                        except Exception as e:
                            r = {"error": str(e), "node_key": futs[f]["key"]}
                        r["step_label"] = stage["name"]
                        stage_results.append(r)
                responses += stage_results
                for r in stage_results:
                    if r.get("content"):
                        prev_results.append({"stage": stage["name"], "stage_index": si, "node": r.get("node_name", r.get("node_key")), "content": r["content"]})
        except Exception as e:
            threads.add_message(thread_id, {"role": "assistant", "content": "", "status": "error", "error": f"Сбой выполнения: {e}", "task_id": task_id})
        finally:
            legacy["status"] = "completed" if any("content" in r for r in responses) else "failed"
            legacy["responses"] = responses
            with self.lock:
                self.active.pop(task_id, None)
                self.cancel_flags.pop(task_id, None)


runner = TaskRunner()


# ---------------------------------------------------------------------------
# Per-node telemetry poller
# ---------------------------------------------------------------------------

def filter_sensors(sensors, gpu_match=""):
    """Node-scoped view of a host's sensors: keep only the GPU this node uses."""
    if not sensors:
        return {}
    out = copy.deepcopy(sensors)
    if gpu_match:
        kept = [g for g in out.get("gpus") or [] if gpu_match in (g.get("name") or "").lower()]
        if kept:
            out["gpus"] = kept
            gw = round(sum(g.get("power_w") or 0 for g in kept), 1)
            p = out.get("power") or {}
            p["gpu_w"] = gw or None
            p["total_w_est"] = round((p.get("cpu_w") or 0) + gw + (p.get("ram_w_est") or 0) + (p.get("platform_w_est") or 0), 1)
            p["shared_host"] = True
            out["power"] = p
    return out


class NodeTelemetry:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.hist = {}
        self.last_speed = {}
        self.last_llm_ping = {}
        self.host_hist = {"cpu": collections.deque(maxlen=120), "ram": collections.deque(maxlen=120)}

    def _h(self, key):
        n = int(config.settings["telemetry"].get("history_len", 60))
        if key not in self.hist or self.hist[key]["cpu"].maxlen != n:
            self.hist[key] = {k: collections.deque(maxlen=n) for k in ("cpu", "ram", "gpu", "vram", "lat", "llm_lat", "mbps")}
        return self.hist[key]

    def run(self):
        while True:
            t_settings = config.settings["telemetry"]
            try:
                host = agentlib.telemetry.get() or {}
                if host.get("cpu"):
                    self.host_hist["cpu"].append(host["cpu"]["percent"])
                    self.host_hist["ram"].append((host.get("ram") or {}).get("percent", 0))
            except Exception:
                host = {}
            keys = list(config.nodes.keys())
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(keys))) as ex:
                list(ex.map(lambda k: self._poll_node(k, host, t_settings), keys))
            time.sleep(max(0.5, float(t_settings.get("interval_s", 1.5))))

    def _poll_node(self, key, host, t_settings):
        cfg = config.nodes.get(key)
        if not cfg:
            return
        tel = resolve_telemetry(cfg)
        h = self._h(key)
        now = time.time()
        entry = {"node_key": key, "source": tel.get("type", "none"), "online": False, "updated_at": now_iso(), "cpu": None, "ram": None, "gpus": [], "net": {}, "error": ""}
        if now - self.last_llm_ping.get(key, 0) > float(t_settings.get("llm_ping_interval_s", 10)):
            self.last_llm_ping[key] = now
            r = ping_node(key)
            if r.get("status") == "online":
                h["llm_lat"].append(r["latency_ms"])
        entry["net"].update(llm_status=cfg.get("status", "unknown"), llm_latency_ms=cfg.get("latency_ms", 0), llm_error=cfg.get("last_error", ""))
        if tel.get("type") == "local":
            entry.update(online=True, hostname=host.get("hostname"), platform=host.get("platform"), cpu=host.get("cpu"), ram=dict(host.get("ram") or {}))
            match = (tel.get("gpu_match") or "").lower()
            entry["gpus"] = [g for g in host.get("gpus", []) if not match or match in g["name"].lower()]
            entry["sensors"] = filter_sensors(host.get("sensors") or {}, match)
            entry["net"].update(agent_latency_ms=0, down_mbps=None, local=True)
        elif tel.get("type") == "agent" and tel.get("url"):
            base = tel["url"]
            t0 = time.time()
            try:
                agent = http_get_json(base + "/telemetry", timeout=2.5)
                lat = max(1, int((time.time() - t0) * 1000))
                entry.update(online=True, hostname=agent.get("hostname"), platform=agent.get("platform"), cpu=agent.get("cpu"), ram=agent.get("ram"),
                             gpus=agent.get("gpus") or [], agent_version=agent.get("version"),
                             sensors=filter_sensors(agent.get("sensors") or {}, (tel.get("gpu_match") or "").lower()))
                entry["net"]["agent_latency_ms"] = lat
                h["lat"].append(lat)
                st_int = float(t_settings.get("speedtest_interval_s", 30))
                if st_int > 0 and now - self.last_speed.get(key, {}).get("ts", 0) > st_int:
                    self.last_speed.setdefault(key, {})["ts"] = now
                    threading.Thread(target=self._speedtest, args=(key, base, int(t_settings.get("speedtest_bytes", 2000000))), daemon=True).start()
            except Exception as e:
                entry["error"] = f"агент недоступен ({base}): {str(e)[:100]}"
                entry["net"]["agent_latency_ms"] = None
            sp = self.last_speed.get(key, {})
            entry["net"].update(down_mbps=sp.get("down_mbps"), up_mbps=sp.get("up_mbps"), speed_ts=sp.get("done_ts"), agent_url=base)
        else:
            entry["error"] = "телеметрия отключена для узла"
        if entry["cpu"]:
            h["cpu"].append(entry["cpu"].get("percent", 0) or 0)
        if entry["ram"]:
            h["ram"].append(entry["ram"].get("percent", 0) or 0)
        if entry["gpus"]:
            h["gpu"].append(max(g.get("util_percent", 0) or 0 for g in entry["gpus"]))
            h["vram"].append(max(g.get("mem_percent", 0) or 0 for g in entry["gpus"]))
        entry["history"] = {k: list(v) for k, v in h.items()}
        with self.lock:
            self.data[key] = entry

    def _speedtest(self, key, base, nbytes):
        try:
            t0 = time.time()
            req = urllib.request.Request(base + f"/speedtest?bytes={nbytes}", method="GET")
            with urllib.request.urlopen(req, timeout=20) as resp:
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
            down = round(total * 8 / max(0.001, time.time() - t0) / 1e6, 1)
            up = None
            try:
                t1 = time.time()
                req = urllib.request.Request(base + "/upload", data=b"\0" * nbytes, method="POST", headers={"Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    resp.read()
                up = round(nbytes * 8 / max(0.001, time.time() - t1) / 1e6, 1)
            except Exception:
                pass
            with self.lock:
                self.last_speed.setdefault(key, {}).update({"down_mbps": down, "up_mbps": up, "done_ts": now_iso()})
                self._h(key)["mbps"].append(down)
        except Exception:
            with self.lock:
                self.last_speed.setdefault(key, {}).update({"down_mbps": None, "up_mbps": None})

    def snapshot(self):
        with self.lock:
            host = dict(agentlib.telemetry.get() or {})
            host["cpu"] = host.get("cpu") or {"percent": 0, "name": agentlib.cpu_brand()}
            host["history"] = {"cpu": list(self.host_hist["cpu"]), "ram": list(self.host_hist["ram"])}
            return {"nodes": copy.deepcopy(self.data), "host": host, "ts": now_iso()}

    def hardware_for(self, key):
        with self.lock:
            d = self.data.get(key) or {}
        return {"gpus": d.get("gpus") or [], "ram": d.get("ram") or {}, "cpu": d.get("cpu") or {}, "platform": d.get("platform") or "", "hostname": d.get("hostname") or "", "online": d.get("online", False)}


node_telemetry = NodeTelemetry()


# ---------------------------------------------------------------------------
# Clients registry (+ Tailscale enrichment, admin flag)
# ---------------------------------------------------------------------------

def is_admin_ip(ip):
    """IP-based admin check (used when password auth is off, and for setup)."""
    adm = config.settings.get("admin") or {}
    if not adm.get("enabled", True):
        return True
    if ip in ("127.0.0.1", "::1", "localhost") or ip in host_ips() or ip in (adm.get("extra_ips") or []):
        return True
    return False


def tailscale_cached():
    return agentlib.cached("ts", 20, agentlib.tailscale_status)


def tailscale_peer_for_ip(ip):
    ts = tailscale_cached() or {}
    if not ts.get("available"):
        return None
    for p in [ts.get("self")] + (ts.get("peers") or []):
        if p and ip in (p.get("ips") or []):
            return p
    return None


_geo_cache = {}


def geo_lookup(ip):
    if not ip or agentlib._is_private(ip):
        return None
    c = _geo_cache.get(ip)
    if c and time.time() - c[0] < 3600:
        return c[1]
    try:
        d = http_get_json(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org,as,lat,lon,timezone,query&lang=ru", timeout=5)
        val = d if d.get("status") == "success" else None
    except Exception:
        val = None
    _geo_cache[ip] = (time.time(), val)
    return val


class ClientRegistry:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = {}

    def touch(self, ip, headers, path, role=None):
        cid = headers.get("X-BF-Client-Id") or ""
        name = headers.get("X-BF-Client-Name") or ""
        ua = headers.get("User-Agent", "")[:200]
        if not cid:
            cid = "anon:" + ip + ":" + str(abs(hash(ua)) % 10000)
        try:
            name = urllib.parse.unquote(name)
        except Exception:
            pass
        with self.lock:
            c = self.clients.get(cid)
            if not c:
                c = {"id": cid, "name": name or "", "ip": ip, "user_agent": ua, "first_seen": now_iso(), "last_seen": now_iso(), "requests": 0,
                     "tasks": 0, "last_path": path, "device": guess_device(ua), "device_info": {}, "is_admin": role == "admin"}
                self.clients[cid] = c
            c.update(last_seen=now_iso(), last_seen_ts=time.time(), ip=ip, is_admin=role == "admin")
            if name:
                c["name"] = name
            c["requests"] += 1
            if not path.startswith(("/api/state", "/api/telemetry", "/api/threads/", "/api/clients", "/api/models/downloads")):
                c["last_path"] = path
            if path == "/api/execute":
                c["tasks"] += 1
            if len(self.clients) > 200:
                for o in sorted(self.clients.values(), key=lambda x: x.get("last_seen_ts", 0))[:50]:
                    self.clients.pop(o["id"], None)
            return c

    def set_device_info(self, cid, info):
        with self.lock:
            c = self.clients.get(cid)
            if c and isinstance(info, dict):
                c["device_info"] = {k: (str(v)[:120] if not isinstance(v, (int, float, bool)) else v) for k, v in info.items() if k in
                                    ("platform", "os", "browser", "screen", "language", "timezone", "cores", "memory_gb", "touch", "connection", "brands", "mobile", "model")}

    def list(self, with_ts=True):
        with self.lock:
            now = time.time()
            out = []
            for c in self.clients.values():
                d = dict(c)
                idle = now - c.get("last_seen_ts", 0)
                d["idle_s"] = int(idle)
                d["active"] = idle < 15
                d["is_anon"] = c["id"].startswith("anon:")
                if with_ts:
                    p = tailscale_peer_for_ip(c["ip"])
                    d["tailscale"] = {"hostname": p.get("hostname"), "os": p.get("os"), "dns_name": p.get("dns_name"), "online": p.get("online"), "cur_addr": p.get("cur_addr")} if p else None
                out.append(d)
            out.sort(key=lambda x: (not x["active"], x["idle_s"]))
            return out

    def forget(self, cid):
        with self.lock:
            self.clients.pop(cid, None)


def guess_device(ua):
    u = ua.lower()
    if "iphone" in u:
        return "iPhone"
    if "ipad" in u:
        return "iPad"
    if "android" in u:
        return "Android"
    if "macintosh" in u or "mac os" in u:
        return "macOS"
    if "windows" in u:
        return "Windows"
    if "linux" in u:
        return "Linux"
    if "python" in u or "curl" in u:
        return "script"
    return "?"


clients = ClientRegistry()


# ---------------------------------------------------------------------------
# Node management (local via agentlib, remote via agent HTTP)
# ---------------------------------------------------------------------------

def node_is_local(cfg):
    return resolve_telemetry(cfg).get("type") == "local"


def node_exec(node_key, action, args=None, timeout=120):
    cfg = config.nodes.get(node_key)
    if not cfg:
        return {"ok": False, "error": "unknown node"}
    if node_is_local(cfg):
        return agentlib.run_action(action, args or {}, local=True)
    url = agent_url_for(cfg)
    if not url:
        return {"ok": False, "error": "у узла нет агента (задайте URL агента в настройках узла)"}
    try:
        payload = {"action": action, "args": args or {}}
        raw = json.dumps(payload).encode("utf-8")
        return http_post_json(url + "/exec", payload, timeout=timeout, headers=agent_headers("POST", "/exec", raw), raw=raw)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"agent HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"агент недоступен: {e}"}


def node_sysinfo(node_key, light=False):
    cfg = config.nodes.get(node_key)
    if not cfg:
        return {"error": "unknown node"}
    if node_is_local(cfg):
        return agentlib.sysinfo(light=light)
    url = agent_url_for(cfg)
    if not url:
        return {"error": "нет агента"}
    try:
        return http_get_json(url + "/sysinfo" + ("?light=1" if light else ""), timeout=8, headers=agent_headers("GET", "/sysinfo"))
    except Exception as e:
        return {"error": f"агент недоступен: {e}"}


def node_downloads(node_key):
    cfg = config.nodes.get(node_key)
    if not cfg:
        return []
    if node_is_local(cfg):
        return agentlib.downloads.list()
    url = agent_url_for(cfg)
    if not url:
        return []
    try:
        return http_get_json(url + "/downloads", timeout=3).get("downloads", [])
    except Exception:
        return []


_sysinfo_cache = {}


def node_sysinfo_cached(key, ttl=30, light=True):
    c = _sysinfo_cache.get((key, light))
    if c and time.time() - c[0] < ttl:
        return c[1]
    v = node_sysinfo(key, light=light)
    _sysinfo_cache[(key, light)] = (time.time(), v)
    return v


def admin_overview():
    ts = tailscale_cached() or {}
    host_si = agentlib.sysinfo(light=True)
    host_geo = geo_lookup((host_si.get("network") or {}).get("public_ip"))
    nodes = {}

    def one(key):
        cfg = config.nodes[key]
        si = node_sysinfo_cached(key, ttl=30, light=True)
        net = (si or {}).get("network") or {}
        peer = tailscale_peer_for_ip(endpoint_host(cfg["endpoint"]))
        pub = net.get("public_ip")
        return key, {"node": config.node_public(key), "sysinfo": si if not si.get("error") else None, "error": si.get("error"),
                     "tailscale_peer": peer, "geo": geo_lookup(pub) if pub else None, "hardware": node_telemetry.hardware_for(key)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for key, val in ex.map(one, list(config.nodes.keys())):
            nodes[key] = val
    return {"tailscale": ts, "host": {"sysinfo": host_si, "geo": host_geo}, "nodes": nodes, "actions": agentlib.ACTIONS, "ts": now_iso()}


ADMIN_ASK_SYSTEM = """Ты — помощник администратора кластера LLM-узлов. Тебе дают команду администратора на естественном языке и список доступных действий.
Выбери ОДНО подходящее действие и его аргументы. Верни ТОЛЬКО JSON без пояснений вне JSON:
{"action": "<имя действия или null>", "args": {...}, "explanation": "<одно предложение, что будет сделано и почему>", "confidence": 0.0-1.0}
Если команда не соответствует ни одному действию, небезопасна или неоднозначна — action: null и объясни, чего не хватает.
Идентификаторы моделей бери ТОЧНО из контекста узла. Никогда не придумывай действия, которых нет в списке."""


def admin_ask(node_key, text, via_node=None):
    cfg = config.nodes.get(node_key)
    if not cfg:
        return {"error": "unknown node"}
    si = node_sysinfo_cached(node_key, ttl=60, light=False)
    lms = (si or {}).get("lms") or {}
    ctx = {"node": cfg["name"], "endpoint": cfg["endpoint"], "platform": (si or {}).get("platform"), "kind": cfg.get("kind"),
           "models": [m.get("modelKey") for m in (lms.get("models") or [])][:60], "loaded": [m.get("modelKey") for m in (lms.get("loaded") or [])],
           "api_models": [m["id"] for m in (list_node_models(node_key).get("models") or [])][:60], "launch": cfg.get("launch") or {}}
    actions_text = "\n".join(f"- {k}: {v}" for k, v in agentlib.ACTIONS.items())
    user = f"ДОСТУПНЫЕ ДЕЙСТВИЯ:\n{actions_text}\n\nКОНТЕКСТ УЗЛА:\n{json.dumps(ctx, ensure_ascii=False)}\n\nКОМАНДА АДМИНИСТРАТОРА:\n{text}"
    executor = via_node if via_node in config.nodes else (config.settings.get("admin", {}).get("command_node") or node_key)
    if executor not in config.nodes or not config.nodes[executor].get("enabled", True):
        # fall back to any enabled node (the target node's LLM may be off)
        executor = next((k for k in config.nodes if config.nodes[k].get("enabled", True)), node_key)
    r = quick_llm(executor, ADMIN_ASK_SYSTEM, user, max_tokens=400, temperature=0.0,
                  timeout_s=int(config.nodes[executor].get("timeout_s") or config.llm().get("timeout_s") or 180))
    if "error" in r:
        return {"error": r["error"], "executor": executor}
    parsed = extract_json(r.get("content", "")) or {}
    action = parsed.get("action")
    if action and action not in agentlib.ACTIONS:
        parsed["explanation"] = f"Модель предложила неизвестное действие «{action}»: " + str(parsed.get("explanation", ""))
        parsed["action"] = None
    return {"proposal": parsed, "raw": r.get("content", ""), "executor": executor, "latency_ms": r.get("latency_ms")}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "BlackFoxHub/3.0"
    # HTTP/1.1 keeps connections alive: without it every response closed the socket,
    # which breaks TLS clients that reuse pooled connections (PowerShell/.NET).
    # Every response below sets Content-Length, so keep-alive is safe.
    protocol_version = "HTTP/1.1"

    # http.server не знает про .webmanifest — без типа браузер игнорирует манифест
    extensions_map = dict(http.server.SimpleHTTPRequestHandler.extensions_map,
                          **{".webmanifest": "application/manifest+json", ".mjs": "text/javascript"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def address_string(self):
        return str(self.client_address[0])

    def log_message(self, fmt, *args):
        pass

    def _origin_ok(self):
        """Защита от CSRF: чужой сайт, открытый в браузере пользователя, не управляет хабом.
        Origin либо отсутствует (не браузер), либо совпадает с адресом хаба, либо это
        оболочка приложения / прокси ядра на этой машине (APP_ORIGIN_RE)."""
        origin = (getattr(self, "headers", None) or {}).get("Origin")
        if origin is None:
            return True
        if APP_ORIGIN_RE.match(origin):
            return True
        netloc = urllib.parse.urlsplit(origin).netloc.lower()
        return bool(netloc) and netloc == (self.headers.get("Host") or "").lower()

    def end_headers(self):
        origin = (getattr(self, "headers", None) or {}).get("Origin")
        if origin and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")   # клиент не будет слать следующий запрос в закрываемое соединение
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-BF-Client-Id, X-BF-Client-Name")
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        elif self.path.split("?")[0].endswith((".js", ".css", ".html", ".webmanifest", "/")):
            # the SPA must never run a stale build against a newer hub
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")   # keep-alive: без длины клиент ждал бы тело до закрытия соединения
        self.end_headers()

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", code)

    def _send_bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _touch(self):
        path = self.path.split("?")[0]
        if not path.startswith("/api/"):
            return None
        try:
            return clients.touch(self.client_address[0], self.headers, path, client_role(self))
        except Exception:
            return None

    def _you(self, client, role=None):
        ip = self.client_address[0]
        role = role or client_role(self) or "user"
        return {"ip": ip, "role": role, "is_admin": role == "admin", "id": (client or {}).get("id", ""),
                "name": (client or {}).get("name", ""), "auth_enabled": bool(sec().get("enabled"))}

    def _reject(self, data, code):
        # Тело запроса ещё не прочитано: на keep-alive соединении оно стало бы началом
        # следующего запроса (400 Bad Request) — поэтому после отказа соединение закрывается.
        self.close_connection = True
        self._json(data, code)
        return None, True

    def _gate(self, path):
        """Network allowlist + authentication. Returns (role, error_response_sent)."""
        ip = self.client_address[0]
        if not network_allowed(ip):
            security.audit("blocked_network", ip, path)
            return self._reject({"error": "Подключение из этой сети запрещено настройками безопасности"}, 403)
        if not self._origin_ok():
            security.audit("blocked_origin", ip, "%s <- %s" % (path, self.headers.get("Origin")))
            return self._reject({"error": "Запрос с чужого сайта отклонён"}, 403)
        if not path.startswith("/api/") or path in AUTH_FREE or path == "/api/speedtest":
            return client_role(self) or "anon", False
        role = client_role(self)
        if role is None:
            return self._reject({"error": "Требуется вход", "auth_required": True}, 401)
        return role, False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        role, stop = self._gate(path)
        if stop:
            return
        client = self._touch()
        admin = role == "admin"
        try:
            if path == "/api/state":
                return self._json({"nodes": config.nodes_public(), "threads": threads.summaries(), "active": runner.active_info(), "is_busy": runner.is_busy(),
                                   "active_task_info": (runner.active_info() or [None])[0], "tasks": list(runner.legacy_tasks),
                                   "tunnel_host": config.settings["connection"]["tunnel_host"], "port": config.settings["connection"]["port"],
                                   "settings": redact_settings(config.settings, role), "you": self._you(client, role), "server_time": now_iso()})
            if path == "/api/telemetry":
                return self._json(node_telemetry.snapshot())
            if path.startswith("/updates/"):
                # обновления приложения (ADR §6): data/updates/latest.json + подписанные артефакты;
                # без входа — целостность гарантирует подпись updater'а, а сеть — allowlist
                name = os.path.basename(urllib.parse.unquote(path[len("/updates/"):]))
                p = DATA_DIR / "updates" / name if name else None
                if not p or not p.is_file():
                    return self._json({"error": "нет такого обновления"}, 404)
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json" if name.endswith(".json") else "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/files/get":
                m = FILES.get((q.get("id") or [""])[0])
                p = FILES.path(m["id"]) if m else None
                if not m or not p or not p.exists():
                    return self._json({"error": "нет такого файла"}, 404)
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", hub_files.IMAGE_MIME.get(m["ext"], "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(m["name"]))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/threads":
                return self._json({"threads": threads.summaries()})
            if path.startswith("/api/threads/"):
                tid = path.split("/")[3]
                t = threads.get(tid)
                if not t:
                    return self._json({"error": "not found"}, 404)
                return self._json({"thread": t, "busy": runner.thread_busy(tid), "active": [a for a in runner.active_info() if a["thread_id"] == tid]})
            if path == "/api/node/models":
                key = (q.get("node_key") or [""])[0]
                if key not in config.nodes:
                    return self._json({"error": "Unknown node"}, 404)
                return self._json(list_node_models(key, force=bool(q.get("force"))))
            if path == "/api/node/recommend":
                key = (q.get("node_key") or [""])[0]
                if key not in config.nodes:
                    return self._json({"error": "Unknown node"}, 404)
                hw = node_telemetry.hardware_for(key)
                models = list_node_models(key).get("models") or []
                return self._json({"hardware": hw, "recommendation": recommend_node(hw, models, config.nodes[key].get("kind", ""))})
            if path == "/api/clients":
                return self._json({"clients": clients.list(), "you": self._you(client, role)})
            if path == "/api/settings":
                return self._json({"settings": redact_settings(config.settings, role), "defaults": DEFAULT_SETTINGS, "builtin_skills": BUILTIN_SKILLS, "role_library": ROLE_LIBRARY})
            if path == "/api/skills":
                return self._json({"skills": [s for s in config.settings.get("skills", []) if not s.get("hidden")]})
            if path == "/api/models/catalog":
                key = (q.get("node_key") or [""])[0]
                if key not in config.nodes:
                    return self._json({"error": "Unknown node"}, 404)
                cfg = config.nodes[key]
                api = list_node_models(key, force=bool(q.get("force")))
                hw = node_telemetry.hardware_for(key)
                out = {"node": config.node_public(key), "api_models": api.get("models") or [], "api_error": api.get("error"), "kind": cfg.get("kind", ""),
                       "hardware": hw, "bandwidth_gbs": gpu_bandwidth(((max(hw["gpus"], key=lambda g: g.get("mem_total_mb", 0)) if hw["gpus"] else {}).get("name")) or (hw.get("cpu") or {}).get("name") or ""),
                       "lms": None, "downloads": node_downloads(key) if admin else [], "is_admin": admin}
                if admin:
                    si = node_sysinfo_cached(key, ttl=20, light=False)
                    if si and not si.get("error"):
                        out["lms"] = si.get("lms")
                        out["disks"] = si.get("disks")
                        out["processes"] = si.get("processes")
                    else:
                        out["sysinfo_error"] = (si or {}).get("error")
                return self._json(out)
            if path == "/api/models/downloads":
                if not admin:
                    return self._json({"error": "admin only"}, 403)
                return self._json({"downloads": {k: node_downloads(k) for k in config.nodes}})
            if path == "/api/models/hf_search":
                if not admin:
                    return self._json({"error": "admin only"}, 403)
                query = (q.get("q") or [""])[0].strip()
                fmt = (q.get("format") or ["gguf"])[0]
                limit = min(30, int((q.get("limit") or ["15"])[0]))
                if not query:
                    return self._json({"models": []})
                url = f"https://huggingface.co/api/models?search={urllib.parse.quote(query)}&filter={urllib.parse.quote(fmt)}&limit={limit}&sort=downloads&direction=-1"
                try:
                    data = http_get_json(url, timeout=12, headers={"User-Agent": UA})
                    models = [{"id": m.get("id") or m.get("modelId"), "downloads": m.get("downloads"), "likes": m.get("likes"), "tags": [t for t in (m.get("tags") or []) if t in ("gguf", "mlx", "safetensors")][:3],
                               "pipeline": m.get("pipeline_tag"), "params_b": parse_params_b(m.get("id") or "")} for m in data]
                    return self._json({"models": models})
                except Exception as e:
                    return self._json({"models": [], "error": str(e)})
            if path == "/api/models/hf_files":
                if not admin:
                    return self._json({"error": "admin only"}, 403)
                repo = (q.get("repo") or [""])[0].strip()
                try:
                    data = http_get_json(f"https://huggingface.co/api/models/{urllib.parse.quote(repo)}?blobs=true", timeout=12, headers={"User-Agent": UA})
                    files = []
                    for s in data.get("siblings") or []:
                        fn = s.get("rfilename") or ""
                        if fn.lower().endswith((".gguf", ".safetensors")) or "mlx" in fn.lower() or fn.endswith("config.json"):
                            files.append({"file": fn, "size_bytes": s.get("size"), "quant": parse_quant(fn), "url": f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(fn)}"})
                    files.sort(key=lambda f: (f["file"].count("/"), f["file"]))
                    return self._json({"repo": repo, "files": files, "params_b": parse_params_b(repo), "tags": (data.get("tags") or [])[:20]})
                except Exception as e:
                    return self._json({"files": [], "error": str(e)})
            if path == "/api/admin/overview":
                if not admin:
                    return self._json({"error": "admin only"}, 403)
                return self._json(admin_overview())
            if path == "/api/admin/node":
                if not admin:
                    return self._json({"error": "admin only"}, 403)
                key = (q.get("node_key") or [""])[0]
                if key not in config.nodes:
                    return self._json({"error": "Unknown node"}, 404)
                si = node_sysinfo(key, light=False)
                net = (si or {}).get("network") or {}
                return self._json({"node": config.node_public(key), "sysinfo": si, "tailscale_peer": tailscale_peer_for_ip(endpoint_host(config.nodes[key]["endpoint"])),
                                   "geo": geo_lookup(net.get("public_ip")) if net.get("public_ip") else None, "actions": agentlib.ACTIONS, "hardware": node_telemetry.hardware_for(key)})
            if path in ("/node_agent.py", "/agent.py"):
                return self._send_bytes((BASE_DIR / "node_agent.py").read_bytes(), "text/plain; charset=utf-8")
            if path in ("/install-agent.ps1", "/install-agent.sh"):
                hub = hub_public_url(self.headers.get("Host"))
                port = int((q.get("port") or [config.settings["telemetry"].get("agent_port", 8766)])[0])
                # Установщик несёт секрет кластера открытым текстом, поэтому доступ к нему
                # имеют: машина-администратор, вошедший админ и одноразовый ключ установки,
                # выпущенный админом для конкретного нового узла.
                itok = (self.headers.get("X-Install-Token") or (q.get("t") or [""])[0] or "").strip()
                # при включённой защите IP хаба ничего не даёт — только сессия администратора или ключ
                allowed = client_role(self) == "admin"   # без входа по паролю — машина-администратор (is_admin_ip)
                if not allowed and itok:
                    ok_tok, why_tok = security.use_install_token(itok, self.client_address[0])
                    if ok_tok:
                        allowed = True
                        security.audit("install_token_used", self.client_address[0], path)
                    else:
                        security.audit("installer_denied", self.client_address[0], f"{path}: {why_tok}")
                        return self._send_bytes(("# " + why_tok + chr(10) + "# Выпустите новый ключ: Настройки -> Узлы -> Установить агент" + chr(10)).encode("utf-8"),
                                                "text/plain; charset=utf-8", 403)
                if not allowed:
                    security.audit("installer_denied", self.client_address[0], path + ": нет ключа установки")
                    msg = ("# Установщик доступен администратору кластера или по одноразовому ключу." + chr(10) +
                           "# Откройте интерфейс -> Настройки -> Узлы -> «Установить агент»: там готовая команда с ключом." + chr(10))
                    return self._send_bytes(msg.encode("utf-8"), "text/plain; charset=utf-8", 403)
                secret = security.agent_secret()
                token = config.settings["telemetry"].get("agent_token") or ""
                pins = cert_pins() if scheme_is_https() else {}
                if not pins.get("self_signed"):
                    pins = {}   # публично доверенный сертификат: проверяется обычным способом
                body = (INSTALL_PS1 if path.endswith(".ps1") else INSTALL_SH)
                body = (body.replace("%HUB%", hub).replace("%PORT%", str(port)).replace("%TOKEN%", token)
                            .replace("%SECRET%", secret).replace("%FP%", pins.get("sha256", "")).replace("%PIN%", pins.get("spki", "")))
                security.audit("installer_served", self.client_address[0], path)
                raw = body.encode("utf-8")
                if path.endswith(".ps1"):
                    # PowerShell 5.1 без BOM читает .ps1 как ANSI — кириллица ломает разбор скрипта
                    raw = codecs.BOM_UTF8 + raw
                return self._send_bytes(raw, "text/plain; charset=utf-8")
            if path == "/api/speedtest":
                n = min(20_000_000, max(1000, int((q.get("bytes") or ["1000000"])[0])))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(n))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                chunk = os.urandom(65536)
                sent = 0
                while sent < n:
                    piece = chunk[: min(len(chunk), n - sent)]
                    self.wfile.write(piece)
                    sent += len(piece)
                return
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        role, stop = self._gate(path)
        if stop:
            return
        client = self._touch()
        body = self._read_body()
        admin = role == "admin"
        self.role = role
        try:
            handler = POST_ROUTES.get(path)
            if handler is None:
                return self._json({"error": "Not found"}, 404)
            if path in ADMIN_ROUTES and not admin:
                security.audit("denied_admin_route", self.client_address[0], path, role)
                return self._json({"error": "Доступно только администратору"}, 403)
            code, data = handler(body, self, client)
            return self._json(data, code)
        except Exception as e:
            return self._json({"error": str(e)}, 500)


# --- POST handlers: (body, handler, client) -> (code, data)

def r_execute(body, h, client):
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return 400, {"status": "error", "message": "Prompt is required"}
    tid = body.get("thread_id")
    if not tid or not threads.get(tid):
        t = threads.create(owner_id=(client or {}).get("id", ""), owner_name=(client or {}).get("name", ""))
        tid = t["id"]
    if runner.thread_busy(tid):
        return 409, {"status": "error", "message": "В этой ветке уже выполняется задача"}
    mode = body.get("mode", "single")
    target = body.get("target_node", "node_1")
    if "plan" in body and body["plan"]:
        threads.set_meta(tid, plan=normalize_plan(body["plan"]))
    overrides = {k: body.get(k) for k in ("max_tokens", "temperature", "top_p", "reasoning_effort", "stream", "timeout_s", "history_turns", "skill", "no_roles", "council") if k in body}
    if body.get("files"):
        metas = [FILES.get(fid) for fid in body["files"] if isinstance(fid, str)]
        overrides["files"] = [FILES.public(m) for m in metas if m]
    if body.get("tools"):
        if getattr(h, "role", None) != "admin":
            return 403, {"status": "error", "message": "«Агент ПК» доступен только администратору"}
        overrides["tools"] = True
    task_id, err = runner.start(tid, mode, target, prompt, overrides, client=client)
    if err:
        return 400, {"status": "error", "message": err}
    return 200, {"status": "started", "task_id": task_id, "thread_id": tid}


def r_files_upload(body, h, client):
    name = (body.get("name") or "").strip()
    data = body.get("data") or ""
    if not name or not data:
        return 400, {"error": "нужны name и data (base64)"}
    try:
        raw = base64.b64decode(data.split(",", 1)[-1], validate=False)
    except Exception:
        return 400, {"error": "data не base64"}
    FILES.max_bytes = int(config.settings.get("files", {}).get("max_mb", 25)) * 1024 * 1024
    try:
        m = FILES.add(name, raw, thread_id=body.get("thread_id") or "", owner_id=(client or {}).get("id", ""), owner_name=(client or {}).get("name", ""))
    except ValueError as e:
        return 413, {"error": str(e)}
    security.audit("file_upload", h.client_address[0], f"{m['name']} ({m['size']} б, {m['kind']})", getattr(h, "role", ""))
    return 200, {"file": FILES.public(m), "preview": FILES.text(m["id"])[:400]}


def r_files_list(body, h, client):
    return 200, {"files": [FILES.public(m) for m in FILES.list(body.get("thread_id"))]}


def r_files_delete(body, h, client):
    m = FILES.get(body.get("id") or "")
    if not m:
        return 404, {"error": "нет такого файла"}
    if getattr(h, "role", None) != "admin" and (not m.get("owner_id") or m["owner_id"] != (client or {}).get("id")):
        return 403, {"error": "чужой файл может удалить только администратор"}
    FILES.delete(m["id"])
    return 200, {"status": "ok"}


def r_tools_exec(body, h, client):
    """Ручное выполнение команды администратором (без LLM)."""
    key = body.get("node_key", "")
    cfg = config.nodes.get(key)
    if not cfg:
        return 404, {"error": "Unknown node"}
    if not cfg.get("exec_enabled"):
        return 403, {"error": "на этом узле выполнение команд выключено"}
    cmd = (body.get("command") or "").strip()
    if not cmd:
        return 400, {"error": "command required"}
    shell = body.get("shell") or "powershell"
    ts = config.settings.get("tools", {})
    security.audit("tool_exec", h.client_address[0], f"{key} [{shell}] (вручную): {cmd[:300]}", "admin")
    r = node_exec(key, "shell", {"shell": shell, "command": cmd, "timeout": int(body.get("timeout") or ts.get("timeout_s", 120))},
                  timeout=int(body.get("timeout") or ts.get("timeout_s", 120)) + 15)
    return 200, {"node_key": key, "result": r}


def r_tools_approve(body, h, client):
    ok = runner.approve(body.get("call_id") or "", bool(body.get("approve")), by=(client or {}).get("name", "") or h.client_address[0])
    return (200, {"status": "ok"}) if ok else (404, {"error": "команда уже выполнена или не найдена"})


def r_cancel(body, h, client):
    runner.cancel(task_id=body.get("task_id"), thread_id=body.get("thread_id"))
    return 200, {"status": "ok"}


def r_clear(body, h, client):
    tid = body.get("thread_id")
    if tid:
        threads.clear(tid)
    else:
        runner.legacy_tasks.clear()
    return 200, {"status": "cleared"}


def r_thread_create(body, h, client):
    t = threads.create(body.get("title") or "Новый чат", body.get("mode", "single"), body.get("node", "node_1"),
                       owner_id=(client or {}).get("id", ""), owner_name=(client or {}).get("name", ""))
    return 200, {"status": "ok", "thread": t}


def r_thread_delete(body, h, client):
    tid = body.get("thread_id")
    if tid:
        runner.cancel(thread_id=tid)
        threads.delete(tid)
    return 200, {"status": "ok", "threads": threads.summaries()}


def r_thread_rename(body, h, client):
    threads.rename(body.get("thread_id", ""), body.get("title", ""))
    return 200, {"status": "ok"}


def r_thread_meta(body, h, client):
    kw = {k: body[k] for k in ("mode", "node", "pinned", "no_roles", "skill", "council") if k in body}
    if "plan" in body:
        kw["plan"] = normalize_plan(body["plan"]) if body["plan"] else None
    threads.set_meta(body.get("thread_id", ""), **kw)
    return 200, {"status": "ok", "thread": threads.get(body.get("thread_id", ""))}


def r_thread_clear(body, h, client):
    tid = body.get("thread_id", "")
    runner.cancel(thread_id=tid)
    threads.clear(tid)
    return 200, {"status": "ok"}


def r_message_delete(body, h, client):
    threads.delete_message(body.get("thread_id", ""), body.get("message_id", ""))
    return 200, {"status": "ok"}


def r_node_toggle(body, h, client):
    key = body.get("node_key", "")
    if key not in config.nodes:
        return 404, {"status": "error", "message": "Unknown node"}
    config.update_node(key, {"enabled": bool(body.get("enabled", True))})
    if body.get("enabled", True):
        threading.Thread(target=ping_node, args=(key,), daemon=True).start()
    return 200, {"status": "ok", "node_key": key, "enabled": bool(body.get("enabled", True))}


def r_node_ping(body, h, client):
    key = body.get("node_key", "")
    if key not in config.nodes:
        return 404, {"status": "error", "message": "Unknown node"}
    return 200, {"status": "ok", "node_key": key, "result": ping_node(key)}


def r_node_update(body, h, client):
    key = body.get("node_key", "")
    if key not in config.nodes:
        return 404, {"status": "error", "message": "Unknown node"}
    config.update_node(key, body)
    if "endpoint" in body or "model" in body:
        with _models_cache_lock:
            _models_cache.pop(key, None)
        _sysinfo_cache.clear()
        threading.Thread(target=ping_node, args=(key,), daemon=True).start()
    return 200, {"status": "ok", "node": config.node_public(key)}


def r_node_set_model(body, h, client):
    key = body.get("node_key", "")
    model = (body.get("model") or "").strip()
    if key not in config.nodes or not model:
        return 400, {"status": "error", "message": "node_key and model required"}
    config.update_node(key, {"model": model})
    result = {"status": "ok", "node_key": key, "model": model}
    if body.get("warmup"):
        res = query_llm(key, [{"role": "user", "content": "ping"}], {"max_tokens": 1, "temperature": 0, "stream": False, "timeout_s": 300, "reasoning_effort": "off"})
        result["warmup"] = {"ok": "error" not in res, "latency_ms": res.get("latency_ms"), "error": res.get("error")}
        with _models_cache_lock:
            _models_cache.pop(key, None)
    return 200, result


def r_node_reset_prompt(body, h, client):
    key = body.get("node_key", "")
    if key not in config.nodes:
        return 404, {"status": "error", "message": "Unknown node"}
    config.reset_node_prompt(key)
    return 200, {"status": "ok", "node": config.node_public(key)}


def r_node_test(body, h, client):
    key = body.get("node_key", "")
    if key not in config.nodes:
        return 404, {"status": "error", "message": "Unknown node"}
    sys_p = body.get("system_prompt") or config.nodes[key].get("system_prompt") or ""
    prompt = body.get("prompt") or "Кратко представься."
    if config.settings["llm"].get("force_language", True):
        sys_p = (sys_p + "\n\n" + language_rule(prompt)).strip()
    opts = config.llm()
    opts.update({"stream": False, "max_tokens": min(512, int(opts.get("max_tokens", 512)))})
    msgs = ([{"role": "system", "content": sys_p}] if sys_p else []) + [{"role": "user", "content": prompt}]
    return 200, query_llm(key, msgs, opts)


def r_node_probe(body, h, client):
    """Probe an endpoint before adding it: models, kind, agent hardware, recommendations."""
    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        return 400, {"error": "endpoint required"}
    if not endpoint.endswith("/chat/completions"):
        endpoint = node_base_url(endpoint) + "/v1/chat/completions"
    out = {"endpoint": endpoint, "reachable": False, "models": [], "kind": "", "hardware": None, "agent_url": "", "agent_ok": False}
    err = tcp_reachable(endpoint, timeout=3.0)
    if err:
        out["error"] = f"недоступен: {err}"
        return 200, out
    out["reachable"] = True
    try:
        out["models"], out["kind"] = probe_models(endpoint)
    except Exception as e:
        out["models_error"] = str(e)
    host = endpoint_host(endpoint)
    local = is_local_endpoint(endpoint)
    out["is_local"] = local
    if local:
        t = agentlib.telemetry.get() or {}
        out["hardware"] = {"gpus": t.get("gpus") or [], "ram": t.get("ram") or {}, "cpu": t.get("cpu") or {}, "platform": t.get("platform"), "hostname": t.get("hostname")}
        out["agent_ok"] = True
    else:
        agent_url = (body.get("agent_url") or "").strip() or f"http://{host}:{config.settings['telemetry'].get('agent_port', 8766)}"
        out["agent_url"] = agent_url
        try:
            t = http_get_json(agent_url + "/telemetry", timeout=4)
            out["hardware"] = {"gpus": t.get("gpus") or [], "ram": t.get("ram") or {}, "cpu": t.get("cpu") or {}, "platform": t.get("platform"), "hostname": t.get("hostname")}
            out["agent_ok"] = True
        except Exception as e:
            out["agent_error"] = str(e)
    peer = tailscale_peer_for_ip(host)
    if peer:
        out["tailscale_peer"] = peer
        if not out["hardware"]:
            out["hardware"] = {"gpus": [], "ram": {}, "cpu": {}, "platform": peer.get("os"), "hostname": peer.get("hostname")}
    hw = out["hardware"] or {"gpus": [], "ram": {}, "cpu": {}, "platform": ""}
    if body.get("manual_hw"):
        mh = body["manual_hw"]
        if mh.get("vram_gb"):
            hw["gpus"] = [{"name": mh.get("gpu_name") or "GPU", "mem_total_mb": int(float(mh["vram_gb"]) * 1024), "unified": bool(mh.get("unified"))}]
        if mh.get("ram_gb"):
            hw["ram"] = {"total_gb": float(mh["ram_gb"])}
    out["recommendation"] = recommend_node(hw, out["models"], out["kind"])
    return 200, out


def r_node_add(body, h, client):
    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        return 400, {"error": "endpoint required"}
    if not endpoint.endswith("/chat/completions"):
        endpoint = node_base_url(endpoint) + "/v1/chat/completions"
    body["endpoint"] = endpoint
    if body.get("agent_url"):
        body["telemetry"] = {"type": "agent", "url": body["agent_url"].strip()}
    key = config.add_node(body)
    threading.Thread(target=ping_node, args=(key,), daemon=True).start()
    if body.get("add_to_pipeline"):
        config.settings["pipeline"].append({"node": key, "label": f"Шаг {len(config.settings['pipeline']) + 1}: {config.nodes[key]['name']}", "prompt": ""})
        config.save()
    return 200, {"status": "ok", "key": key, "node": config.node_public(key)}


def r_node_delete(body, h, client):
    key = body.get("node_key", "")
    if not config.delete_node(key):
        return 400, {"error": "нельзя удалить (неизвестный или последний узел)"}
    return 200, {"status": "ok"}


def r_settings(body, h, client):
    config.update_settings(body)
    return 200, {"status": "ok", "settings": config.settings}


def r_settings_reset(body, h, client):
    config.reset_settings(body.get("section"))
    return 200, {"status": "ok", "settings": config.settings}


def r_client_hello(body, h, client):
    if client and isinstance(body.get("device"), dict):
        clients.set_device_info(client["id"], body["device"])
    return 200, {"status": "ok", "id": (client or {}).get("id", ""), "ip": h.client_address[0], "is_admin": client_role(h) == "admin"}


def r_client_forget(body, h, client):
    clients.forget(body.get("id", ""))
    return 200, {"status": "ok"}


def r_admin_exec(body, h, client):
    key = body.get("node_key", "")
    action = body.get("action", "")
    if key not in config.nodes:
        return 404, {"error": "Unknown node"}
    res = node_exec(key, action, body.get("args") or {}, timeout=int(body.get("timeout") or 180))
    if action in ("lms_load", "lms_unload", "llama_server_start", "llama_server_stop", "download", "delete_model"):
        _sysinfo_cache.clear()
        with _models_cache_lock:
            _models_cache.pop(key, None)
    return 200, {"node_key": key, "action": action, "result": res}


def r_admin_ask(body, h, client):
    key = body.get("node_key", "")
    if key not in config.nodes:
        return 404, {"error": "Unknown node"}
    text = (body.get("text") or "").strip()
    if not text:
        return 400, {"error": "text required"}
    return 200, admin_ask(key, text, via_node=body.get("via_node"))


def r_models_download(body, h, client):
    key = body.get("node_key", "")
    if key not in config.nodes:
        return 404, {"error": "Unknown node"}
    source = (body.get("source") or "").strip()
    if not source:
        return 400, {"error": "source required"}
    res = node_exec(key, "download", {"source": source, "dest_subdir": body.get("dest_subdir", "")}, timeout=20)
    return 200, {"node_key": key, "result": res}


def r_models_download_cancel(body, h, client):
    key = body.get("node_key", "")
    if key not in config.nodes:
        return 404, {"error": "Unknown node"}
    return 200, {"result": node_exec(key, "download_cancel", {"id": body.get("id")}, timeout=10)}


def r_models_estimate(body, h, client):
    """Server-side estimate (same formula the UI uses) + optional exact lms estimate for admins."""
    est = estimate_model(float(body.get("params_b") or 0), float(body.get("bpw") or quant_bpw(body.get("quant"))), int(body.get("ctx") or 8192),
                         float(body.get("vram_gb") or 0), float(body.get("ram_gb") or 0), float(body.get("bandwidth") or 300), bool(body.get("unified")),
                         body.get("size_bytes"))
    out = {"estimate": est}
    if body.get("node_key") in config.nodes and body.get("model") and client_role(h) == "admin":
        out["lms"] = node_exec(body["node_key"], "lms_estimate", {"model": body["model"], "ctx": body.get("ctx")}, timeout=60)
    return 200, out



_cert_pin_cache = {}


def hub_public_url(host_header=""):
    """Канонический адрес хаба для команд и установщиков.

    Берём имя из настроек подключения: сертификат выписан именно на него, а
    открытая по IP или 127.0.0.1 страница дала бы команду, которая не пройдёт
    проверку сертификата на другой машине.
    """
    scheme = "https" if scheme_is_https() else "http"
    tunnel = (config.settings["connection"].get("tunnel_host") or "").strip()
    if tunnel:
        return "%s://%s:%s" % (scheme, tunnel, config.settings["connection"]["port"])
    return "%s://%s" % (scheme, host_header or ("127.0.0.1:%s" % config.settings["connection"]["port"]))


def cert_pins(cert_path=None):
    """SHA-256 fingerprint and SPKI pin of the active certificate (for pinned installs)."""
    tls = sec().get("tls") or {}
    cert_path = cert_path or tls.get("cert") or ""
    if not cert_path or not Path(cert_path).is_file():
        return {}
    try:
        st = Path(cert_path).stat().st_mtime
    except OSError:
        return {}
    c = _cert_pin_cache.get(cert_path)
    if c and c[0] == st:
        return c[1]
    ossl = find_openssl()
    out = {"cert": cert_path, "self_signed": "selfsigned" in Path(cert_path).name}
    if ossl:
        rc, txt, _ = agentlib._run([ossl, "x509", "-in", cert_path, "-noout", "-issuer", "-subject"], timeout=20)
        if rc == 0:
            issuer = subject = None
            for line in txt.splitlines():
                if line.startswith("issuer="):
                    issuer = line.split("=", 1)[1].strip()
                elif line.startswith("subject="):
                    subject = line.split("=", 1)[1].strip()
            if issuer and subject:
                # Сертификат, выписанный сторонним центром (Let's Encrypt через Tailscale),
                # браузеры проверяют сами — пиннинг тогда только мешает.
                out["self_signed"] = issuer == subject
                out["issuer"] = issuer
        rc, fp, _ = agentlib._run([ossl, "x509", "-in", cert_path, "-noout", "-fingerprint", "-sha256"], timeout=20)
        if rc == 0 and "=" in fp:
            out["sha256"] = fp.split("=", 1)[1].strip().replace(":", "").upper()
        try:
            pub = subprocess.run([ossl, "x509", "-in", cert_path, "-pubkey", "-noout"], capture_output=True, timeout=20)
            der = subprocess.run([ossl, "pkey", "-pubin", "-outform", "der"], input=pub.stdout, capture_output=True, timeout=20)
            out["spki"] = base64.b64encode(hashlib.sha256(der.stdout).digest()).decode()
        except Exception:
            pass
    _cert_pin_cache[cert_path] = (st, out)
    return out


def find_openssl():
    cands = ["openssl", r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\Git\mingw64\bin\openssl.exe",
             "/usr/bin/openssl", "/opt/homebrew/bin/openssl", "/usr/local/opt/openssl/bin/openssl"]
    for c in cands:
        import shutil as _sh
        if _sh.which(c) or os.path.exists(c):
            return c
    return None


def hub_san_names():
    """Every name/address this hub may be reached by — all go into the certificate."""
    dns, ips = [], set()
    host = socket.gethostname()
    dns.append(host.lower())
    dns.append("localhost")
    try:
        ts = agentlib.tailscale_status()
        self_ = ts.get("self") or {}
        name = (self_.get("dns_name") or "").rstrip(".")
        if name:
            dns.append(name)
            dns.append(name.split(".")[0])
        for ip in self_.get("ips") or []:
            ips.add(ip)
    except Exception:
        pass
    for ip in host_ips():
        ips.add(ip)
    ips.update({"127.0.0.1", "::1"})
    tunnel = config.settings["connection"].get("tunnel_host") or ""
    if tunnel:
        (ips if re.match(r"^[\d.]+$|:", tunnel) else dns).add(tunnel) if isinstance(ips, set) else None
        if re.match(r"^[\d.]+$", tunnel):
            ips.add(tunnel)
        else:
            dns.append(tunnel)
    seen, dns_u = set(), []
    for d in dns:
        if d and d not in seen:
            seen.add(d)
            dns_u.append(d)
    return dns_u, sorted(ips)


def make_self_signed(days=825):
    """Self-signed certificate covering every address of this hub. Returns (cert, key, info)."""
    ossl = find_openssl()
    if not ossl:
        raise RuntimeError("openssl не найден. Установите Git for Windows (в нём есть openssl) или включите "
                           "HTTPS-сертификаты Tailscale — тогда сертификат будет настоящим.")
    certs = DATA_DIR / "certs"
    certs.mkdir(exist_ok=True)
    dns, ips = hub_san_names()
    cn = dns[0] if dns else "blackfox-hub"
    san = ",".join(["DNS:" + d for d in dns] + ["IP:" + i for i in ips])
    crt, key = certs / "hub-selfsigned.crt", certs / "hub-selfsigned.key"
    cmd = [ossl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", str(days), "-nodes",
           "-keyout", str(key), "-out", str(crt),
           "-subj", f"/CN={cn}/O=BlackFox AI Workstation",
           "-addext", "subjectAltName=" + san,
           "-addext", "keyUsage=digitalSignature,keyEncipherment",
           "-addext", "extendedKeyUsage=serverAuth"]
    rc, out, err = agentlib._run(cmd, timeout=90)
    if rc != 0 or not crt.exists():
        raise RuntimeError((err or out or "openssl вернул ошибку").strip()[:400])
    try:
        os.chmod(key, 0o600)
    except Exception:
        pass
    rc, fp, _ = agentlib._run([ossl, "x509", "-in", str(crt), "-noout", "-fingerprint", "-sha256"], timeout=20)
    return str(crt), str(key), {"cn": cn, "dns": dns, "ips": ips,
                                "fingerprint": (fp.split("=", 1)[-1].strip() if "=" in fp else ""), "days": days}


# --- authentication & security management ---------------------------------

def r_auth_status(body, h, client):
    ip = h.client_address[0]
    s = sec()
    return 200, {"enabled": bool(s.get("enabled")), "needs_setup": security.needs_setup(),
                 "can_setup": security.needs_setup() and is_admin_ip(ip),
                 "authenticated": client_role(h) is not None, "role": client_role(h),
                 "user_password_set": security.has_password("user"), "ip": ip,
                 "tls": bool((s.get("tls") or {}).get("enabled"))}


def r_auth_setup(body, h, client):
    """First run: only from an admin address, only while no admin password exists."""
    ip = h.client_address[0]
    if not security.needs_setup():
        return 409, {"error": "Пароль администратора уже задан"}
    if not is_admin_ip(ip):
        security.audit("setup_denied", ip)
        return 403, {"error": "Первичная настройка возможна только с хост-машины кластера"}
    admin_pw = (body.get("admin_password") or "").strip()
    user_pw = (body.get("user_password") or "").strip()
    if len(admin_pw) < 8:
        return 400, {"error": "Пароль администратора: минимум 8 символов"}
    if user_pw and len(user_pw) < 6:
        return 400, {"error": "Пароль пользователя: минимум 6 символов"}
    security.set_password("admin", admin_pw)
    if user_pw:
        security.set_password("user", user_pw)
    secret = security.ensure_agent_secret()
    config.update_settings({"security": {"enabled": True}})
    security.audit("setup_complete", ip, "admin" + (" + user" if user_pw else ""))
    token = security.create_session("admin", ip, (client or {}).get("name", ""), h.headers.get("User-Agent", ""),
                                    int(sec().get("session_hours", 72)))
    return 200, {"status": "ok", "token": token, "role": "admin", "agent_secret_set": bool(secret)}


def r_auth_login(body, h, client):
    ip = h.client_address[0]
    s = sec()
    wait = security.locked_for(ip, int(s.get("lockout_fails", 8)), int(s.get("lockout_minutes", 15)))
    if wait:
        return 429, {"error": "Слишком много неудачных попыток. Повторите через %d мин." % (wait // 60 + 1)}
    want = (body.get("role") or "").strip()          # приложение выбирает роль явно; браузер — как раньше, по паролю
    if want and want not in ("admin", "user"):
        return 400, {"error": "role: admin|user"}
    role = security.check_password(body.get("password") or "", want or None)
    if not role:
        locked = security.note_fail(ip, int(s.get("lockout_fails", 8)), int(s.get("lockout_minutes", 15)))
        security.audit("login_failed", ip, ("role=" + want + " " if want else "") + ("lockout" if locked else ""))
        msg = "Неверный пароль" if not want else ("Неверный пароль администратора" if want == "admin" else "Неверный пароль пользователя")
        if want == "user" and not security.has_password("user"):
            msg = "Пароль пользователя не задан — вход только как администратор"
        return 401, {"error": msg + (" — IP заблокирован" if locked else "")}
    security.note_success(ip)
    token = security.create_session(role, ip, (body.get("name") or (client or {}).get("name", "")),
                                    h.headers.get("User-Agent", ""), int(s.get("session_hours", 72)))
    security.audit("login_ok", ip, body.get("name", ""), role)
    return 200, {"status": "ok", "token": token, "role": role}


def r_auth_logout(body, h, client):
    token = h.headers.get("X-BF-Auth") or ""
    if security.revoke(token):
        security.audit("logout", h.client_address[0])
    return 200, {"status": "ok"}


def r_auth_password(body, h, client):
    which = body.get("which", "user")
    if which not in ("admin", "user"):
        return 400, {"error": "which must be admin|user"}
    new = (body.get("new_password") or "").strip()
    if new and len(new) < (8 if which == "admin" else 6):
        return 400, {"error": "Минимальная длина: %d символов" % (8 if which == "admin" else 6)}
    if which == "admin" and not new:
        return 400, {"error": "Пароль администратора нельзя убрать"}
    if security.check_password(body.get("current_password") or "") != "admin":
        return 403, {"error": "Нужен текущий пароль администратора"}
    security.set_password(which, new)
    security.audit("password_changed", h.client_address[0], which, "admin")
    return 200, {"status": "ok", "which": which, "set": bool(new)}


def r_auth_sessions(body, h, client):
    return 200, {"sessions": [{k: v for k, v in s.items() if k != "token"} for s in security.sessions()],
                 "current": (h.headers.get("X-BF-Auth") or "")[:8]}


def r_auth_revoke(body, h, client):
    if body.get("all"):
        security.revoke_all(keep_token=h.headers.get("X-BF-Auth") if body.get("keep_me") else None)
        security.audit("sessions_revoked_all", h.client_address[0], "", "admin")
        return 200, {"status": "ok"}
    tok = ""
    for sess in security.sessions():
        if sess["id"] == body.get("id"):
            tok = sess["token"]
            break
    ok = security.revoke(tok)
    security.audit("session_revoked", h.client_address[0], body.get("id", ""), "admin")
    return 200, {"status": "ok" if ok else "not_found"}


def r_security_update(body, h, client):
    """Admin-only knobs: network restriction, bind, TLS, session lifetime, agent signing."""
    allowed = ("enabled", "restrict_network", "allow_cidrs", "bind", "session_hours",
               "lockout_fails", "lockout_minutes", "sign_agents")
    patch = {k: v for k, v in body.items() if k in allowed}
    if patch.get("enabled") and security.needs_setup():
        return 400, {"error": "Сначала задайте пароль администратора"}
    if isinstance(patch.get("allow_cidrs"), str):
        patch["allow_cidrs"] = [c for c in re.split(r"[,\s]+", patch["allow_cidrs"]) if c]
    if "allow_cidrs" in patch:
        bad = [c for c in patch["allow_cidrs"] if not agentlib.parse_cidrs([c])]
        if bad:
            return 400, {"error": "Некорректные сети: " + ", ".join(bad)}
    if patch.get("bind") not in (None, "0.0.0.0", "tailscale", "127.0.0.1"):
        return 400, {"error": "bind: 0.0.0.0 | tailscale | 127.0.0.1"}
    config.update_settings({"security": patch})
    security.audit("security_settings", h.client_address[0], json.dumps(patch, ensure_ascii=False), "admin")
    return 200, {"status": "ok", "settings": redact_settings(config.settings, "admin"),
                 "restart_required": "bind" in patch}


def r_security_agent_secret(body, h, client):
    """Rotate the cluster secret; returned once so the admin can reinstall agents."""
    secret = security.rotate_agent_secret()
    security.audit("agent_secret_rotated", h.client_address[0], "", "admin")
    # установщик несёт секрет и отдаётся узлу только по ключу — команды сразу с ключом на все удалённые узлы
    remote = sum(1 for cfg in config.nodes.values() if not node_is_local(cfg))
    _, tok = r_install_token({"uses": max(1, min(10, remote)), "note": "переустановка после смены секрета"}, h, client)
    return 200, {"status": "ok", "secret": secret, "install_win": tok["install_win"], "install_unix": tok["install_unix"],
                 "note": "Переустановите агент на каждом узле — старый секрет больше не действует. "
                         "Ключ в командах действует %d мин, установок: %d." % (tok["expires_in_min"], tok["uses"])}


def r_security_agent_check(body, h, client):
    """Verify every agent accepts our signature — and refuses unsigned commands."""
    out = {}

    def probe(key):
        cfg = config.nodes[key]
        if node_is_local(cfg):
            return key, {"ok": True, "detail": "локальный узел — сетевой канал не используется", "unsigned_accepted": False}
        url = agent_url_for(cfg)
        if not url:
            return key, {"ok": False, "detail": "нет адреса агента", "unsigned_accepted": None}
        signed = node_exec(key, "disk_usage", {}, timeout=15)
        res = {"ok": bool(signed.get("ok")), "detail": signed.get("error") or "подписанный запрос принят",
               "unsigned_accepted": None}
        try:
            http_post_json(url + "/exec", {"action": "disk_usage"}, timeout=8)
            res["unsigned_accepted"] = True
            res["ok"] = False
            res["detail"] += " | ОПАСНО: агент принимает неподписанные команды — обновите его"
        except urllib.error.HTTPError as e:
            res["unsigned_accepted"] = e.code != 403
            res["detail"] += " | неподписанные команды отклоняются" if e.code == 403 else " | агент ответил %d на неподписанный запрос" % e.code
        except Exception as e:
            res["detail"] += " | проверка без подписи не удалась: %s" % str(e)[:60]
        return key, res

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(config.nodes))) as ex:
        for k, v in ex.map(probe, list(config.nodes.keys())):
            out[k] = v
    return 200, {"nodes": out, "secret_set": bool(security.agent_secret())}


def r_security_tls(body, h, client):
    """Get a real TLS certificate from Tailscale, or point the hub at existing files."""
    action = body.get("action", "status")
    if action == "tailscale_cert":
        ts = agentlib.tailscale_status()
        name = ((ts.get("self") or {}).get("dns_name") or "").rstrip(".")
        if not name:
            return 400, {"error": "Не удалось определить DNS-имя Tailscale (MagicDNS выключен?)"}
        cmd = agentlib.tailscale_cmd()
        if not cmd:
            return 400, {"error": "tailscale CLI не найден"}
        certs = DATA_DIR / "certs"
        certs.mkdir(exist_ok=True)
        crt, key = certs / (name + ".crt"), certs / (name + ".key")
        rc, out, err = agentlib._run(cmd + ["cert", "--cert-file", str(crt), "--key-file", str(key), name], timeout=120)
        if rc != 0 or not crt.exists():
            msg = (err or out or "tailscale cert не выполнен").strip()[:400]
            hint = ("Откройте https://login.tailscale.com/admin/dns и включите HTTPS Certificates для тейлнета, "
                    "затем повторите. Пока это не включено — используйте самоподписанный сертификат.")
            if "does not support" in msg or "not enabled" in msg.lower():
                msg = "Tailscale: для этого тейлнета не включены HTTPS-сертификаты"
            return 400, {"error": msg, "hint": hint, "admin_url": "https://login.tailscale.com/admin/dns",
                         "dns_name": name, "can_self_sign": bool(find_openssl())}
        config.update_settings({"security": {"tls": {"enabled": True, "cert": str(crt), "key": str(key)}}})
        security.audit("tls_cert_issued", h.client_address[0], name, "admin")
        return 200, {"status": "ok", "dns_name": name, "cert": str(crt), "restart_required": True,
                     "url": "https://%s:%s" % (name, config.settings["connection"]["port"])}
    if action == "self_signed":
        try:
            crt, key, info = make_self_signed()
        except Exception as e:
            return 400, {"error": str(e)}
        config.update_settings({"security": {"tls": {"enabled": True, "cert": crt, "key": key}}})
        security.audit("tls_self_signed", h.client_address[0], info.get("cn", ""), "admin")
        port = config.settings["connection"]["port"]
        return 200, {"status": "ok", "self_signed": True, "cert": crt, "info": info, "restart_required": True,
                     "urls": ["https://%s:%s" % (d, port) for d in info["dns"][:3]] + ["https://%s:%s" % (i, port) for i in info["ips"] if ":" not in i][:3],
                     "note": "Сертификат самоподписанный: канал шифруется, но браузер предупредит о доверии, пока сертификат не установлен на устройстве."}
    if action == "download_cert":
        tls = sec().get("tls") or {}
        cert = tls.get("cert") or ""
        if not cert or not Path(cert).is_file():
            return 404, {"error": "Сертификат не найден"}
        return 200, {"status": "ok", "filename": Path(cert).name, "pem": Path(cert).read_text(encoding="utf-8")}
    if action == "set":
        cert, key = (body.get("cert") or "").strip(), (body.get("key") or "").strip()
        if cert and not Path(cert).is_file():
            return 400, {"error": "Файл сертификата не найден"}
        if key and not Path(key).is_file():
            return 400, {"error": "Файл ключа не найден"}
        config.update_settings({"security": {"tls": {"enabled": bool(body.get("enabled")), "cert": cert, "key": key}}})
        security.audit("tls_settings", h.client_address[0], cert, "admin")
        return 200, {"status": "ok", "restart_required": True}
    dns, ips = hub_san_names()
    return 200, {"tls": dict(sec().get("tls") or {}),
                 "tailscale_dns": ((agentlib.tailscale_status().get("self") or {}).get("dns_name") or "").rstrip("."),
                 "openssl": bool(find_openssl()), "names": dns, "ips": ips}


def r_security_audit(body, h, client):
    return 200, {"events": security.audit_tail(int(body.get("limit") or 200))}


def r_install_token(body, h, client):
    """Одноразовый ключ установки агента + готовые команды для нового узла.

    Установщик отдаёт секрет кластера, поэтому новый узел (у которого ещё нет
    ни сессии, ни прав админа) получает доступ только по такому ключу.
    """
    action = (body.get("action") or "issue").strip()
    if action == "list":
        return 200, {"tokens": security.install_tokens()}
    if action == "revoke":
        return 200, {"status": "ok", "revoked": security.revoke_install_token(body.get("token") or "")}

    minutes = max(5, min(240, int(body.get("minutes") or 30)))
    uses = max(1, min(10, int(body.get("uses") or 1)))
    port = int(body.get("port") or config.settings["telemetry"].get("agent_port", 8766))
    tok = security.new_install_token(minutes, uses, str(body.get("note") or ""), h.client_address[0])
    security.audit("install_token_issued", h.client_address[0], "%d мин, установок: %d" % (minutes, uses), "admin")

    https = scheme_is_https()
    hub = hub_public_url(h.headers.get("Host"))
    cp = cert_pins() if https else {}
    pin = cp.get("spki", "") if cp.get("self_signed") else ""

    url_ps = '"%s/install-agent.ps1?port=%d"' % (hub, port)
    url_sh = '"%s/install-agent.sh?port=%d"' % (hub, port)
    hdr = 'X-Install-Token: %s' % tok
    if pin:
        win = ('curl.exe -sS -k --pinnedpubkey sha256//%s -H "%s" -o "$env:TEMP\bf-install.ps1" %s; '
               '& "$env:TEMP\bf-install.ps1"' % (pin, hdr, url_ps))
        unix = 'curl -fsSL -k --pinnedpubkey sha256//%s -H "%s" %s | sh' % (pin, hdr, url_sh)
    else:
        win = "$h=@{'X-Install-Token'='%s'}; irm -Headers $h %s | iex" % (tok, url_ps)
        unix = 'curl -fsSL -H "%s" %s | sh' % (hdr, url_sh)

    return 200, {"status": "ok", "token": tok, "expires_in_min": minutes, "uses": uses,
                 "hub": hub, "pinned": bool(pin), "agent_port": port,
                 "install_win": win, "install_unix": unix,
                 "note": "Ключ действует %d мин и гасится после установки. Выполнять на самом новом узле." % minutes}


LLM_PORTS = {1234: "LM Studio", 1235: "LM Studio (второй)", 8080: "llama-server", 11434: "Ollama", 8000: "vLLM / OpenAI-совместимый"}


def _port_open(host, port, timeout=1.2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def connect_info():
    """Адреса, по которым к платформе подключаются люди (браузер) и узлы (агент)."""
    https = scheme_is_https()
    port = config.settings["connection"]["port"]
    ts = tailscale_cached() or {}
    self_ = ts.get("self") or {}
    dns = (self_.get("dns_name") or "").rstrip(".")
    pins = cert_pins() if https else {}
    cert_names = set()
    if https and pins.get("cert"):
        ossl = find_openssl()
        if ossl:
            rc, out, _ = agentlib._run([ossl, "x509", "-in", pins["cert"], "-noout", "-ext", "subjectAltName"], timeout=10)
            if rc == 0:
                for m in re.finditer(r"(?:DNS|IP Address):([^,\s]+)", out):
                    cert_names.add(m.group(1).lower())
    scheme = "https" if https else "http"

    def entry(host, kind, note=""):
        url = "%s://%s:%s" % (scheme, host, port)
        ok_cert = (not https) or (host.lower() in cert_names) or (not cert_names)
        return {"url": url, "host": host, "kind": kind, "cert_ok": ok_cert, "self_signed": bool(pins.get("self_signed")),
                "note": note or ("" if ok_cert else "сертификат выписан не на этот адрес — браузер предупредит")}

    addrs = []
    if dns:
        addrs.append(entry(dns, "tailscale_dns", "основной адрес: имя из сертификата, работает со всех устройств тейлнета"))
    for ip in self_.get("ips") or []:
        if ":" not in ip:
            addrs.append(entry(ip, "tailscale_ip"))
    for ip in sorted(agentlib.local_ips()):
        if ip.startswith("100.") or ip.startswith("127.") or ":" in ip:
            continue
        addrs.append(entry(ip, "lan", "только внутри локальной сети"))
    primary = addrs[0] if addrs else entry("127.0.0.1", "local")
    sec_ = sec()
    return {"primary": primary, "addresses": addrs, "tls": https, "self_signed": bool(pins.get("self_signed")),
            "fingerprint": pins.get("sha256", ""), "tailnet": ts.get("magic_dns") or "", "tailscale_ok": bool(dns),
            "auth_enabled": bool(sec_.get("enabled")), "user_password_set": security.has_password("user"),
            "admin_password_set": security.has_password("admin"),
            "restrict_network": bool(sec_.get("restrict_network", True)), "agent_port": config.settings["telemetry"].get("agent_port", 8766),
            "tailscale_account": (self_.get("dns_name") or "")}


def discover_devices():
    """Устройства тейлнета: есть ли на них агент и открытые LLM-порты, добавлены ли уже как узлы."""
    ts = agentlib.tailscale_status() or {}
    agent_port = int(config.settings["telemetry"].get("agent_port", 8766))
    known = {}
    for key, cfg in config.nodes.items():
        known.setdefault(endpoint_host(cfg["endpoint"]), []).append(key)
    self_ips = set((ts.get("self") or {}).get("ips") or [])

    def probe(peer):
        ip = next((i for i in (peer.get("ips") or []) if ":" not in i), None)
        out = {"hostname": peer.get("hostname"), "dns_name": peer.get("dns_name"), "os": peer.get("os"), "ip": ip,
               "online": bool(peer.get("online")), "last_seen": peer.get("last_seen"), "nodes": known.get(ip, []),
               "agent": None, "llm_ports": [], "is_hub": ip in self_ips}
        if not ip or not peer.get("online"):
            return out
        if _port_open(ip, agent_port, 1.5):
            try:
                t = http_get_json("http://%s:%d/telemetry" % (ip, agent_port), timeout=4)
                gpus = t.get("gpus") or []
                out["agent"] = {"ok": True, "version": t.get("version"), "platform": t.get("platform"), "hostname": t.get("hostname"),
                                "cpu": (t.get("cpu") or {}).get("name"), "ram_gb": (t.get("ram") or {}).get("total_gb"),
                                "gpus": [{"name": g.get("name"), "vram_gb": round((g.get("mem_total_mb") or 0) / 1024, 1)} for g in gpus]}
            except Exception as e:
                out["agent"] = {"ok": False, "error": str(e)[:120]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(LLM_PORTS)) as ex:
            res = list(ex.map(lambda p: (p, _port_open(ip, p, 1.2)), LLM_PORTS.keys()))
        for p, ok in res:
            if ok:
                item = {"port": p, "label": LLM_PORTS[p], "endpoint": "http://%s:%d" % (ip, p), "models": []}
                try:
                    models, kind = probe_models("http://%s:%d/v1/chat/completions" % (ip, p))
                    item["models"] = [m.get("id") if isinstance(m, dict) else m for m in (models or [])][:20]
                    item["kind"] = kind
                except Exception as e:
                    item["error"] = str(e)[:100]
                out["llm_ports"].append(item)
        return out

    peers = [p for p in (ts.get("peers") or [])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        devices = list(ex.map(probe, peers))
    devices.sort(key=lambda d: (not d["online"], not d["agent"], d["hostname"] or ""))
    return {"devices": devices, "agent_port": agent_port, "ts": now_iso(), "tailscale_ok": bool(ts.get("peers") is not None)}


def r_admin_connect(body, h, client):
    return 200, connect_info()


def r_admin_discover(body, h, client):
    return 200, discover_devices()


def r_pair_code(body, h, client):
    """Парный код для регистрации узла из приложения (ADR §5). Только администратор."""
    action = body.get("action") or "issue"
    if action == "list":
        return 200, {"codes": security.pair_codes()}
    minutes = max(2, min(60, int(body.get("minutes") or 10)))
    code = security.new_pair_code(minutes, str(body.get("note") or ""), h.client_address[0])
    hub = hub_public_url(h.headers.get("Host"))
    security.audit("pair_code_issued", h.client_address[0], "%d мин" % minutes, "admin")
    return 200, {"status": "ok", "code": code, "expires_in_min": minutes, "hub": hub,
                 "link": "blackfox://pair?hub=%s&code=%s" % (urllib.parse.quote(hub, safe=""), code),
                 "note": "Введите код в мастере приложения на новой машине. Код действует %d мин и гаснет после использования." % minutes}


def r_node_pair(body, h, client):
    """Регистрация узла приложением: код → секрет кластера + запись узла. Публичный маршрут, защищён кодом и lockout."""
    ip = h.client_address[0]
    s = sec()
    wait = security.locked_for(ip, int(s.get("lockout_fails", 8)), int(s.get("lockout_minutes", 15)))
    if wait:
        return 429, {"error": "Слишком много неудачных попыток. Повторите через %d мин." % (wait // 60 + 1)}
    ok, why = security.use_pair_code(body.get("code") or "", ip)
    if not ok:
        locked = security.note_fail(ip, int(s.get("lockout_fails", 8)), int(s.get("lockout_minutes", 15)))
        security.audit("pair_failed", ip, why + (" lockout" if locked else ""))
        return 403, {"error": why}
    security.note_success(ip)
    hostname = (body.get("hostname") or "node").strip()[:40]
    agent_port = int(body.get("agent_port") or config.settings["telemetry"].get("agent_port", 8766))
    gpus = body.get("gpus") or []
    hw = {"gpus": gpus, "ram": body.get("ram") or {}, "cpu": body.get("cpu") or {}, "platform": body.get("platform") or ""}
    hw_text = ", ".join("%s %dGB" % (g.get("name", "GPU"), round((g.get("mem_total_mb") or 0) / 1024)) for g in gpus) or (hw["platform"] or "")
    try:
        rec = recommend_node(hw, [], "")
        role = (rec.get("roles") or [{}])[0]
    except Exception:
        rec, role = {}, {}
    # узел уже был спарен с этого адреса — обновляем, а не плодим дубликаты
    existing = next((k for k, c in config.nodes.items() if endpoint_host(c.get("endpoint") or "") == ip
                     or ((c.get("telemetry") or {}).get("url") or "").startswith("http://%s:" % ip)), None)
    node = {"name": hostname, "tag": (role.get("tag") or "NODE")[:10], "avatar": role.get("avatar") or "🖥️",
            "role": role.get("name") or "Универсальный ассистент", "system_prompt": role.get("prompt") or "",
            "hardware": hw_text, "endpoint": "http://%s:1234/v1/chat/completions" % ip,
            "telemetry": {"type": "agent", "url": "http://%s:%d" % (ip, agent_port)}}
    if existing:
        with config.lock:
            upd = {"hardware": node["hardware"]}
            if not is_local_endpoint(config.nodes[existing].get("endpoint") or ""):
                upd["telemetry"] = node["telemetry"]     # локальному узлу хаба телеметрию не переписываем
            config.nodes[existing].update(upd)
            config.save()
        key = existing
    else:
        key = config.add_node(node)
        with config.lock:
            config.nodes[key]["enabled"] = False      # включит администратор, когда укажет модель
            config.save()
    threading.Thread(target=ping_node, args=(key,), daemon=True).start()
    security.audit("node_paired", ip, "%s → %s" % (hostname, key))
    return 200, {"status": "ok", "key": key, "secret": security.ensure_agent_secret(), "hub": hub_public_url(h.headers.get("Host")),
                 "agent_port": agent_port, "recommendation": rec, "existing": bool(existing),
                 "note": "Узел добавлен выключенным: администратор укажет LLM-сервер и включит его в Настройки → Узлы."}


POST_ROUTES = {
    "/api/execute": r_execute, "/api/cancel": r_cancel, "/api/clear": r_clear,
    "/api/files/upload": r_files_upload, "/api/files/list": r_files_list, "/api/files/delete": r_files_delete,
    "/api/tools/exec": r_tools_exec, "/api/tools/approve": r_tools_approve,
    "/api/threads/create": r_thread_create, "/api/threads/delete": r_thread_delete, "/api/threads/rename": r_thread_rename,
    "/api/threads/meta": r_thread_meta, "/api/threads/clear": r_thread_clear, "/api/messages/delete": r_message_delete,
    "/api/node/toggle": r_node_toggle, "/api/node/ping": r_node_ping, "/api/node/update": r_node_update, "/api/node/set_model": r_node_set_model,
    "/api/node/reset_prompt": r_node_reset_prompt, "/api/node/test": r_node_test, "/api/node/probe": r_node_probe, "/api/node/add": r_node_add,
    "/api/node/delete": r_node_delete, "/api/settings": r_settings, "/api/settings/reset": r_settings_reset,
    "/api/client/hello": r_client_hello, "/api/client/forget": r_client_forget,
    "/api/admin/exec": r_admin_exec, "/api/admin/ask": r_admin_ask,
    "/api/admin/connect": r_admin_connect, "/api/admin/discover": r_admin_discover,
    "/api/models/download": r_models_download, "/api/models/download/cancel": r_models_download_cancel, "/api/models/estimate": r_models_estimate,
    "/api/auth/status": r_auth_status, "/api/auth/setup": r_auth_setup, "/api/auth/login": r_auth_login, "/api/auth/logout": r_auth_logout,
    "/api/auth/password": r_auth_password, "/api/auth/sessions": r_auth_sessions, "/api/auth/revoke": r_auth_revoke,
    "/api/security/update": r_security_update, "/api/security/agent_secret": r_security_agent_secret,
    "/api/security/install_token": r_install_token, "/api/security/pair_code": r_pair_code, "/api/node/pair": r_node_pair,
    "/api/security/agent_check": r_security_agent_check, "/api/security/tls": r_security_tls, "/api/security/audit": r_security_audit,
}
ADMIN_ROUTES = {"/api/node/toggle", "/api/node/update", "/api/node/set_model", "/api/node/reset_prompt", "/api/node/probe", "/api/node/add", "/api/node/delete",
                "/api/settings", "/api/settings/reset", "/api/client/forget", "/api/admin/exec", "/api/admin/ask",
                "/api/admin/connect", "/api/admin/discover", "/api/tools/exec", "/api/tools/approve",
                "/api/models/download", "/api/models/download/cancel",
                "/api/auth/password", "/api/auth/sessions", "/api/auth/revoke", "/api/security/update",
                "/api/security/agent_secret", "/api/security/install_token", "/api/security/pair_code", "/api/security/agent_check", "/api/security/tls", "/api/security/audit"}


def resolve_bind(mode):
    """0.0.0.0 (все интерфейсы) | tailscale (только тейлнет) | 127.0.0.1 (только эта машина)."""
    if mode == "127.0.0.1":
        return "127.0.0.1"
    if mode == "tailscale":
        try:
            ips = (agentlib.tailscale_status().get("self") or {}).get("ips") or []
            for ip in ips:
                if ":" not in ip:
                    return ip
        except Exception:
            pass
        print("[!] Не удалось определить адрес Tailscale — слушаю 0.0.0.0")
    return "0.0.0.0"


TLS_CTX = None          # активный SSL-контекст: нужен, чтобы подхватить продлённый сертификат без перезапуска


def tls_renew_loop():
    """Сертификат Tailscale живёт ~3 месяца. Раз в 12 часов просим его заново:
    команда `tailscale cert` сама решает, пора ли продлевать, и при обновлении
    файлов контекст перечитывает их — новые соединения сразу с новым сертификатом."""
    while True:
        time.sleep(12 * 3600)
        try:
            tls = sec().get("tls") or {}
            crt, key = tls.get("cert") or "", tls.get("key") or ""
            if not (tls.get("enabled") and crt and key and Path(crt).is_file()):
                continue
            name = Path(crt).name[:-4]
            if not name.endswith(".ts.net"):
                continue        # самоподписанный или свой сертификат — продлевать нечем
            cmd = agentlib.tailscale_cmd()
            if not cmd:
                continue
            before = Path(crt).stat().st_mtime
            rc, out, err = agentlib._run(cmd + ["cert", "--cert-file", crt, "--key-file", key, name], timeout=180)
            if rc != 0:
                security.audit("tls_renew_failed", "", (err or out or "").strip()[:200])
                continue
            if Path(crt).stat().st_mtime > before and TLS_CTX is not None:
                TLS_CTX.load_cert_chain(crt, key)
                security.audit("tls_renewed", "", name)
                print(f"[*] Сертификат {name} продлён и перечитан")
        except Exception as e:
            security.audit("tls_renew_failed", "", str(e)[:200])


class HubServer(http.server.ThreadingHTTPServer):
    """Сканеры и клиенты, стучащие обычным http в TLS-порт, роняли в лог полный
    traceback на каждое соединение — реальные ошибки терялись в шуме."""
    QUIET = (ssl.SSLError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, self.QUIET):
            print(f"[~] {client_address[0]}: {type(exc).__name__}: {str(exc)[:90]}")
            return
        super().handle_error(request, client_address)


def start_server(port=8765):
    config.settings["connection"]["port"] = port
    s = sec()
    bind = resolve_bind(s.get("bind", "0.0.0.0"))
    server = HubServer((bind, port), RequestHandler)
    server.daemon_threads = True
    scheme = "http"
    tls = s.get("tls") or {}
    if tls.get("enabled") and tls.get("cert") and tls.get("key"):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(tls["cert"], tls["key"])
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            scheme = "https"
            globals()["TLS_CTX"] = ctx
            pins = cert_pins(tls["cert"])
            print("[*] Сертификат: %s" % ("самоподписанный (браузер предупредит о доверии)" if pins.get("self_signed")
                                          else "выдан " + pins.get("issuer", "внешним центром")))
            if Path(tls["cert"]).name.endswith(".ts.net.crt"):
                threading.Thread(target=tls_renew_loop, daemon=True).start()
        except Exception as e:
            print(f"[!] TLS не включён: {e}")
    host = config.settings["connection"]["tunnel_host"]
    print(f"[*] BlackFox AI Workstation v3 on {scheme}://127.0.0.1:{port} (bind {bind})")
    print(f"[*] Tunnel access: {scheme}://{host}:{port}" if host else "[*] Адрес в тейлнете не задан: Настройки → Подключение")
    print(f"[*] Data dir: {DATA_DIR}; nodes: {', '.join(config.nodes)}")
    if security.needs_setup():
        print("[!] ЗАЩИТА НЕ НАСТРОЕНА: откройте интерфейс на этой машине и задайте пароль (Настройки → Безопасность)")
    else:
        print(f"[*] Вход по паролю: {'включён' if s.get('enabled') else 'ВЫКЛЮЧЕН'}; "
              f"сеть: {'только приватная' if s.get('restrict_network', True) else 'любая'}; "
              f"подпись команд узлам: {'да' if security.agent_secret() and s.get('sign_agents', True) else 'нет'}")
    security.audit("hub_start", "", f"{scheme}://{bind}:{port}")
    threading.Thread(target=node_telemetry.run, daemon=True).start()
    for nk in config.nodes:
        threading.Thread(target=ping_node, args=(nk,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        try:
            threads.save()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    if "--reset-auth" in sys.argv:
        # Recovery: run on the host machine if the admin password is lost.
        security.purge_sessions()
        security.data["passwords"] = {}
        security.save()
        config.update_settings({"security": {"enabled": False}})
        security.audit("auth_reset_cli", "local")
        print("[*] Пароли сброшены, вход отключён. Запустите хаб и задайте пароль заново.")
        sys.exit(0)
    start_server(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
