"""
BlackFox node agent v2 — telemetry, system info, model downloads and a
whitelisted set of management actions for one cluster node.

Run on every remote node (MacBook, laptop, …) next to LM Studio / llama.cpp:

    python3 node_agent.py                 # 0.0.0.0:8766
    python3 node_agent.py 9000            # custom port
    python3 node_agent.py 8766 --token S  # require X-BF-Token: S on /exec and /download

Endpoints (all JSON):
    GET  /telemetry              CPU / RAM / GPU sample
    GET  /sysinfo                hostname, OS, network interfaces, Wi-Fi, Tailscale, public IP,
                                 LM Studio models (lms ls/ps), disks, llama-server processes
    GET  /speedtest?bytes=N      N random bytes (download throughput measurement)
    POST /upload                 discards the body (upload throughput measurement)
    POST /exec {action, args}    whitelisted management action (see ACTIONS)
    GET  /downloads              download jobs
    POST /download {source}      start a download ("lms:<model>" or "url:https://…")
    POST /download/cancel {id}

The hub imports this file as a library for the node(s) running on the hub
machine itself, so everything is written as plain functions.  Stdlib only;
psutil is used if present.
"""

import base64
import copy
import ctypes
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import uuid
from pathlib import Path

# Вывод всегда в UTF-8: при перенаправлении в файл или в консоли с cp866/cp1252
# кириллица в сообщениях иначе роняла сервер (UnicodeEncodeError) ещё до запуска.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


VERSION = "2.0"
SYSTEM = platform.system()
IS_WIN = SYSTEM == "Windows"
HOME = Path.home()
LMSTUDIO_DIR = HOME / ".lmstudio"
MODELS_DIR = LMSTUDIO_DIR / "models"
AGENT_TOKEN = ""     # legacy shared bearer token
AGENT_SECRET = ""    # HMAC secret for signed control requests (preferred)
ALLOW_CIDRS = []     # extra networks allowed to reach control endpoints

try:
    import psutil  # optional
except Exception:  # pragma: no cover
    psutil = None


def _run(cmd, timeout=15, **kw):
    """Run a command, return (rc, stdout, stderr). Never raises."""
    try:
        if IS_WIN:
            if "creationflags" not in kw:
                kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            if "startupinfo" not in kw:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0  # SW_HIDE
                kw["startupinfo"] = si
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             timeout=timeout, encoding="utf-8", errors="replace", **kw)
        return res.returncode, res.stdout, res.stderr
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


def cpu_brand():
    try:
        if IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        if SYSTEM == "Darwin":
            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True, timeout=2).strip()
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or platform.machine()


# ---------------------------------------------------------------------------
# CPU / RAM / GPU sampling
# ---------------------------------------------------------------------------

class CpuSampler:
    def __init__(self):
        self.prev = None
        self.percent = 0.0
        self.name = cpu_brand()
        self.cores = os.cpu_count() or 0

    def sample(self):
        try:
            if psutil:
                self.percent = round(psutil.cpu_percent(interval=None), 1)
            elif IS_WIN:
                self._win()
            elif SYSTEM == "Linux":
                self._linux()
            elif SYSTEM == "Darwin":
                self._mac()
        except Exception:
            pass
        return {"percent": self.percent, "name": self.name, "cores": self.cores}

    def _win(self):
        class FILETIME(ctypes.Structure):
            _fields_ = [('lo', ctypes.c_ulong), ('hi', ctypes.c_ulong)]
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        f = lambda t: (t.hi << 32) + t.lo
        cur = (f(idle), f(kern), f(user))
        if self.prev:
            di, dk, du = (cur[i] - self.prev[i] for i in range(3))
            tot = dk + du
            if tot > 0:
                self.percent = round(max(0.0, min(100.0, (tot - di) / tot * 100)), 1)
        self.prev = cur

    def _linux(self):
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = list(map(int, parts[1:8]))
        idle = vals[3] + vals[4]
        total = sum(vals)
        if self.prev:
            dt = total - self.prev[1]
            di = idle - self.prev[0]
            if dt > 0:
                self.percent = round(max(0.0, min(100.0, (dt - di) / dt * 100)), 1)
        self.prev = (idle, total)

    def _mac(self):
        out = subprocess.check_output(["top", "-l", "1", "-n", "0", "-s", "0"], text=True, timeout=4)
        m = re.search(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle", out)
        if m:
            self.percent = round(100.0 - float(m.group(3)), 1)


def sample_ram():
    try:
        if psutil:
            vm = psutil.virtual_memory()
            return {"percent": int(vm.percent), "used_gb": round((vm.total - vm.available) / 1024 ** 3, 1), "total_gb": round(vm.total / 1024 ** 3, 1)}
        if IS_WIN:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('sullAvailExtendedVirtual', ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return {"percent": int(st.dwMemoryLoad), "used_gb": round((st.ullTotalPhys - st.ullAvailPhys) / 1024 ** 3, 1), "total_gb": round(st.ullTotalPhys / 1024 ** 3, 1)}
        if SYSTEM == "Linux":
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.strip().split()[0]) * 1024
            total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
            return {"percent": int((total - avail) / total * 100), "used_gb": round((total - avail) / 1024 ** 3, 1), "total_gb": round(total / 1024 ** 3, 1)}
        if SYSTEM == "Darwin":
            total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=2).strip())
            out = subprocess.check_output(["vm_stat"], text=True, timeout=3)
            page = 4096
            m = re.search(r"page size of (\d+) bytes", out)
            if m:
                page = int(m.group(1))
            stats = {}
            for line in out.splitlines():
                m = re.match(r"^(.+?):\s+(\d+)\.", line)
                if m:
                    stats[m.group(1).strip()] = int(m.group(2)) * page
            used = stats.get("Pages active", 0) + stats.get("Pages wired down", 0) + stats.get("Pages occupied by compressor", 0)
            return {"percent": int(used / total * 100), "used_gb": round(used / 1024 ** 3, 1), "total_gb": round(total / 1024 ** 3, 1)}
    except Exception:
        pass
    return {"percent": 0, "used_gb": 0, "total_gb": 0}


def nvidia_gpus():
    if Nvml.load():
        return [{"index": g["index"], "name": g["name"], "util_percent": g["util_percent"] or 0,
                 "mem_used_mb": g["mem_used_mb"] or 0, "mem_total_mb": g["mem_total_mb"] or 0,
                 "mem_percent": g["mem_percent"] or 0.0, "temp_c": g["temp_c"] or 0,
                 "power_w": g["power_w"] or 0.0, "clock_mhz": g["core_clock_mhz"] or 0,
                 "fan_percent": g["fan_percent"] or 0} for g in gpus_detailed_fresh()]
    cmd = ["nvidia-smi",
           "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,fan.speed",
           "--format=csv,noheader,nounits"]
    rc, out, _ = _run(cmd, timeout=3)
    if rc != 0:
        return []

    def num(v, cast=float, default=0):
        try:
            return cast(float(v))
        except Exception:
            return default

    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 7:
            continue
        used, total = num(p[3], int), num(p[4], int)
        gpus.append({"index": num(p[0], int), "name": p[1], "util_percent": num(p[2], int),
                     "mem_used_mb": used, "mem_total_mb": total, "mem_percent": round(used / max(1, total) * 100, 1),
                     "temp_c": num(p[5], int), "power_w": round(num(p[6]), 1),
                     "clock_mhz": num(p[7], int) if len(p) > 7 else 0, "fan_percent": num(p[8], int) if len(p) > 8 else 0})
    return gpus


def apple_gpu():
    """Apple Silicon: ioreg exposes 'Device Utilization %' without sudo; memory is unified (= RAM)."""
    rc, out, _ = _run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], timeout=4)
    if rc != 0:
        return []
    util = None
    for pat in (r'"Device Utilization %"\s*=\s*(\d+)', r'"GPU Activity\(%\)"\s*=\s*(\d+)'):
        m = re.search(pat, out)
        if m:
            util = int(m.group(1))
            break
    name = cpu_brand() + " GPU"
    m = re.search(r'"In use system memory"\s*=\s*(\d+)', out)
    mem_used_mb = int(int(m.group(1)) / 1024 ** 2) if m else 0
    ram = sample_ram()
    mem_total_mb = int(ram.get("total_gb", 0) * 1024)
    if util is None and not mem_used_mb:
        return []
    return [{"index": 0, "name": name, "util_percent": util or 0, "mem_used_mb": mem_used_mb, "mem_total_mb": mem_total_mb,
             "mem_percent": round(mem_used_mb / max(1, mem_total_mb) * 100, 1), "temp_c": 0, "power_w": 0, "unified": True}]


