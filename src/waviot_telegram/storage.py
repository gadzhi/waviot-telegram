import json
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def stamp(now=None):
    return (now or datetime.now(timezone.utc)).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=15)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY, gas_id TEXT NOT NULL, label TEXT NOT NULL,
                kind TEXT NOT NULL, evidence TEXT NOT NULL, demo INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'open', version INTEGER NOT NULL DEFAULT 1,
                assignee TEXT, assignee_id INTEGER, diagnosis TEXT NOT NULL DEFAULT 'unknown',
                reason TEXT NOT NULL DEFAULT '', recovered INTEGER DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, message_id INTEGER
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_meter ON incidents(gas_id) WHERE state='open';
            CREATE TABLE IF NOT EXISTS episodes (gas_id TEXT PRIMARY KEY, incident_id INTEGER NOT NULL REFERENCES incidents(id));
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL REFERENCES incidents(id),
                action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                not_before REAL NOT NULL DEFAULT 0, message_id INTEGER, error TEXT
            );
            CREATE TABLE IF NOT EXISTS replies (
                message_id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL,
                version INTEGER NOT NULL, action TEXT NOT NULL, actor_id INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS pending_updates ON updates(done,id);
            CREATE INDEX IF NOT EXISTS pending_outbox ON outbox(state,id);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(incidents)")}
        if "meter_snapshot" not in columns:
            self.db.execute("ALTER TABLE incidents ADD COLUMN meter_snapshot TEXT NOT NULL DEFAULT '{}'")
        from .access import initialize
        initialize(self)
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    @contextmanager
    def atomic(self):
        nested = self.db.in_transaction
        if not nested:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            if not nested:
                self.db.commit()
        except Exception:
            if not nested:
                self.db.rollback()
            raise

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.atomic():
            self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    def incident(self, incident_id):
        row = self.db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return self._incident(row) if row else None

    def incidents(self, *, open_only=True):
        return [self._incident(row) for row in self.db.execute("SELECT * FROM incidents " + ("WHERE state='open' " if open_only else "") + "ORDER BY id")]

    @staticmethod
    def _incident(row):
        result = dict(row)
        result["meter_snapshot"] = json.loads(result["meter_snapshot"])
        return result

    def update_meter_snapshot(self, incident_id, snapshot):
        with self.atomic():
            item = self.incident(incident_id)
            if not item or item["state"] != "open" or item["meter_snapshot"] == snapshot:
                return
            data = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            self.db.execute("UPDATE incidents SET meter_snapshot=? WHERE id=?", (data, incident_id))
            digest = hashlib.sha256(data.encode()).hexdigest()
            self.enqueue(f"meter-details:{incident_id}:{digest}:{item['version']}", "card", {"incident_id": incident_id, "version": item["version"]})

    def history(self, incident_id):
        return [dict(row) for row in self.db.execute("SELECT * FROM history WHERE incident_id=? ORDER BY id", (incident_id,))]

    def set_incident_message_id(self, incident_id, message_id):
        with self.atomic():
            self.db.execute("UPDATE incidents SET message_id=? WHERE id=?", (message_id, incident_id))

    def enqueue(self, key, kind, payload, not_before=0):
        with self.atomic():
            if kind in ("card", "digest", "reminder"):
                payload = {**payload, "chat_id": self.get("group_id"), "group_generation": self.get("group_generation")}
            elif isinstance(payload.get("chat_id"), int) and payload["chat_id"] < 0:
                payload = {**payload, "group_generation": self.get("group_generation")}
            self.db.execute("INSERT OR IGNORE INTO outbox(key,kind,payload,not_before) VALUES (?,?,?,?)", (key, kind, json.dumps(payload, ensure_ascii=False), not_before))

    def _event(self, incident_id, action, actor, reason, now):
        self.db.execute("INSERT INTO history(incident_id,action,actor,reason,at) VALUES (?,?,?,?,?)", (incident_id, action, actor, reason, stamp(now)))
        incident = self.incident(incident_id)
        self.enqueue(f"card:{incident_id}:{incident['version']}", "card", {"incident_id": incident_id, "version": incident["version"]})

    def open_incident(self, gas_id, label, kind, evidence, *, demo=False, meter_snapshot=None, now=None):
        with self.atomic():
            episode = self.db.execute("SELECT incident_id FROM episodes WHERE gas_id=?", (gas_id,)).fetchone()
            if episode:
                return self.incident(episode[0])
            current = self.db.execute("SELECT id FROM incidents WHERE gas_id=? AND state='open'", (gas_id,)).fetchone()
            if current:
                return self.incident(current[0])
            cursor = self.db.execute("INSERT INTO incidents(gas_id,label,kind,evidence,demo,created_at,updated_at,meter_snapshot) VALUES (?,?,?,?,?,?,?,?)", (gas_id, label, kind, evidence, int(demo), stamp(now), stamp(now), json.dumps(meter_snapshot or {}, ensure_ascii=False)))
            incident_id = cursor.lastrowid
            self.db.execute("INSERT INTO episodes VALUES (?,?)", (gas_id, incident_id))
            self._event(incident_id, "created", "Система", evidence, now)
            return self.incident(incident_id)

    def transition(self, incident_id, version, action, actor_id, actor, reason="", *, now=None):
        with self.atomic():
            item = self.incident(incident_id)
            if not item or item["version"] != version or item["state"] != "open":
                return item, False
            if action == "claim":
                changes = {"assignee": actor[:100], "assignee_id": actor_id}
            elif action in ("fault", "unknown", "normal", "repaired"):
                if not reason.strip():
                    raise ValueError("Причина или результат проверки обязательны.")
                if action == "repaired" and item["diagnosis"] != "fault":
                    return item, False
                changes = {"diagnosis": action, "reason": reason[:2000]}
                if action in ("normal", "repaired"):
                    changes["state"] = "closed"
            else:
                raise ValueError("Unknown incident action.")
            changes.update(version=version + 1, updated_at=stamp(now))
            columns = ",".join(f"{key}=?" for key in changes)
            self.db.execute(f"UPDATE incidents SET {columns} WHERE id=?", (*changes.values(), incident_id))
            self._event(incident_id, action, actor[:100], reason[:2000], now)
            return self.incident(incident_id), True

    def mark_recovered(self, gas_id, *, now=None):
        with self.atomic():
            row = self.db.execute("SELECT id FROM incidents WHERE gas_id=? AND state='open' AND recovered=0", (gas_id,)).fetchone()
            if row:
                self.db.execute("UPDATE incidents SET recovered=1,version=version+1,updated_at=? WHERE id=?", (stamp(now), row[0]))
                self._event(row[0], "recovered", "Система", "Данные восстановились; требуется ручное закрытие", now)

    def reset_episode(self, gas_id):
        with self.atomic():
            self.db.execute("DELETE FROM episodes WHERE gas_id=?", (gas_id,))

    def ingest(self, updates):
        with self.atomic():
            for update in updates:
                # Do not persist invitation plaintext in the durable Telegram inbox.
                from .access import Access, code_hash
                update = json.loads(json.dumps(update))
                message = update.get("message", {})
                text = message.get("text")
                actor_id = message.get("from", {}).get("id")
                if isinstance(text, str):
                    connect = text.split(" ")[0].split("@")[0] == "/connect"
                    registration = message.get("chat", {}).get("type") == "private" and not Access(self).employee(actor_id) and not text.startswith("/")
                    if connect or registration:
                        message["registration_code_hash"] = code_hash(text.partition(" ")[2] if connect else text)
                        message["text"] = "/connect" if connect else ""
                self.db.execute("INSERT OR IGNORE INTO updates(id,payload) VALUES (?,?)", (update["update_id"], json.dumps(update, ensure_ascii=False)))

    def offset(self):
        row = self.db.execute("SELECT MAX(id) FROM updates").fetchone()
        return row[0] + 1 if row[0] is not None else None

    def pending_updates(self):
        return [(row["id"], json.loads(row["payload"])) for row in self.db.execute("SELECT * FROM updates WHERE done=0 ORDER BY id")]

    def jobs(self, now):
        return [dict(row) for row in self.db.execute("SELECT * FROM outbox WHERE state='pending' AND kind!='flat_lookup' AND not_before<=? ORDER BY kind IN ('card','digest','reminder'),id LIMIT 30", (now,))]

    def job_state(self, job_id, state, *, error=None, message_id=None, not_before=None):
        with self.atomic():
            self.db.execute("UPDATE outbox SET state=?,error=?,message_id=COALESCE(?,message_id),not_before=COALESCE(?,not_before) WHERE id=?", (state, error, message_id, not_before, job_id))

    def uncertain_jobs(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM outbox WHERE state='unknown' ORDER BY id")]
