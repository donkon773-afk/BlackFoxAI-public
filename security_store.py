"""
BlackFox hub — security store.

Holds everything secret outside of config.json:
  * password hashes (PBKDF2-HMAC-SHA256, per-password random salt)
  * the cluster HMAC secret used to sign hub -> agent control requests
  * active sessions (revocable)
and provides brute-force lockout plus an append-only audit log.

The file is created with owner-only permissions (chmod 600 / icacls on
Windows) and is never sent to the browser — the API only ever reports
whether a secret exists, never its value.

Stdlib only.
"""

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

PBKDF2_ITERATIONS = 240_000
IS_WINDOWS = platform.system() == "Windows"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _restrict_permissions(path: Path):
    """Owner-only access, best effort on both platforms."""
    try:
        if IS_WINDOWS:
            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"algo": "pbkdf2_sha256", "iterations": iterations,
            "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(dk).decode(),
            "set_at": now_iso()}


def verify_password(password: str, record: dict) -> bool:
    if not record or not password:
        return False
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(record["iterations"]))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def new_token(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode().rstrip("=")


class SecurityStore:
    def __init__(self, data_dir: Path):
        self.lock = threading.RLock()
        self.path = Path(data_dir) / "secrets.json"
        self.audit_path = Path(data_dir) / "security.log"
        self.data = {"passwords": {}, "agent_secret": "", "sessions": {}, "install_tokens": {}, "created_at": now_iso()}
        self.fails = {}          # ip -> {"n": int, "until": epoch}
        self.load()

    # ---- persistence -----------------------------------------------------
    def load(self):
        if not self.path.exists():
            return
        try:
            with self.lock:
                self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                self.data.setdefault("passwords", {})
                self.data.setdefault("sessions", {})
                self.data.setdefault("agent_secret", "")
                self.data.setdefault("install_tokens", {})
        except Exception as e:
            print(f"[!] secrets.json unreadable ({e}) — доступ будет запрошен заново")

    def save(self):
        with self.lock:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)
        _restrict_permissions(self.path)

    # ---- audit -----------------------------------------------------------
    def audit(self, event, ip="", detail="", role=""):
        line = json.dumps({"ts": now_iso(), "event": event, "ip": ip, "role": role, "detail": str(detail)[:400]}, ensure_ascii=False)
        try:
            with self.lock, open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def audit_tail(self, n=200):
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()[-n:]
        except Exception:
            return []
        out = []
        for l in lines:
            try:
                out.append(json.loads(l))
            except Exception:
                continue
        out.reverse()
        return out

    # ---- state -----------------------------------------------------------
    def needs_setup(self):
        with self.lock:
            return not self.data["passwords"].get("admin")

    def has_password(self, which):
        with self.lock:
            return bool(self.data["passwords"].get(which))

    def set_password(self, which, password):
        if which not in ("admin", "user"):
            raise ValueError("which must be admin|user")
        with self.lock:
            if password:
                self.data["passwords"][which] = hash_password(password)
            else:
                self.data["passwords"].pop(which, None)
                # sessions of that role are no longer valid
                for t, sess in list(self.data["sessions"].items()):
                    if sess.get("role") == which:
                        del self.data["sessions"][t]
        self.save()

    def check_password(self, password, role=None):
        """-> role ('admin'/'user') or None. Без role — подходит любой пароль (admin проверяется первым);
        с role — только пароль этой роли (приложение с явным выбором роли)."""
        with self.lock:
            pw = dict(self.data["passwords"])
        for r in ((role,) if role else ("admin", "user")):
            if verify_password(password, pw.get(r)):
                return r
        return None

    # ---- agent secret ----------------------------------------------------
    def agent_secret(self):
        with self.lock:
            return self.data.get("agent_secret") or ""

    def rotate_agent_secret(self):
        sec = new_token(24)
        with self.lock:
            self.data["agent_secret"] = sec
        self.save()
        return sec

    def ensure_agent_secret(self):
        return self.agent_secret() or self.rotate_agent_secret()

    # ---- одноразовые ключи установки агента -------------------------------
    # Установщик содержит секрет кластера в открытом виде, поэтому отдавать его
    # всем подряд нельзя. Админ выпускает короткоживущий ключ, вводит команду на
    # новом узле — ключ гасится после использования или по истечении срока.
    def new_install_token(self, minutes=30, uses=1, note="", ip=""):
        tok = new_token(18)
        with self.lock:
            self.data.setdefault("install_tokens", {})[tok] = {
                "created": now_iso(), "expires": time.time() + minutes * 60,
                "uses_left": max(1, int(uses)), "note": str(note)[:80], "by_ip": ip, "used_by": []}
        self.save()
        return tok

    def use_install_token(self, tok, ip=""):
        """-> (ok, причина отказа). Списывает одно использование."""
        if not tok:
            return False, "ключ установки не указан"
        with self.lock:
            rec = self.data.setdefault("install_tokens", {}).get(tok)
            if not rec:
                return False, "ключ установки неизвестен или уже использован"
            if rec.get("expires", 0) < time.time():
                del self.data["install_tokens"][tok]
                dirty = True
            else:
                rec["uses_left"] = int(rec.get("uses_left", 1)) - 1
                rec.setdefault("used_by", []).append({"ip": ip, "ts": now_iso()})
                if rec["uses_left"] <= 0:
                    del self.data["install_tokens"][tok]
                dirty = False
        self.save()
        return (False, "срок действия ключа установки истёк") if dirty else (True, "")

    # ---- парные коды (регистрация узла из приложения) ---------------------
    # Код XXXX-XXXX без похожих символов, 10 минут, одно использование. Обмен:
    # код -> секрет кластера. Выдаёт администратор из «🔗 Подключение».
    PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def new_pair_code(self, minutes=10, note="", ip=""):
        raw = "".join(secrets.choice(self.PAIR_ALPHABET) for _ in range(8))
        code = raw[:4] + "-" + raw[4:]
        with self.lock:
            codes = self.data.setdefault("pair_codes", {})
            for c in [c for c, r in codes.items() if r.get("expires", 0) < time.time()]:
                del codes[c]
            codes[code] = {"created": now_iso(), "expires": time.time() + minutes * 60, "note": str(note)[:80], "by_ip": ip}
        self.save()
        return code

    def use_pair_code(self, code, ip=""):
        """-> (ok, причина). Код гасится при первом использовании."""
        code = (code or "").strip().upper().replace(" ", "")
        if len(code) == 8:
            code = code[:4] + "-" + code[4:]
        with self.lock:
            codes = self.data.setdefault("pair_codes", {})
            rec = codes.pop(code, None)
        if rec is not None:
            self.save()
        if not rec:
            return False, "парный код неизвестен или уже использован"
        if rec.get("expires", 0) < time.time():
            return False, "срок действия парного кода истёк"
        return True, ""

    def pair_codes(self):
        now = time.time()
        with self.lock:
            codes = self.data.setdefault("pair_codes", {})
            for c in [c for c, r in codes.items() if r.get("expires", 0) < now]:
                del codes[c]
            return [{"code": c, "note": r.get("note", ""), "created": r["created"], "expires_in_min": max(0, round((r["expires"] - now) / 60))}
                    for c, r in sorted(codes.items(), key=lambda kv: kv[1]["created"], reverse=True)]

    def install_tokens(self):
        now = time.time()
        with self.lock:
            toks = self.data.setdefault("install_tokens", {})
            for t in [t for t, r in toks.items() if r.get("expires", 0) < now]:
                del toks[t]
            return [{"id": t[:6], "token": t, "note": r.get("note", ""), "created": r["created"],
                     "uses_left": r.get("uses_left", 1), "by_ip": r.get("by_ip", ""),
                     "expires_in_min": max(0, round((r["expires"] - now) / 60))}
                    for t, r in sorted(toks.items(), key=lambda kv: kv[1]["created"], reverse=True)]

    def revoke_install_token(self, tok):
        with self.lock:
            gone = self.data.setdefault("install_tokens", {}).pop(tok, None) is not None
        if gone:
            self.save()
        return gone

    # ---- brute force -----------------------------------------------------
    def locked_for(self, ip, max_fails, minutes):
        rec = self.fails.get(ip)
        if not rec:
            return 0
        if rec.get("until", 0) > time.time():
            return int(rec["until"] - time.time())
        if rec.get("n", 0) >= max_fails:
            self.fails.pop(ip, None)
        return 0

    def note_fail(self, ip, max_fails, minutes):
        rec = self.fails.setdefault(ip, {"n": 0, "until": 0})
        rec["n"] += 1
        if rec["n"] >= max_fails:
            rec["until"] = time.time() + minutes * 60
            rec["n"] = 0
            return True
        return False

    def note_success(self, ip):
        self.fails.pop(ip, None)

    # ---- sessions --------------------------------------------------------
    def create_session(self, role, ip, name="", ua="", hours=72):
        token = new_token()
        with self.lock:
            self.data["sessions"][token] = {"role": role, "ip": ip, "name": name, "ua": ua[:180],
                                            "created": now_iso(), "last": now_iso(),
                                            "expires": time.time() + hours * 3600}
        self.save()
        return token

    def session(self, token, touch=True):
        if not token:
            return None
        with self.lock:
            sess = self.data["sessions"].get(token)
            if not sess:
                return None
            if sess.get("expires", 0) < time.time():
                del self.data["sessions"][token]
                return None
            if touch:
                sess["last"] = now_iso()
            return dict(sess, token=token)

    def revoke(self, token):
        with self.lock:
            existed = self.data["sessions"].pop(token, None) is not None
        if existed:
            self.save()
        return existed

    def revoke_all(self, keep_token=None):
        with self.lock:
            keep = self.data["sessions"].get(keep_token)
            self.data["sessions"] = {keep_token: keep} if keep else {}
        self.save()

    def sessions(self):
        with self.lock:
            now = time.time()
            stale = [t for t, s in self.data["sessions"].items() if s.get("expires", 0) < now]
            for t in stale:
                del self.data["sessions"][t]
            return [{"id": t[:8], "token": t, "role": s["role"], "ip": s["ip"], "name": s.get("name", ""),
                     "ua": s.get("ua", ""), "created": s["created"], "last": s["last"],
                     "expires_in_h": round((s["expires"] - now) / 3600, 1)}
                    for t, s in sorted(self.data["sessions"].items(), key=lambda kv: kv[1]["last"], reverse=True)]

    def purge_sessions(self):
        with self.lock:
            self.data["sessions"] = {}
        self.save()
