from notion_client import Client
from config import NOTION_API_KEY, NOTION_CALENDAR_DATABASE_ID
from datetime import datetime

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(auth=NOTION_API_KEY)
    return _client


def get_upcoming_events(days_ahead: int = 7) -> list[dict]:
    client = get_client()
    today = datetime.utcnow().date().isoformat()

    result = client.databases.query(
        database_id=NOTION_CALENDAR_DATABASE_ID,
        filter={
            "property": "日付",
            "date": {"on_or_after": today},
        },
        sorts=[{"property": "日付", "direction": "ascending"}],
        page_size=20,
    )

    events = []
    for page in result.get("results", []):
        props = page.get("properties", {})
        title_prop = props.get("名前") or props.get("title") or {}
        title = ""
        for t in title_prop.get("title", []):
            title += t.get("plain_text", "")

        date_prop = props.get("日付", {}).get("date", {})
        start = date_prop.get("start", "") if date_prop else ""

        events.append({"id": page["id"], "title": title, "start": start})
    return events


def create_event(title: str, date: str, notes: str = "") -> dict:
    client = get_client()
    page = client.pages.create(
        parent={"database_id": NOTION_CALENDAR_DATABASE_ID},
        properties={
            "名前": {"title": [{"text": {"content": title}}]},
            "日付": {"date": {"start": date}},
        },
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": notes}}]},
            }
        ] if notes else [],
    )
    return {"id": page["id"], "title": title, "date": date}


def update_event(page_id: str, title: str | None = None, date: str | None = None) -> None:
    client = get_client()
    properties = {}
    if title:
        properties["名前"] = {"title": [{"text": {"content": title}}]}
    if date:
        properties["日付"] = {"date": {"start": date}}
    if properties:
        client.pages.update(page_id=page_id, properties=properties)


def delete_event(page_id: str) -> None:
    client = get_client()
    client.pages.update(page_id=page_id, archived=True)


def sync_todo(title: str, due_date: str | None = None) -> dict:
    client = get_client()
    date = due_date or datetime.utcnow().date().isoformat()
    page = client.pages.create(
        parent={"database_id": NOTION_CALENDAR_DATABASE_ID},
        properties={
            "名前": {"title": [{"text": {"content": title}}]},
            "日付": {"date": {"start": date}},
        },
    )
    return {"id": page["id"], "title": title, "date": date}