def sample_gpus():
    gpus = nvidia_gpus()
    if gpus:
        return gpus
    if SYSTEM == "Darwin":
        return apple_gpu()
    return []


# ---------------------------------------------------------------------------
# Deep sensors: temperatures, clocks, fans and power draw
#
# Sources, best first:
#   * LibreHardwareMonitor / OpenHardwareMonitor via WMI — full set on Windows
#     (CPU package temp and power, per-core clocks, fan RPM, board sensors)
#   * nvidia-smi — everything about NVIDIA GPUs
#   * Windows perf counters — effective core frequency (same math as Task Manager)
#   * WMI Win32_* — base clock, configured RAM frequency
#   * Linux /sys/class/hwmon + RAPL, macOS ioreg/sysctl
# Anything the hardware does not expose is reported as null with a reason,
# and values we compute rather than measure are marked estimated.
# ---------------------------------------------------------------------------

SENSOR_INTERVAL = 5.0          # sensors are costly (WMI); sample slower than cpu/ram
PLATFORM_IDLE_W = 35.0         # board, drives, fans, USB — used only for the total estimate
DIMM_IDLE_W, DIMM_LOAD_W = 1.6, 3.2

PS_SENSORS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @{}
foreach ($ns in @('root\LibreHardwareMonitor','root\OpenHardwareMonitor')) {
  $s = Get-CimInstance -Namespace $ns -ClassName Sensor
  if ($s) {
    $out.lhm_ns = $ns
    $out.lhm = @($s | Where-Object { $_.SensorType -in @('Temperature','Clock','Fan','Power','Load','Voltage','Control') } |
      Select-Object @{n='n';e={$_.Name}}, @{n='t';e={$_.SensorType}}, @{n='v';e={[math]::Round([double]$_.Value,1)}}, @{n='p';e={$_.Parent}}, @{n='i';e={$_.Identifier}})
    break
  }
}
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
if ($cpu) { $out.cpu_name = $cpu.Name; $out.cpu_base_mhz = $cpu.MaxClockSpeed; $out.cpu_wmi_mhz = $cpu.CurrentClockSpeed; $out.cpu_cores = $cpu.NumberOfCores; $out.cpu_threads = $cpu.NumberOfLogicalProcessors }
$perf = (Get-Counter '\Processor Information(_Total)\% Processor Performance').CounterSamples
if ($perf) { $out.cpu_perf_pct = [math]::Round($perf[0].CookedValue,1) }
$cores = (Get-Counter '\Processor Information(*)\% Processor Performance').CounterSamples | Where-Object { $_.InstanceName -match '^\d+,\d+$' }
if ($cores) { $out.cores = @($cores | ForEach-Object { @{ id = $_.InstanceName; perf = [math]::Round($_.CookedValue,1) } }) }
$mem = Get-CimInstance Win32_PhysicalMemory
if ($mem) { $out.ram = @($mem | ForEach-Object { @{ gb = [math]::Round($_.Capacity/1GB,1); speed = $_.Speed; configured = $_.ConfiguredClockSpeed; part = $_.PartNumber } }) }
$tz = Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature
if ($tz) { $out.acpi_temps = @($tz | ForEach-Object { [math]::Round(($_.CurrentTemperature/10)-273.15,1) }) }
$fan = Get-CimInstance Win32_Fan
if ($fan) { $out.wmi_fans = @($fan | ForEach-Object { @{ name = $_.Name; rpm = $_.DesiredSpeed } }) }
$bat = Get-CimInstance Win32_Battery
if ($bat) { $out.battery = @($bat | ForEach-Object { @{ charge = $_.EstimatedChargeRemaining; status = $_.BatteryStatus } }) }
$out | ConvertTo-Json -Depth 5 -Compress
"""


def cpu_tdp_guess(name):
    """Rough package TDP, only used when no power sensor exists."""
    n = (name or "").lower()
    table = [("5600x", 65), ("5600", 65), ("5700x", 65), ("5800x3d", 105), ("5800", 105), ("5900", 105), ("5950", 105),
             ("7600", 65), ("7700", 65), ("7800x3d", 120), ("7900", 170), ("7950", 170), ("9600", 65), ("9700", 65), ("9800x3d", 120),
             ("3600", 65), ("3700", 65), ("3900", 105), ("2600", 65),
             ("i3", 60), ("i5", 65), ("i7", 125), ("i9", 125), ("ultra 9", 125), ("ultra 7", 65),
             ("m1 max", 30), ("m1 pro", 30), ("m1", 20), ("m2 max", 35), ("m2 pro", 32), ("m2", 22),
             ("m3 max", 40), ("m3 pro", 35), ("m3", 24), ("m4 max", 45), ("m4 pro", 38), ("m4", 26)]
    for key, w in table:
        if key in n:
            return w
    return 65


def _num(v):
    try:
        f = float(v)
        return None if f != f else round(f, 1)
    except (TypeError, ValueError):
        return None


def parse_lhm(items):
    """LibreHardwareMonitor sensor list -> grouped dict by type and name."""
    groups = {}
    for it in items or []:
        t = (it.get("t") or "").lower()
        groups.setdefault(t, []).append({"name": it.get("n") or "", "value": _num(it.get("v")),
                                         "parent": it.get("p") or "", "id": it.get("i") or ""})
    return groups


def pick(sensors, *patterns, exclude=()):
    """First sensor whose name matches any pattern (case-insensitive substring)."""
    for pat in patterns:
        for s in sensors or []:
            n = s["name"].lower()
            if pat in n and not any(x in n for x in exclude) and s["value"] is not None:
                return s
    return None


def is_cpu_id(ident):
    i = (ident or "").lower()
    return "/amdcpu/" in i or "/intelcpu/" in i or "/cpu/" in i


def is_gpu_id(ident):
    i = (ident or "").lower()
    return "/gpu" in i or "nvidia" in i or "amdgpu" in i or "intelgpu" in i


class Sensors:
    """Background sampler for the deep hardware sensors."""

    def __init__(self, start=True):
        self.lock = threading.Lock()
        self.data = {"source": "none", "available": False, "ts": 0}
        self.raw = {}
        self.last_error = ""
        if start:
            threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            t0 = time.time()
            try:
                d = self.sample()
                with self.lock:
                    self.data = d
            except Exception as e:
                self.last_error = str(e)[:200]
            time.sleep(max(1.0, SENSOR_INTERVAL - (time.time() - t0)))

    def get(self):
        with self.lock:
            d = copy.deepcopy(self.data)
        return self._refresh_gpus(d)

    def _refresh_gpus(self, d):
        """Датчики GPU подменяются свежими (≤1 с): опрос WMI занимает 10–20 с, и
        без этого частота видеокарты показывала бы состояние далёкого прошлого."""
        try:
            gpus = gpus_detailed_fresh()
        except Exception:
            return d
        if not gpus:
            return d
        if not d.get("ts"):
            # первый медленный опрос ещё идёт — GPU показываем сразу, остальное подтянется
            d = {"source": "nvml", "available": True, "ts": time.time(), "cpu": {}, "ram": {}, "fans": [],
                 "power": {}, "battery": None, "notes": ["Датчики процессора и платы ещё собираются…"]}
        d["gpus"] = gpus
        d["gpu_ts"] = time.time()
        p = d.get("power") or {}
        gpu_w = round(sum((g.get("power_w") or 0) for g in gpus), 1)
        if p:
            p["gpu_w"] = gpu_w or None
            p["total_w_est"] = round((p.get("cpu_w") or 0) + gpu_w + (p.get("ram_w_est") or 0) + (p.get("platform_w_est") or 0), 1)
        return d

    # ---- platform collectors -------------------------------------------
    def _windows_raw(self):
        rc, out, err = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SENSORS], timeout=25)
        if rc != 0 or not out.strip():
            self.last_error = (err or "powershell вернул пустой ответ")[:200]
            return {}
        try:
            return json.loads(out)
        except Exception as e:
            self.last_error = "разбор ответа powershell: %s" % e
            return {}

    def _linux_raw(self):
        raw = {"hwmon": [], "rapl": []}
        base = "/sys/class/hwmon"
        try:
            for d in sorted(os.listdir(base)):
                p = os.path.join(base, d)
                try:
                    name = open(os.path.join(p, "name")).read().strip()
                except Exception:
                    continue
                entry = {"name": name, "temps": [], "fans": []}
                for f in sorted(os.listdir(p)):
                    try:
                        if re.match(r"^temp\d+_input$", f):
                            label_f = os.path.join(p, f.replace("_input", "_label"))
                            label = open(label_f).read().strip() if os.path.exists(label_f) else f
                            entry["temps"].append({"label": label, "c": round(int(open(os.path.join(p, f)).read()) / 1000, 1)})
                        elif re.match(r"^fan\d+_input$", f):
                            entry["fans"].append({"label": f, "rpm": int(open(os.path.join(p, f)).read())})
                    except Exception:
                        continue
                if entry["temps"] or entry["fans"]:
                    raw["hwmon"].append(entry)
        except Exception:
            pass
        try:
            rapl = "/sys/class/powercap"
            for d in sorted(os.listdir(rapl)):
                if d.startswith("intel-rapl:") and d.count(":") == 1:
                    p = os.path.join(rapl, d)
                    raw["rapl"].append({"name": open(os.path.join(p, "name")).read().strip(),
                                        "uj": int(open(os.path.join(p, "energy_uj")).read()), "t": time.time()})
        except Exception:
            pass
        try:
            mhz = [float(l.split(":")[1]) for l in open("/proc/cpuinfo") if l.lower().startswith("cpu mhz")]
            if mhz:
                raw["core_mhz"] = [round(m) for m in mhz]
        except Exception:
            pass
        return raw

    def _mac_raw(self):
        raw = {}
        rc, out, _ = _run(["sysctl", "-n", "hw.cpufrequency_max"], timeout=3)
        if rc == 0 and out.strip().isdigit():
            raw["cpu_max_mhz"] = round(int(out.strip()) / 1e6)
        rc, out, _ = _run(["ioreg", "-r", "-c", "AppleSmartBattery"], timeout=4)
        if rc == 0:
            m = re.search(r'"Temperature"\s*=\s*(\d+)', out)
            if m:
                raw["battery_temp_c"] = round(int(m.group(1)) / 100, 1)
        return raw

    # ---- main --------------------------------------------------------------
    def sample(self):
        gpus = nvidia_gpus_detailed()
        cpu_load = telemetry.get().get("cpu", {}).get("percent", 0) if telemetry else 0
        res = {"source": "none", "available": False, "ts": time.time(), "notes": [],
               "cpu": {}, "ram": {}, "gpus": gpus, "fans": [], "power": {}, "battery": None}

        if IS_WIN:
            raw = self._windows_raw()
            self.raw = raw
            lhm = parse_lhm(raw.get("lhm"))
            has_lhm = bool(raw.get("lhm"))
            res["source"] = ("LibreHardwareMonitor" if "libre" in (raw.get("lhm_ns") or "").lower() else "OpenHardwareMonitor") if has_lhm else "windows"
            base = _num(raw.get("cpu_base_mhz"))
            perf = _num(raw.get("cpu_perf_pct"))
            cpu = {"name": raw.get("cpu_name") or cpu_brand(), "cores": raw.get("cpu_cores"), "threads": raw.get("cpu_threads"),
                   "base_mhz": base, "load_percent": cpu_load,
                   "effective_mhz": round(base * perf / 100) if (base and perf) else None,
                   "effective_source": "счётчик % Processor Performance" if perf else None}
            temps, clocks, fans, powers = lhm.get("temperature", []), lhm.get("clock", []), lhm.get("fan", []), lhm.get("power", [])
            if has_lhm:
                cpu_temps = [s for s in temps if is_cpu_id(s["id"])]
                t = pick(cpu_temps, "package", "tctl", "tdie", "cpu total", "core (tctl", "core #1") or (cpu_temps[0] if cpu_temps else None)
                if t:
                    cpu["temp_c"] = t["value"]
                    cpu["temp_label"] = t["name"]
                cpu["core_temps"] = [{"name": s["name"], "c": s["value"]} for s in cpu_temps if "core #" in s["name"].lower()][:32]
                cpu_clocks = [s for s in clocks if is_cpu_id(s["id"])]
                cpu["core_clocks"] = [{"name": s["name"], "mhz": s["value"]} for s in cpu_clocks if "core #" in s["name"].lower()][:32]
                bus = pick(cpu_clocks, "bus speed")
                if bus:
                    cpu["bus_mhz"] = bus["value"]
                if cpu["core_clocks"]:
                    vals = [c["mhz"] for c in cpu["core_clocks"] if c["mhz"]]
                    if vals:
                        cpu["clock_mhz"] = round(max(vals))
                        cpu["clock_avg_mhz"] = round(sum(vals) / len(vals))
                p = pick([s for s in powers if is_cpu_id(s["id"])], "package", "cpu total", "cores")
                if p:
                    cpu["power_w"] = p["value"]
                    cpu["power_measured"] = True
                res["fans"] = [{"name": s["name"], "rpm": s["value"], "parent": s["parent"]} for s in fans if s["value"]]
                mb = pick([s for s in temps if not is_cpu_id(s["id"]) and not is_gpu_id(s["id"])], "motherboard", "system", "temperature")
                if mb:
                    res["motherboard_temp_c"] = mb["value"]
            else:
                acpi = [t for t in (raw.get("acpi_temps") or []) if isinstance(t, (int, float)) and 20 <= t <= 110]
                if acpi:
                    cpu["temp_c"] = acpi[0]
                    cpu["temp_label"] = "ACPI thermal zone"
                else:
                    cpu["temp_c"] = None
                    res["notes"].append("Температура и мощность CPU, обороты кулеров: нужен LibreHardwareMonitor "
                                        "(запустите его с опцией Remote/WMI — агент подхватит данные автоматически)")
                res["fans"] = [{"name": f.get("name"), "rpm": f.get("rpm")} for f in (raw.get("wmi_fans") or []) if f.get("rpm")]
            if not cpu.get("clock_mhz") and cpu.get("effective_mhz"):
                cpu["clock_mhz"] = cpu["effective_mhz"]
            if raw.get("cores") and not cpu.get("core_clocks") and base:
                cpu["core_clocks"] = [{"name": "Core " + c["id"], "mhz": round(base * _num(c.get("perf")) / 100)}
                                      for c in raw["cores"] if _num(c.get("perf")) is not None][:32]
            res["cpu"] = cpu
            mods = raw.get("ram") or []
            res["ram"] = {"modules": len(mods), "total_gb": round(sum(_num(m.get("gb")) or 0 for m in mods), 1) or None,
                          "speed_mhz": _num(mods[0].get("configured")) or _num(mods[0].get("speed")) if mods else None,
                          "rated_mhz": _num(mods[0].get("speed")) if mods else None,
                          "part": (mods[0].get("part") or "").strip() if mods else ""}
            if raw.get("battery"):
                b = raw["battery"][0]
                res["battery"] = {"percent": b.get("charge"), "status": b.get("status")}

        elif SYSTEM == "Linux":
            raw = self._linux_raw()
            self.raw = raw
            res["source"] = "hwmon"
            cpu = {"name": cpu_brand(), "cores": os.cpu_count(), "load_percent": cpu_load}
            for h in raw.get("hwmon", []):
                if h["name"] in ("k10temp", "coretemp", "zenpower") and h["temps"]:
                    t = next((x for x in h["temps"] if x["label"].lower() in ("tctl", "tdie", "package id 0")), h["temps"][0])
                    cpu["temp_c"] = t["c"]
                    cpu["temp_label"] = t["label"]
                for f in h["fans"]:
                    res["fans"].append({"name": "%s %s" % (h["name"], f["label"]), "rpm": f["rpm"]})
            mhz = raw.get("core_mhz") or []
            if mhz:
                cpu["core_clocks"] = [{"name": "Core %d" % i, "mhz": m} for i, m in enumerate(mhz)][:32]
                cpu["clock_mhz"] = max(mhz)
                cpu["clock_avg_mhz"] = round(sum(mhz) / len(mhz))
                cpu["effective_mhz"] = cpu["clock_avg_mhz"]
            res["cpu"] = cpu
            res["ram"] = {"total_gb": sample_ram().get("total_gb")}

        elif SYSTEM == "Darwin":
            raw = self._mac_raw()
            self.raw = raw
            res["source"] = "macos"
            res["cpu"] = {"name": cpu_brand(), "cores": os.cpu_count(), "load_percent": cpu_load,
                          "base_mhz": raw.get("cpu_max_mhz")}
            res["ram"] = {"total_gb": sample_ram().get("total_gb")}
            res["notes"].append("macOS не отдаёт температуру и мощность без прав администратора "
                                "(powermetrics требует sudo). Показаны загрузка и частоты, доступные без него.")

        # ---- power budget ---------------------------------------------------
        cpu = res["cpu"]
        gpu_w = round(sum((g.get("power_w") or 0) for g in gpus), 1)
        if cpu.get("power_w") is None:
            tdp = cpu_tdp_guess(cpu.get("name"))
            load = max(0.0, min(100.0, cpu_load)) / 100.0
            cpu["power_w"] = round(tdp * (0.22 + 0.78 * (load ** 1.25)), 1)
            cpu["power_measured"] = False
            cpu["tdp_w"] = tdp
        mods = res["ram"].get("modules") or (2 if (res["ram"].get("total_gb") or 0) > 8 else 1)
        ram_w = round(mods * (DIMM_IDLE_W + (DIMM_LOAD_W - DIMM_IDLE_W) * min(1.0, cpu_load / 100.0)), 1)
        res["ram"]["power_w_est"] = ram_w
        res["power"] = {"cpu_w": cpu.get("power_w"), "cpu_measured": bool(cpu.get("power_measured")),
                        "gpu_w": gpu_w or None, "ram_w_est": ram_w, "platform_w_est": PLATFORM_IDLE_W,
                        "total_w_est": round((cpu.get("power_w") or 0) + gpu_w + ram_w + PLATFORM_IDLE_W, 1),
                        "estimated": not bool(cpu.get("power_measured"))}
        res["available"] = bool(cpu.get("temp_c") or cpu.get("clock_mhz") or gpus)
        res["error"] = self.last_error
        return res


# ---------------------------------------------------------------------------
# NVML напрямую (nvml.dll / libnvidia-ml): те же данные, что у nvidia-smi, но за
# доли миллисекунды и без запуска процесса. Это позволяет обновлять частоты,
# температуру и мощность GPU каждую секунду, а не раз в 15–20 с вместе с
# медленным опросом WMI. Если библиотеки нет — тихий откат на nvidia-smi.
# ---------------------------------------------------------------------------

class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class Nvml:
    lib = None
    ok = None            # None — ещё не пробовали, False — недоступно
    lock = threading.Lock()
    CLOCK_GRAPHICS, CLOCK_SM, CLOCK_MEM, CLOCK_VIDEO = 0, 1, 2, 3

    @classmethod
    def load(cls):
        if cls.ok is not None:
            return cls.ok
        with cls.lock:
            if cls.ok is not None:
                return cls.ok
            names = (["nvml.dll", os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll"),
                      r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"] if IS_WIN
                     else ["libnvidia-ml.so.1", "libnvidia-ml.so"])
            for n in names:
                try:
                    cls.lib = ctypes.CDLL(n)
                    break
                except OSError:
                    continue
            if cls.lib is None:
                cls.ok = False
                return False
            try:
                cls.ok = cls.lib.nvmlInit_v2() == 0
            except Exception:
                cls.ok = False
            return cls.ok

    @classmethod
    def _u(cls, fn, handle, *args):
        """Вызов функции, возвращающей одно unsigned int; None при любой ошибке."""
        out = ctypes.c_uint()
        try:
            rc = getattr(cls.lib, fn)(handle, *args, ctypes.byref(out))
        except Exception:
            return None
        return int(out.value) if rc == 0 else None

    @classmethod
    def gpus(cls):
        """Полный набор датчиков по каждому GPU — та же форма, что у nvidia_gpus_detailed()."""
        if not cls.load():
            return None
        lib = cls.lib
        cnt = ctypes.c_uint()
        if lib.nvmlDeviceGetCount_v2(ctypes.byref(cnt)) != 0:
            return None
        out = []
        for i in range(cnt.value):
            h = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(i), ctypes.byref(h)) != 0:
                continue
            name = ctypes.create_string_buffer(96)
            lib.nvmlDeviceGetName(h, name, ctypes.c_uint(96))
            util = _NvmlUtil()
            has_util = lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(util)) == 0
            mem = _NvmlMem()
            has_mem = lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem)) == 0
            pstate = ctypes.c_int()
            has_ps = lib.nvmlDeviceGetPerformanceState(h, ctypes.byref(pstate)) == 0
            power = cls._u("nvmlDeviceGetPowerUsage", h)
            limit = cls._u("nvmlDeviceGetEnforcedPowerLimit", h)
            used_mb = round(mem.used / 1048576) if has_mem else None
            total_mb = round(mem.total / 1048576) if has_mem else None
            gr = cls._u("nvmlDeviceGetClockInfo", h, ctypes.c_int(cls.CLOCK_GRAPHICS))
            sm = cls._u("nvmlDeviceGetClockInfo", h, ctypes.c_int(cls.CLOCK_SM))
            out.append({
                "index": i, "name": name.value.decode("utf-8", "replace"),
                "temp_c": cls._u("nvmlDeviceGetTemperature", h, ctypes.c_int(0)),
                "mem_temp_c": None,                      # NVML не отдаёт для потребительских карт
                "core_clock_mhz": gr or sm, "sm_clock_mhz": sm,
                "mem_clock_mhz": cls._u("nvmlDeviceGetClockInfo", h, ctypes.c_int(cls.CLOCK_MEM)),
                "video_clock_mhz": cls._u("nvmlDeviceGetClockInfo", h, ctypes.c_int(cls.CLOCK_VIDEO)),
                "power_w": round(power / 1000, 1) if power is not None else None,
                "power_limit_w": round(limit / 1000, 1) if limit is not None else None,
                "fan_percent": cls._u("nvmlDeviceGetFanSpeed", h),
                "util_percent": int(util.gpu) if has_util else None,
                "mem_util_percent": int(util.memory) if has_util else None,
                "mem_used_mb": used_mb, "mem_total_mb": total_mb,
                "mem_percent": round(used_mb / max(1, total_mb) * 100, 1) if (used_mb is not None and total_mb) else None,
                "pstate": ("P%d" % pstate.value) if has_ps else "",
                "source": "nvml",
            })
        return out


_GPU_CACHE = {"ts": 0.0, "data": None}
GPU_CACHE_TTL = 1.0      # NVML дёшев, но десятки вызовов в секунду не нужны


def gpus_detailed_fresh():
    """Свежие датчики GPU (не старше секунды): NVML, иначе nvidia-smi."""
    now = time.time()
    if now - _GPU_CACHE["ts"] < GPU_CACHE_TTL and _GPU_CACHE["data"] is not None:
        return copy.deepcopy(_GPU_CACHE["data"])
    data = Nvml.gpus()
    if data is None:
        data = nvidia_gpus_detailed_smi()
    _GPU_CACHE.update(ts=now, data=data)
    return copy.deepcopy(data)


def nvidia_gpus_detailed():
    """Full per-GPU sensor set: NVML if available, otherwise nvidia-smi."""
    return gpus_detailed_fresh()


def nvidia_gpus_detailed_smi():
    """Full per-GPU sensor set via nvidia-smi (fallback when NVML is unavailable)."""
    fields = ["index", "name", "temperature.gpu", "temperature.memory", "clocks.sm", "clocks.gr", "clocks.mem",
              "clocks.video", "power.draw", "power.limit", "fan.speed", "utilization.gpu", "utilization.memory",
              "memory.used", "memory.total", "pstate"]
    rc, out, _ = _run(["nvidia-smi", "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"], timeout=4)
    if rc != 0:
        return []
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < len(fields):
            continue
        g = dict(zip(fields, p))
        used, total = _num(g["memory.used"]), _num(g["memory.total"])
        gpus.append({
            "index": int(_num(g["index"]) or 0), "name": g["name"],
            "temp_c": _num(g["temperature.gpu"]), "mem_temp_c": _num(g["temperature.memory"]),
            "core_clock_mhz": _num(g["clocks.gr"]) or _num(g["clocks.sm"]), "sm_clock_mhz": _num(g["clocks.sm"]),
            "mem_clock_mhz": _num(g["clocks.mem"]), "video_clock_mhz": _num(g["clocks.video"]),
            "power_w": _num(g["power.draw"]), "power_limit_w": _num(g["power.limit"]),
            "fan_percent": _num(g["fan.speed"]), "util_percent": _num(g["utilization.gpu"]),
            "mem_util_percent": _num(g["utilization.memory"]),
            "mem_used_mb": used, "mem_total_mb": total,
            "mem_percent": round(used / max(1, total) * 100, 1) if (used and total) else None,
            "pstate": g["pstate"],
        })
    return gpus


class Telemetry:
    """Background sampler; .get() returns the latest sample."""

    def __init__(self, start=True):
        self.lock = threading.Lock()
        self.cpu = CpuSampler()
        self.data = {}
        self.hostname = socket.gethostname()
        if start:
            threading.Thread(target=self.loop, daemon=True).start()

    def sample_once(self):
        cpu = self.cpu.sample()
        ram = sample_ram()
        gpus = sample_gpus()
        return {"version": VERSION, "hostname": self.hostname, "platform": f"{SYSTEM} {platform.release()} ({platform.machine()})",
                "cpu": cpu, "ram": ram, "gpus": gpus, "sensors": sensors.get() if sensors else {}, "ts": time.time()}

    def loop(self):
        while True:
            try:
                d = self.sample_once()
                with self.lock:
                    self.data = d
            except Exception as e:
                with self.lock:
                    self.data = {"version": VERSION, "hostname": self.hostname, "error": str(e)}
            time.sleep(1.5)

    def get(self):
        with self.lock:
            return dict(self.data)


# ---------------------------------------------------------------------------
# System / network information (admin view)
# ---------------------------------------------------------------------------

_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    c = _cache.get(key)
    if c and now - c[0] < ttl:
        return c[1]
    try:
        val = fn()
    except Exception as e:
        val = {"error": str(e)}
    _cache[key] = (now, val)
    return val


def local_ips():
    """All IPv4 addresses of this machine (best effort, no external deps)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    if IS_WIN:
        rc, out, _ = _run(["ipconfig"], timeout=5)
        for m in re.finditer(r"IPv4[^:]*:\s*([\d.]+)", out):
            ips.add(m.group(1))
    else:
        rc, out, _ = _run(["ifconfig"], timeout=5)
        if rc != 0:
            rc, out, _ = _run(["ip", "-4", "addr"], timeout=5)
        for m in re.finditer(r"inet (?:addr:)?([\d.]+)", out):
            ips.add(m.group(1))
    ips.discard("127.0.0.1")
    return sorted(ips)


