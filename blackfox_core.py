"""
BlackFox AI Workstation — ядро приложения (sidecar для оболочки Tauri).

Один процесс, режим аргументом (docs/ADR_APP.md §3.1):

  blackfox_core --mode hub  --port 8765 --data <dir> --control 8770 --control-token <t>
  blackfox_core --mode node --agent-port 8766 --hub https://<хаб>:8765 --data <dir> \
                --control 8770 --control-token <t>

  hub  — поднимает хаб (hub_server) + локальный агент; интерфейс отдаёт сам хаб.
  node — поднимает агент (node_agent) и прокси интерфейса хаба: всё, что не
         /control/*, на http://127.0.0.1:<control>/ уходит на хаб — оболочке
         не нужно знать его адрес, а интерфейс грузится с абсолютными путями.

Control API (только 127.0.0.1, заголовок X-Control-Token — токен создаёт оболочка):
  GET  /control/status        режим, порты, состояние подсистем, версия
  POST /control/login         {role, password} → сессионный токен хаба
  POST /control/logout        {token}
  POST /control/pair          {hub_url, code} → регистрация узла (хаб: B3)
  POST /control/mode          {mode, hub_url} → сохранить и перезапустить только ядро
  POST /control/restart       перезапустить только ядро (окно приложения остаётся открытым)
  GET  /control/telemetry     телеметрия этой машины
  POST /control/shutdown

Stdlib only. Данные — в --data (у приложения: %APPDATA%\\BlackFox), лог — <data>/core.log.
"""

import http.server
import hashlib
import hmac
import http.client
import json
import re
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CORE_VERSION = "0.1.0"
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # PyInstaller onefile → временная папка
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))
STARTED = time.time()


# ---------------------------------------------------------------------------
# аргументы и окружение
# ---------------------------------------------------------------------------
def parse_args(argv):
    a = {"mode": "node", "port": 8765, "agent_port": 8766, "hub": "", "data": "", "control": 8770,
         "control_token": "", "web": "", "bind": "0.0.0.0"}
    i = 0
    while i < len(argv):
        k = argv[i]
        v = argv[i + 1] if i + 1 < len(argv) else ""
        if k == "--mode":
            a["mode"] = v; i += 2
        elif k == "--port":
            a["port"] = int(v); i += 2
        elif k == "--agent-port":
            a["agent_port"] = int(v); i += 2
        elif k == "--hub":
            a["hub"] = v.rstrip("/"); i += 2
        elif k == "--data":
            a["data"] = v; i += 2
        elif k == "--web":
            a["web"] = v; i += 2
        elif k == "--control":
            a["control"] = int(v); i += 2
        elif k == "--control-token":
            a["control_token"] = v; i += 2
        elif k == "--bind":
            a["bind"] = v; i += 2
        else:
            i += 1
    if not a["data"]:
        base = os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        a["data"] = str(Path(base) / "BlackFox")
    if not a["web"]:
        a["web"] = str(BASE_DIR / "web") if (BASE_DIR / "web").exists() else str(SRC_DIR / "web")
    return a


ARGS = parse_args(sys.argv[1:])
DATA_DIR = Path(ARGS["data"])
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["BLACKFOX_DATA"] = str(DATA_DIR)
os.environ["BLACKFOX_WEB"] = ARGS["web"]
NODE_FILE = DATA_DIR / "node.json"          # режим node: {hub_url, secret, node_key, paired_at}
CORE_FILE = DATA_DIR / "core.json"          # последняя выбранная конфигурация (режим, хаб)

LOG_PATH = DATA_DIR / "core.log"
_log_lock = threading.Lock()
_REDIRECTED = {"stdout": False, "stderr": False}    # stdout уже пишет в core.log — строки не дублировать


