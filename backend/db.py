import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/Projects/summary-miniapp/backend/data.db")


def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                openid TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                free_uses_today INTEGER NOT NULL DEFAULT 0,
                last_free_date TEXT NOT NULL DEFAULT '',
                subscription_expiry TEXT,
                extra_uses INTEGER NOT NULL DEFAULT 0,
                total_tasks INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                user_openid TEXT NOT NULL,
                url TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'other',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT,
                error_message TEXT,
                FOREIGN KEY (user_openid) REFERENCES users(openid)
            );

            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                redeemed_by TEXT,
                redeemed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS summaries (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL DEFAULT '',
                full_text TEXT NOT NULL DEFAULT '',
                card_image_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_openid, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_codes_redeemed ON codes(redeemed_by);
        """)
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