def default_gateway():
    try:
        if IS_WIN:
            rc, out, _ = _run(["ipconfig"], timeout=5)
            m = re.search(r"Default Gateway[^:]*:\s*([\d.]+)", out) or re.search(r"Основной шлюз[^:]*:\s*([\d.]+)", out)
            return m.group(1) if m else ""
        if SYSTEM == "Darwin":
            rc, out, _ = _run(["route", "-n", "get", "default"], timeout=5)
            m = re.search(r"gateway:\s*([\d.]+)", out)
            return m.group(1) if m else ""
        rc, out, _ = _run(["ip", "route"], timeout=5)
        m = re.search(r"default via ([\d.]+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def wifi_ssid():
    try:
        if IS_WIN:
            rc, out, _ = _run(["netsh", "wlan", "show", "interfaces"], timeout=5)
            m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
            return m.group(1).strip() if m else ""
        if SYSTEM == "Darwin":
            rc, out, _ = _run(["ipconfig", "getsummary", "en0"], timeout=5)
            m = re.search(r"\bSSID\s*:\s*(.+)$", out, re.M)
            if m:
                return m.group(1).strip()
            rc, out, _ = _run(["networksetup", "-getairportnetwork", "en0"], timeout=5)
            m = re.search(r"Network:\s*(.+)$", out, re.M)
            return m.group(1).strip() if m else ""
        rc, out, _ = _run(["iwgetid", "-r"], timeout=5)
        return out.strip() if rc == 0 else ""
    except Exception:
        return ""


def tailscale_cmd():
    for c in (["tailscale"], [r"C:\Program Files\Tailscale\tailscale.exe"], ["/Applications/Tailscale.app/Contents/MacOS/Tailscale"], ["/usr/bin/tailscale"]):
        if shutil.which(c[0]) or os.path.exists(c[0]):
            return c
    return None


def tailscale_status():
    cmd = tailscale_cmd()
    if not cmd:
        return {"available": False}
    rc, out, err = _run(cmd + ["status", "--json"], timeout=8)
    if rc != 0:
        return {"available": True, "error": err.strip() or out.strip()}
    try:
        d = json.loads(out)
    except Exception as e:
        return {"available": True, "error": str(e)}
    def peer(p):
        return {"hostname": p.get("HostName"), "dns_name": (p.get("DNSName") or "").rstrip("."), "os": p.get("OS"),
                "ips": p.get("TailscaleIPs") or [], "online": p.get("Online"), "relay": p.get("Relay"),
                "last_seen": p.get("LastSeen"), "cur_addr": p.get("CurAddr"), "exit_node": p.get("ExitNode"),
                "rx_bytes": p.get("RxBytes"), "tx_bytes": p.get("TxBytes")}
    self_ = d.get("Self") or {}
    return {"available": True, "self": peer(self_), "magic_dns": d.get("MagicDNSSuffix"),
            "peers": [peer(p) for p in (d.get("Peer") or {}).values()], "backend_state": d.get("BackendState")}


def public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ip = r.read().decode().strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    return ip
        except Exception:
            continue
    return ""


def disks():
    out = []
    paths = [str(MODELS_DIR if MODELS_DIR.exists() else HOME)]
    if IS_WIN:
        for d in "CDEF":
            p = f"{d}:\\"
            if os.path.exists(p):
                paths.append(p)
    else:
        paths.append("/")
    seen = set()
    for p in paths:
        try:
            u = shutil.disk_usage(p)
            key = (u.total, u.free)
            if key in seen:
                continue
            seen.add(key)
            out.append({"path": p, "total_gb": round(u.total / 1024 ** 3, 1), "free_gb": round(u.free / 1024 ** 3, 1), "used_percent": round((u.total - u.free) / max(1, u.total) * 100, 1)})
        except Exception:
            pass
    return out


def lms_path():
    cands = [str(LMSTUDIO_DIR / "bin" / ("lms.exe" if IS_WIN else "lms")), "lms"]
    for c in cands:
        if os.path.exists(c) or shutil.which(c):
            return c
    return None


def lms(args, timeout=60):
    p = lms_path()
    if not p:
        return 127, "", "lms CLI not found (LM Studio → Developer → install lms)"
    return _run([p] + args, timeout=timeout)


def lms_json(args, timeout=60):
    rc, out, err = lms(args + ["--json"], timeout=timeout)
    if rc != 0:
        return None, (err or out).strip()
    try:
        return json.loads(out), ""
    except Exception:
        # some lms versions print a banner before json
        m = re.search(r"(\[.*\]|\{.*\})\s*$", out, re.S)
        if m:
            try:
                return json.loads(m.group(1)), ""
            except Exception:
                pass
        return None, "unparseable lms output"


def llama_processes():
    procs = []
    try:
        if psutil:
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    name = p.info.get('name') or ''
                    if re.search(r"llama|ollama|koboldcpp|vllm|LM Studio|lms", name, re.I) and "node_agent" not in name:
                        cmd = " ".join(p.info.get('cmdline') or [])
                        procs.append({"pid": p.info['pid'], "name": name, "cmd": cmd[:400]})
                except Exception:
                    pass
            return procs
        if IS_WIN:
            rc, out, _ = _run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
                               "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'llama|ollama|koboldcpp|vllm|LM Studio' } | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"], timeout=12)
            if rc == 0 and out.strip():
                data = json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                for p in data:
                    procs.append({"pid": p.get("ProcessId"), "name": p.get("Name"), "cmd": (p.get("CommandLine") or "")[:400]})
        else:
            rc, out, _ = _run(["ps", "-axo", "pid=,comm=,args="], timeout=8)
            for line in out.splitlines():
                if re.search(r"llama|ollama|koboldcpp|vllm|LM Studio|lms", line, re.I) and "node_agent" not in line:
                    parts = line.strip().split(None, 2)
                    if len(parts) >= 2:
                        procs.append({"pid": int(parts[0]), "name": os.path.basename(parts[1]), "cmd": (parts[2] if len(parts) > 2 else "")[:400]})
    except Exception:
        pass
    return procs


