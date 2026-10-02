"""Файлы в чате (B16) и «Агент ПК» (B17) — без живой LLM: хаб-копия + локальный агент."""
import base64
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _b64(b):
    return base64.b64encode(b).decode()


def test_upload_text_and_download(hub):
    st, r = hub["user"].call("/api/files/upload", {"name": "notes.txt", "data": _b64("Привет, файл\n".encode())})
    assert st == 200 and r["file"]["kind"] == "text" and r["file"]["chars"] > 0, r
    fid = r["file"]["id"]
    st, body = hub["user"].call(f"/api/files/get?id={fid}", raw=True)
    assert st == 200 and "Привет".encode() in body
    st, r = hub["anon"].call(f"/api/files/get?id={fid}", raw=True)
    assert st == 401                                   # без входа файлы не отдаются
    st, r = hub["user"].call("/api/files/list", {})
    assert any(f["id"] == fid for f in r["files"])


def test_upload_docx_xlsx(hub, tmp_path):
    docx = tmp_path / "t.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Абзац один</w:t></w:r></w:p></w:body></w:document>')
    st, r = hub["admin"].call("/api/files/upload", {"name": "t.docx", "data": _b64(docx.read_bytes())})
    assert st == 200 and "Абзац один" in r["preview"]
    xlsx = tmp_path / "t.xlsx"
    with zipfile.ZipFile(xlsx, "w") as z:
        z.writestr("xl/sharedStrings.xml", '<sst xmlns="x"><si><t>Итого</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="x"><sheetData><row><c t="s"><v>0</v></c><c><v>5</v></c></row></sheetData></worksheet>')
    st, r = hub["admin"].call("/api/files/upload", {"name": "t.xlsx", "data": _b64(xlsx.read_bytes())})
    assert st == 200 and "Итого\t5" in r["preview"]


def test_upload_limits(hub):
    st, r = hub["user"].call("/api/files/upload", {"name": "x.bin", "data": "!!!notbase64"})
    assert st in (400, 200)                            # мусор либо отвергается, либо сохраняется как бинарный
    st, r = hub["user"].call("/api/files/upload", {"name": "", "data": ""})
    assert st == 400


def test_delete_foreign_file_forbidden(hub):
    st, r = hub["admin"].call("/api/files/upload", {"name": "a.txt", "data": _b64(b"hi")})
    fid = r["file"]["id"]
    st, r = hub["user"].call("/api/files/delete", {"id": fid})
    assert st == 403
    st, r = hub["admin"].call("/api/files/delete", {"id": fid})
    assert st == 200


def test_parse_tool_calls_and_block():
    os.environ.setdefault("BLACKFOX_DATA", str(ROOT / "tests" / "_tmpdata"))
    import hub_files
    import hub_server as hs
    calls = hs.parse_tool_calls("текст\n```run:powershell@node_1\nGet-Date\n```\n```run:bash@node_2\nuname -a\n```\n```run:pwsh\n$x\n```")
    assert [c["shell"] for c in calls] == ["powershell", "bash", "powershell"]
    assert calls[0]["node"] == "node_1" and calls[0]["command"] == "Get-Date" and calls[2]["node"] == ""
    assert hs.parse_tool_calls("```powershell\nGet-Date\n```") == []      # обычный код — не команда
    txt = hs.tool_result_text({"node_name": "N", "shell": "powershell", "exit_code": 0, "output": "ok", "denied": True})
    assert "отклонена" in txt and "Не повторяй" in txt
    store = hub_files.FileStore(ROOT / "tests" / "_tmpdata" / "files")
    m = store.add("a.txt", ("x" * 100).encode())
    blk = hub_files.files_block(store, [m], per_file_chars=10, total_chars=100)
    assert "обрезано" in blk and "a.txt" in blk


def test_tools_admin_only(hub):
    st, r = hub["user"].call("/api/execute", {"prompt": "x", "target_node": "node_1", "tools": True})
    assert st == 403
    st, r = hub["user"].call("/api/tools/exec", {"node_key": "node_1", "command": "dir"})
    assert st == 403


def test_tools_exec_requires_node_flag_then_runs(hub):
    st, r = hub["admin"].call("/api/tools/exec", {"node_key": "node_1", "command": "Write-Output ping"})
    assert st == 403                                                       # exec_enabled выключен по умолчанию
    st, r = hub["admin"].call("/api/node/update", {"node_key": "node_1", "exec_enabled": True})
    assert st == 200 and r["node"]["exec_enabled"] is True
    st, r = hub["admin"].call("/api/tools/exec", {"node_key": "node_1", "command": "Write-Output ping; exit 0"})
    assert st == 200 and r["result"]["ok"] and "ping" in r["result"]["output"]["stdout"], r
    st, r = hub["admin"].call("/api/tools/exec", {"node_key": "node_1", "command": "exit 7"})
    assert r["result"]["ok"] is False and r["result"]["output"]["exit_code"] == 7
    st, r = hub["admin"].call("/api/security/audit", {})
    assert any(e.get("event") == "tool_exec" for e in (r.get("events") or r.get("audit") or []))
    hub["admin"].call("/api/node/update", {"node_key": "node_1", "exec_enabled": False})


def test_approve_unknown_call(hub):
    st, r = hub["admin"].call("/api/tools/approve", {"call_id": "nope", "approve": True})
    assert st == 404
