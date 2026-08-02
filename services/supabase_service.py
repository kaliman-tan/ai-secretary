from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY
from datetime import datetime
import json
import time
from typing import Any

_client: Client | None = None

# --- TTL in-memory cache ---
_CACHE: dict[str, tuple[Any, float]] = {}
_CACHE_TTL = 300.0  # 5 minutes


def _cache_get(key: str) -> tuple[bool, Any]:
    entry = _CACHE.get(key)
    if entry and time.monotonic() - entry[1] < _CACHE_TTL:
        return True, entry[0]
    return False, None


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (value, time.monotonic())


def _cache_invalidate(*keys: str) -> None:
    for k in keys:
        _CACHE.pop(k, None)


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# --- Memory (user profile & rules) ---

def get_user_profile() -> dict:
    hit, val = _cache_get("user_profile")
    if hit:
        return val
    client = get_client()
    res = client.table("user_profile").select("*").limit(1).execute()
    result = res.data[0] if res.data else {}
    _cache_set("user_profile", result)
    return result


def upsert_user_profile(data: dict) -> None:
    _cache_invalidate("user_profile")
    client = get_client()
    existing = get_user_profile()
    if existing:
        client.table("user_profile").update(data).eq("id", existing["id"]).execute()
    else:
        client.table("user_profile").insert(data).execute()
    _cache_invalidate("user_profile")


def merge_user_profile(new_fields: dict) -> dict:
    """既存プロフィールに新しいフィールドをマージして保存する。nullは無視する。"""
    _cache_invalidate("user_profile")
    client = get_client()
    existing = get_user_profile()

    # null値を除外
    updates = {k: v for k, v in new_fields.items() if v is not None}
    if not updates:
        return existing or {}

    # preferences は JSON マージ
    if "preferences" in updates and isinstance(updates["preferences"], dict) and existing.get("preferences"):
        merged_prefs = {**existing["preferences"], **updates["preferences"]}
        updates["preferences"] = merged_prefs

    updates["updated_at"] = datetime.utcnow().isoformat()

    if existing:
        client.table("user_profile").update(updates).eq("id", existing["id"]).execute()
        result = {**existing, **updates}
    else:
        client.table("user_profile").insert(updates).execute()
        result = updates
    _cache_set("user_profile", result)
    return result


def get_custom_rules() -> list[dict]:
    hit, val = _cache_get("custom_rules")
    if hit:
        return val
    client = get_client()
    res = client.table("custom_rules").select("*").execute()
    result = res.data or []
    _cache_set("custom_rules", result)
    return result