def models_dir_listing(max_items=400):
    items = []
    if not MODELS_DIR.exists():
        return items
    for root, dirs, files in os.walk(MODELS_DIR):
        for f in files:
            if f.lower().endswith((".gguf", ".safetensors", ".bin", ".mlx")) or "mlx" in root.lower():
                p = Path(root) / f
                try:
                    items.append({"path": str(p.relative_to(MODELS_DIR)).replace("\\", "/"), "size_gb": round(p.stat().st_size / 1024 ** 3, 2), "mtime": int(p.stat().st_mtime)})
                except Exception:
                    pass
            if len(items) >= max_items:
                return items
    items.sort(key=lambda x: -x["mtime"])
    return items


def sysinfo(light=False):
    tel = telemetry.get() if telemetry else {}
    info = {
        "version": VERSION, "hostname": socket.gethostname(), "platform": f"{SYSTEM} {platform.release()} ({platform.machine()})",
        "os_detail": platform.platform(), "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "python": platform.python_version(), "cpu": {"name": cpu_brand(), "cores": os.cpu_count()},
        "ram": tel.get("ram") or sample_ram(), "gpus": tel.get("gpus") or sample_gpus(),
        "uptime_s": _uptime(), "ts": time.time(), "sensors": sensors.get() if sensors else {},
    }
    info["network"] = {
        "local_ips": cached("local_ips", 60, local_ips), "gateway": cached("gateway", 120, default_gateway),
        "wifi_ssid": cached("wifi", 60, wifi_ssid), "public_ip": cached("public_ip", 600, public_ip),
        "tailscale": cached("ts", 20, tailscale_status),
    }
    if light:
        return info
    info["disks"] = cached("disks", 60, disks)
    info["lms"] = {"path": lms_path(), "models_dir": str(MODELS_DIR), "models_dir_exists": MODELS_DIR.exists()}
    ls, err = cached("lms_ls", 30, lambda: lms_json(["ls"], timeout=40))
    info["lms"]["models"] = ls if isinstance(ls, list) else []
    info["lms"]["error"] = err
    ps, _ = cached("lms_ps", 5, lambda: lms_json(["ps"], timeout=20))
    info["lms"]["loaded"] = ps if isinstance(ps, list) else []
    info["processes"] = cached("procs", 10, llama_processes)
    info["downloads"] = downloads.list()
    return info