def _stdio_to_log():
    """Sidecar без консоли (PyInstaller --noconsole, закрытый pipe оболочки): stdout
    либо None, либо print() падает с OSError 22. Тогда весь вывод — и хаба, и агента,
    которые пишут print() — уходит в core.log вместо падения процесса."""
    for name in ("stdout", "stderr"):
        st = getattr(sys, name)
        ok = False
        try:
            if st is not None:
                st.write("")
                st.flush()
                ok = True
        except Exception:
            ok = False
        if not ok:
            try:
                setattr(sys, name, open(LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1))
                _REDIRECTED[name] = True
            except Exception:
                setattr(sys, name, open(os.devnull, "w"))


_stdio_to_log()


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    try:
        print(line, flush=True)
    except Exception:
        _stdio_to_log()          # труба оборвалась на ходу — переключаемся на файл и продолжаем
    if _REDIRECTED["stdout"]:
        return
    try:
        with _log_lock, open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# подсистемы
# ---------------------------------------------------------------------------
STATE = {"mode": ARGS["mode"], "hub_url": ARGS["hub"], "hub": {"running": False, "error": ""},
         "agent": {"running": False, "error": ""}, "errors": []}
agentlib = None
hubmod = None


def hub_base():
    """Адрес хаба, куда ходит ядро: в режиме hub — сам себе, в node — из аргумента/node.json."""
    if STATE["mode"] == "hub":
        scheme = "https" if hubmod and hubmod.scheme_is_https() else "http"
        return "%s://127.0.0.1:%d" % (scheme, ARGS["port"])
    return STATE["hub_url"] or read_json(NODE_FILE, {}).get("hub_url", "")


def start_agent():
    """Агент телеметрии этой машины (в обоих режимах)."""
    global agentlib
    import node_agent as agentlib
    node = read_json(NODE_FILE, {})
    secret = node.get("secret") or os.environ.get("BLACKFOX_SECRET", "")
    if STATE["mode"] == "hub" and hubmod is not None:
        secret = hubmod.security.agent_secret() or secret     # хаб-машина подписывается своим же секретом
    agentlib.AGENT_SECRET = secret
    try:
        srv = http.server.ThreadingHTTPServer((ARGS["bind"], ARGS["agent_port"]), agentlib.Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True, name="agent").start()
        STATE["agent"] = {"running": True, "error": "", "port": ARGS["agent_port"], "signed": bool(secret)}
        log("агент: порт %d, подпись команд: %s" % (ARGS["agent_port"], "да" if secret else "нет (узел не спарен)"))
    except Exception as e:
        STATE["agent"] = {"running": False, "error": str(e)}
        log("агент: не запущен — %s" % e)


def start_hub():
    global hubmod
    try:
        import hub_server as hubmod
        threading.Thread(target=hubmod.start_server, args=(ARGS["port"],), daemon=True, name="hub").start()
        for _ in range(60):
            time.sleep(0.25)
            if _port_open("127.0.0.1", ARGS["port"]):
                break
        STATE["hub"] = {"running": True, "error": "", "port": ARGS["port"], "tls": hubmod.scheme_is_https()}
        log("хаб: порт %d (%s)" % (ARGS["port"], "https" if hubmod.scheme_is_https() else "http"))
    except Exception as e:
        STATE["hub"] = {"running": False, "error": str(e)}
        log("хаб: не запущен — %s" % e)


def _port_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _normalise_fingerprint(value):
    """Return a SHA-256 certificate fingerprint as 64 uppercase hex chars, or empty.

    We deliberately accept the usual colon-separated presentation too, but never
    let a malformed value disable TLS verification.
    """
    value = re.sub(r"[^0-9a-fA-F]", "", str(value or ""))
    return value.upper() if len(value) == 64 else ""


