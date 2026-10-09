"""ローカル SQLite による永続化層。

旧 supabase_service と関数名・引数・戻り値の形を揃えてある（呼び出し側は無修正で動く）。
bot は単一プロセスだが asyncio.to_thread 経由で複数スレッドから呼ばれるため、
接続は呼び出しごとに開いて閉じる。WAL モード + busy timeout で並行アクセスにも耐える。
"""
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profile (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    role        TEXT,
    priorities  TEXT,
    timezone    TEXT DEFAULT 'Asia/Tokyo',
    preferences TEXT DEFAULT '{}',
    updated_at  TEXT,
    hobbies     TEXT,
    traits      TEXT,
    notes       TEXT,
    career_goal TEXT
);

CREATE TABLE IF NOT EXISTS custom_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type  TEXT NOT NULL,
    target     TEXT NOT NULL,
    action     TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT UNIQUE NOT NULL,
    name       TEXT NOT NULL,
    notes      TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS todos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    priority         TEXT DEFAULT 'normal',
    done             INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT,
    completed_at     TEXT,
    due_date         TEXT,
    source           TEXT,
    source_id        TEXT,
    synced_to_notion INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_todos_done ON todos(done);
CREATE INDEX IF NOT EXISTS idx_todos_source ON todos(source, source_id);