def _uptime():
    try:
        if IS_WIN:
            return int(ctypes.windll.kernel32.GetTickCount64() / 1000)
        if SYSTEM == "Darwin":
            rc, out, _ = _run(["sysctl", "-n", "kern.boottime"], timeout=3)
            m = re.search(r"sec = (\d+)", out)
            return int(time.time() - int(m.group(1))) if m else 0
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Downloads (lms get / direct URL into the LM Studio models dir)
# ---------------------------------------------------------------------------

class Downloads:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}

    def list(self):
        with self.lock:
            return [self._pub(j) for j in sorted(self.jobs.values(), key=lambda j: -j["started"])][:30]

    def _pub(self, j):
        return {k: v for k, v in j.items() if k not in ("proc", "cancel")} | {"log": j["log"][-12:]}

    def start(self, source, dest_subdir=""):
        source = (source or "").strip()
        if not source:
            return {"error": "empty source"}
        jid = uuid.uuid4().hex[:8]
        job = {"id": jid, "source": source, "status": "queued", "progress": None, "speed_mbps": None, "done_bytes": 0,
               "total_bytes": None, "started": time.time(), "finished": None, "error": "", "log": [], "dest": "", "cancel": threading.Event(), "proc": None}
        with self.lock:
            self.jobs[jid] = job
        threading.Thread(target=self._worker, args=(job, dest_subdir), daemon=True).start()
        return self._pub(job)

    def cancel(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
        if not j:
            return False
        j["cancel"].set()
        p = j.get("proc")
        if p:
            try:
                p.kill()
            except Exception:
                pass
        return True

    def _worker(self, job, dest_subdir):
        src = job["source"]
        try:
            job["status"] = "running"
            if src.startswith("lms:"):
                self._lms_get(job, src[4:].strip())
            elif src.startswith("url:"):
                self._url(job, src[4:].strip(), dest_subdir)
            elif src.startswith("http://") or src.startswith("https://"):
                self._url(job, src, dest_subdir)
            else:
                self._lms_get(job, src)
            if job["status"] == "running":
                job["status"] = "cancelled" if job["cancel"].is_set() else "done"
                if job["status"] == "done":
                    job["progress"] = 100
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
        finally:
            job["finished"] = time.time()
            job["proc"] = None
            _cache.pop("lms_ls", None)

    def _lms_get(self, job, name):
        p = lms_path()
        if not p:
            raise RuntimeError("lms CLI not found on this node")
        proc = subprocess.Popen([p, "get", name, "-y"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
        job["proc"] = proc
        job["dest"] = str(MODELS_DIR)
        buf = ""
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf).strip()
                buf = ""
                if not line:
                    continue
                job["log"].append(line[:200])
                if len(job["log"]) > 200:
                    job["log"] = job["log"][-120:]
                m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
                if m:
                    job["progress"] = min(100.0, float(m.group(1)))
                m = re.search(r"([\d.]+)\s*(MB|GB|KB)/s", line, re.I)
                if m:
                    v = float(m.group(1)); u = m.group(2).upper()
                    job["speed_mbps"] = round(v * 8 * (1000 if u == "GB" else 1 if u == "MB" else 0.001), 1)
            else:
                buf += ch
            if job["cancel"].is_set():
                proc.kill()
                break
        rc = proc.wait()
        if rc != 0 and not job["cancel"].is_set():
            job["status"] = "error"
            job["error"] = f"lms get exited with code {rc}: " + " | ".join(job["log"][-3:])

    def _url(self, job, url, dest_subdir):
        fname = urllib.parse.unquote(url.split("?")[0].rstrip("/").split("/")[-1]) or "model.bin"
        sub = re.sub(r"[^\w.\-/]", "_", dest_subdir or "downloads")
        dest_dir = MODELS_DIR / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / fname
        part = dest.with_suffix(dest.suffix + ".part")
        job["dest"] = str(dest)
        req = urllib.request.Request(url, headers={"User-Agent": "BlackFoxAgent/" + VERSION})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            total = r.headers.get("Content-Length")
            job["total_bytes"] = int(total) if total else None
            t0 = time.time(); last = t0; last_b = 0; done = 0
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                job["done_bytes"] = done
                now = time.time()
                if now - last >= 1.0:
                    job["speed_mbps"] = round((done - last_b) * 8 / (now - last) / 1e6, 1)
                    last, last_b = now, done
                if job["total_bytes"]:
                    job["progress"] = round(done / job["total_bytes"] * 100, 1)
                if job["cancel"].is_set():
                    break
        if job["cancel"].is_set():
            try:
                part.unlink()
            except Exception:
                pass
            return
        os.replace(part, dest)
        job["log"].append(f"saved to {dest}")


downloads = Downloads()
telemetry = None  # set in main() / by the hub
sensors = None    # deep sensor sampler, started alongside telemetry


# ---------------------------------------------------------------------------
# Whitelisted management actions
# ---------------------------------------------------------------------------

ACTIONS = {
    "lms_ls": "Список моделей LM Studio на узле (lms ls)",
    "lms_ps": "Загруженные сейчас модели (lms ps)",
    "lms_load": "Загрузить модель в память: args {model, ctx?, gpu?('max'|'off'|0..1), identifier?, ttl?}",
    "lms_unload": "Выгрузить модель: args {identifier} или {all: true}",
    "lms_server": "Сервер LM Studio: args {cmd: 'status'|'start'|'stop', port?}",
    "lms_estimate": "Оценка ресурсов для загрузки модели без загрузки: args {model, ctx?}",
    "nvidia_smi": "Вывод nvidia-smi",
    "tailscale_status": "Состояние Tailscale (пиры, адреса)",
    "disk_usage": "Свободное место на дисках",
    "list_models_dir": "Файлы моделей в каталоге LM Studio",
    "delete_model": "Удалить файл модели из каталога LM Studio: args {path} (относительный путь из list_models_dir)",
    "processes": "Процессы inference-серверов на узле",
    "llama_server_start": "Запустить llama-server: args {model_path, port, ctx?, ngl?, device?, alias?, parallel?}",
    "llama_server_stop": "Остановить llama-server: args {port} или {pid}",
    "download": "Скачать модель: args {source: 'lms:<имя|hf-репозиторий[@квант]>' | 'url:https://…', dest_subdir?}",
    "download_cancel": "Отменить загрузку: args {id}",
    "sysinfo": "Полная информация об узле",
    "shell": "Выполнить команду («Агент ПК»): args {shell: 'powershell'|'cmd'|'bash', command, timeout?}. Только по подписи HMAC; BLACKFOX_NO_SHELL=1 выключает",
}

SHELL_MAX_OUTPUT = 60000


def run_shell(shell, command, timeout=120):
    """Неинтерактивное выполнение команды с таймаутом. Вывод в UTF-8 (PowerShell переключается явно)."""
    shell = (shell or "powershell").lower()
    command = command or ""
    if not command.strip():
        return {"ok": False, "error": "пустая команда"}
    timeout = max(5, min(int(timeout or 120), 1800))
    if shell == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            return {"ok": False, "error": "PowerShell не найден на этом узле"}
        prelude = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        argv = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", prelude + command]
    elif shell == "cmd":
        if not IS_WIN:
            return {"ok": False, "error": "cmd есть только на Windows"}
        argv = ["cmd.exe", "/d", "/c", "chcp 65001 >nul & " + command]
    elif shell == "bash":
        exe = shutil.which("bash") or shutil.which("sh")
        if not exe:
            return {"ok": False, "error": "bash не найден на этом узле"}
        argv = [exe, "-lc", command]
    else:
        return {"ok": False, "error": f"неизвестная оболочка: {shell}"}
    t0 = time.time()
    try:
        res = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, stdin=subprocess.DEVNULL,
                             cwd=str(HOME), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = {"exit_code": res.returncode, "stdout": _dec(res.stdout)[-SHELL_MAX_OUTPUT:], "stderr": _dec(res.stderr)[-SHELL_MAX_OUTPUT:],
               "duration_ms": int((time.time() - t0) * 1000), "timed_out": False, "shell": shell}
        return {"ok": res.returncode == 0, "output": out, "error": "" if res.returncode == 0 else f"код выхода {res.returncode}"}
    except subprocess.TimeoutExpired as e:
        out = {"exit_code": -1, "stdout": _dec(e.stdout or b"")[-SHELL_MAX_OUTPUT:], "stderr": _dec(e.stderr or b"")[-SHELL_MAX_OUTPUT:],
               "duration_ms": int((time.time() - t0) * 1000), "timed_out": True, "shell": shell}
        return {"ok": False, "output": out, "error": f"превышено время ({timeout} с) — процесс остановлен"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _dec(b):
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")


def find_llama_server():
    cands = []
    ext = LMSTUDIO_DIR / "extensions" / "backends"
    if ext.exists():
        for p in ext.rglob("llama-server*"):
            if p.is_file() and (p.suffix.lower() == ".exe" or not IS_WIN):
                cands.append(str(p))
    w = shutil.which("llama-server")
    if w:
        cands.append(w)
    cands.sort(reverse=True)
    return cands[0] if cands else None


def run_action(action, args=None, local=False):
    """Executes one whitelisted action. Returns {"ok": bool, "output": str|obj, "error": str}.
    local=True — вызов из процесса хаба на его же машине (без сетевого запроса); по сети флаг никогда не ставится."""
    args = args or {}
    try:
        if action not in ACTIONS:
            return {"ok": False, "error": f"неизвестное действие: {action}", "actions": list(ACTIONS)}
        if action == "shell":
            if os.environ.get("BLACKFOX_NO_SHELL"):
                return {"ok": False, "error": "выполнение команд на этом узле выключено (BLACKFOX_NO_SHELL)"}
            if not local and not AGENT_SECRET:
                return {"ok": False, "error": "выполнение команд доступно только по подписанному каналу (узел не спарен / нет секрета кластера)"}
            return run_shell(args.get("shell"), args.get("command"), args.get("timeout") or 120)
        if action == "sysinfo":
            return {"ok": True, "output": sysinfo()}
        if action == "lms_ls":
            data, err = lms_json(["ls"], timeout=40)
            return {"ok": data is not None, "output": data, "error": err}
        if action == "lms_ps":
            data, err = lms_json(["ps"], timeout=20)
            return {"ok": data is not None, "output": data, "error": err}
        if action == "lms_load":
            model = str(args.get("model") or "").strip()
            if not model or not re.match(r"^[\w.\-/@:+ ]+$", model):
                return {"ok": False, "error": "model required"}
            cmd = ["load", model, "-y"]
            if args.get("ctx"):
                cmd += ["--context-length", str(int(args["ctx"]))]
            if args.get("gpu") not in (None, ""):
                g = str(args["gpu"])
                if g not in ("max", "off") and not re.match(r"^(0(\.\d+)?|1(\.0+)?)$", g):
                    return {"ok": False, "error": "gpu must be max|off|0..1"}
                cmd += ["--gpu", g]
            if args.get("identifier"):
                cmd += ["--identifier", re.sub(r"[^\w.\-@]", "", str(args["identifier"]))]
            if args.get("ttl"):
                cmd += ["--ttl", str(int(args["ttl"]))]
            if args.get("parallel"):
                cmd += ["--parallel", str(int(args["parallel"]))]
            rc, out, err = lms(cmd, timeout=600)
            _cache.pop("lms_ps", None)
            return {"ok": rc == 0, "output": _clean(out), "error": _clean(err) if rc != 0 else ""}
        if action == "lms_estimate":
            model = str(args.get("model") or "").strip()
            cmd = ["load", model, "--estimate-only"]
            if args.get("ctx"):
                cmd += ["--context-length", str(int(args["ctx"]))]
            rc, out, err = lms(cmd, timeout=60)
            return {"ok": rc == 0, "output": _clean(out), "error": _clean(err) if rc != 0 else ""}
        if action == "lms_unload":
            cmd = ["unload", "--all"] if args.get("all") else ["unload", re.sub(r"[^\w.\-@/:+]", "", str(args.get("identifier") or ""))]
            if cmd[-1] == "":
                return {"ok": False, "error": "identifier or all required"}
            rc, out, err = lms(cmd, timeout=60)
            _cache.pop("lms_ps", None)
            return {"ok": rc == 0, "output": _clean(out), "error": _clean(err) if rc != 0 else ""}
        if action == "lms_server":
            sub = str(args.get("cmd") or "status")
            if sub not in ("status", "start", "stop"):
                return {"ok": False, "error": "cmd must be status|start|stop"}
            cmd = ["server", sub]
            if sub == "start" and args.get("port"):
                cmd += ["--port", str(int(args["port"]))]
            rc, out, err = lms(cmd, timeout=60)
            return {"ok": rc == 0, "output": _clean(out + "\n" + err)}
        if action == "nvidia_smi":
            rc, out, err = _run(["nvidia-smi"], timeout=8)
            return {"ok": rc == 0, "output": out or err}
        if action == "tailscale_status":
            return {"ok": True, "output": tailscale_status()}
        if action == "disk_usage":
            return {"ok": True, "output": disks()}
        if action == "list_models_dir":
            return {"ok": True, "output": {"dir": str(MODELS_DIR), "files": models_dir_listing()}}
        if action == "delete_model":
            rel = str(args.get("path") or "")
            target = (MODELS_DIR / rel).resolve()
            if not rel or MODELS_DIR.resolve() not in target.parents or not target.is_file():
                return {"ok": False, "error": "path must be a file inside the models dir"}
            target.unlink()
            _cache.pop("lms_ls", None)
            return {"ok": True, "output": f"deleted {rel}"}
        if action == "processes":
            return {"ok": True, "output": llama_processes()}
        if action == "llama_server_start":
            exe = args.get("exe") or find_llama_server()
            if not exe or not os.path.exists(exe):
                return {"ok": False, "error": "llama-server executable not found"}
            mp = str(args.get("model_path") or "")
            model = (MODELS_DIR / mp).resolve() if not os.path.isabs(mp) else Path(mp)
            if not model.is_file():
                return {"ok": False, "error": f"model file not found: {model}"}
            port = int(args.get("port") or 1235)
            cmd = [exe, "-m", str(model), "--port", str(port), "--host", str(args.get("host") or "127.0.0.1"),
                   "-c", str(int(args.get("ctx") or 8192)), "-ngl", str(int(args.get("ngl") if args.get("ngl") is not None else 99)),
                   "-np", str(int(args.get("parallel") or 1)), "--jinja"]
            if args.get("device"):
                cmd += ["--device", re.sub(r"[^\w,]", "", str(args["device"]))]
            if args.get("alias"):
                cmd += ["--alias", re.sub(r"[^\w.\-@]", "", str(args["alias"]))]
            if args.get("reasoning_off", True):
                cmd += ["--reasoning", "off", "--reasoning-format", "none"]
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0) if IS_WIN else 0
            log = open(LMSTUDIO_DIR / f"llama-server-{port}.log", "ab")
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=flags, start_new_session=not IS_WIN)
            _cache.pop("procs", None)
            return {"ok": True, "output": {"pid": proc.pid, "cmd": " ".join(cmd)}}
        if action == "llama_server_stop":
            pid = args.get("pid")
            if not pid and args.get("port"):
                for p in llama_processes():
                    if f"--port {int(args['port'])}" in (p.get("cmd") or "") or f"--port={int(args['port'])}" in (p.get("cmd") or ""):
                        pid = p["pid"]
            if not pid:
                return {"ok": False, "error": "no matching llama-server process"}
            if IS_WIN:
                rc, out, err = _run(["taskkill", "/F", "/PID", str(int(pid))], timeout=10)
            else:
                os.kill(int(pid), 15)
                rc, out, err = 0, "sent SIGTERM", ""
            _cache.pop("procs", None)
            return {"ok": rc == 0, "output": out, "error": err if rc != 0 else ""}
        if action == "download":
            return {"ok": True, "output": downloads.start(args.get("source"), args.get("dest_subdir", ""))}
        if action == "download_cancel":
            return {"ok": downloads.cancel(str(args.get("id") or "")), "output": "cancel requested"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "unhandled"}


def _clean(s):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s or "").strip()[-4000:]


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Security: request signing (HMAC-SHA256 + timestamp + nonce) and IP allowlist
#
# Shared by the hub, which imports this module, so both sides use one
# implementation.  A signed request proves the caller knows the cluster secret
# without ever putting it on the wire, and the timestamp+nonce make a captured
# request useless a couple of minutes later (replay protection).
# ---------------------------------------------------------------------------

