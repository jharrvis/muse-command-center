#!/usr/bin/env python3
"""
Muse Command Center — task bus + dashboard untuk akun-akun Muse milik user.
- Task bus : akun Muse melaporkan tugas (mulai / selesai / gagal)
- Inbox    : titip tugas untuk akun Muse lain
- Heartbeat: sinyal "masih hidup" per akun
Hanya Python stdlib. Bind ke IP tailnet saja. Basic auth.
"""
import base64
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mcc.db")
ENV_PATH = os.path.join(BASE_DIR, ".env")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
LOGIN_PATH = os.path.join(BASE_DIR, "login.html")
UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")
MAX_UPLOAD_SIZE = 10 * 1024 * 1024

BIND_IP = os.environ.get("MCC_BIND", "127.0.0.1")
PORT = int(os.environ.get("MCC_PORT", "9120"))
AUTH_USER = os.environ.get("MCC_USER", "admin")
# Nama pengirim yang boleh memakai auto_execute (koma-dipisah, lowercase
# dibandingkan). Default: hanya akun utama. Contoh: "julian,jharrvis".
AUTO_EXECUTE_ALLOW = {
    u.strip().lower()
    for u in os.environ.get("MCC_AUTO_EXECUTE_ALLOW", "").split(",")
    if u.strip()
} or {AUTH_USER.lower()}
SESSION_DAYS = 30
COOKIE_NAME = "mcc_session"
USERS_PATH = os.path.join(BASE_DIR, ".users")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_password():
    pw = os.environ.get("MCC_PASSWORD")
    if pw:
        return pw
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("MCC_PASSWORD="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except FileNotFoundError:
        pass
    return None


PASSWORD = load_password()


def load_users():
    users = {}
    try:
        with open(USERS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    u, p = line.split(":", 1)
                    users[u.strip()] = p.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return users


EXTRA_USERS = load_users()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_account TEXT NOT NULL,
    to_account TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeats (
    account TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_inbox_to ON inbox(to_account, status);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    created_at TEXT NOT NULL
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    # Migrasi: kolom project_id untuk DB yang sudah ada
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    if "project_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(inbox)")]
    if "project_id" not in cols:
        conn.execute("ALTER TABLE inbox ADD COLUMN project_id INTEGER")
    if "attachment_id" not in cols:
        conn.execute("ALTER TABLE inbox ADD COLUMN attachment_id INTEGER")
    if "auto_execute" not in cols:
        conn.execute(
            "ALTER TABLE inbox ADD COLUMN auto_execute INTEGER DEFAULT 0")
    if "approved_by" not in cols:
        conn.execute("ALTER TABLE inbox ADD COLUMN approved_by TEXT")
    # Migrasi: external_id untuk sinkronisasi satu arah dari sumber luar
    # (mis. kanban Hermes). Format: "<sumber>:<id-asli>", mis.
    # "kanban:mci-team:t_abc123".
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    if "external_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN external_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_external "
                 "ON tasks(external_id)")
    conn.commit()
    conn.close()


def parse_multipart_file(body, boundary):
    """Ekstrak field file dari multipart body. Kembalikan
    (filename, mime, data) atau None bila tidak ada."""
    sep = b"--" + boundary
    for part in body.split(sep):
        if not part or part.strip(b"-\r\n") == b"":
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        head, found, data = part.partition(b"\r\n\r\n")
        if not found:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = head.decode("latin1")
        if 'name="file"' not in headers:
            continue
        m = re.search(r'filename="([^"]*)"', headers)
        fname = m.group(1) if m else ""
        m = re.search(r"(?im)^Content-Type:\s*(\S+)", headers)
        mime = m.group(1) if m else "application/octet-stream"
        return fname, mime, data
    return None


def check_credentials(user, password):
    user = user or ""
    password = password or ""
    if PASSWORD and hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(password, PASSWORD):
        return True
    for u, p in EXTRA_USERS.items():
        if hmac.compare_digest(user, u) and hmac.compare_digest(password, p):
            return True
    return False


