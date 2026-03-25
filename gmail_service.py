"""Gmail integration for reading PO requests and sending PDF replies."""

from __future__ import annotations

import base64
import email.mime.application
import email.mime.multipart
import email.mime.text
import os
from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import config

_gmail_service = None
_creds = None

# Labels for tracking processed emails
LABEL_PROCESSING = "PO/Processing"
LABEL_COMPLETED = "PO/Completed"
LABEL_ERROR = "PO/Error"


@dataclass
class IncomingEmail:
    message_id: str
    sender: str
    subject: str
    body: str
    thread_id: str


def _get_creds() -> Credentials:
    global _creds
    if _creds and _creds.valid:
        return _creds

    if _creds and _creds.expired and _creds.refresh_token:
        _creds.refresh(Request())
    elif os.path.exists(config.GMAIL_TOKEN_PATH):
        _creds = Credentials.from_authorized_user_file(
            config.GMAIL_TOKEN_PATH,
            config.GMAIL_SCOPES + config.SHEETS_SCOPES,
        )
        if _creds.expired and _creds.refresh_token:
            _creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            config.GMAIL_CREDENTIALS_PATH,
            config.GMAIL_SCOPES + config.SHEETS_SCOPES,
        )
        _creds = flow.run_local_server(port=0)
        with open(config.GMAIL_TOKEN_PATH, "w") as f:
            f.write(_creds.to_json())

    return _creds


def _get_service():
    global _gmail_service
    if _gmail_service is None:
        creds = _get_creds()
        _gmail_service = build("gmail", "v1", credentials=creds)
    return _gmail_service


def _ensure_label(label_name: str) -> str:
    """Get or create a Gmail label, return its ID."""
    service = _get_service()
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"] == label_name:
            return label["id"]

    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    return created["id"]


def fetch_unread_po_emails() -> list[IncomingEmail]:
    """Fetch unread emails that look like PO requests."""
    service = _get_service()

    # Search for unread emails; exclude ones already labeled
    query = "is:unread -label:PO/Processing -label:PO/Completed -label:PO/Error"
    results = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=10)
        .execute()
    )
    messages = results.get("messages", [])
    if not messages:
        return []

    emails = []
    for msg_ref in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_ref["id"], format="full")
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        sender = headers.get("From", "")
        subject = headers.get("Subject", "")

        # Extract plain text body
        body = _extract_body(msg["payload"])

        emails.append(
            IncomingEmail(
                message_id=msg["id"],
                sender=sender,
                subject=subject,
                body=body,
                thread_id=msg.get("threadId", ""),
            )
        )

    return emails


def _extract_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8")

    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result

    return ""


def label_email(message_id: str, label_name: str) -> None:
    """Add a label to an email."""
    service = _get_service()
    label_id = _ensure_label(label_name)
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def send_po_reply(
    to: str,
    thread_id: str,
    subject: str,
    body_text: str,
    pdf_path: str | None = None,
) -> None:
    """Send a reply with an optional PDF attachment."""
    service = _get_service()

    msg = email.mime.multipart.MIMEMultipart()
    msg["To"] = to
    msg["Subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject

    msg.attach(email.mime.text.MIMEText(body_text, "plain"))

    if pdf_path:
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
        attachment = email.mime.application.MIMEApplication(pdf_data, _subtype="pdf")
        attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=os.path.basename(pdf_path),
        )
        msg.attach(attachment)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": thread_id},
    ).execute()
