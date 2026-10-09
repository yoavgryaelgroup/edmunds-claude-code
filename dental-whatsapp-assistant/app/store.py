"""The assistant's own records in SQLite: patients, conversations, the waitlist, handoffs and an audit log.

Appointments themselves live only in the clinic calendar. No medical information is stored here.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS patient (
    phone            TEXT PRIMARY KEY,        -- WhatsApp number, digits only (e.g. 972501234567)
    full_name        TEXT NOT NULL,
    birth_date       TEXT,
    existing_patient INTEGER NOT NULL DEFAULT 0,   -- 1 = known to the clinic (staff set it); 0 = registered via WhatsApp
    consent_at       TEXT,                    -- when the patient agreed to WhatsApp messages and the privacy notice
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation (
    phone       TEXT PRIMARY KEY,
    messages    TEXT NOT NULL,                -- the recent chat with the AI, as sent to the model
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_message (
    message_id  TEXT PRIMARY KEY,             -- WhatsApp may deliver a webhook twice
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waitlist (
    id          INTEGER PRIMARY KEY,
    phone       TEXT NOT NULL,
    type_key    TEXT NOT NULL,
    notes       TEXT,
    created_at  TEXT NOT NULL,
    offered_at  TEXT,
    closed_at   TEXT
);
CREATE TABLE IF NOT EXISTS handoff (
    id          INTEGER PRIMARY KEY,
    phone       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    urgent      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    phone   TEXT,
    action  TEXT NOT NULL,
    detail  TEXT
);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # patients
    def patient(self, phone):
        r = self._x("SELECT * FROM patient WHERE phone = ?", (phone,)).fetchone()
        return dict(r) if r else None

    def register_patient(self, phone, full_name, birth_date, consent):
        self._x("""INSERT INTO patient (phone, full_name, birth_date, consent_at, created_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (phone) DO UPDATE SET full_name = excluded.full_name,
                     birth_date = excluded.birth_date, consent_at = coalesce(patient.consent_at, excluded.consent_at)""",
                (phone, full_name, birth_date, utcnow() if consent else None, utcnow()))
        self.audit(phone, "register_patient", full_name)

    def set_existing(self, phone, existing=True):
        self._x("UPDATE patient SET existing_patient = ? WHERE phone = ?", (int(existing), phone))

    # conversations
    def history(self, phone):
        r = self._x("SELECT messages, updated_at FROM conversation WHERE phone = ?", (phone,)).fetchone()
        return (json.loads(r["messages"]), r["updated_at"]) if r else ([], None)

    def save_history(self, phone, messages):
        self._x("""INSERT INTO conversation (phone, messages, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT (phone) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at""",
                (phone, json.dumps(messages, ensure_ascii=False), utcnow()))

    def first_time(self, message_id):
        """True the first time a WhatsApp message id is seen."""
        try:
            self._x("INSERT INTO processed_message (message_id, at) VALUES (?, ?)", (message_id, utcnow()))
            return True
        except sqlite3.IntegrityError:
            return False

    # waitlist
    def add_waitlist(self, phone, type_key, notes):
        self._x("INSERT INTO waitlist (phone, type_key, notes, created_at) VALUES (?, ?, ?, ?)",
                (phone, type_key, notes, utcnow()))
        self.audit(phone, "waitlist", type_key)

    def next_waiting(self, type_keys, exclude_phone=None):
        marks = ",".join("?" * len(type_keys))
        r = self._x(f"""SELECT * FROM waitlist WHERE closed_at IS NULL AND offered_at IS NULL
                        AND type_key IN ({marks}) AND phone <> ? ORDER BY created_at LIMIT 1""",
                    (*type_keys, exclude_phone or "")).fetchone()
        return dict(r) if r else None

    def mark_offered(self, waitlist_id):
        self._x("UPDATE waitlist SET offered_at = ? WHERE id = ?", (utcnow(), waitlist_id))

    def close_waitlist(self, phone):
        self._x("UPDATE waitlist SET closed_at = ? WHERE phone = ? AND closed_at IS NULL", (utcnow(), phone))

    def waitlist_for(self, phone):
        return [dict(r) for r in self._x("SELECT * FROM waitlist WHERE phone = ? AND closed_at IS NULL", (phone,))]

    # handoffs and audit
    def add_handoff(self, phone, reason, urgent):
        self._x("INSERT INTO handoff (phone, reason, urgent, created_at) VALUES (?, ?, ?, ?)",
                (phone, reason, int(urgent), utcnow()))
        self.audit(phone, "handoff", reason)

    def open_handoffs(self):
        return [dict(r) for r in self._x("SELECT * FROM handoff WHERE resolved_at IS NULL ORDER BY created_at")]

    def audit(self, phone, action, detail=None):
        self._x("INSERT INTO audit (at, phone, action, detail) VALUES (?, ?, ?, ?)", (utcnow(), phone, action, detail))
