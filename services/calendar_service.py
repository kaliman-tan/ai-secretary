from datetime import datetime, timedelta, date as date_t
from zoneinfo import ZoneInfo
from googleapiclient.discovery import build
from services.gmail_service import _get_creds
from config import TIMEZONE

_TZ = ZoneInfo(TIMEZONE)
_calendar_svc = None


def _service():
    global _calendar_svc
    if _calendar_svc is None:
        _calendar_svc = build("calendar", "v3", credentials=_get_creds())
    return _calendar_svc


def get_today_events(days_ahead: int = 1, start_date: date_t | None = None) -> list[dict]:
    service = _service()
    if start_date:
        start = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=_TZ)
    else:
        now = datetime.now(_TZ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days_ahead)

    result = service.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = []
    for item in result.get("items", []):
        start_time = item["start"].get("dateTime", item["start"].get("date", ""))
        events.append({
            "id": item["id"],
            "title": item.get("summary", "（タイトルなし）"),
            "start": start_time,
            "end": item["end"].get("dateTime", item["end"].get("date", "")),
            "description": item.get("description", ""),
            "location": item.get("location", ""),
        })
    return events


def create_event(title: str, start_dt: datetime, end_dt: datetime, description: str = "", location: str = "") -> dict:
    service = _service()
    event_body = {
        "summary": title,
        "description": description,
        "location": location,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }
    created = service.events().insert(calendarId="primary", body=event_body).execute()
    google_event_id = created["id"]

    from services import supabase_service
    supabase_service.save_event_log(
        title=title,
        start_datetime=start_dt.isoformat(),
        end_datetime=end_dt.isoformat(),
        google_event_id=google_event_id,
    )

    return {"id": google_event_id, "title": title, "start": start_dt.isoformat(), "link": created.get("htmlLink", "")}


def update_event(event_id: str, **kwargs) -> dict:
    service = _service()
    event = service.events().get(calendarId="primary", eventId=event_id).execute()
    for key, value in kwargs.items():
        event[key] = value
    updated = service.events().update(calendarId="primary", eventId=event_id, body=event).execute()
    return updated


def delete_event(event_id: str) -> None:
    service = _service()
    service.events().delete(calendarId="primary", eventId=event_id).execute()
