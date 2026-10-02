"""Роли по паролю, парные коды, ключи установки (B3)."""
from conftest import ADMIN_PW, USER_PW


def test_role_specific_login(hub):
    c = hub["anon"]
    st, r = c.call("/api/auth/login", {"password": ADMIN_PW, "role": "user"})
    assert st == 401, r                       # пароль администратора не подходит для роли user
    st, r = c.call("/api/auth/login", {"password": USER_PW, "role": "admin"})
    assert st == 401, r
    st, r = c.call("/api/auth/login", {"password": USER_PW})
    assert st == 200 and r["role"] == "user"   # без role — как раньше, любая из двух


def test_status_reflects_session(hub):
    st, r = hub["user"].call("/api/auth/status", {})
    assert r["authenticated"] and r["role"] == "user"
    st, r = hub["anon"].call("/api/auth/status", {})
    assert not r["authenticated"]


def test_admin_routes_denied_for_user(hub):
    st, r = hub["user"].call("/api/security/pair_code", {})
    assert st == 403
    st, r = hub["user"].call("/api/settings", {"llm": {"max_tokens": 1}})
    assert st == 403


def test_pair_code_single_use(hub):
    st, r = hub["admin"].call("/api/security/pair_code", {})
    assert st == 200 and len(r["code"]) == 9 and r["link"].startswith("blackfox://pair?")
    body = {"code": r["code"], "hostname": "test-node", "agent_port": 8766, "platform": "Windows"}
    st1, r1 = hub["anon"].call("/api/node/pair", body)
    assert st1 == 200 and r1.get("secret") and r1.get("key"), r1
    st2, r2 = hub["anon"].call("/api/node/pair", body)
    assert st2 == 403                          # второй раз — отказ
    st3, r3 = hub["anon"].call("/api/node/pair", {"code": "AAAA-BBBB", "hostname": "x", "agent_port": 1})
    assert st3 == 403


def test_install_token_required_when_protected(hub):
    st, r = hub["anon"].call("/install-agent.ps1", raw=True)
    assert st == 403
    st, r = hub["admin"].call("/api/security/install_token", {"action": "issue"})
    assert st == 200 and r.get("token")
    st, body = hub["anon"].call("/install-agent.ps1", headers={"X-Install-Token": r["token"]}, raw=True)
    assert st == 200 and b"node_agent" in body