SIG_WINDOW_S = 120          # how far a request timestamp may drift
_nonce_seen = {}            # nonce -> first seen monotonic time
_nonce_lock = threading.Lock()

PRIVATE_NETS = [ipaddress.ip_network(c) for c in
                ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "169.254.0.0/16", "100.64.0.0/10",           # incl. Tailscale CGNAT
                 "::1/128", "fc00::/7", "fe80::/10")]


def ip_in(ip, nets):
    try:
        a = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    for n in nets:
        try:
            if a in n:
                return True
        except TypeError:
            continue
    return False


def parse_cidrs(items):
    out = []
    for c in items or []:
        c = str(c).strip()
        if not c:
            continue
        try:
            out.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            try:
                out.append(ipaddress.ip_network(c + ("/32" if ":" not in c else "/128"), strict=False))
            except ValueError:
                pass
    return out


def _is_private(ip):
    return ip_in(ip, PRIVATE_NETS)


def canonical_request(method, path, body, ts, nonce):
    if isinstance(body, str):
        body = body.encode("utf-8")
    digest = hashlib.sha256(body or b"").hexdigest()
    parts = [method.upper(), path, str(ts), str(nonce), digest]
    return chr(10).join(parts).encode("utf-8")


def sign_headers(secret, method, path, body=b""):
    """Headers proving this request was made by someone holding `secret`."""
    ts = str(int(time.time()))
    nonce = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), canonical_request(method, path, body, ts, nonce), hashlib.sha256).hexdigest()
    return {"X-BF-Ts": ts, "X-BF-Nonce": nonce, "X-BF-Sig": sig}


