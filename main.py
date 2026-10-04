"""Dependency-free local server for the triage and nurse queue MVP.

Patient-provided answers are stored for staff review. The built-in sample scoring
is not a diagnosis and must not replace assessment by qualified clinical staff.
"""
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
import json
import sqlite3
import secrets
import math
import re
import hashlib
import hmac
import os
from http.cookies import SimpleCookie
from datetime import timezone

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "triage.db"
BANGKOK = timezone(timedelta(hours=7), "ICT")
HOST, PORT = "127.0.0.1", 8000

try:
    from vercel_db import DatabaseError as RemoteDatabaseError
    from vercel_db import IntegrityError as RemoteIntegrityError
except ImportError:
    RemoteDatabaseError = type("RemoteDatabaseError", (Exception,), {})
    RemoteIntegrityError = type("RemoteIntegrityError", (Exception,), {})

DATABASE_ERRORS = (sqlite3.Error, RemoteDatabaseError)
DATABASE_INTEGRITY_ERRORS = (sqlite3.IntegrityError, RemoteIntegrityError)


def db_connect():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        from vercel_db import connect as connect_remote
        return connect_remote(database_url)
    if os.environ.get("VERCEL") == "1":
        raise RuntimeError("DATABASE_URL is required for the Vercel deployment")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.execute("""CREATE TABLE IF NOT EXISTS triage_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        assessment_timestamp TEXT NOT NULL,
        is_immediate_red_flag BOOLEAN NOT NULL DEFAULT 0,
        main_category TEXT NOT NULL,
        symptom_name TEXT NOT NULL,
        sub_answers TEXT NOT NULL,
        triage_level TEXT NOT NULL,
        recommended_action TEXT NOT NULL,
        queue_number TEXT,
        estimated_time TEXT,
        status TEXT NOT NULL DEFAULT 'waiting'
    )""")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(triage_records)")}
    if "status" not in columns:
        conn.execute("ALTER TABLE triage_records ADD COLUMN status TEXT NOT NULL DEFAULT 'waiting'")
    conn.execute("""CREATE TABLE IF NOT EXISTS emergency_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        patient_id TEXT,
        alert_type TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        acknowledged_at TEXT,
        latitude REAL,
        longitude REAL,
        assigned_resource_id INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS patients (
        patient_id TEXT PRIMARY KEY,
        full_name TEXT NOT NULL DEFAULT '',
        age INTEGER,
        sex TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '',
        chronic_conditions TEXT NOT NULL DEFAULT '[]',
        medications TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL
    )""")
    patient_columns = {row["name"] for row in conn.execute("PRAGMA table_info(patients)")}
    for name in ("latitude", "longitude"):
        if name not in patient_columns:
            conn.execute(f"ALTER TABLE patients ADD COLUMN {name} REAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS vital_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        measured_at TEXT NOT NULL,
        systolic REAL,
        diastolic REAL,
        glucose REAL,
        glucose_type TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        recorded_by TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS home_visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        scheduled_for TEXT,
        reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        assigned_volunteer TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        completed_at TEXT
    )""")
    visit_columns={row["name"] for row in conn.execute("PRAGMA table_info(home_visits)")}
    if "scheduled_for" not in visit_columns:
        conn.execute("ALTER TABLE home_visits ADD COLUMN scheduled_for TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS care_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        request_type TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        staff_note TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS dispatch_resources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        resource_type TEXT NOT NULL DEFAULT 'รถฉุกเฉิน',
        phone TEXT NOT NULL DEFAULT '',
        latitude REAL,
        longitude REAL,
        status TEXT NOT NULL DEFAULT 'available',
        updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS call_sessions (
        code TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'waiting'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS call_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_code TEXT NOT NULL,
        sender TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS training_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        course_id TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        UNIQUE(role, course_id)
    )""")
    emergency_columns = {row["name"] for row in conn.execute("PRAGMA table_info(emergency_alerts)")}
    for name, definition in (("latitude", "REAL"), ("longitude", "REAL"), ("assigned_resource_id", "INTEGER")):
        if name not in emergency_columns:
            conn.execute(f"ALTER TABLE emergency_alerts ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_triage_status ON triage_records(status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_status ON emergency_alerts(status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vitals_patient ON vital_records(patient_id, measured_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_status ON home_visits(status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_signals ON call_signals(call_code, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS app_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS app_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES app_users(id),
        expires_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        client_ip TEXT NOT NULL,
        attempted_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(username, client_ip, attempted_at)")
    conn.commit()
    conn.close()


def json_safe(row):
    item = dict(row)
    if "sub_answers" in item:
        try:
            item["sub_answers"] = json.loads(item["sub_answers"] or "{}")
        except (TypeError, json.JSONDecodeError):
            item["sub_answers"] = {}
    return item


def distance_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_list(value):
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def valid_patient_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", str(value or "")))


COURSES = [
    {"id":"community-app","role":"อสม.","title":"ใช้แอปและบันทึกข้อมูลชุมชน","duration":"15 นาที","lessons":["ค้นหาหรือสร้างระเบียนด้วย HN ทดสอบ","บันทึกค่าความดัน/น้ำตาลตามเครื่องวัดและระบุเวลา","ส่งคำขอเยี่ยมบ้านและอัปเดตสถานะ"]},
    {"id":"community-first-aid","role":"อสม.","title":"สังเกตอาการและประสานเหตุฉุกเฉิน","duration":"20 นาที","lessons":["ใช้ SOS เมื่อผู้ป่วยต้องการความช่วยเหลือเร่งด่วน","ตรวจตำแหน่งและข้อมูลติดต่อก่อนประสานทีม","เหตุวิกฤตให้โทร 1669 และแจ้งเจ้าหน้าที่ใกล้ตัว"]},
    {"id":"pcu-telemed","role":"รพ.สต.","title":"ติดตามผู้ป่วยด้วย Telemedicine","duration":"25 นาที","lessons":["ทบทวนประวัติและค่าที่ผู้ป่วยบันทึก","เปิดห้องวิดีโอและแชร์รหัสให้ผู้ป่วย","ระบบนี้เป็นเพียงห้องทดลอง ไม่แทนกระบวนการหน่วยบริการ"]},
    {"id":"hospital-triage","role":"รพช.","title":"ทบทวนข้อมูลส่งต่อและจัดลำดับบริการ","duration":"20 นาที","lessons":["ตรวจข้อมูลส่งต่อและประวัติล่าสุด","ยืนยันการคัดกรองโดยบุคลากรทางการแพทย์","ประสานกลับ รพ.สต. เมื่อจำเป็น"]},
    {"id":"hospital-first-aid","role":"รพช.","title":"ทบทวน CPR / First Aid","duration":"30 นาที","lessons":["หลักสูตรนี้ไม่ใช้แทนการฝึกปฏิบัติกับผู้สอน","เข้ารับการอบรมจากหน่วยงานที่ได้รับอนุมัติ","บันทึกการผ่านเฉพาะเมื่ออบรมจริง"]},
    {"id":"municipal-dispatch","role":"เทศบาล","title":"จัดการหน่วยและสวัสดิการชุมชน","duration":"15 นาที","lessons":["ตั้งค่าหน่วยพร้อมใช้และพิกัดฐาน","ตรวจพิกัด SOS ก่อนมอบหมายหน่วย","รับคำขอสวัสดิการและประสานผู้รับผิดชอบ"]}
]


def assign_nearest_resource(conn, alert_id, latitude, longitude):
    if latitude is None or longitude is None:
        return None
    resources = conn.execute("SELECT * FROM dispatch_resources WHERE status='available' AND latitude IS NOT NULL AND longitude IS NOT NULL").fetchall()
    ranked = sorted(((distance_km(latitude, longitude, r["latitude"], r["longitude"]), r) for r in resources), key=lambda pair: pair[0])
    if not ranked:
        return None
    km, resource = ranked[0]
    conn.execute("UPDATE dispatch_resources SET status='dispatched', updated_at=? WHERE id=?", (datetime.now(BANGKOK).isoformat(), resource["id"]))
    conn.execute("UPDATE emergency_alerts SET assigned_resource_id=? WHERE id=?", (resource["id"], alert_id))
    return {"id": resource["id"], "name": resource["name"], "phone": resource["phone"], "distance_km": round(km, 2)}


class TriageHandler(BaseHTTPRequestHandler):
    server_version = "TriageLocal/1.0"

    def log_message(self, fmt, *args):
        # Query strings can contain patient identifiers; keep them out of request logs.
        message = fmt % args
        message = re.sub(r"(GET|POST|PATCH|OPTIONS) [^ ]+", lambda m: m.group(0).split(" ", 1)[0] + " [path hidden]", message)
        print("[%s] %s" % (self.log_date_time_string(), message))

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_page(self, filename):
        path = BASE_DIR / filename
        body = path.read_bytes()
        if filename in ("community_portal.html", "dispatch_dashboard.html", "patient_portal.html"):
            map_assets = b'''<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script src="/map_picker.js"></script><style>.location-map{height:260px;min-height:220px;width:100%;border-radius:.75rem;z-index:0}.leaflet-container{font:inherit}</style>'''
            body = body.replace(b"</head>", map_assets + b"</head>", 1)
        if filename not in ("login.html", "setup.html"):
            toolbar = '''<script>(()=>{const nativeFetch=window.fetch.bind(window);window.fetch=(input,options={})=>nativeFetch(input,{...options,credentials:"same-origin"})})()</script><style>#med-session-bar{position:fixed;right:12px;top:10px;z-index:99999;background:#fff;border:1px solid #cbd5e1;border-radius:999px;padding:5px 10px;font:13px system-ui;box-shadow:0 2px 8px #0002}#med-session-bar button{border:0;background:#0f766e;color:#fff;border-radius:999px;padding:5px 10px;cursor:pointer}</style><div id="med-session-bar">ล็อกอินแล้ว <button onclick="fetch('/api/v1/auth/logout',{method:'POST'}).then(()=>location.href='/login')">ออกจากระบบ</button></div>'''.encode("utf-8")
            body = body.replace(b"<body>", b"<body>" + toolbar, 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def user_count(self):
        conn = db_connect()
        count = conn.execute("SELECT COUNT(*) FROM app_users").fetchone()[0]
        conn.close()
        return count

    def current_user(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("med_session")
        if not morsel:
            return None
        digest = hashlib.sha256(morsel.value.encode()).hexdigest()
        conn = db_connect()
        row = conn.execute("SELECT u.id, u.username FROM app_sessions s JOIN app_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (digest, datetime.now(BANGKOK).isoformat())).fetchone()
        conn.close()
        return dict(row) if row else None

    def require_login(self, api=False):
        if self.current_user():
            return True
        if api:
            self.send_json({"detail": "กรุณาเข้าสู่ระบบก่อน"}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/setup" if self.user_count() == 0 else "/login")
            self.end_headers()
        return False

    def set_session(self, user_id):
        token = secrets.token_urlsafe(32)
        expires = datetime.now(BANGKOK) + timedelta(hours=12)
        digest = hashlib.sha256(token.encode()).hexdigest()
        conn = db_connect()
        conn.execute("DELETE FROM app_sessions WHERE expires_at<=?", (datetime.now(BANGKOK).isoformat(),))
        conn.execute("INSERT INTO app_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (digest, user_id, expires.isoformat()))
        conn.commit(); conn.close()
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        self.send_header("Set-Cookie", f"med_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200{secure}")

    def auth_post(self, path, data):
        if path == "/api/v1/auth/setup":
            if self.user_count():
                return self.send_json({"detail": "ตั้งค่าเจ้าของระบบได้ครั้งเดียว"}, 409)
            if os.environ.get("VERCEL") == "1":
                setup_key = os.environ.get("SETUP_KEY", "")
                provided_key = str(data.get("setup_key", ""))
                if not setup_key:
                    return self.send_json({"detail": "ยังไม่ได้ตั้งค่า SETUP_KEY ใน Vercel"}, 503)
                if not hmac.compare_digest(provided_key, setup_key):
                    return self.send_json({"detail": "รหัสตั้งค่าไม่ถูกต้อง"}, 403)
            username = str(data.get("username", "")).strip().lower()
            password = str(data.get("password", ""))
            if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
                return self.send_json({"detail": "ชื่อผู้ใช้ต้องเป็น a-z, 0-9 หรือ . _ - ยาว 3-40 ตัว"}, 422)
            if len(password) < 12:
                return self.send_json({"detail": "รหัสผ่านต้องยาวอย่างน้อย 12 ตัวอักษร"}, 422)
            salt = secrets.token_bytes(16)
            derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
            encoded = salt.hex() + ":" + derived.hex()
            conn = db_connect()
            try:
                cur = conn.execute("INSERT INTO app_users(username,password_hash,created_at) VALUES(?,?,?)", (username, encoded, datetime.now(BANGKOK).isoformat()))
                conn.commit()
            except DATABASE_INTEGRITY_ERRORS:
                conn.close(); return self.send_json({"detail": "ชื่อผู้ใช้นี้ถูกใช้แล้ว"}, 409)
            user_id = cur.lastrowid
            conn.close()
            self.send_response(201); self.send_header("Content-Type", "application/json; charset=utf-8"); self.set_session(user_id); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
            return
        if path == "/api/v1/auth/login":
            username = str(data.get("username", "")).strip().lower()
            password = str(data.get("password", ""))
            client_ip = self.headers.get("CF-Connecting-IP", self.client_address[0])[:80]
            now = datetime.now(BANGKOK)
            cutoff = (now - timedelta(minutes=15)).isoformat()
            conn = db_connect()
            conn.execute("DELETE FROM login_attempts WHERE attempted_at<?", ((now - timedelta(hours=24)).isoformat(),))
            recent = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE attempted_at>=? AND (username=? OR client_ip=?)", (cutoff, username, client_ip)).fetchone()[0]
            if recent >= 8:
                conn.close()
                return self.send_json({"detail": "ลองเข้าสู่ระบบถี่เกินไป กรุณารอ 15 นาทีแล้วลองใหม่"}, 429)
            row = conn.execute("SELECT id,password_hash FROM app_users WHERE username=?", (username,)).fetchone()
            valid = False
            if row:
                try:
                    salt_hex, hash_hex = row["password_hash"].split(":", 1)
                    valid = hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 310000).hex(), hash_hex)
                except ValueError:
                    valid = False
            if not valid:
                conn.execute("INSERT INTO login_attempts(username,client_ip,attempted_at) VALUES(?,?,?)", (username, client_ip, now.isoformat()))
                conn.commit(); conn.close()
                return self.send_json({"detail": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}, 401)
            conn.execute("DELETE FROM login_attempts WHERE username=? AND client_ip=?", (username, client_ip))
            conn.commit(); conn.close()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.set_session(row["id"]); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
            return
        return None

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("ข้อมูลมีขนาดใหญ่เกินไป")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self):
        self.send_response(403); self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/setup" and self.user_count() == 0:
            return self.send_page("setup.html")
        if parsed.path == "/login":
            if self.current_user():
                self.send_response(302); self.send_header("Location", "/"); self.end_headers(); return
            return self.send_page("login.html")
        if parsed.path == "/api/v1/auth/me":
            user = self.current_user()
            return self.send_json({"authenticated": bool(user), "username": user["username"] if user else None})
        if parsed.path.startswith("/api/") and not self.require_login(api=True):
            return
        if parsed.path not in ("/api/v1/health",) and not parsed.path.startswith("/api/") and parsed.path not in ("/setup", "/login") and not self.require_login():
            return
        if parsed.path == "/map_picker.js":
            body = (BASE_DIR / "map_picker.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/":
            return self.send_page("home.html")
        if parsed.path == "/screening":
            return self.send_page("triage_frontend.html")
        if parsed.path == "/nurse":
            return self.send_page("nurse_dashboard.html")
        if parsed.path == "/community":
            return self.send_page("community_portal.html")
        if parsed.path == "/dispatch":
            return self.send_page("dispatch_dashboard.html")
        if parsed.path == "/telemedicine":
            return self.send_page("telemedicine.html")
        if parsed.path == "/training":
            return self.send_page("training.html")
        if parsed.path == "/patient":
            return self.send_page("patient_portal.html")
        if parsed.path == "/api/v1/health":
            return self.send_json({"status": "ok"})
        if parsed.path == "/api/v1/queue":
            include = parse_qs(parsed.query).get("include_completed", ["false"])[0] == "true"
            conn = db_connect()
            today = datetime.now(BANGKOK).date().isoformat()
            status_filter = "" if include else " AND status IN ('waiting','calling','in_progress')"
            rows = conn.execute(f"SELECT * FROM triage_records WHERE assessment_timestamp LIKE ?{status_filter} ORDER BY CASE triage_level WHEN 'Red' THEN 0 WHEN 'Yellow' THEN 1 ELSE 2 END, id", (f"{today}%",)).fetchall()
            conn.close()
            return self.send_json({"items": [json_safe(row) for row in rows]})
        if parsed.path == "/api/v1/emergencies":
            include = parse_qs(parsed.query).get("include_resolved", ["false"])[0] == "true"
            where = "" if include else "WHERE status = 'active'"
            conn = db_connect()
            rows = conn.execute(f"SELECT e.*, r.name AS assigned_resource, r.phone AS resource_phone, r.status AS resource_status FROM emergency_alerts e LEFT JOIN dispatch_resources r ON r.id=e.assigned_resource_id {where.replace('status', 'e.status')} ORDER BY e.id DESC LIMIT 100").fetchall()
            conn.close()
            return self.send_json({"items": [dict(row) for row in rows]})
        if parsed.path == "/api/v1/patients":
            query = parse_qs(parsed.query).get("q", [""])[0].strip()
            conn = db_connect()
            rows = conn.execute("SELECT patient_id, full_name, age, sex, phone, address, chronic_conditions, medications, updated_at FROM patients WHERE patient_id LIKE ? OR full_name LIKE ? ORDER BY full_name LIMIT 100", (f"%{query}%", f"%{query}%")).fetchall()
            conn.close()
            items = [dict(row) for row in rows]
            for item in items:
                item["chronic_conditions"] = parse_list(item["chronic_conditions"])
                item["medications"] = parse_list(item["medications"])
            return self.send_json({"items": items})
        if parsed.path == "/api/v1/patient":
            patient_id = parse_qs(parsed.query).get("patient_id", [""])[0].strip()
            if not patient_id:
                return self.send_json({"detail": "ต้องระบุ HN"}, 422)
            conn = db_connect()
            patient = conn.execute("SELECT * FROM patients WHERE patient_id=?", (patient_id,)).fetchone()
            vitals = conn.execute("SELECT * FROM vital_records WHERE patient_id=? ORDER BY measured_at DESC LIMIT 30", (patient_id,)).fetchall()
            visits = conn.execute("SELECT * FROM home_visits WHERE patient_id=? ORDER BY id DESC LIMIT 20", (patient_id,)).fetchall()
            triage = conn.execute("SELECT id, assessment_timestamp, main_category, symptom_name, triage_level, queue_number, estimated_time, status FROM triage_records WHERE patient_id=? ORDER BY id DESC LIMIT 20", (patient_id,)).fetchall()
            conn.close()
            if not patient:
                return self.send_json({"detail": "ไม่พบผู้ป่วยรายนี้"}, 404)
            item = dict(patient)
            item["chronic_conditions"] = parse_list(item["chronic_conditions"])
            item["medications"] = parse_list(item["medications"])
            return self.send_json({"patient": item, "vitals": [dict(r) for r in vitals], "home_visits": [dict(r) for r in visits], "triage": [dict(r) for r in triage]})
        if parsed.path == "/api/v1/vitals":
            patient_id = parse_qs(parsed.query).get("patient_id", [""])[0]
            conn = db_connect()
            rows = conn.execute("SELECT * FROM vital_records WHERE patient_id=? ORDER BY measured_at DESC LIMIT 100", (patient_id,)).fetchall() if patient_id else conn.execute("SELECT * FROM vital_records ORDER BY measured_at DESC LIMIT 100").fetchall()
            conn.close()
            return self.send_json({"items": [dict(r) for r in rows]})
        if parsed.path == "/api/v1/home-visits":
            status = parse_qs(parsed.query).get("status", [""])[0]
            patient_id = parse_qs(parsed.query).get("patient_id", [""])[0]
            filters, values = [], []
            if status:
                filters.append("v.status=?"); values.append(status)
            if patient_id:
                filters.append("v.patient_id=?"); values.append(patient_id)
            where = " WHERE " + " AND ".join(filters) if filters else ""
            conn = db_connect()
            rows = conn.execute("SELECT v.*, p.full_name, p.phone, p.address FROM home_visits v LEFT JOIN patients p ON p.patient_id=v.patient_id" + where + " ORDER BY v.id DESC LIMIT 200", values).fetchall()
            conn.close()
            return self.send_json({"items": [dict(r) for r in rows]})
        if parsed.path == "/api/v1/resources":
            conn = db_connect(); rows = conn.execute("SELECT * FROM dispatch_resources ORDER BY status, name").fetchall(); conn.close()
            return self.send_json({"items": [dict(r) for r in rows]})
        if parsed.path == "/api/v1/care-requests":
            status=parse_qs(parsed.query).get("status",[""])[0]
            patient_id=parse_qs(parsed.query).get("patient_id",[""])[0]
            filters,values=[],[]
            if status: filters.append("status=?");values.append(status)
            if patient_id: filters.append("patient_id=?");values.append(patient_id)
            where=" WHERE "+" AND ".join(filters) if filters else ""
            conn=db_connect();rows=conn.execute("SELECT * FROM care_requests"+where+" ORDER BY id DESC LIMIT 200",values).fetchall();conn.close()
            return self.send_json({"items":[dict(r) for r in rows]})
        if parsed.path == "/api/v1/training/courses":
            conn=db_connect();completed={(r["role"],r["course_id"]):r["completed_at"] for r in conn.execute("SELECT role,course_id,completed_at FROM training_progress")};conn.close()
            return self.send_json({"items":[{**c,"completed_at":completed.get((c["role"],c["id"]))} for c in COURSES]})
        if parsed.path.startswith("/api/v1/calls/"):
            code = parsed.path.rsplit("/", 1)[1].upper()
            params = parse_qs(parsed.query)
            try: after_id = max(0, int(params.get("after_id", ["0"])[0]))
            except ValueError: after_id = 0
            sender = params.get("participant", [""])[0]
            conn = db_connect()
            session = conn.execute("SELECT * FROM call_sessions WHERE code=?", (code,)).fetchone()
            signals = conn.execute("SELECT * FROM call_signals WHERE call_code=? AND id>? AND sender!=? ORDER BY id LIMIT 100", (code, after_id, sender)).fetchall()
            conn.close()
            if not session: return self.send_json({"detail":"ไม่พบห้องปรึกษา"},404)
            return self.send_json({"session": dict(session), "signals": [dict(s, payload=json.loads(s["payload"])) for s in signals]})
        return self.send_json({"detail": "ไม่พบหน้าเว็บ"}, 404)

    def do_POST(self):
        try:
            data = self.read_json()
            path = urlparse(self.path).path
            if path in ("/api/v1/auth/setup", "/api/v1/auth/login"):
                return self.auth_post(path, data)
            if path == "/api/v1/auth/logout":
                user = self.current_user()
                if user:
                    cookie = SimpleCookie(self.headers.get("Cookie", "")); morsel = cookie.get("med_session")
                    if morsel:
                        conn = db_connect(); conn.execute("DELETE FROM app_sessions WHERE token_hash=?", (hashlib.sha256(morsel.value.encode()).hexdigest(),)); conn.commit(); conn.close()
                self.send_response(200); self.send_header("Set-Cookie", "med_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"); self.send_header("Content-Length", "0"); self.end_headers(); return
            if not self.require_login(api=True):
                return
            if self.path == "/api/v1/triage":
                return self.submit_triage(data)
            if self.path == "/api/v1/emergencies":
                patient_id = str(data.get("patient_id", ""))[:80].strip() or None
                if patient_id and not valid_patient_id(patient_id): return self.send_json({"detail":"HN ใช้ตัวอักษรอังกฤษ ตัวเลข จุด ขีดกลาง หรือขีดล่างเท่านั้น"},422)
                alert_type = str(data.get("alert_type", "ผู้ป่วยต้องการความช่วยเหลือด่วน"))[:120].strip()
                details = str(data.get("details", ""))[:1000].strip()
                latitude = data.get("latitude")
                longitude = data.get("longitude")
                if (latitude is None) != (longitude is None):
                    return self.send_json({"detail": "พิกัดไม่ครบถ้วน"}, 422)
                if latitude is not None:
                    latitude, longitude = float(latitude), float(longitude)
                    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                        return self.send_json({"detail": "พิกัดไม่ถูกต้อง"}, 422)
                if not alert_type:
                    return self.send_json({"detail": "กรุณาระบุประเภทเหตุ"}, 422)
                conn = db_connect()
                cursor = conn.execute("INSERT INTO emergency_alerts (created_at, patient_id, alert_type, details, latitude, longitude) VALUES (?, ?, ?, ?, ?, ?)", (datetime.now(BANGKOK).isoformat(), patient_id, alert_type, details, latitude, longitude))
                alert_id = cursor.lastrowid
                assigned = assign_nearest_resource(conn, alert_id, latitude, longitude)
                row = conn.execute("SELECT * FROM emergency_alerts WHERE id = ?", (cursor.lastrowid,)).fetchone()
                conn.commit(); conn.close()
                item = dict(row); item["assigned_resource"] = assigned
                return self.send_json(item, 201)
            if self.path == "/api/v1/patients":
                patient_id = str(data.get("patient_id", "")).strip()[:80]
                if not valid_patient_id(patient_id):
                    return self.send_json({"detail": "กรุณากรอก HN"}, 422)
                latitude, longitude = data.get("latitude"), data.get("longitude")
                if (latitude is None) != (longitude is None):
                    return self.send_json({"detail": "ต้องระบุพิกัดทั้งละติจูดและลองจิจูด"}, 422)
                if latitude is not None:
                    try:
                        latitude, longitude = float(latitude), float(longitude)
                    except (ValueError, TypeError):
                        return self.send_json({"detail": "พิกัดไม่ถูกต้อง"}, 422)
                    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                        return self.send_json({"detail": "พิกัดไม่ถูกต้อง"}, 422)
                try: age = int(data["age"]) if data.get("age") not in (None, "") else None
                except (ValueError, TypeError): return self.send_json({"detail": "อายุไม่ถูกต้อง"}, 422)
                if age is not None and not (0 <= age <= 125): return self.send_json({"detail": "อายุไม่ถูกต้อง"}, 422)
                conditions = data.get("chronic_conditions", [])
                medications = data.get("medications", [])
                if not isinstance(conditions, list) or not isinstance(medications, list):
                    return self.send_json({"detail": "รูปแบบโรคประจำตัวหรือยาไม่ถูกต้อง"}, 422)
                now = datetime.now(BANGKOK).isoformat()
                conn = db_connect()
                conn.execute("""INSERT INTO patients (patient_id, full_name, age, sex, phone, address, chronic_conditions, medications, updated_at, latitude, longitude)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(patient_id) DO UPDATE SET
                    full_name=excluded.full_name, age=excluded.age, sex=excluded.sex, phone=excluded.phone,
                    address=excluded.address, chronic_conditions=excluded.chronic_conditions,
                    medications=excluded.medications, updated_at=excluded.updated_at,
                    latitude=excluded.latitude, longitude=excluded.longitude""", (
                    patient_id, str(data.get("full_name", ""))[:150], age, str(data.get("sex", ""))[:30],
                    str(data.get("phone", ""))[:40], str(data.get("address", ""))[:400],
                    json.dumps([str(x)[:120] for x in conditions if str(x).strip()], ensure_ascii=False),
                    json.dumps([str(x)[:160] for x in medications if str(x).strip()], ensure_ascii=False), now,
                    latitude, longitude))
                row = conn.execute("SELECT * FROM patients WHERE patient_id=?", (patient_id,)).fetchone()
                conn.commit(); conn.close()
                item = dict(row); item["chronic_conditions"] = parse_list(item["chronic_conditions"]); item["medications"] = parse_list(item["medications"])
                return self.send_json(item, 201)
            if self.path == "/api/v1/vitals":
                patient_id = str(data.get("patient_id", "")).strip()[:80]
                if not valid_patient_id(patient_id): return self.send_json({"detail":"กรุณาระบุ HN ที่ถูกต้อง"},422)
                def number(key, low, high):
                    raw = data.get(key)
                    if raw in (None, ""): return None
                    value = float(raw)
                    if not low <= value <= high: raise ValueError(f"{key} อยู่นอกช่วงที่รับได้")
                    return value
                systolic, diastolic, glucose = number("systolic", 40, 300), number("diastolic", 20, 200), number("glucose", 1, 2000)
                if systolic is None and diastolic is None and glucose is None:
                    return self.send_json({"detail":"กรุณากรอกค่าที่วัดอย่างน้อยหนึ่งค่า"},422)
                conn = db_connect()
                if not conn.execute("SELECT 1 FROM patients WHERE patient_id=?", (patient_id,)).fetchone():
                    conn.close(); return self.send_json({"detail":"ไม่พบ HN นี้ กรุณาลงทะเบียนประวัติก่อน"},404)
                cursor = conn.execute("INSERT INTO vital_records (patient_id, measured_at, systolic, diastolic, glucose, glucose_type, note, recorded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                    patient_id, datetime.now(BANGKOK).isoformat(), systolic, diastolic, glucose,
                    str(data.get("glucose_type", ""))[:60], str(data.get("note", ""))[:300], str(data.get("recorded_by", ""))[:100]))
                row = conn.execute("SELECT * FROM vital_records WHERE id=?", (cursor.lastrowid,)).fetchone()
                conn.commit(); conn.close(); return self.send_json(dict(row),201)
            if self.path == "/api/v1/home-visits":
                patient_id = str(data.get("patient_id", "")).strip()[:80]
                if not valid_patient_id(patient_id): return self.send_json({"detail":"กรุณาระบุ HN ที่ถูกต้อง"},422)
                conn = db_connect()
                if not patient_id or not conn.execute("SELECT 1 FROM patients WHERE patient_id=?", (patient_id,)).fetchone():
                    conn.close(); return self.send_json({"detail":"ไม่พบประวัติผู้ป่วย กรุณาลงทะเบียนก่อน"},404)
                cursor = conn.execute("INSERT INTO home_visits (patient_id, requested_at, scheduled_for, reason) VALUES (?, ?, ?, ?)", (patient_id, datetime.now(BANGKOK).isoformat(), str(data.get("scheduled_for", ""))[:40] or None, str(data.get("reason", "ขอเยี่ยมบ้าน"))[:300]))
                row = conn.execute("SELECT * FROM home_visits WHERE id=?", (cursor.lastrowid,)).fetchone()
                conn.commit(); conn.close(); return self.send_json(dict(row),201)
            if self.path == "/api/v1/care-requests":
                patient_id=str(data.get("patient_id"," ")).strip()[:80]; request_type=str(data.get("request_type","refill"))[:40]; details=str(data.get("details",""))[:500]
                if not valid_patient_id(patient_id): return self.send_json({"detail":"กรุณาระบุ HN ที่ถูกต้อง"},422)
                if request_type not in {"refill","teleconsult","social_support","other"}: return self.send_json({"detail":"ประเภทคำขอไม่ถูกต้อง"},422)
                conn=db_connect()
                if not conn.execute("SELECT 1 FROM patients WHERE patient_id=?",(patient_id,)).fetchone(): conn.close();return self.send_json({"detail":"ไม่พบประวัติผู้ป่วย"},404)
                cursor=conn.execute("INSERT INTO care_requests(patient_id,request_type,details,created_at) VALUES(?,?,?,?)",(patient_id,request_type,details,datetime.now(BANGKOK).isoformat()));row=conn.execute("SELECT * FROM care_requests WHERE id=?",(cursor.lastrowid,)).fetchone();conn.commit();conn.close();return self.send_json(dict(row),201)
            if self.path == "/api/v1/training/progress":
                role=str(data.get("role",""))[:40]; course_id=str(data.get("course_id",""))[:80]
                if not any(c["role"]==role and c["id"]==course_id for c in COURSES): return self.send_json({"detail":"ไม่พบหลักสูตรสำหรับบทบาทนี้"},404)
                conn=db_connect();conn.execute("INSERT INTO training_progress(role,course_id,completed_at) VALUES(?,?,?) ON CONFLICT(role,course_id) DO UPDATE SET completed_at=excluded.completed_at",(role,course_id,datetime.now(BANGKOK).isoformat()));row=conn.execute("SELECT * FROM training_progress WHERE role=? AND course_id=?",(role,course_id)).fetchone();conn.commit();conn.close();return self.send_json(dict(row),201)
            if self.path == "/api/v1/resources":
                name = str(data.get("name", "")).strip()[:120]
                if not name: return self.send_json({"detail":"กรุณาระบุชื่อหน่วยช่วยเหลือ"},422)
                lat, lng = data.get("latitude"), data.get("longitude")
                if (lat is None) != (lng is None): return self.send_json({"detail":"ต้องระบุพิกัดทั้งละติจูดและลองจิจูด"},422)
                if lat is not None:
                    lat,lng=float(lat),float(lng)
                    if not (-90<=lat<=90 and -180<=lng<=180): return self.send_json({"detail":"พิกัดไม่ถูกต้อง"},422)
                conn=db_connect(); cursor=conn.execute("INSERT INTO dispatch_resources (name,resource_type,phone,latitude,longitude,status,updated_at) VALUES (?,?,?,?,?,'available',?)",(name,str(data.get("resource_type","รถฉุกเฉิน"))[:80],str(data.get("phone",""))[:40],lat,lng,datetime.now(BANGKOK).isoformat()))
                row=conn.execute("SELECT * FROM dispatch_resources WHERE id=?",(cursor.lastrowid,)).fetchone();conn.commit();conn.close();return self.send_json(dict(row),201)
            if self.path == "/api/v1/calls":
                code=secrets.token_hex(3).upper(); patient_id=str(data.get("patient_id",""))[:80]; created_by=str(data.get("created_by",""))[:80]
                conn=db_connect(); conn.execute("INSERT INTO call_sessions(code,patient_id,created_by,created_at) VALUES(?,?,?,?)",(code,patient_id,created_by,datetime.now(BANGKOK).isoformat()));conn.commit();conn.close()
                return self.send_json({"code":code,"patient_id":patient_id,"status":"waiting"},201)
            if self.path.startswith("/api/v1/calls/") and self.path.endswith("/signals"):
                code=self.path.split("/")[-2].upper(); sender=str(data.get("sender",""))[:30]; kind=str(data.get("kind",""))[:30]
                if not sender or kind not in {"offer","answer","candidate","hangup"}: return self.send_json({"detail":"ข้อมูลสัญญาณไม่ถูกต้อง"},422)
                conn=db_connect()
                if not conn.execute("SELECT 1 FROM call_sessions WHERE code=? AND status='waiting'",(code,)).fetchone(): conn.close(); return self.send_json({"detail":"ห้องนี้ปิดแล้วหรือไม่มีอยู่"},404)
                cursor=conn.execute("INSERT INTO call_signals(call_code,sender,kind,payload,created_at) VALUES(?,?,?,?,?)",(code,sender,kind,json.dumps(data.get("payload"),ensure_ascii=False),datetime.now(BANGKOK).isoformat()));conn.commit();conn.close();return self.send_json({"id":cursor.lastrowid},201)
            if self.path == "/api/v1/update_status":
                params = parse_qs(urlparse(self.path).query)
                return self.send_json({"detail": "ใช้ PATCH /api/v1/queue/{id} เพื่ออัปเดตสถานะ"}, 405)
            return self.send_json({"detail": "ไม่พบ endpoint"}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json({"detail": str(exc) or "ข้อมูลไม่ถูกต้อง"}, 422)
        except Exception as exc:
            return self.send_json({"detail": "เกิดข้อผิดพลาดภายในระบบ"}, 500)

    def submit_triage(self, data):
        patient_id = str(data.get("patient_id", "")).strip()[:80]
        triage = data.get("triage_data") or {}
        result = data.get("computed_result") or {}
        category = str(triage.get("main_category", "")).strip()[:100]
        symptom = str(triage.get("symptom_name", "")).strip()[:200]
        level = result.get("triage_level")
        if not valid_patient_id(patient_id) or not category or not symptom or level not in ("Green", "Yellow", "Red"):
            return self.send_json({"detail": "กรุณาตรวจสอบข้อมูลผู้ป่วยและแบบประเมิน"}, 422)
        latitude, longitude = triage.get("latitude"), triage.get("longitude")
        if (latitude is None) != (longitude is None):
            return self.send_json({"detail":"พิกัดไม่ครบถ้วน"},422)
        if latitude is not None:
            latitude,longitude=float(latitude),float(longitude)
            if not (-90<=latitude<=90 and -180<=longitude<=180): return self.send_json({"detail":"พิกัดไม่ถูกต้อง"},422)
        now = datetime.now(BANGKOK)
        created_at = now.isoformat()
        status, queue_number, estimated = ("emergency", None, None) if level == "Red" else ("waiting", None, None)
        conn = db_connect()
        if level != "Red":
            prefix = "F" if level == "Yellow" else "A"
            count = conn.execute("SELECT COUNT(*) n FROM triage_records WHERE queue_number LIKE ? AND assessment_timestamp LIKE ?", (f"{prefix}-%", f"{now.date()}%" )).fetchone()["n"]
            queue_number = f"{prefix}-{count+1:03d}"
            duration = 10 if level == "Yellow" else {"Respiratory":7,"Gastrointestinal":15,"Musculoskeletal":20}.get(category,10)
            previous = conn.execute("SELECT estimated_time FROM triage_records WHERE queue_number LIKE ? AND assessment_timestamp LIKE ? ORDER BY id DESC LIMIT 1", (f"{prefix}-%", f"{now.date()}%")).fetchone()
            try:
                est = datetime.fromisoformat(previous["estimated_time"]) + timedelta(minutes=duration) if previous and previous["estimated_time"] else now + timedelta(minutes=5)
            except ValueError:
                est = now + timedelta(minutes=5)
            if est < now:
                est = now + timedelta(minutes=5)
            estimated = est.strftime("%H:%M")
        conn.execute("""INSERT INTO triage_records (patient_id, assessment_timestamp,
            is_immediate_red_flag, main_category, symptom_name, sub_answers,
            triage_level, recommended_action, queue_number, estimated_time, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            patient_id, created_at, bool(triage.get("is_immediate_red_flag", False)),
            category, symptom, json.dumps(triage.get("sub_answers", {}), ensure_ascii=False),
            level, str(result.get("recommended_action", ""))[:500], queue_number, estimated, status,
        ))
        if level == "Red":
            cursor=conn.execute("INSERT INTO emergency_alerts (created_at, patient_id, alert_type, details, latitude, longitude) VALUES (?, ?, ?, ?, ?, ?)",(created_at,patient_id,"สัญญาณเตือนจากแบบประเมิน",symptom,latitude,longitude))
            assign_nearest_resource(conn,cursor.lastrowid,latitude,longitude)
        conn.commit(); conn.close()
        return self.send_json({"status":"success","message":"บันทึกข้อมูลแล้ว","queue_number":queue_number,"estimated_time":estimated,"triage_level":level})

    def do_DELETE(self):
        if not self.require_login(api=True):
            return
        path = urlparse(self.path).path.rstrip("/")
        resource_prefix = "/api/v1/resources/"
        if path.startswith(resource_prefix):
            try:
                resource_id = int(path[len(resource_prefix):])
            except ValueError:
                return self.send_json({"detail": "รหัสหน่วยไม่ถูกต้อง"}, 422)
            conn = db_connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not conn.execute("SELECT 1 FROM dispatch_resources WHERE id=?", (resource_id,)).fetchone():
                    conn.rollback()
                    return self.send_json({"detail": "ไม่พบรถหรือหน่วยนี้"}, 404)
                if conn.execute("SELECT 1 FROM emergency_alerts WHERE assigned_resource_id=? AND status='active' LIMIT 1", (resource_id,)).fetchone():
                    conn.rollback()
                    return self.send_json({"detail": "หน่วยนี้ยังถูกจัดไปเหตุ SOS ที่เปิดอยู่ กรุณาปิดเหตุหรือเปลี่ยนหน่วยก่อนลบ"}, 409)
                conn.execute("UPDATE emergency_alerts SET assigned_resource_id=NULL WHERE assigned_resource_id=?", (resource_id,))
                conn.execute("DELETE FROM dispatch_resources WHERE id=?", (resource_id,))
                conn.commit()
                return self.send_json({"status": "deleted", "resource_id": resource_id})
            except DATABASE_ERRORS:
                conn.rollback()
                return self.send_json({"detail": "ลบรถหรือหน่วยไม่สำเร็จ กรุณาลองใหม่"}, 500)
            finally:
                conn.close()
        prefix = "/api/v1/patients/"
        if not path.startswith(prefix):
            return self.send_json({"detail": "ไม่พบ endpoint"}, 404)
        patient_id = unquote(path[len(prefix):]).strip()
        if not valid_patient_id(patient_id):
            return self.send_json({"detail": "HN ไม่ถูกต้อง"}, 422)
        conn = db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM patients WHERE patient_id=?", (patient_id,)).fetchone():
                conn.rollback()
                return self.send_json({"detail": "ไม่พบผู้ป่วยรายนี้"}, 404)
            blockers = [
                ("SELECT 1 FROM emergency_alerts WHERE patient_id=? AND status='active' LIMIT 1", "ยังมี SOS ที่กำลังดำเนินการ"),
                ("SELECT 1 FROM triage_records WHERE patient_id=? AND status IN ('waiting','calling','in_progress') LIMIT 1", "ยังมีคิวหรือการประเมินที่กำลังดำเนินการ"),
                ("SELECT 1 FROM home_visits WHERE patient_id=? AND status IN ('pending','assigned','in_progress') LIMIT 1", "ยังมีงานเยี่ยมบ้านค้างอยู่"),
                ("SELECT 1 FROM care_requests WHERE patient_id=? AND status IN ('pending','in_progress') LIMIT 1", "ยังมีคำขอดูแลที่กำลังดำเนินการ"),
            ]
            for query, message in blockers:
                if conn.execute(query, (patient_id,)).fetchone():
                    conn.rollback()
                    return self.send_json({"detail": message + " กรุณาปิดงานให้เรียบร้อยก่อนลบ"}, 409)
            call_codes = [r["code"] for r in conn.execute("SELECT code FROM call_sessions WHERE patient_id=?", (patient_id,)).fetchall()]
            if call_codes:
                placeholders = ",".join("?" for _ in call_codes)
                conn.execute(f"DELETE FROM call_signals WHERE call_code IN ({placeholders})", call_codes)
            conn.execute("DELETE FROM call_sessions WHERE patient_id=?", (patient_id,))
            for table in ("vital_records", "home_visits", "care_requests", "triage_records", "emergency_alerts"):
                conn.execute(f"DELETE FROM {table} WHERE patient_id=?", (patient_id,))
            conn.execute("DELETE FROM patients WHERE patient_id=?", (patient_id,))
            conn.commit()
            return self.send_json({"status": "deleted", "patient_id": patient_id})
        except DATABASE_ERRORS:
            conn.rollback()
            return self.send_json({"detail": "ลบข้อมูลไม่สำเร็จ กรุณาลองใหม่"}, 500)
        finally:
            conn.close()

    def do_PATCH(self):
        if not self.require_login(api=True):
            return
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path.startswith("/api/v1/queue/"):
                record_id = int(path.rsplit("/", 1)[1])
                status = self.read_json().get("status")
                allowed = {"waiting", "calling", "in_progress", "done", "no_show", "cancelled"}
                if status not in allowed:
                    return self.send_json({"detail": "สถานะไม่ถูกต้อง"}, 422)
                conn = db_connect()
                result = conn.execute("UPDATE triage_records SET status = ? WHERE id = ?", (status, record_id))
                row = conn.execute("SELECT * FROM triage_records WHERE id = ?", (record_id,)).fetchone()
                conn.commit(); conn.close()
                return self.send_json(json_safe(row)) if result.rowcount else self.send_json({"detail":"ไม่พบรายการคิว"},404)
            if path.startswith("/api/v1/emergencies/"):
                alert_id = int(path.rsplit("/",1)[1])
                data = self.read_json()
                action = data.get("action", "acknowledge")
                conn = db_connect()
                if action == "acknowledge":
                    result = conn.execute("UPDATE emergency_alerts SET acknowledged_at=? WHERE id=? AND status='active'", (datetime.now(BANGKOK).isoformat(), alert_id))
                elif action == "close":
                    result = conn.execute("UPDATE emergency_alerts SET status='closed', acknowledged_at=COALESCE(acknowledged_at, ?) WHERE id=? AND status!='closed'", (datetime.now(BANGKOK).isoformat(), alert_id))
                    if result.rowcount:
                        conn.execute("UPDATE dispatch_resources SET status='available', updated_at=? WHERE id=(SELECT assigned_resource_id FROM emergency_alerts WHERE id=?)", (datetime.now(BANGKOK).isoformat(), alert_id))
                elif action == "assign":
                    resource_id = int(data.get("resource_id", 0))
                    alert = conn.execute("SELECT * FROM emergency_alerts WHERE id=? AND status!='closed'", (alert_id,)).fetchone()
                    resource = conn.execute("SELECT * FROM dispatch_resources WHERE id=? AND status='available'", (resource_id,)).fetchone()
                    if not alert or not resource:
                        conn.close(); return self.send_json({"detail":"ไม่พบเหตุหรือหน่วยที่พร้อมใช้งาน"},409)
                    old = alert["assigned_resource_id"]
                    conn.execute("UPDATE dispatch_resources SET status='available',updated_at=? WHERE id=?",(datetime.now(BANGKOK).isoformat(),old)) if old else None
                    conn.execute("UPDATE dispatch_resources SET status='dispatched',updated_at=? WHERE id=?",(datetime.now(BANGKOK).isoformat(),resource_id))
                    result = conn.execute("UPDATE emergency_alerts SET assigned_resource_id=?, acknowledged_at=COALESCE(acknowledged_at, ?) WHERE id=?",(resource_id,datetime.now(BANGKOK).isoformat(),alert_id))
                else:
                    conn.close(); return self.send_json({"detail":"คำสั่งไม่ถูกต้อง"},422)
                row = conn.execute("SELECT * FROM emergency_alerts WHERE id=?", (alert_id,)).fetchone()
                conn.commit(); conn.close()
                return self.send_json(dict(row)) if result.rowcount else self.send_json({"detail":"ไม่พบเหตุที่รอรับทราบ"},404)
            if path.startswith("/api/v1/home-visits/"):
                visit_id=int(path.rsplit("/",1)[1]); data=self.read_json(); status=data.get("status")
                if status not in {"pending","assigned","in_progress","done","cancelled"}: return self.send_json({"detail":"สถานะไม่ถูกต้อง"},422)
                volunteer=str(data.get("assigned_volunteer",""))[:100]; note=str(data.get("note",""))[:1000]; scheduled=str(data.get("scheduled_for",""))[:40]
                conn=db_connect(); result=conn.execute("UPDATE home_visits SET status=?,assigned_volunteer=CASE WHEN ?!='' THEN ? ELSE assigned_volunteer END,note=CASE WHEN ?!='' THEN ? ELSE note END,scheduled_for=CASE WHEN ?!='' THEN ? ELSE scheduled_for END,completed_at=CASE WHEN ?='done' THEN ? ELSE completed_at END WHERE id=?",(status,volunteer,volunteer,note,note,scheduled,scheduled,status,datetime.now(BANGKOK).isoformat(),visit_id)); row=conn.execute("SELECT * FROM home_visits WHERE id=?",(visit_id,)).fetchone();conn.commit();conn.close()
                return self.send_json(dict(row)) if result.rowcount else self.send_json({"detail":"ไม่พบคำขอเยี่ยมบ้าน"},404)
            if path.startswith("/api/v1/care-requests/"):
                request_id=int(path.rsplit("/",1)[1]);data=self.read_json();status=data.get("status")
                if status not in {"pending","in_progress","done","cancelled"}: return self.send_json({"detail":"สถานะไม่ถูกต้อง"},422)
                conn=db_connect();result=conn.execute("UPDATE care_requests SET status=?,staff_note=? WHERE id=?",(status,str(data.get("staff_note",""))[:500],request_id));row=conn.execute("SELECT * FROM care_requests WHERE id=?",(request_id,)).fetchone();conn.commit();conn.close()
                return self.send_json(dict(row)) if result.rowcount else self.send_json({"detail":"ไม่พบคำขอ"},404)
            if path.startswith("/api/v1/resources/"):
                resource_id=int(path.rsplit("/",1)[1]); data=self.read_json(); status=data.get("status")
                if status not in {"available","unavailable","dispatched"}: return self.send_json({"detail":"สถานะไม่ถูกต้อง"},422)
                conn=db_connect(); result=conn.execute("UPDATE dispatch_resources SET status=?,updated_at=? WHERE id=?",(status,datetime.now(BANGKOK).isoformat(),resource_id)); row=conn.execute("SELECT * FROM dispatch_resources WHERE id=?",(resource_id,)).fetchone();conn.commit();conn.close()
                return self.send_json(dict(row)) if result.rowcount else self.send_json({"detail":"ไม่พบหน่วย"},404)
            if path.startswith("/api/v1/calls/"):
                code=path.rsplit("/",1)[1].upper(); conn=db_connect(); result=conn.execute("UPDATE call_sessions SET status='closed' WHERE code=? AND status='waiting'",(code,));conn.commit();conn.close()
                return self.send_json({"status":"closed","code":code}) if result.rowcount else self.send_json({"detail":"ไม่พบห้อง"},404)
            return self.send_json({"detail":"ไม่พบ endpoint"},404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json({"detail":str(exc)},422)


if __name__ == "__main__":
    init_db()
    print(f"Triage local server running at http://{HOST}:{PORT}")
    print("Patient page: http://127.0.0.1:8000/  |  Nurse board: http://127.0.0.1:8000/nurse")
    ThreadingHTTPServer((HOST, PORT), TriageHandler).serve_forever()