CREATE TABLE IF NOT EXISTS decision_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    context    TEXT NOT NULL,
    decision   TEXT NOT NULL,
    outcome    TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS email_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    sender     TEXT NOT NULL,
    subject    TEXT NOT NULL,
    importance TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS research_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT UNIQUE NOT NULL,
    title      TEXT NOT NULL,
    summary    TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS conversation_history (
    channel_id INTEGER PRIMARY KEY,
    messages   TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS event_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT,
    start_datetime  TEXT,
    end_datetime    TEXT,
    google_event_id TEXT,
    created_at      TEXT
);
"""

# 読み出し時に Python 型へ戻す列
_JSON_COLS = {"preferences", "messages"}
_BOOL_COLS = {"done", "synced_to_notion"}

_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    """UTC の ISO 文字列（タイムゾーン表記なし）。旧実装の datetime.utcnow().isoformat() と同形式。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _ensure_schema() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        _initialized = True


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    _ensure_schema()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _to_dict(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    d = dict(row)
    for k in _JSON_COLS & d.keys():
        d[k] = json.loads(d[k]) if d[k] else None
    for k in _BOOL_COLS & d.keys():
        d[k] = bool(d[k])
    return d


def _insert(conn: sqlite3.Connection, table: str, data: dict) -> int:
    cols = list(data)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    return conn.execute(sql, [_encode(data[c]) for c in cols]).lastrowid


def _update(conn: sqlite3.Connection, table: str, data: dict, where: str, params: tuple) -> None:
    sets = ", ".join(f"{c} = ?" for c in data)
    conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", [_encode(v) for v in data.values()] + list(params))


def _fetch_one(conn: sqlite3.Connection, table: str, row_id: int) -> dict:
    return _to_dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


# --- Memory (user profile & rules) ---

def get_user_profile() -> dict:
    with _conn() as conn:
        return _to_dict(conn.execute("SELECT * FROM user_profile ORDER BY id LIMIT 1").fetchone())


def upsert_user_profile(data: dict) -> None:
    existing = get_user_profile()
    with _conn() as conn:
        if existing:
            _update(conn, "user_profile", data, "id = ?", (existing["id"],))
        else:
            _insert(conn, "user_profile", data)


def merge_user_profile(new_fields: dict) -> dict:
    """既存プロフィールに新しいフィールドをマージして保存する。nullは無視する。"""
    existing = get_user_profile()

    # null値を除外
    updates = {k: v for k, v in new_fields.items() if v is not None}
    if not updates:
        return existing or {}

    # preferences は JSON マージ
    if "preferences" in updates and isinstance(updates["preferences"], dict) and existing.get("preferences"):
        updates["preferences"] = {**existing["preferences"], **updates["preferences"]}

    updates["updated_at"] = _now()

    with _conn() as conn:
        if existing:
            _update(conn, "user_profile", updates, "id = ?", (existing["id"],))
            return {**existing, **updates}
        _insert(conn, "user_profile", updates)
        return updates


def get_custom_rules() -> list[dict]:
    with _conn() as conn:
        return [_to_dict(r) for r in conn.execute("SELECT * FROM custom_rules ORDER BY id")]


def add_custom_rule(rule_type: str, target: str, action: str) -> None:
    with _conn() as conn:
        _insert(conn, "custom_rules", {
            "rule_type": rule_type,
            "target": target,
            "action": action,
            "created_at": _now(),
        })


def get_important_contacts() -> list[dict]:
    with _conn() as conn:
        return [_to_dict(r) for r in conn.execute("SELECT * FROM contacts ORDER BY id")]


def upsert_contact(email: str, name: str, notes: str = "") -> None:
    with _conn() as conn:
        existing = conn.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone()
        if existing:
            _update(conn, "contacts", {"name": name, "notes": notes}, "email = ?", (email,))
        else:
            _insert(conn, "contacts", {
                "email": email,
                "name": name,
                "notes": notes,
                "created_at": _now(),
            })


# --- Todo ---

def get_todos(done: bool | None = None) -> list[dict]:
    sql, params = "SELECT * FROM todos", ()
    if done is not None:
        sql, params = sql + " WHERE done = ?", (int(done),)
    with _conn() as conn:
        return [_to_dict(r) for r in conn.execute(sql + " ORDER BY created_at ASC, id ASC", params)]


def add_todo(title: str, priority: str = "normal", due_date: str | None = None) -> dict:
    data: dict = {
        "title": title,
        "priority": priority,
        "done": False,
        "created_at": _now(),
    }
    if due_date:
        data["due_date"] = due_date
    with _conn() as conn:
        return _fetch_one(conn, "todos", _insert(conn, "todos", data))


def _add_sourced_todo(
    source: str, title: str, due_date: str | None, source_id: str, priority: str | int,
) -> dict | None:
    """source/source_id 付きの ToDo を保存する。同一 source_id+title は重複追加しない。"""
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM todos WHERE source_id = ? AND title = ?", (source_id, title)
        ).fetchone()
        if existing:
            return None
        new_id = _insert(conn, "todos", {
            "title": title,
            "due_date": due_date,
            "source": source,
            "source_id": source_id,
            "priority": priority,
            "done": False,
            "created_at": _now(),
        })
        return _fetch_one(conn, "todos", new_id)


def add_email_todo(title: str, due_date: str | None, source_id: str, priority: int) -> dict | None:
    """メールから生成したToDoをtodosテーブルに保存する。同一source_id+titleは重複追加しない。"""
    return _add_sourced_todo("email", title, due_date, source_id, priority)


def add_research_todo(title: str, due_date: str | None, source_id: str, priority: str = "normal") -> dict | None:
    """就活リサーチから生成したToDoをtodosテーブルに保存する。同一source_id+titleは重複追加しない。"""
    return _add_sourced_todo("research", title, due_date, source_id, priority)


def has_research_todo(source_id: str) -> bool:
    """指定した source_id（リサーチ結果のURL）が既にToDo化されているか確認する。"""
    with _conn() as conn:
        return conn.execute(
            "SELECT 1 FROM todos WHERE source = 'research' AND source_id = ? LIMIT 1", (source_id,)
        ).fetchone() is not None


def complete_todo(todo_id: int) -> None:
    with _conn() as conn:
        _update(conn, "todos", {"done": True, "completed_at": _now()}, "id = ?", (todo_id,))


def complete_todo_by_title(title_fragment: str) -> list[dict]:
    todos = get_todos(done=False)
    matched = [t for t in todos if title_fragment.lower() in t["title"].lower()]
    for t in matched:
        complete_todo(t["id"])
    return matched