def verify_signature(secret, method, path, body, headers):
    """-> (ok, reason). Rejects bad signatures, stale timestamps and replays."""
    ts = headers.get("X-BF-Ts") or ""
    nonce = headers.get("X-BF-Nonce") or ""
    sig = headers.get("X-BF-Sig") or ""
    if not (ts and nonce and sig):
        return False, "нет подписи"
    try:
        drift = abs(time.time() - int(ts))
    except ValueError:
        return False, "некорректная метка времени"
    if drift > SIG_WINDOW_S:
        return False, f"метка времени устарела на {int(drift)} с (проверьте часы)"
    expect = hmac.new(secret.encode("utf-8"), canonical_request(method, path, body, ts, nonce), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return False, "подпись не совпадает"
    now = time.monotonic()
    with _nonce_lock:
        for k, seen in list(_nonce_seen.items()):
            if now - seen > SIG_WINDOW_S * 2:
                del _nonce_seen[k]
        if nonce in _nonce_seen:
            return False, "повтор запроса (nonce уже использован)"
        _nonce_seen[nonce] = now
    return True, ""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "BlackFoxAgent/" + VERSION
    protocol_version = "HTTP/1.1"   # keep-alive; all responses carry Content-Length

    def log_message(self, *a):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, body=b""):
        """Control endpoints: IP allowlist first, then signature / token / private-network."""
        ip = self.client_address[0]
        allowed_net = _is_private(ip) or (ALLOW_CIDRS and ip_in(ip, ALLOW_CIDRS))
        if not allowed_net:
            self._deny = f"адрес {ip} вне разрешённых сетей"
            return False
        if AGENT_SECRET:
            ok, why = verify_signature(AGENT_SECRET, self.command, urllib.parse.urlparse(self.path).path, body, self.headers)
            if not ok:
                self._deny = why
            return ok
        if AGENT_TOKEN:
            ok = hmac.compare_digest(self.headers.get("X-BF-Token") or "", AGENT_TOKEN)
            if not ok:
                self._deny = "неверный токен"
            return ok
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/telemetry"):
            return self._json(telemetry.get())
        if parsed.path == "/sensors":
            return self._json(sensors.get() if sensors else {"available": False, "error": "сборщик датчиков не запущен"})
        if parsed.path == "/sysinfo":
            if not self._authorized():
                return self._json({"error": "forbidden", "reason": getattr(self, "_deny", "")}, 403)
            q = urllib.parse.parse_qs(parsed.query)
            return self._json(sysinfo(light=bool(q.get("light"))))
        if parsed.path == "/downloads":
            return self._json({"downloads": downloads.list()})
        if parsed.path == "/actions":
            return self._json({"actions": ACTIONS})
        if parsed.path == "/speedtest":
            q = urllib.parse.parse_qs(parsed.query)
            n = min(50_000_000, max(1000, int((q.get("bytes") or ["1000000"])[0])))
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
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/upload":
            n = int(self.headers.get("Content-Length", 0) or 0)
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            return self._json({"received": n - remaining})
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
        except Exception:
            body = {}
        if parsed.path in ("/exec", "/download", "/download/cancel"):
            if not self._authorized(raw):
                return self._json({"error": "forbidden", "reason": getattr(self, "_deny", "")}, 403)
        if parsed.path == "/exec":
            return self._json(run_action(body.get("action", ""), body.get("args") or {}))
        if parsed.path == "/download":
            return self._json(downloads.start(body.get("source", ""), body.get("dest_subdir", "")))
        if parsed.path == "/download/cancel":
            return self._json({"ok": downloads.cancel(str(body.get("id") or ""))})
        self._json({"error": "not found"}, 404)


