"""Браузерный CSRF и выдача установщика агента (секрет кластера) не-администратору."""
import http.client
import json
import re

import pytest

from conftest import Client, _spawn, free_port, wait_port


def _post(port, path, origin=None, ctype="application/json", source=None):
    kw = {"source_address": (source, 0)} if source else {}
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30, **kw)
    h = {"Content-Type": ctype}
    if origin:
        h["Origin"] = origin
    c.request("POST", path, body=b"{}", headers=h)
    r = c.getresponse()
    body = r.read()
    return r.status, r.getheader("Access-Control-Allow-Origin"), body


def _get(port, path, headers=None, source=None):
    kw = {"source_address": (source, 0)} if source else {}
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30, **kw)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, r.read().decode("utf-8", "replace")


def test_foreign_origin_rejected(hub):
    port = hub["port"]
    # чужой сайт: «простой» запрос без preflight (text/plain) не выполняется и не получает CORS
    st, acao, _ = _post(port, "/api/auth/status", origin="https://evil.example", ctype="text/plain")
    assert st == 403 and acao is None
    # свой адрес, оболочка приложения и прокси ядра — пропускаются, CORS только им
    for origin in (f"http://127.0.0.1:{port}", "tauri://localhost", "http://127.0.0.1:41234"):
        st, acao, _ = _post(port, "/api/auth/status", origin=origin)
        assert st == 200 and acao == origin, origin
    st, acao, _ = _post(port, "/api/auth/status")              # не браузер — без Origin
    assert st == 200 and acao is None
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("OPTIONS", "/api/settings", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    r = c.getresponse(); r.read()
    assert r.getheader("Access-Control-Allow-Origin") is None    # preflight чужого сайта не проходит


def test_rejected_post_does_not_break_keepalive(hub):
    """Отказ (401) до чтения тела POST не должен превращать следующий запрос в 400."""
    c = http.client.HTTPConnection("127.0.0.1", hub["port"], timeout=30)
    c.request("POST", "/api/client/hello", body=b'{"name": "x"}', headers={"Content-Type": "application/json"})
    r = c.getresponse(); r.read()
    assert r.status == 401 and r.getheader("Connection") == "close"
    c.request("POST", "/api/auth/status", body=b"{}", headers={"Content-Type": "application/json"})   # http.client переподключится
    r = c.getresponse()
    assert r.status == 200, r.read()[:200]


@pytest.fixture(scope="module")
def open_hub(tmp_path_factory):
    """Хаб без входа по паролю: администратор — по адресу машины хаба."""
    data = tmp_path_factory.mktemp("openhub")
    port = free_port()
    proc = _spawn(["hub_server.py", str(port)], {"BLACKFOX_DATA": str(data)}, data / "hub.log")
    assert wait_port(port), (data / "hub.log").read_text(encoding="utf-8")
    yield {"port": port, "admin": Client(f"http://127.0.0.1:{port}")}
    proc.kill()


def test_installer_not_for_non_admin_without_auth(open_hub):
    port = open_hub["port"]
    st, r = open_hub["admin"].call("/api/security/agent_secret", {})
    assert st == 200 and r["secret"] and "X-Install-Token" in r["install_unix"], r
    try:
        st, body = _get(port, "/install-agent.sh", source="127.0.0.2")
    except OSError:
        pytest.skip("нет второго адреса loopback")
    assert st == 403 and "SECRET=" not in body                     # не-админ без ключа — отказ
    tok = re.search(r"X-Install-Token: ([^\"]+)\"", r["install_unix"]).group(1)
    st, body = _get(port, "/install-agent.sh", {"X-Install-Token": tok}, source="127.0.0.2")
    assert st == 200 and 'SECRET="%s"' % r["secret"] in body       # по одноразовому ключу — можно
    st, body = _get(port, "/install-agent.sh", {"X-Install-Token": tok}, source="127.0.0.2")
    assert st == 403                                                # ключ погашен
    st, body = _get(port, "/install-agent.sh")                      # машина-администратор
    assert st == 200
