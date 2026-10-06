"""Local SQLite store for web UI accounts, audit log and login throttling."""
import os
import sqlite3
import time

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_login INTEGER
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    username TEXT,
    ip TEXT,
    instance TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    success INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS login_failures (
    username TEXT NOT NULL,
    ip TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_ts ON login_failures(ts);
"""

ROLES = ["viewer", "operator", "admin"]


def connect(path):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, 0o750)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["WEBUI"].db_path)
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def audit(username, ip, instance, action, detail="", success=True):
    db = get_db()
    db.execute("INSERT INTO audit (ts, username, ip, instance, action, detail, success) "
               "VALUES (?,?,?,?,?,?,?)",
               (int(time.time()), username, ip, instance, action, (detail or "")[:4000],
                1 if success else 0))
    # keep the audit log bounded
    db.execute("DELETE FROM audit WHERE id <= (SELECT MAX(id) FROM audit) - 100000")
    db.commit()