def _node_fingerprint():
    return _normalise_fingerprint(read_json(NODE_FILE, {}).get("cert_sha256"))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection which accepts an explicitly pinned leaf certificate only."""
    def __init__(self, host, fingerprint, **kwargs):
        self._fingerprint = fingerprint
        super().__init__(host, **kwargs)

    def connect(self):
        super().connect()
        cert = self.sock.getpeercert(binary_form=True) if self.sock else None
        actual = hashlib.sha256(cert or b"").hexdigest().upper()
        if not cert or not hmac.compare_digest(actual, self._fingerprint):
            self.close()
            raise ssl.SSLCertVerificationError("отпечаток сертификата хаба не совпадает с сохранённым")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, fingerprint):
        super().__init__(context=ssl._create_unverified_context())
        self._fingerprint = fingerprint

    def https_open(self, req):
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(host, self._fingerprint, context=self._context, **kwargs), req
        )


def _pki_ok(url, timeout=10):
    """True, если сертификат хаба проходит обычную проверку (публичный ЦС, имя совпадает).
    Такой хаб НЕ пиннится: сертификаты Let's Encrypt через Tailscale продлеваются
    автоматически (у хаба цикл каждые 12 ч), и закреплённый отпечаток сломал бы
    узел при первом же продлении."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return True
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=timeout) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname):
                return True
    except ssl.SSLCertVerificationError:
        return False