def main():
    global AGENT_TOKEN, AGENT_SECRET, ALLOW_CIDRS, telemetry, sensors
    args = [a for a in sys.argv[1:]]
    port = 8766
    if args and args[0].isdigit():
        port = int(args.pop(0))

    def opt(name):
        if name in args:
            i = args.index(name)
            return args[i + 1] if i + 1 < len(args) else ""
        return ""

    AGENT_TOKEN = opt("--token")
    AGENT_SECRET = opt("--secret") or os.environ.get("BLACKFOX_SECRET", "")
    ALLOW_CIDRS = parse_cidrs([c for c in opt("--allow").split(",") if c.strip()])
    bind = opt("--bind") or "0.0.0.0"
    telemetry = Telemetry()
    sensors = Sensors()
    srv = http.server.ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    mode = "подпись HMAC" if AGENT_SECRET else ("токен" if AGENT_TOKEN else "только приватные сети")
    print(f"[*] BlackFox node agent v{VERSION} on {bind}:{port}  ({SYSTEM}, psutil={'yes' if psutil else 'no'}, lms={'yes' if lms_path() else 'no'})")
    print(f"[*] Защита управления: {mode}" + (f"; дополнительные сети: {[str(c) for c in ALLOW_CIDRS]}" if ALLOW_CIDRS else ""))
    print(f"[*] models dir: {MODELS_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
else:
    telemetry = Telemetry()
    sensors = Sensors()