def add_custom_rule(rule_type: str, target: str, action: str) -> None:
    _cache_invalidate("custom_rules")
    client = get_client()
    client.table("custom_rules").insert({
        "rule_type": rule_type,
        "target": target,
        "action": action,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()


def get_important_contacts() -> list[dict]:
    hit, val = _cache_get("contacts")
    if hit:
        return val
    client = get_client()
    res = client.table("contacts").select("*").execute()
    result = res.data or []
    _cache_set("contacts", result)
    return result


def upsert_contact(email: str, name: str, notes: str = "") -> None:
    _cache_invalidate("contacts")
    client = get_client()
    existing = client.table("contacts").select("*").eq("email", email).execute()
    if existing.data:
        client.table("contacts").update({"name": name, "notes": notes}).eq("email", email).execute()
    else:
        client.table("contacts").insert({
            "email": email,
            "name": name,
            "notes": notes,
            "created_at": datetime.utcnow().isoformat(),
        }).execute()


# --- Todo ---

def get_todos(done: bool | None = None) -> list[dict]:
    client = get_client()
    query = client.table("todos").select("*").order("created_at", desc=False)
    if done is not None:
        query = query.eq("done", done)
    return query.execute().data or []


def add_todo(title: str, priority: str = "normal", due_date: str | None = None) -> dict:
    client = get_client()
    data: dict = {
        "title": title,
        "priority": priority,
        "done": False,
        "created_at": datetime.utcnow().isoformat(),
    }
    if due_date:
        data["due_date"] = due_date
    res = client.table("todos").insert(data).execute()
    return res.data[0]


def add_email_todo(title: str, due_date: str | None, source_id: str, priority: int) -> dict | None:
    """メールから生成したToDoをtodosテーブルに保存する。同一source_id+titleは重複追加しない。"""
    client = get_client()
    existing = client.table("todos").select("id").eq("source_id", source_id).eq("title", title).execute()
    if existing.data:
        return None
    res = client.table("todos").insert({
        "title": title,
        "due_date": due_date,
        "source": "email",
        "source_id": source_id,
        "priority": priority,
        "done": False,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()
    return res.data[0] if res.data else None


def add_research_todo(title: str, due_date: str | None, source_id: str, priority: str = "normal") -> dict | None:
    """就活リサーチから生成したToDoをtodosテーブルに保存する。同一source_id+titleは重複追加しない。"""
    client = get_client()
    existing = client.table("todos").select("id").eq("source_id", source_id).eq("title", title).execute()
    if existing.data:
        return None
    res = client.table("todos").insert({
        "title": title,
        "due_date": due_date,
        "source": "research",
        "source_id": source_id,
        "priority": priority,
        "done": False,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()
    return res.data[0] if res.data else None


def complete_todo(todo_id: int) -> None:
    client = get_client()
    client.table("todos").update({
        "done": True,
        "completed_at": datetime.utcnow().isoformat(),
    }).eq("id", todo_id).execute()


def complete_todo_by_title(title_fragment: str) -> list[dict]:
    client = get_client()
    todos = client.table("todos").select("*").eq("done", False).execute().data or []
    matched = [t for t in todos if title_fragment.lower() in t["title"].lower()]
    for t in matched:
        complete_todo(t["id"])
    return matched


def delete_todo(todo_id: int) -> None:
    client = get_client()
    client.table("todos").delete().eq("id", todo_id).execute()


def delete_todo_by_title(title_fragment: str) -> list[dict]:
    """タイトル部分一致でToDoを削除する（完了ではなく物理削除）。"""
    client = get_client()
    todos = client.table("todos").select("*").execute().data or []
    matched = [t for t in todos if title_fragment.lower() in t["title"].lower()]
    for t in matched:
        delete_todo(t["id"])
    return matched


# --- Decision history ---

def save_decision(context: str, decision: str, outcome: str = "") -> None:
    client = get_client()
    client.table("decision_history").insert({
        "context": context,
        "decision": decision,
        "outcome": outcome,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()


def get_recent_decisions(limit: int = 20) -> list[dict]:
    client = get_client()
    res = client.table("decision_history").select("*").order("created_at", desc=True).limit(limit).execute()
    return res.data or []


# --- Email log ---

def save_email_log(message_id: str, sender: str, subject: str, importance: str, summary: str) -> None:
    client = get_client()
    existing = client.table("email_logs").select("id").eq("message_id", message_id).execute()
    if not existing.data:
        client.table("email_logs").insert({
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "importance": importance,
            "summary": summary,
            "created_at": datetime.utcnow().isoformat(),
        }).execute()


def get_recent_email_logs(limit: int = 10) -> list[dict]:
    client = get_client()
    res = client.table("email_logs").select("*").order("created_at", desc=True).limit(limit).execute()
    return res.data or []


def is_email_logged(message_id: str) -> bool:
    client = get_client()
    res = client.table("email_logs").select("id").eq("message_id", message_id).execute()
    return bool(res.data)


# --- Job research logs ---

def is_research_logged(url: str) -> bool:
    client = get_client()
    res = client.table("research_logs").select("id").eq("url", url).execute()
    return bool(res.data)


def save_research_log(url: str, title: str, summary: str = "") -> None:
    client = get_client()
    existing = client.table("research_logs").select("id").eq("url", url).execute()
    if not existing.data:
        client.table("research_logs").insert({
            "url": url,
            "title": title,
            "summary": summary,
            "created_at": datetime.utcnow().isoformat(),
        }).execute()


# --- Notion sync ---

def get_unsynced_todos() -> list[dict]:
    client = get_client()
    res = (
        client.table("todos")
        .select("*")
        .eq("done", False)
        .eq("synced_to_notion", False)
        .execute()
    )
    return res.data or []


def mark_todos_synced(todo_ids: list[int]) -> None:
    if not todo_ids:
        return
    client = get_client()
    client.table("todos").update({"synced_to_notion": True}).in_("id", todo_ids).execute()


# --- Event log ---

# --- Conversation history ---

def get_conversation_history(channel_id: int) -> list[dict]:
    client = get_client()
    res = client.table("conversation_history").select("messages").eq("channel_id", channel_id).execute()
    if res.data:
        return res.data[0]["messages"] or []
    return []


def save_conversation_history(channel_id: int, messages: list[dict]) -> None:
    client = get_client()
    client.table("conversation_history").upsert({
        "channel_id": channel_id,
        "messages": messages,
        "updated_at": datetime.utcnow().isoformat(),
    }).execute()


# --- Event log ---

def save_event_log(title: str, start_datetime: str, end_datetime: str, google_event_id: str) -> None:
    client = get_client()
    client.table("event_logs").insert({
        "title": title,
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "google_event_id": google_event_id,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()
