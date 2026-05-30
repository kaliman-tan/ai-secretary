from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from googleapiclient.discovery import build
from services.gmail_service import _get_creds

_TZ = ZoneInfo("Asia/Tokyo")
_TASKLIST_ID = "@default"
_tasks_svc = None


def _service():
    global _tasks_svc
    if _tasks_svc is None:
        _tasks_svc = build("tasks", "v1", credentials=_get_creds())
    return _tasks_svc


def get_tasks() -> list[dict]:
    result = _service().tasks().list(
        tasklist=_TASKLIST_ID,
        showCompleted=False,
        showHidden=False,
    ).execute()
    return [_parse_task(t) for t in result.get("items", [])]


def create_task(title: str, due_iso: str | None = None, notes: str | None = None) -> dict:
    body: dict = {"title": title, "status": "needsAction"}
    if due_iso:
        try:
            dt = datetime.fromisoformat(due_iso).astimezone(timezone.utc)
            body["due"] = dt.strftime("%Y-%m-%dT00:00:00.000Z")
        except Exception:
            pass
    if notes:
        body["notes"] = notes
    result = _service().tasks().insert(tasklist=_TASKLIST_ID, body=body).execute()
    return _parse_task(result)


def complete_task(task_id: str) -> bool:
    try:
        _service().tasks().patch(
            tasklist=_TASKLIST_ID,
            task=task_id,
            body={"status": "completed"},
        ).execute()
        return True
    except Exception:
        return False


def delete_task(task_id: str) -> bool:
    try:
        _service().tasks().delete(tasklist=_TASKLIST_ID, task=task_id).execute()
        return True
    except Exception:
        return False


def search_tasks(query: str) -> list[dict]:
    q = query.lower()
    return [t for t in get_tasks() if q in t["title"].lower()]


def _parse_task(t: dict) -> dict:
    return {
        "id": t["id"],
        "title": t.get("title", ""),
        "notes": t.get("notes", ""),
        "due": t.get("due", ""),
        "done": t.get("status") == "completed",
    }
