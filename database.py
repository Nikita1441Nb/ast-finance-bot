import sqlite3
import json
from contextlib import contextmanager
from config import DB_PATH

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                added_by_username TEXT,
                added_by_name TEXT,
                doc_date TEXT,
                doc_type TEXT,
                company TEXT,
                counterparty TEXT,
                operation_type TEXT,
                amount REAL DEFAULT 0,
                details TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                added_by_name TEXT,
                title TEXT,
                formatted_summary TEXT,
                raw_json TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                uinfo_json TEXT,
                data_json TEXT,
                waiting_edit INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                mode TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                rule_text TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                step TEXT,
                temp_name TEXT,
                temp_username TEXT,
                temp_target TEXT,
                temp_remind_text TEXT
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(user_states)").fetchall()]
        if "temp_target" not in cols:
            conn.execute("ALTER TABLE user_states ADD COLUMN temp_target TEXT;")
        if "temp_remind_text" not in cols:
            conn.execute("ALTER TABLE user_states ADD COLUMN temp_remind_text TEXT;")

        c.execute("""
            CREATE TABLE IF NOT EXISTS telegram_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                creator_name TEXT,
                target_user TEXT,
                remind_text TEXT,
                remind_time TEXT,
                is_sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)

def get_user_state(user_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT step, temp_name, temp_username, temp_target, temp_remind_text FROM user_states WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        if not row:
            return None
        return dict(row)

def set_user_state(user_id: int, step: str, temp_name: str = None, temp_username: str = None, temp_target: str = None, temp_remind_text: str = None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO user_states (user_id, step, temp_name, temp_username, temp_target, temp_remind_text)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   step = excluded.step,
                   temp_name = excluded.temp_name,
                   temp_username = excluded.temp_username,
                   temp_target = excluded.temp_target,
                   temp_remind_text = excluded.temp_remind_text""",
            (user_id, step, temp_name, temp_username, temp_target, temp_remind_text),
        )

def clear_user_state(user_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))

def get_user_mode(user_id: int, is_admin: bool) -> str:
    default_mode = "admin" if is_admin else "worker"
    with get_db() as conn:
        row = conn.execute("SELECT mode FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return row["mode"] if row else default_mode

def set_user_mode(user_id: int, mode: str):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO user_settings (user_id, mode) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode""",
            (user_id, mode),
        )

def save_chat_message(user_id: int, role: str, content: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_history (user_id, role, content, created_at) VALUES (?, ?, ?, datetime('now', 'localtime'))",
            (user_id, role, content),
        )

def get_recent_history(user_id: int, limit: int = 10):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def clear_user_history(user_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))

def add_user_rule(user_id: int, rule_text: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("INSERT INTO user_rules (user_id, rule_text) VALUES (?, ?)", (user_id, rule_text.strip()))
        return cur.rowcount > 0

def get_user_rules(user_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT id, rule_text FROM user_rules WHERE user_id = ? ORDER BY id ASC", (user_id,)).fetchall()
        return [{"id": r["id"], "text": r["rule_text"]} for r in rows]

def clear_user_rules(user_id: int) -> int:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM user_rules WHERE user_id = ?", (user_id,))
        return cur.rowcount

def cancel_waiting_edits(user_id: int):
    with get_db() as conn:
        conn.execute("UPDATE drafts SET waiting_edit = 0 WHERE user_id = ?", (user_id,))

def create_draft(user_id: int, uinfo: dict, parsed_data: dict) -> int:
    with get_db() as conn:
        conn.execute("UPDATE drafts SET waiting_edit = 0 WHERE user_id = ?", (user_id,))
        cur = conn.execute(
            """INSERT INTO drafts (user_id, uinfo_json, data_json, waiting_edit, created_at)
               VALUES (?, ?, ?, 0, datetime('now', 'localtime'))""",
            (user_id, json.dumps(uinfo, ensure_ascii=False), json.dumps(parsed_data, ensure_ascii=False)),
        )
        return cur.lastrowid

def get_draft(draft_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "uinfo": json.loads(row["uinfo_json"]),
            "data": json.loads(row["data_json"]),
            "waiting_edit": bool(row["waiting_edit"]),
        }

def get_waiting_edit_draft(user_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE user_id = ? AND waiting_edit = 1 ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "uinfo": json.loads(row["uinfo_json"]),
            "data": json.loads(row["data_json"]),
            "waiting_edit": True,
        }

def update_draft(draft_id: int, parsed_data: dict = None, waiting_edit: bool = False):
    with get_db() as conn:
        if parsed_data is not None:
            conn.execute(
                "UPDATE drafts SET data_json = ?, waiting_edit = ? WHERE id = ?",
                (json.dumps(parsed_data, ensure_ascii=False), int(waiting_edit), draft_id),
            )
        else:
            conn.execute("UPDATE drafts SET waiting_edit = ? WHERE id = ?", (int(waiting_edit), draft_id))

def delete_draft(draft_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))