def _observed_fingerprint(url, timeout=10):
    """Read the current leaf certificate only while pairing a new node (TOFU)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    port = parsed.port or 443
    with socket.create_connection((parsed.hostname, port), timeout=timeout) as sock:
        with ssl._create_unverified_context().wrap_socket(sock, server_hostname=parsed.hostname) as tls:
            cert = tls.getpeercert(binary_form=True)
    return hashlib.sha256(cert or b"").hexdigest().upper() if cert else ""


def _hub_open(req, base, timeout, fingerprint=None):
    """Open a hub request with normal PKI or an existing self-signed certificate pin.

    A pin wins over every other mode.  Thus a node that has paired once cannot
    silently follow a different self-signed certificate.  Loopback is kept as a
    compatibility exception for the local hub mode, where both endpoints belong
    to this process.
    """
    parsed = urllib.parse.urlparse(base)
    pin = _normalise_fingerprint(fingerprint) or _node_fingerprint()
    if parsed.scheme == "https" and pin:
        return urllib.request.build_opener(_PinnedHTTPSHandler(pin)).open(req, timeout=timeout)
    if parsed.scheme == "https" and parsed.hostname in ("127.0.0.1", "::1", "localhost"):
        return urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())
    return urllib.request.urlopen(req, timeout=timeout)


def hub_request(method, path, body=None, headers=None, timeout=15, fingerprint=None):
    """Request the hub, enforcing the certificate fingerprint stored for a node."""
    base = hub_base()
    if not base:
        raise RuntimeError("адрес хаба не задан")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"User-Agent": "BlackFoxCore/%s" % CORE_VERSION, "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    with _hub_open(req, base, timeout, fingerprint) as r:
        return r.status, r.read()


def restart_core(mode=None, hub_url=None):
    """Replace only the sidecar process, preserving its control port and app window.

    The old implementation exited with code 3 and relied on an external watcher
    which the current Tauri shell does not have.  exec keeps the desktop app up
    and starts the requested mode again with the same isolated data directory.
    """
    mode = mode or STATE["mode"]
    hub_url = (hub_url if hub_url is not None else STATE["hub_url"]).rstrip("/")
    write_json(CORE_FILE, {"mode": mode, "hub_url": hub_url})
    args = ["--mode", mode, "--port", str(ARGS["port"]), "--agent-port", str(ARGS["agent_port"]),
            "--control", str(ARGS["control"]), "--data", str(DATA_DIR), "--web", ARGS["web"], "--bind", ARGS["bind"]]
    if hub_url:
        args += ["--hub", hub_url]
    if ARGS["control_token"]:
        args += ["--control-token", ARGS["control_token"]]
    if getattr(sys, "frozen", False):
        command = [sys.executable] + args
    else:
        # On Windows os.execv forwards argv through CreateProcess.  Quote the
        # source path explicitly: the project folder contains a space and an
        # unquoted argument makes Python try to execute only the part before the space.
        command = [sys.executable, '"%s"' % Path(__file__).resolve()] + args
    log("перезапуск ядра: режим %s" % mode)
    os.execv(sys.executable, command)


# ---------------------------------------------------------------------------
# control API + прокси интерфейса
# ---------------------------------------------------------------------------
class ControlHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # Оболочка (WebView) живёт на своём origin — tauri://localhost на Windows/Linux,
    # http(s)://tauri.localhost, в dev-режиме http://localhost:<port>. Без CORS браузер
    # внутри оболочки не пропустит запрос к control API. Разрешаем только эти
    # origin'ы; токен всё равно обязателен, поэтому чужая страница без него
    # ничего не сделает даже с разрешённого origin.
    _CORS_RE = re.compile(r"^(tauri://localhost|https?://tauri\.localhost|https?://(localhost|127\.0\.0\.1)(:\d+)?)$")

    def _cors(self):
        origin = self.headers.get("Origin") or ""
        if origin and self._CORS_RE.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Control-Token, X-BF-Auth")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        # preflight для control API из оболочки; для прокси хаба CORS не нужен
        # (интерфейс грузится с того же origin, что и прокси).
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        tok = ARGS["control_token"]
        return (not tok) or self.headers.get("X-Control-Token") == tok

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return {}

    # ---- GET ---------------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/control/"):
            return self._proxy()          # всё, что не control — интерфейс хаба (режим node) или сам хаб
        if not self._authorized():
            return self._json({"error": "control token"}, 403)
        if path == "/control/status":
            node = read_json(NODE_FILE, {})
            return self._json({"version": CORE_VERSION, "mode": STATE["mode"], "hub_url": hub_base(),
                               "hub": STATE["hub"], "agent": STATE["agent"], "paired": bool(node.get("secret")),
                               "node_key": node.get("node_key"), "data_dir": str(DATA_DIR),
                               "certificate_pinned": bool(_normalise_fingerprint(node.get("cert_sha256"))),
                               "hub_reachable": _port_open(*_host_port(hub_base())) if hub_base() else False,
                               "uptime_s": round(time.time() - STARTED), "errors": STATE["errors"][-10:]})
        if path == "/control/telemetry":
            t = agentlib.telemetry.get() if agentlib else {}
            return self._json(t or {"error": "агент не запущен"})
        return self._json({"error": "not found"}, 404)

    # ---- POST --------------------------------------------------------------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/control/"):
            return self._proxy()
        if not self._authorized():
            return self._json({"error": "control token"}, 403)
        body = self._body()
        try:
            if path == "/control/login":
                role = body.get("role") or ""
                st, raw = hub_request("POST", "/api/auth/login", {"password": body.get("password", ""), "role": role})
                data = json.loads(raw or b"{}")
                if role and data.get("role") and data["role"] != role:
                    # хаб до B3 проверяет «любой из паролей»; приложение выбрало роль явно
                    return self._json({"error": "пароль не от роли «%s»" % ("администратор" if role == "admin" else "пользователь")}, 401)
                return self._json(data, st)
            if path == "/control/logout":
                st, raw = hub_request("POST", "/api/auth/logout", {}, {"X-BF-Auth": body.get("token", "")})
                return self._json(json.loads(raw or b"{}"), st)
            if path == "/control/pair":
                return self._pair(body)
            if path == "/control/mode":
                mode = body.get("mode")
                if mode not in ("hub", "node"):
                    return self._json({"error": "mode: hub|node"}, 400)
                hub_url = (body.get("hub_url") or "").rstrip("/")
                self._json({"status": "restart", "note": "ядро перезапускает свои подсистемы; окно приложения остаётся открытым"})
                threading.Timer(0.3, lambda: restart_core(mode, hub_url)).start()
                return
            if path == "/control/restart":
                self._json({"status": "restart", "note": "ядро перезапускает свои подсистемы; окно приложения остаётся открытым"})
                threading.Timer(0.3, restart_core).start()
                return
            if path == "/control/shutdown":
                self._json({"status": "bye"})
                log("штатная остановка")
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
        except urllib.error.HTTPError as e:
            try:
                return self._json(json.loads(e.read() or b"{}"), e.code)
            except Exception:
                return self._json({"error": "хаб: HTTP %d" % e.code}, e.code)
        except Exception as e:
            return self._json({"error": str(e)}, 502)
        return self._json({"error": "not found"}, 404)

    def _pair(self, body):
        hub = (body.get("hub_url") or "").rstrip("/")
        code = (body.get("code") or "").strip().upper()
        if not hub or not code:
            return self._json({"error": "нужны hub_url и code"}, 400)
        # With a public CA certificate the platform validates the name normally.
        # A self-signed hub has no chain to validate, so pair it once using the
        # supplied QR fingerprint or TOFU and pin that exact leaf certificate.
        # Subsequent calls (including the UI proxy) use only this stored pin.
        fingerprint = _normalise_fingerprint(body.get("cert_sha256"))
        if hub.startswith("https://") and not fingerprint and not _pki_ok(hub):
            fingerprint = _observed_fingerprint(hub)
            log("хаб %s: сертификат не проходит обычную проверку — закрепляю отпечаток (TOFU)" % hub)
        STATE["hub_url"] = hub
        hw = agentlib.telemetry.get() if agentlib else {}
        payload = {"code": code, "hostname": socket.gethostname(), "agent_port": ARGS["agent_port"],
                   "platform": hw.get("platform"), "gpus": hw.get("gpus") or [], "ram": hw.get("ram") or {}, "cpu": hw.get("cpu") or {},
                   "core_version": CORE_VERSION}
        st, raw = hub_request("POST", "/api/node/pair", payload, fingerprint=fingerprint)
        data = json.loads(raw or b"{}")
        if st != 200 or not data.get("secret"):
            return self._json(data or {"error": "хаб отказал"}, st if st != 200 else 502)
        node = {"hub_url": hub, "secret": data["secret"], "node_key": data.get("key"), "paired_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if fingerprint:
            node["cert_sha256"] = fingerprint
        write_json(NODE_FILE, node)
        try:
            os.chmod(NODE_FILE, 0o600)
        except Exception:
            pass
        if agentlib:
            agentlib.AGENT_SECRET = node["secret"]
            STATE["agent"]["signed"] = True
        log("узел спарен с %s как %s" % (hub, node["node_key"]))
        return self._json({"status": "ok", "node_key": node["node_key"], "hub_url": hub})

    # ---- прокси интерфейса хаба (режим node) --------------------------------
    def _proxy(self):
        base = hub_base()
        if not base:
            return self._json({"error": "хаб не задан — пройдите мастер подключения"}, 503)
        target = base + self.path
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else None
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
        fwd["X-Forwarded-For"] = self.client_address[0]
        try:
            req = urllib.request.Request(target, data=data, method=self.command, headers=fwd)
            with _hub_open(req, base, 180) as r:
                body = r.read()
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json({"error": "хаб недоступен: %s" % e}, 502)


def _host_port(url):
    p = urllib.parse.urlparse(url)
    return p.hostname or "127.0.0.1", p.port or (443 if p.scheme == "https" else 80)


# ---------------------------------------------------------------------------
def main():
    saved = read_json(CORE_FILE, {})
    if "--mode" not in sys.argv and saved.get("mode"):
        STATE["mode"] = ARGS["mode"] = saved["mode"]
    if not STATE["hub_url"] and saved.get("hub_url"):
        STATE["hub_url"] = saved["hub_url"]
    log("BlackFox core v%s: режим %s, данные %s" % (CORE_VERSION, STATE["mode"], DATA_DIR))
    if STATE["mode"] == "hub":
        start_hub()
    start_agent()
    ctl = http.server.ThreadingHTTPServer(("127.0.0.1", ARGS["control"]), ControlHandler)
    ctl.daemon_threads = True
    log("control API: http://127.0.0.1:%d  (токен: %s)" % (ARGS["control"], "задан" if ARGS["control_token"] else "НЕТ — только для разработки"))
    try:
        ctl.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
