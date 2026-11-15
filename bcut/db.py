"""A small SQLite store. One file in the data folder, safe to use from several threads.

Rows are plain dicts; JSON columns (names ending in `_json`) are decoded on the way out.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    channels_json TEXT NOT NULL DEFAULT '{}',      -- {"kick": "https://kick.com/x", "twitch": ..., "youtube": ...}
    rights TEXT NOT NULL DEFAULT 'unknown',          -- own | permission | clip_program | unknown
    rights_note TEXT NOT NULL DEFAULT '',            -- proof: link to the clip-program rules, email, contract
    credit TEXT NOT NULL DEFAULT '',                 -- how to credit them, e.g. "@name on Kick"
    auto_live INTEGER NOT NULL DEFAULT 1,
    auto_uploads INTEGER NOT NULL DEFAULT 1,
    auto_post INTEGER NOT NULL DEFAULT 0,
    post_to_json TEXT NOT NULL DEFAULT '[]',         -- account ids
    layout TEXT NOT NULL DEFAULT 'auto',             -- auto | face | split | center | fit
    language TEXT NOT NULL DEFAULT '',
    seen_json TEXT NOT NULL DEFAULT '[]',            -- upload ids already handled
    status_json TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                               -- video | upload | live
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    streamer_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',            -- queued | working | done | failed | recording | stopped
    step TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    streamer_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    hook TEXT NOT NULL DEFAULT '',
    start REAL NOT NULL,
    end REAL NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    why TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '',
    thumb TEXT NOT NULL DEFAULT '',
    captions_json TEXT NOT NULL DEFAULT '{}',          -- per platform: {"instagram": "...", "youtube": {...}}
    guard_json TEXT NOT NULL DEFAULT '{}',              -- full guard report
    verdict TEXT NOT NULL DEFAULT 'pending',            -- pending | pass | review | block
    state TEXT NOT NULL DEFAULT 'new',                  -- new | approved | rejected | queued | posted
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,                             -- instagram | youtube | tiktok | facebook
    name TEXT NOT NULL DEFAULT '',
    secrets_json TEXT NOT NULL DEFAULT '{}',
    settings_json TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    clip_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',          -- scheduled | posting | posted | failed | cancelled
    due REAL NOT NULL,
    remote_id TEXT NOT NULL DEFAULT '',
    remote_url TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    tries INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    streamer_id TEXT,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS clips_job ON clips(job_id);
CREATE INDEX IF NOT EXISTS posts_due ON posts(status, due);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            _conn = sqlite3.connect(config.DATA / "bcut.db", check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if k.endswith("_json"):
            out[k[:-5]] = json.loads(v or "null")
        else:
            out[k] = v
    return out


def _encode(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            out[k + "_json"] = json.dumps(v)
        else:
            out[k] = v
    return out


def insert(table: str, data: dict) -> dict:
    row = _encode(data)
    cols = ",".join(row)
    marks = ",".join("?" for _ in row)
    with _lock:
        conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
    return get(table, data["id"])


def update(table: str, rid: str, **fields) -> dict | None:
    if not fields:
        return get(table, rid)
    row = _encode(fields)
    if table in ("jobs", "posts"):
        row["updated"] = time.time()
    sets = ",".join(f"{k}=?" for k in row)
    with _lock:
        conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", [*row.values(), rid])
    return get(table, rid)


def get(table: str, rid: str) -> dict | None:
    with _lock:
        return _decode(conn().execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone())


def query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [_decode(r) for r in conn().execute(sql, args).fetchall()]


def delete(table: str, rid: str) -> None:
    with _lock:
        conn().execute(f"DELETE FROM {table} WHERE id=?", (rid,))


def log(text: str, level: str = "info", streamer_id: str | None = None) -> None:
    with _lock:
        conn().execute("INSERT INTO events (at, level, streamer_id, text) VALUES (?,?,?,?)",
                       (time.time(), level, streamer_id, text[:500]))


def kv_get(k: str, default=None):
    with _lock:
        r = conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(k: str, v) -> None:
    with _lock:
        conn().execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                       (k, json.dumps(v)))
