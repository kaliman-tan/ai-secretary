import base64
import os
import time
from datetime import datetime, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from config import GOOGLE_CREDENTIALS_PATH, GOOGLE_TOKEN_PATH

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]


def _get_creds() -> Credentials:
    creds = None
    if os.path.exists(GOOGLE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(GOOGLE_TOKEN_PATH), exist_ok=True)
        with open(GOOGLE_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


_gmail_svc = None


def _gmail_service():
    global _gmail_svc
    if _gmail_svc is None:
        _gmail_svc = build("gmail", "v1", credentials=_get_creds())
    return _gmail_svc


def get_unread_emails(max_results: int = 10) -> list[dict]:
    service = _gmail_service()
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
        maxResults=max_results,
    ).execute()

    messages = result.get("messages", [])
    emails = []
    for msg in messages:
        detail = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
        emails.append(_parse_message(detail))
    return emails


def _parse_message(msg: dict) -> dict:
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    body = _extract_body(msg["payload"])
    return {
        "id": msg["id"],
        "sender": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "body": body,
        "snippet": msg.get("snippet", ""),
    }


def get_label_id(label_name: str) -> str | None:
    service = _gmail_service()
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    return None


def get_emails_metadata(label_name: str, hours: int = 24, max_results: int = 500) -> list[dict]:
    """件名・送信者のみ取得（本文なし）。ラベル名と時間範囲で絞り込む。"""
    service = _gmail_service()
    after_ts = int(time.time()) - hours * 3600
    label_id = get_label_id(label_name)

    list_kwargs: dict = {
        "userId": "me",
        "maxResults": max_results,
        "q": f"after:{after_ts}",
    }
    if label_id:
        list_kwargs["labelIds"] = [label_id]

    result = service.users().messages().list(**list_kwargs).execute()
    messages = result.get("messages", [])

    emails = []
    for msg in messages:
        detail = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in detail["payload"].get("headers", [])}
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", "（件名なし）"),
            "sender": headers.get("From", ""),
            "date": headers.get("Date", ""),
        })
    return emails


def get_email_body(message_id: str) -> str:
    """特定メールの本文を取得する。"""
    service = _gmail_service()
    detail = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    return _extract_body(detail["payload"])


def _extract_body(payload: dict) -> str:
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                data = part["body"].get("data", "")
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in payload["parts"]:
            result = _extract_body(part)
            if result:
                return result
    else:
        data = payload["body"].get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""
