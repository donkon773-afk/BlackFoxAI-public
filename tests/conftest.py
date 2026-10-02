"""Общие фикстуры: изолированный хаб и ядро на свободных портах с пустой data/.

Живой хаб на 8765 не трогается: каждый тест-сеанс поднимает свою копию
(`python hub_server.py <port>` с BLACKFOX_DATA=<tmp>) и гасит её в конце.
"""
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ADMIN_PW = "A-" + secrets.token_urlsafe(12)   # пароли временного хаба — новые на каждый прогон
USER_PW = "U-" + secrets.token_urlsafe(8)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


class Client:
    def __init__(self, base, token="", cid=""):
        self.base = base
        self.token = token
        self.cid = cid or f"test-{token[:6] or 'anon'}"     # разные «устройства» для admin/user
        self.ctx = ssl._create_unverified_context()

    def call(self, path, body=None, method=None, headers=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", "X-BF-Client-Id": self.cid}
        if self.token:
            h["X-BF-Auth"] = self.token
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data is not None else "GET"), headers=h)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as r:
                payload = r.read()
                return r.status, (payload if raw else json.loads(payload or b"{}"))
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload or b"{}")
            except Exception:
                return e.code, payload


def _spawn(args, env, log):
    e = dict(os.environ)
    e.update(env)
    e["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen([PY, "-u", *args], cwd=str(ROOT), env=e, stdout=open(log, "w", encoding="utf-8"), stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.fixture(scope="session")
def hub(tmp_path_factory):
    data = tmp_path_factory.mktemp("hubdata")
    port = free_port()
    proc = _spawn(["hub_server.py", str(port)], {"BLACKFOX_DATA": str(data)}, data / "hub.log")
    assert wait_port(port), (data / "hub.log").read_text(encoding="utf-8")
    c = Client(f"http://127.0.0.1:{port}")
    st, r = c.call("/api/auth/setup", {"admin_password": ADMIN_PW, "user_password": USER_PW})
    assert st == 200, r
    admin = Client(c.base, r["token"])
    st, r = c.call("/api/auth/login", {"password": USER_PW, "role": "user"})
    assert st == 200, r
    user = Client(c.base, r["token"])
    yield {"port": port, "data": data, "anon": c, "admin": admin, "user": user, "proc": proc}
    proc.kill()


@pytest.fixture(scope="session")
def core(hub, tmp_path_factory):
    data = tmp_path_factory.mktemp("coredata")
    cport, aport = free_port(), free_port()
    proc = _spawn(["blackfox_core.py", "--mode", "node", "--hub", hub["anon"].base, "--agent-port", str(aport),
                   "--control", str(cport), "--control-token", "t-test", "--data", str(data)], {}, data / "core.out")
    assert wait_port(cport), (data / "core.out").read_text(encoding="utf-8")
    yield {"port": cport, "agent_port": aport, "data": data, "token": "t-test",
           "c": Client(f"http://127.0.0.1:{cport}"), "proc": proc}
    proc.kill()
