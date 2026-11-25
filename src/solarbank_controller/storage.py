"""Private SQLite-Journale, Backups und Messprotokolle ohne Identitaeten im Log."""

import json
import math
import os
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .schedule import fingerprint


def private_dir(path: Path):
    if path.is_symlink():
        raise ValueError("Private Verzeichnisse duerfen keine Symlinks sein")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def private_json(path: Path, payload: dict):
    private_dir(path.parent)
    # Kein Ueberschreiben eines Originalbackups, auch nicht ueber Symlinks.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(payload, out, indent=2, default=str)
        out.flush()
        os.fsync(out.fileno())


class Store:
    def __init__(self, directory: Path):
        private_dir(directory)
        self.directory = directory
        db = directory / "telemetry.sqlite"
        if db.is_symlink():
            raise ValueError("SQLite-Symlink abgewiesen")
        self.db = sqlite3.connect(db)
        db.chmod(0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS writes (at REAL NOT NULL, kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cycles (at TEXT NOT NULL, payload TEXT NOT NULL);
        """)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def write_attempt(self, now, kind):
        with self.db:
            self.db.execute("INSERT INTO writes VALUES (?,?)", (now.timestamp(), kind))

    def count(self, since):
        return self.db.execute("SELECT count(*) FROM writes WHERE at>?", (since,)).fetchone()[0]

    def backup(self, sample):
        payload = {
            "device_id": sample.device_id,
            "site_id": sample.site_id,
            "model": sample.model,
            "firmware": sample.firmware,
            "created_at": sample.observed_at.isoformat(),
            "schedule": sample.schedule,
            "sha256": fingerprint(sample.schedule),
        }
        filename = f"backup-{sample.observed_at.strftime('%Y%m%dT%H%M%S%f')}.json"
        private_json(self.directory / filename, payload)
        return filename

    def cycle(self, sample, decision, result, *, communication_error=False, now=None):
        measured = None
        if sample:
            measured = {
                k: v
                for k, v in asdict(sample).items()
                if k not in {"device_id", "site_id", "schedule", "firmware"}
            }
        at = sample.observed_at.isoformat() if sample else (now or datetime.now(UTC)).isoformat()
        record = {
            "recorded_at": at,
            "sample": measured,
            "decision": asdict(decision) if decision else None,
            "result": asdict(result),
            "communication_error": communication_error,
        }

        def clean(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            return value

        payload = json.dumps(clean(record), default=str, allow_nan=False)
        with self.db:
            self.db.execute("INSERT INTO cycles VALUES (?,?)", (at, payload))
        path = self.directory / "events.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as out:
            out.write(payload + "\n")

    def close(self):
        self.db.close()
