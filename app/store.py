"""SQLite persistence for zone versions.

A publish is one transaction: insert version row + all record rows + move
the current_version pointer. Any failure rolls the whole thing back, so the
database never holds a half-written version and the previous version stays
current. A restart simply reloads whatever the pointer names."""

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS records (
    version_id INTEGER NOT NULL REFERENCES versions(id),
    name       TEXT NOT NULL,
    rtype      TEXT NOT NULL CHECK (rtype IN ('A', 'CNAME')),
    value      TEXT NOT NULL,
    ttl        INTEGER NOT NULL,
    UNIQUE (version_id, name, rtype, value)
);
CREATE INDEX IF NOT EXISTS idx_records_version ON records(version_id);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        # autocommit mode; transactions are opened explicitly
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def init(self):
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def publish(self, rows, note=""):
        """Atomically store a full version and make it current.

        rows: list of (name, rtype, value, ttl).
        Returns (new version id, created_at). Raises on any write failure;
        the previous current version is untouched.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            created = utcnow()
            cur = conn.execute(
                "INSERT INTO versions (created_at, note) VALUES (?, ?)",
                (created, note),
            )
            vid = cur.lastrowid
            conn.executemany(
                "INSERT INTO records (version_id, name, rtype, value, ttl)"
                " VALUES (?, ?, ?, ?, ?)",
                [(vid, n, t, v, ttl) for n, t, v, ttl in rows],
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('current_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(vid),),
            )
            conn.execute("COMMIT")
            return vid, created
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def current_version_id(self):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'current_version'"
            ).fetchone()
            return int(row["value"]) if row else None
        finally:
            conn.close()

    def load_version(self, vid):
        conn = self._connect()
        try:
            v = conn.execute(
                "SELECT id, created_at, note FROM versions WHERE id = ?", (vid,)
            ).fetchone()
            if v is None:
                return None
            rows = conn.execute(
                "SELECT name, rtype, value, ttl FROM records"
                " WHERE version_id = ? ORDER BY name, rtype, value",
                (vid,),
            ).fetchall()
            return {
                "id": v["id"],
                "created_at": v["created_at"],
                "note": v["note"],
                "rows": [(r["name"], r["rtype"], r["value"], r["ttl"]) for r in rows],
            }
        finally:
            conn.close()

    def load_current(self):
        vid = self.current_version_id()
        return None if vid is None else self.load_version(vid)

    def list_versions(self, limit=50):
        conn = self._connect()
        try:
            current = self.current_version_id()
            rows = conn.execute(
                "SELECT v.id, v.created_at, v.note, COUNT(r.rowid) AS n_records"
                " FROM versions v LEFT JOIN records r ON r.version_id = v.id"
                " GROUP BY v.id ORDER BY v.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "note": r["note"],
                    "records": r["n_records"],
                    "current": r["id"] == current,
                }
                for r in rows
            ]
        finally:
            conn.close()
