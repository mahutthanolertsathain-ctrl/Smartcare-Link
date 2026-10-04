"""Small psycopg adapter that keeps the local SQLite app API working on Vercel."""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg.rows import tuple_row

DatabaseError = psycopg.Error
IntegrityError = psycopg.IntegrityError

_schema_ready = False
_ID_TABLES = {
    "app_users",
    "emergency_alerts",
    "vital_records",
    "home_visits",
    "care_requests",
    "dispatch_resources",
    "call_signals",
    "triage_records",
}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS triage_records (
        id BIGSERIAL PRIMARY KEY,
        patient_id TEXT NOT NULL,
        assessment_timestamp TEXT NOT NULL,
        is_immediate_red_flag BOOLEAN NOT NULL DEFAULT FALSE,
        main_category TEXT NOT NULL,
        symptom_name TEXT NOT NULL,
        sub_answers TEXT NOT NULL,
        triage_level TEXT NOT NULL,
        recommended_action TEXT NOT NULL,
        queue_number TEXT,
        estimated_time TEXT,
        status TEXT NOT NULL DEFAULT 'waiting'
    )""",
    """CREATE TABLE IF NOT EXISTS emergency_alerts (
        id BIGSERIAL PRIMARY KEY,
        created_at TEXT NOT NULL,
        patient_id TEXT,
        alert_type TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        acknowledged_at TEXT,
        latitude DOUBLE PRECISION,
        longitude DOUBLE PRECISION,
        assigned_resource_id BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS patients (
        patient_id TEXT PRIMARY KEY,
        full_name TEXT NOT NULL DEFAULT '',
        age INTEGER,
        sex TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '',
        chronic_conditions TEXT NOT NULL DEFAULT '[]',
        medications TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        latitude DOUBLE PRECISION,
        longitude DOUBLE PRECISION
    )""",
    """CREATE TABLE IF NOT EXISTS vital_records (
        id BIGSERIAL PRIMARY KEY,
        patient_id TEXT NOT NULL,
        measured_at TEXT NOT NULL,
        systolic DOUBLE PRECISION,
        diastolic DOUBLE PRECISION,
        glucose DOUBLE PRECISION,
        glucose_type TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        recorded_by TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS home_visits (
        id BIGSERIAL PRIMARY KEY,
        patient_id TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        scheduled_for TEXT,
        reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        assigned_volunteer TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        completed_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS care_requests (
        id BIGSERIAL PRIMARY KEY,
        patient_id TEXT NOT NULL,
        request_type TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        staff_note TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS dispatch_resources (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        resource_type TEXT NOT NULL DEFAULT 'รถฉุกเฉิน',
        phone TEXT NOT NULL DEFAULT '',
        latitude DOUBLE PRECISION,
        longitude DOUBLE PRECISION,
        status TEXT NOT NULL DEFAULT 'available',
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS call_sessions (
        code TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'waiting'
    )""",
    """CREATE TABLE IF NOT EXISTS call_signals (
        id BIGSERIAL PRIMARY KEY,
        call_code TEXT NOT NULL,
        sender TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS training_progress (
        id BIGSERIAL PRIMARY KEY,
        role TEXT NOT NULL,
        course_id TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        UNIQUE(role, course_id)
    )""",
    """CREATE TABLE IF NOT EXISTS app_users (
        id BIGSERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS app_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES app_users(id),
        expires_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS login_attempts (
        id BIGSERIAL PRIMARY KEY,
        username TEXT NOT NULL,
        client_ip TEXT NOT NULL,
        attempted_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_triage_status ON triage_records(status, id)",
    "CREATE INDEX IF NOT EXISTS idx_alert_status ON emergency_alerts(status, id)",
    "CREATE INDEX IF NOT EXISTS idx_vitals_patient ON vital_records(patient_id, measured_at)",
    "CREATE INDEX IF NOT EXISTS idx_visits_status ON home_visits(status, id)",
    "CREATE INDEX IF NOT EXISTS idx_call_signals ON call_signals(call_code, id)",
    "CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(username, client_ip, attempted_at)",
)


class Row(dict):
    """Mapping-style row that also supports SQLite's positional indexing."""

    def __init__(self, names: list[str], values: tuple[Any, ...]):
        super().__init__(zip(names, values))
        self._names = names
        self._values = values

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self._last_insert_row = None

    def execute(self, sql: str, params=()):
        clean = sql.strip()
        if clean.upper() == "BEGIN IMMEDIATE":
            # psycopg starts a transaction on the first query; SQLite uses this
            # statement only to request a write lock before doing its work.
            return self
        sql = sql.replace("?", "%s")
        match = re.match(r"\s*INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.I)
        if match and match.group(1).lower() in _ID_TABLES and not re.search(r"\bRETURNING\b", sql, re.I):
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        self._cursor.execute(sql, params or ())
        self._last_insert_row = None
        return self

    @property
    def lastrowid(self):
        if self._last_insert_row is None and self._cursor.description:
            self._last_insert_row = self._cursor.fetchone()
        return self._last_insert_row[0] if self._last_insert_row else None

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def _convert(self, values):
        if values is None:
            return None
        names = [column.name for column in self._cursor.description]
        return Row(names, values)

    def fetchone(self):
        return self._convert(self._cursor.fetchone())

    def fetchall(self):
        return [self._convert(row) for row in self._cursor.fetchall()]


class Connection:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        return Cursor(self._raw.cursor()).execute(sql, params)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def connect(database_url: str) -> Connection:
    global _schema_ready
    raw = psycopg.connect(
        database_url,
        connect_timeout=10,
        prepare_threshold=None,
        row_factory=tuple_row,
    )
    if not _schema_ready:
        try:
            with raw.cursor() as cursor:
                for statement in _SCHEMA:
                    cursor.execute(statement)
            raw.commit()
            _schema_ready = True
        except Exception:
            raw.rollback()
            raw.close()
            raise
    return Connection(raw)