def delete_todo(todo_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))


def delete_todo_by_title(title_fragment: str) -> list[dict]:
    """タイトル部分一致でToDoを削除する（完了ではなく物理削除）。"""
    todos = get_todos()
    matched = [t for t in todos if title_fragment.lower() in t["title"].lower()]
    for t in matched:
        delete_todo(t["id"])
    return matched


# --- Decision history ---

def save_decision(context: str, decision: str, outcome: str = "") -> None:
    with _conn() as conn:
        _insert(conn, "decision_history", {
            "context": context,
            "decision": decision,
            "outcome": outcome,
            "created_at": _now(),
        })


def get_recent_decisions(limit: int = 20) -> list[dict]:
    with _conn() as conn:
        return [_to_dict(r) for r in conn.execute(
            "SELECT * FROM decision_history ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )]


# --- Email log ---

def save_email_log(message_id: str, sender: str, subject: str, importance: str, summary: str) -> None:
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM email_logs WHERE message_id = ?", (message_id,)).fetchone():
            return
        _insert(conn, "email_logs", {
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "importance": importance,
            "summary": summary,
            "created_at": _now(),
        })


def get_recent_email_logs(limit: int = 10) -> list[dict]:
    with _conn() as conn:
        return [_to_dict(r) for r in conn.execute(
            "SELECT * FROM email_logs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )]


def is_email_logged(message_id: str) -> bool:
    with _conn() as conn:
        return conn.execute(
            "SELECT 1 FROM email_logs WHERE message_id = ? LIMIT 1", (message_id,)
        ).fetchone() is not None


# --- Job research logs ---

def is_research_logged(url: str) -> bool:
    with _conn() as conn:
        return conn.execute(
            "SELECT 1 FROM research_logs WHERE url = ? LIMIT 1", (url,)
        ).fetchone() is not None


def save_research_log(url: str, title: str, summary: str = "") -> None:
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM research_logs WHERE url = ?", (url,)).fetchone():
            return
        _insert(conn, "research_logs", {
            "url": url,
            "title": title,
            "summary": summary,
            "created_at": _now(),
        })


# --- Notion sync ---

def get_unsynced_todos() -> list[dict]:
    with _conn() as conn:
        return [_to_dict(r) for r in conn.execute(
            "SELECT * FROM todos WHERE done = 0 AND synced_to_notion = 0 ORDER BY id"
        )]


def mark_todos_synced(todo_ids: list[int]) -> None:
    if not todo_ids:
        return
    with _conn() as conn:
        marks = ", ".join("?" * len(todo_ids))
        conn.execute(f"UPDATE todos SET synced_to_notion = 1 WHERE id IN ({marks})", list(todo_ids))


# --- Conversation history ---

def get_conversation_history(channel_id: int) -> list[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT messages FROM conversation_history WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return (json.loads(row["messages"]) if row and row["messages"] else None) or []


def save_conversation_history(channel_id: int, messages: list[dict]) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversation_history (channel_id, messages, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at",
            (channel_id, _encode(messages), _now()),
        )


# --- Event log ---

def save_event_log(title: str, start_datetime: str, end_datetime: str, google_event_id: str) -> None:
    with _conn() as conn:
        _insert(conn, "event_logs", {
            "title": title,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "google_event_id": google_event_id,
            "created_at": _now(),
        })


# --- Backup ---

def backup(dest_dir: str | Path | None = None, keep: int = 14) -> Path:
    """SQLite のオンラインバックアップ API で整合性のあるスナップショットを作り、古いものを間引く。"""
    _ensure_schema()
    dest_dir = Path(dest_dir) if dest_dir else Path(DB_PATH).parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"ai_secretary-{datetime.now():%Y%m%d-%H%M%S}.db"
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
    for old in sorted(dest_dir.glob("ai_secretary-*.db"))[:-keep]:
        old.unlink()
    return dest
