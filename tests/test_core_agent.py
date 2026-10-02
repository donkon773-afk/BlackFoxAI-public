"""Ядро-sidecar (B2): control API, CORS, прокси, pair; агент: действие shell."""
import os
import sys
import urllib.request
from pathlib import Path

from conftest import ADMIN_PW

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_control_token_required(core):
    c = core["c"]
    st, r = c.call("/control/status")
    assert st == 403
    st, r = c.call("/control/status", headers={"X-Control-Token": core["token"]})
    assert st == 200 and r["mode"] == "node" and r["agent"]["running"] and r["hub_reachable"], r


def test_cors_only_for_shell_origins(core):
    req = urllib.request.Request(core["c"].base + "/control/login", method="OPTIONS",
                                 headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "POST"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 204 and r.headers.get("Access-Control-Allow-Origin") == "tauri://localhost"
    req = urllib.request.Request(core["c"].base + "/control/login", method="OPTIONS", headers={"Origin": "https://evil.example"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers.get("Access-Control-Allow-Origin") is None


def test_login_relay_and_proxy(core, hub):
    c = core["c"]
    st, r = c.call("/control/login", {"role": "admin", "password": ADMIN_PW}, headers={"X-Control-Token": core["token"]})
    assert st == 200 and r.get("token") and r["role"] == "admin", r
    st, r = c.call("/control/login", {"role": "user", "password": ADMIN_PW}, headers={"X-Control-Token": core["token"]})
    assert st == 401
    st, body = c.call("/", raw=True)                       # интерфейс хаба через прокси ядра
    assert st == 200 and b"BlackFox" in body
    st, r = c.call("/api/auth/status", {}, headers={"X-BF-Auth": hub["admin"].token})
    assert st == 200 and r["authenticated"]


def test_pair_bad_code(core):
    st, r = core["c"].call("/control/pair", {"hub_url": core["c"].base and None or "", "code": "AAAA-AAAA"}, headers={"X-Control-Token": core["token"]})
    assert st == 400
    st, r = core["c"].call("/control/pair", {"hub_url": "http://127.0.0.1:1", "code": "AAAA-AAAA"}, headers={"X-Control-Token": core["token"]})
    assert st >= 400


def test_agent_shell_action_gates(monkeypatch):
    import node_agent as a
    monkeypatch.setattr(a, "AGENT_SECRET", "")
    r = a.run_action("shell", {"command": "Write-Output x"})
    assert not r["ok"] and "подпис" in r["error"]           # по сети без секрета — отказ
    r = a.run_action("shell", {"command": "Write-Output x"}, local=True)
    assert r["ok"] and r["output"]["stdout"].strip() == "x"
    monkeypatch.setenv("BLACKFOX_NO_SHELL", "1")
    r = a.run_action("shell", {"command": "Write-Output x"}, local=True)
    assert not r["ok"] and "BLACKFOX_NO_SHELL" in r["error"]
    monkeypatch.delenv("BLACKFOX_NO_SHELL")
    r = a.run_action("shell", {"command": "Start-Sleep 30", "timeout": 5}, local=True)
    assert not r["ok"] and r["output"]["timed_out"]
    r = a.run_action("shell", {"shell": "cmd", "command": "echo hi"}, local=True)
    assert r["ok"] and "hi" in r["output"]["stdout"]