def create_session(conn):
    token = secrets.token_hex(32)
    t = now_iso()
    conn.execute("INSERT INTO sessions (token,created_at,last_seen) VALUES (?,?,?)",
                 (token, t, t))
    conn.commit()
    return token


def session_valid(conn, token):
    """True jika token sesi valid & belum kedaluwarsa; perbarui last_seen."""
    if not token:
        return False
    row = conn.execute("SELECT last_seen FROM sessions WHERE token=?",
                       (token,)).fetchone()
    if not row:
        return False
    try:
        last = datetime.fromisoformat(row["last_seen"])
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - last > timedelta(days=SESSION_DAYS):
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return False
    conn.execute("UPDATE sessions SET last_seen=? WHERE token=?",
                 (now_iso(), token))
    conn.commit()
    return True


def destroy_session(conn, token):
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


class Handler(BaseHTTPRequestHandler):
    server_version = "MCC/1.0"
    # HTTP/1.1 + Content-Length di semua respons agar keep-alive browser tidak hang.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (now_iso(), fmt % args))
        sys.stderr.flush()

    # ---------- auth ----------
    def basic_ok(self):
        """Cek HTTP Basic Auth (untuk klien API / skrip)."""
        if not PASSWORD:
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
        except Exception:
            return False
        user, _, pw = decoded.partition(":")
        return check_credentials(user, pw)

    def session_token(self):
        cookie = self.headers.get("Cookie", "")
        if not cookie:
            return None
        c = SimpleCookie()
        try:
            c.load(cookie)
        except Exception:
            return None
        m = c.get(COOKIE_NAME)
        return m.value if m else None

    def auth_ok(self):
        """Lolos jika Basic valid ATAU cookie sesi valid."""
        if self.basic_ok():
            return True
        token = self.session_token()
        if not token:
            return False
        conn = db()
        try:
            return session_valid(conn, token)
        finally:
            conn.close()

    def send_401(self):
        body = b'{"error":"unauthorized"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Muse Command Center"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def set_session_cookie(self, token):
        self.send_header(
            "Set-Cookie",
            "%s=%s; HttpOnly; Path=/; SameSite=Lax; Max-Age=%d"
            % (COOKIE_NAME, token, SESSION_DAYS * 86400))

    def clear_session_cookie(self):
        self.send_header(
            "Set-Cookie",
            "%s=; HttpOnly; Path=/; Max-Age=0" % COOKIE_NAME)

    def serve_file(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            body = b'{"error":"file missing"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---------- routes ----------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/login":
            # Sudah login -> kembali ke dashboard
            if self.auth_ok():
                self.redirect("/")
            else:
                self.serve_file(LOGIN_PATH)
            return
        if u.path == "/" or u.path == "":
            if self.auth_ok():
                self.serve_file(DASHBOARD_PATH)
            elif self.headers.get("Authorization"):
                # Klien API mengirim Basic tapi salah -> 401 semestinya
                self.send_401()
            else:
                # Browser tanpa sesi -> form login (tanpa dialog bawaan browser)
                self.redirect("/login")
            return
        if not self.auth_ok():
            self.send_401()
            return
        if u.path.startswith("/assets/"):
            self.serve_asset(u.path)
            return
        parts = u.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "uploads" \
                and parts[2].isdigit():
            self.serve_upload(int(parts[2]))
            return
        if u.path == "/api/system":
            self.handle_api_system()
            return
        qs = parse_qs(u.query)
        conn = db()
        try:
            if u.path == "/api/tasks":
                status = qs.get("status", ["all"])[0]
                account = qs.get("account", [None])[0]
                try:
                    limit = min(int(qs.get("limit", ["50"])[0]), 200)
                except ValueError:
                    limit = 50
                q = "SELECT * FROM tasks WHERE 1=1"
                args = []
                if status != "all":
                    q += " AND status=?"
                    args.append(status)
                if account:
                    q += " AND account=?"
                    args.append(account)
                q += " ORDER BY updated_at DESC LIMIT ?"
                args.append(limit)
                rows = [dict(r) for r in conn.execute(q, args)]
                self.send_json({"tasks": rows})
            elif u.path == "/api/inbox":
                to = qs.get("for", [None])[0]
                status = qs.get("status", ["open"])[0]
                q = ("SELECT i.*, u.name AS att_name, u.size AS att_size,"
                     " u.mime AS att_mime FROM inbox i"
                     " LEFT JOIN uploads u ON u.id=i.attachment_id WHERE 1=1")
                args = []
                if to:
                    q += " AND (i.to_account=? OR i.to_account='all')"
                    args.append(to)
                if status != "all":
                    q += " AND i.status=?"
                    args.append(status)
                q += " ORDER BY i.created_at DESC LIMIT 100"
                rows = []
                for r in conn.execute(q, args):
                    d = dict(r)
                    att_id = d.pop("attachment_id", None)
                    an = d.pop("att_name", None)
                    d["attachment"] = (
                        {"id": att_id, "name": an,
                         "size": d.pop("att_size", None),
                         "mime": d.pop("att_mime", None)}
                        if att_id else None)
                    d["auto_execute"] = bool(d.get("auto_execute", 0))
                    rows.append(d)
                self.send_json({"inbox": rows})
            elif u.path == "/api/heartbeats":
                rows = [dict(r) for r in
                        conn.execute("SELECT * FROM heartbeats ORDER BY account")]
                self.send_json({"heartbeats": rows})
            elif u.path == "/api/projects":
                rows = [dict(r) for r in
                        conn.execute("SELECT * FROM projects ORDER BY id")]
                self.send_json({"projects": rows})
            elif u.path == "/api/uploads":
                rows = [dict(r) for r in conn.execute(
                    "SELECT id,name,size,mime,created_at FROM uploads"
                    " ORDER BY id DESC")]
                self.send_json({"uploads": rows})
            else:
                self.send_json({"error": "not found"}, 404)
        finally:
            conn.close()

    def handle_upload(self):
        """POST /api/uploads — terima satu file multipart, simpan ke disk."""
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if "multipart/form-data" not in ctype or not m:
            self.send_json(
                {"error": "harap kirim multipart/form-data dengan field file"},
                400)
            return
        boundary = m.group(1).strip().strip('"').encode("latin1")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self.send_json({"error": "body kosong"}, 400)
            return
        if length > MAX_UPLOAD_SIZE + 1024 * 1024:
            # Tolak lebih awal tanpa baca body penuh
            self.send_json(
                {"error": "file melebihi batas 10 MB"}, 413)
            return
        body = self.rfile.read(length)
        parsed = parse_multipart_file(body, boundary)
        if not parsed:
            self.send_json({"error": "field file tidak ditemukan"}, 400)
            return
        fname, mime, data = parsed
        fname = os.path.basename(fname).strip().strip('"')
        if not fname:
            self.send_json({"error": "nama file kosong"}, 400)
            return
        if len(data) > MAX_UPLOAD_SIZE:
            self.send_json(
                {"error": "ukuran file melebihi batas 10 MB"}, 413)
            return
        conn = db()
        try:
            cur = conn.execute(
                "INSERT INTO uploads (name,size,mime,created_at)"
                " VALUES (?,?,?,?)",
                (fname, len(data), mime, now_iso()))
            conn.commit()
            uid = cur.lastrowid
        finally:
            conn.close()
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(os.path.join(UPLOAD_DIR, str(uid)), "wb") as f:
            f.write(data)
        self.send_json(
            {"id": uid, "name": fname, "size": len(data), "mime": mime})

    def serve_upload(self, uid):
        """GET /api/uploads/{id} — unduh file sebagai attachment."""
        conn = db()
        try:
            row = conn.execute(
                "SELECT id,name,size,mime FROM uploads WHERE id=?",
                (uid,)).fetchone()
        finally:
            conn.close()
        if not row:
            self.send_json({"error": "upload id tidak ditemukan"}, 404)
            return
        path = os.path.join(UPLOAD_DIR, str(row["id"]))
        if not os.path.isfile(path):
            self.send_json({"error": "file tidak ada di disk"}, 404)
            return
        disp = row["name"].replace('"', "")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % disp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def serve_asset(self, path):
        """Sajikan file .glb dari folder assets (anti path traversal)."""
        rel = path[len("/assets/"):]
        if not rel or ".." in rel or rel.startswith("/") or "\\" in rel:
            self.send_json({"error": "not found"}, 404)
            return
        if not rel.lower().endswith(".glb"):
            self.send_json({"error": "not found"}, 404)
            return
        base = os.path.join(BASE_DIR, "assets") + os.sep
        full = os.path.normpath(os.path.join(BASE_DIR, "assets", rel))
        if not full.startswith(base):
            self.send_json({"error": "not found"}, 404)
            return
        try:
            with open(full, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "model/gltf-binary")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/login":
            self.handle_login_form()
            return
        if u.path == "/api/login":
            self.handle_api_login()
            return
        if u.path == "/api/logout":
            self.handle_logout()
            return
        if not self.auth_ok():
            self.send_401()
            return
        if u.path == "/api/uploads":
            self.handle_upload()
            return
        parts = u.path.strip("/").split("/")
        data = self.read_json()
        conn = db()
        try:
            if u.path == "/api/tasks":
                account = str(data.get("account", "")).strip()
                title = str(data.get("title", "")).strip()
                detail = str(data.get("detail", ""))
                if not account or not title:
                    self.send_json({"error": "account dan title wajib diisi"}, 400)
                    return
                project_id = data.get("project_id")
                if project_id is not None:
                    try:
                        project_id = int(project_id)
                    except (TypeError, ValueError):
                        self.send_json(
                            {"error": "project_id harus angka atau null"}, 400)
                        return
                cur = conn.execute(
                    "INSERT INTO tasks (account,title,status,detail,project_id,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (account, title, "running", detail, project_id,
                     now_iso(), now_iso()))
                conn.commit()
                self.send_json({"id": cur.lastrowid}, 201)
            elif u.path == "/api/projects":
                name = str(data.get("name", "")).strip()
                description = str(data.get("description", ""))
                if not name:
                    self.send_json({"error": "name wajib diisi"}, 400)
                    return
                cur = conn.execute(
                    "INSERT INTO projects (name,description,created_at)"
                    " VALUES (?,?,?)",
                    (name, description, now_iso()))
                conn.commit()
                self.send_json({"id": cur.lastrowid}, 201)
            elif u.path == "/api/inbox":
                frm = str(data.get("from_account", "")).strip()
                to = str(data.get("to_account", "")).strip()
                title = str(data.get("title", "")).strip()
                body = str(data.get("body", ""))
                if not frm or not to or not title:
                    self.send_json(
                        {"error": "from_account, to_account, title wajib diisi"}, 400)
                    return
                project_id = data.get("project_id")
                if project_id is not None:
                    try:
                        project_id = int(project_id)
                    except (TypeError, ValueError):
                        self.send_json(
                            {"error": "project_id harus angka atau null"}, 400)
                        return
                attachment_id = data.get("attachment_id")
                if attachment_id is not None:
                    try:
                        attachment_id = int(attachment_id)
                    except (TypeError, ValueError):
                        self.send_json(
                            {"error": "attachment_id harus angka atau null"}, 400)
                        return
                    if not conn.execute(
                            "SELECT id FROM uploads WHERE id=?",
                            (attachment_id,)).fetchone():
                        self.send_json(
                            {"error": "attachment_id tidak ditemukan"}, 400)
                        return
                auto_execute = bool(data.get("auto_execute"))
                if auto_execute and frm.lower() not in AUTO_EXECUTE_ALLOW:
                    self.send_json(
                        {"error": "auto_execute hanya berlaku untuk pengirim"
                                  " yang diizinkan (MCC_AUTO_EXECUTE_ALLOW)"}, 400)
                    return
                cur = conn.execute(
                    "INSERT INTO inbox (from_account,to_account,title,body,project_id,attachment_id,auto_execute,status,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (frm, to, title, body, project_id, attachment_id,
                     int(auto_execute), "open", now_iso(), now_iso()))
                conn.commit()
                self.send_json({"id": cur.lastrowid}, 201)
            elif u.path == "/api/heartbeat":
                account = str(data.get("account", "")).strip()
                note = str(data.get("note", ""))
                if not account:
                    self.send_json({"error": "account wajib diisi"}, 400)
                    return
                conn.execute(
                    "INSERT INTO heartbeats (account,last_seen,note) VALUES (?,?,?)"
                    " ON CONFLICT(account) DO UPDATE SET"
                    " last_seen=excluded.last_seen, note=excluded.note",
                    (account, now_iso(), note))
                conn.commit()
                self.send_json({"ok": True})
            elif len(parts) == 4 and parts[0] == "api" and parts[1] == "inbox" \
                    and parts[3] in ("ack", "done", "approve"):
                iid = parts[2]
                if not iid.isdigit():
                    self.send_json({"error": "id tidak valid"}, 400)
                    return
                if parts[3] == "approve":
                    by = str(data.get("by", "")).strip()
                    if not by:
                        self.send_json({"error": "by wajib diisi"}, 400)
                        return
                    row = conn.execute(
                        "SELECT status FROM inbox WHERE id=?",
                        (int(iid),)).fetchone()
                    if not row:
                        self.send_json(
                            {"error": "inbox id tidak ditemukan"}, 404)
                        return
                    if row["status"] == "done":
                        self.send_json(
                            {"error": "titipan sudah selesai"}, 400)
                        return
                    conn.execute(
                        "UPDATE inbox SET auto_execute=1, approved_by=?,"
                        " updated_at=? WHERE id=?",
                        (by, now_iso(), int(iid)))
                    conn.commit()
                    self.send_json({"ok": True})
                    return
                new_status = "acked" if parts[3] == "ack" else "done"
                cur = conn.execute(
                    "UPDATE inbox SET status=?, updated_at=? WHERE id=?",
                    (new_status, now_iso(), int(iid)))
                conn.commit()
                if cur.rowcount == 0:
                    self.send_json({"error": "inbox id tidak ditemukan"}, 404)
                    return
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "not found"}, 404)
        finally:
            conn.close()

    # ---------- login / logout ----------
    def read_form(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 100_000:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw).items() if v}

    def handle_login_form(self):
        """Form HTML -> validasi -> cookie sesi -> redirect dashboard."""
        form = self.read_form()
        if check_credentials(form.get("user"), form.get("password")):
            conn = db()
            try:
                token = create_session(conn)
            finally:
                conn.close()
            self.send_response(302)
            self.set_session_cookie(token)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.redirect("/login?error=1")

    def handle_api_system(self):
        """Metrik sistem live: CPU, RAM, disk, uptime."""
        import shutil
        try:
            load1, load5, load15 = os.getloadavg()
        except Exception:
            load1 = load5 = load15 = 0
        cores = os.cpu_count() or 1
        # RAM dari /proc/meminfo
        mem = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = int(v.strip().split()[0])  # KB
        except Exception:
            pass
        mt = mem.get("MemTotal", 0)
        ma = mem.get("MemAvailable", mem.get("MemFree", 0))
        mu = mt - ma if mt else 0
        # Disk /
        try:
            du = shutil.disk_usage("/")
            dt, du_, df_ = du.total, du.used, du.free
        except Exception:
            dt = du_ = df_ = 0
        # Uptime
        uptime_s = 0
        try:
            with open("/proc/uptime") as f:
                uptime_s = int(float(f.read().split()[0]))
        except Exception:
            pass
        # CPU % dari /proc/stat (delta 0.3s)
        cpu_pct = 0
        try:
            def cpu_times():
                with open("/proc/stat") as f:
                    p = f.readline().split()
                vals = list(map(int, p[1:8]))
                return vals[0] + vals[1] + vals[2], sum(vals)
            u1, t1 = cpu_times()
            import time as _t
            _t.sleep(0.3)
            u2, t2 = cpu_times()
            if t2 > t1:
                cpu_pct = round((u2 - u1) / (t2 - t1) * 100, 1)
        except Exception:
            pass
        data = {
            "cpu_percent": cpu_pct,
            "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
            "cores": cores,
            "ram_total_mb": round(mt / 1024), "ram_used_mb": round(mu / 1024),
            "ram_pct": round(mu / mt * 100, 1) if mt else 0,
            "disk_total_gb": round(dt / 1e9, 1), "disk_used_gb": round(du_ / 1e9, 1),
            "disk_pct": round(du_ / dt * 100, 1) if dt else 0,
            "uptime_s": uptime_s,
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_login(self):
        """Login via JSON (untuk klien non-browser)."""
        data = self.read_json()
        if check_credentials(data.get("user"), data.get("password")):
            conn = db()
            try:
                token = create_session(conn)
            finally:
                conn.close()
            body = b'{"ok":true}'
            self.send_response(200)
            self.set_session_cookie(token)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json({"error": "kredensial salah"}, 401)

    def handle_logout(self):
        conn = db()
        try:
            destroy_session(conn, self.session_token())
        finally:
            conn.close()
        body = b'{"ok":true}'
        self.send_response(200)
        self.clear_session_cookie()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PATCH(self):
        if not self.auth_ok():
            self.send_401()
            return
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "tasks" \
                and parts[2].isdigit():
            data = self.read_json()
            status = str(data.get("status", "")).strip()
            if status not in ("running", "done", "failed"):
                self.send_json({"error": "status harus running|done|failed"}, 400)
                return
            sets = ["status=?"]
            args = [status]
            if "detail" in data:
                sets.append("detail=?")
                args.append(str(data["detail"]))
            if "project_id" in data:
                pid = data["project_id"]
                if pid is not None:
                    try:
                        pid = int(pid)
                    except (TypeError, ValueError):
                        self.send_json(
                            {"error": "project_id harus angka atau null"}, 400)
                        return
                sets.append("project_id=?")
                args.append(pid)
            sets.append("updated_at=?")
            args.append(now_iso())
            args.append(int(parts[2]))
            conn = db()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET %s WHERE id=?" % ",".join(sets), args)
                conn.commit()
            finally:
                conn.close()
            if cur.rowcount == 0:
                self.send_json({"error": "task id tidak ditemukan"}, 404)
                return
            self.send_json({"ok": True})
        elif len(parts) == 3 and parts[0] == "api" and parts[1] == "projects" \
                and parts[2].isdigit():
            data = self.read_json()
            sets = []
            args = []
            if "name" in data:
                name = str(data["name"]).strip()
                if not name:
                    self.send_json({"error": "name tidak boleh kosong"}, 400)
                    return
                sets.append("name=?")
                args.append(name)
            if "description" in data:
                sets.append("description=?")
                args.append(str(data["description"]))
            if not sets:
                self.send_json({"error": "tidak ada field yang diubah"}, 400)
                return
            args.append(int(parts[2]))
            conn = db()
            try:
                cur = conn.execute(
                    "UPDATE projects SET %s WHERE id=?" % ",".join(sets), args)
                conn.commit()
            finally:
                conn.close()
            if cur.rowcount == 0:
                self.send_json({"error": "project id tidak ditemukan"}, 404)
                return
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self.auth_ok():
            self.send_401()
            return
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "projects" \
                and parts[2].isdigit():
            pid = int(parts[2])
            conn = db()
            try:
                cur = conn.execute("DELETE FROM projects WHERE id=?", (pid,))
                if cur.rowcount == 0:
                    conn.commit()
                    self.send_json(
                        {"error": "project id tidak ditemukan"}, 404)
                    return
                # Tugas & titipan tidak ikut terhapus, hanya lepas dari proyek
                conn.execute(
                    "UPDATE tasks SET project_id=NULL WHERE project_id=?",
                    (pid,))
                conn.execute(
                    "UPDATE inbox SET project_id=NULL WHERE project_id=?",
                    (pid,))
                conn.commit()
            finally:
                conn.close()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)


def main():
    os.umask(0o077)
    if not PASSWORD:
        sys.stderr.write("MCC_PASSWORD belum diset (env MCC_PASSWORD atau file .env)\n")
        sys.exit(1)
    init_db()
    server = ThreadingHTTPServer((BIND_IP, PORT), Handler)
    sys.stderr.write("MCC listening on %s:%d\n" % (BIND_IP, PORT))
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
