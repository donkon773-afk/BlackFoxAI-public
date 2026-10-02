"""BlackFox AI Workstation — приложенные файлы.

Хранение в <data>/files и извлечение текста для LLM. Только stdlib: текстовые
форматы читаются напрямую, docx/xlsx/pptx — как zip с XML, pdf — через
`pdftotext`, если он есть на машине, иначе грубым разбором потоков (для
простых PDF с обычным текстом этого достаточно; сканы без OCR текста не дают).
Изображения не разбираются — они уходят узлу с поддержкой vision как есть.
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".bash", ".zsh",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".sql", ".r", ".swift", ".m",
    ".sqf", ".hpp", ".ext", ".env", ".gitignore", ".dockerfile", ".tex", ".srt", ".vtt", ".diff", ".patch",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx"}
SAFE_NAME = re.compile(r"[^\w.\-() Ѐ-ӿ]+", re.UNICODE)


def _decode(data):
    for enc in ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _xml_text(xml_bytes, tag_local):
    """Все текстовые узлы с локальным именем tag_local (w:t, a:t, t) по порядку."""
    out = []
    try:
        for ev, el in ElementTree.iterparse(io.BytesIO(xml_bytes)):
            if el.tag.rsplit("}", 1)[-1] == tag_local and el.text:
                out.append(el.text)
    except Exception:
        pass
    return out


def _docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    # абзацы — по <w:p>, внутри собираем <w:t>
    paras = []
    try:
        root = ElementTree.fromstring(xml)
        for p in root.iter():
            if p.tag.rsplit("}", 1)[-1] == "p":
                t = "".join(x.text or "" for x in p.iter() if x.tag.rsplit("}", 1)[-1] == "t")
                paras.append(t)
    except Exception:
        paras = _xml_text(xml, "t")
    return "\n".join(paras).strip()


def _xlsx(path, max_rows=2000):
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(x.text or "" for x in si.iter() if x.tag.rsplit("}", 1)[-1] == "t"))
        sheets = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        out = []
        rows_total = 0
        for sn in sheets:
            root = ElementTree.fromstring(z.read(sn))
            out.append(f"### Лист {sn.rsplit('/', 1)[-1][:-4]}")
            for row in root.iter():
                if row.tag.rsplit("}", 1)[-1] != "row":
                    continue
                cells = []
                for c in row:
                    if c.tag.rsplit("}", 1)[-1] != "c":
                        continue
                    v = None
                    for ch in c:
                        if ch.tag.rsplit("}", 1)[-1] in ("v", "is"):
                            v = "".join(ch.itertext())
                    if v is None:
                        cells.append("")
                    elif c.get("t") == "s":
                        try:
                            cells.append(shared[int(v)])
                        except Exception:
                            cells.append(v)
                    else:
                        cells.append(v)
                if any(cells):
                    out.append("\t".join(cells))
                    rows_total += 1
                    if rows_total >= max_rows:
                        out.append(f"… (обрезано после {max_rows} строк)")
                        return "\n".join(out)
        return "\n".join(out).strip()


def _pptx(path):
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)))
        out = []
        for i, sn in enumerate(slides, 1):
            texts = _xml_text(z.read(sn), "t")
            out.append(f"### Слайд {i}\n" + "\n".join(t for t in texts if t.strip()))
        return "\n\n".join(out).strip()


def _pdf(path):
    exe = shutil.which("pdftotext")
    if exe:
        try:
            res = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, timeout=60)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.decode("utf-8", errors="replace"), "pdftotext"
        except Exception:
            pass
    # грубый разбор: FlateDecode-потоки → операторы Tj/TJ
    raw = Path(path).read_bytes()
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, flags=re.S):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except Exception:
            pass
        for tj in re.finditer(rb"\[(.*?)\]\s*TJ|\((.*?)\)\s*Tj", data, flags=re.S):
            if tj.group(1) is not None:
                parts = re.findall(rb"\((.*?)(?<!\\)\)", tj.group(1), flags=re.S)
                chunks.append(b"".join(parts))
            else:
                chunks.append(tj.group(2))
        chunks.append(b"\n")
    text = b"".join(chunks).replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\n", b"\n")
    text = re.sub(rb"\n{3,}", b"\n\n", text)
    return _decode(text).strip(), "грубый разбор (установите poppler/pdftotext для точного текста)"


def extract_text(path, ext):
    """→ (text, kind, note). kind: text | image | binary."""
    ext = ext.lower()
    p = Path(path)
    try:
        if ext in IMAGE_EXT:
            return "", "image", ""
        if ext == ".pdf":
            text, note = _pdf(p)
            return text, "text", note
        if ext == ".docx":
            return _docx(p), "text", ""
        if ext == ".xlsx":
            return _xlsx(p), "text", ""
        if ext == ".pptx":
            return _pptx(p), "text", ""
        data = p.read_bytes()
        if ext in TEXT_EXT or (b"\x00" not in data[:4096] and len(data) < 4_000_000):
            return _decode(data), "text", ""
        return "", "binary", "текст не извлечён (бинарный формат)"
    except Exception as e:
        return "", "binary", f"не удалось прочитать: {e}"


class FileStore:
    def __init__(self, root, max_mb=25):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.lock = threading.RLock()
        self.index = {}
        try:
            self.index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            self.index = {}

    def _save(self):
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def add(self, name, data, thread_id="", owner_id="", owner_name=""):
        name = SAFE_NAME.sub("_", os.path.basename(name or "file"))[:120] or "file"
        if len(data) > self.max_bytes:
            raise ValueError(f"файл больше {self.max_bytes // (1024 * 1024)} МБ")
        ext = os.path.splitext(name)[1].lower()
        fid = uuid.uuid4().hex[:12]
        path = self.root / f"{fid}{ext}"
        path.write_bytes(data)
        text, kind, note = extract_text(path, ext)
        if text:
            (self.root / f"{fid}.txt").write_text(text, encoding="utf-8")
        meta = {"id": fid, "name": name, "ext": ext, "size": len(data), "kind": kind, "chars": len(text), "note": note,
                "thread_id": thread_id or "", "owner_id": owner_id, "owner_name": owner_name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with self.lock:
            self.index[fid] = meta
            self._save()
        return dict(meta)

    def get(self, fid):
        with self.lock:
            m = self.index.get(fid)
            return dict(m) if m else None

    def path(self, fid):
        m = self.get(fid)
        return self.root / f"{fid}{m['ext']}" if m else None

    def text(self, fid):
        p = self.root / f"{fid}.txt"
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    def image_data_url(self, fid):
        m = self.get(fid)
        if not m or m["kind"] != "image":
            return ""
        p = self.path(fid)
        try:
            return "data:%s;base64,%s" % (IMAGE_MIME.get(m["ext"], "image/png"), base64.b64encode(p.read_bytes()).decode("ascii"))
        except Exception:
            return ""

    def list(self, thread_id=None):
        with self.lock:
            items = [dict(m) for m in self.index.values() if thread_id is None or m.get("thread_id") == thread_id]
        items.sort(key=lambda m: m["ts"], reverse=True)
        return items

    def delete(self, fid):
        with self.lock:
            m = self.index.pop(fid, None)
            if not m:
                return False
            self._save()
        for p in (self.root / f"{fid}{m['ext']}", self.root / f"{fid}.txt"):
            try:
                p.unlink()
            except Exception:
                pass
        return True

    def public(self, m):
        return {k: m.get(k) for k in ("id", "name", "size", "kind", "chars", "note", "ts", "owner_name")}


def files_block(store, files, per_file_chars=40000, total_chars=120000):
    """Текстовый блок для промпта. files — список meta (id, name…)."""
    parts = []
    used = 0
    for f in files:
        m = store.get(f.get("id") or "") or f
        if m.get("kind") == "image":
            parts.append(f"--- {m['name']} — изображение ({m.get('size', 0) // 1024} КБ) ---")
            continue
        text = store.text(m["id"]) if m.get("id") else ""
        if not text:
            parts.append(f"--- {m['name']} — {m.get('note') or 'текст не извлечён'} ---")
            continue
        limit = min(per_file_chars, max(0, total_chars - used))
        cut = text[:limit]
        used += len(cut)
        tail = f"\n… (обрезано: показано {len(cut)} из {len(text)} символов)" if len(text) > len(cut) else ""
        parts.append(f"--- {m['name']} ({len(text)} симв.) ---\n{cut}{tail}")
        if used >= total_chars:
            parts.append("… (общий лимит контекста файлов исчерпан)")
            break
    return "=== ПРИЛОЖЕННЫЕ ФАЙЛЫ ===\n" + "\n\n".join(parts) if parts else ""
