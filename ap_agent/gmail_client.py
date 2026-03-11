"""Gmail API client for reading and sending emails."""

import base64
import os
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    """Handles all Gmail API interactions."""

    def __init__(self, credentials_path: str, token_path: str):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.service = self._authenticate()

    def _authenticate(self):
        """Authenticate with Gmail API using OAuth2."""
        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self.token_path, "w") as token_file:
                token_file.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_unread_emails(self, label: str = "INBOX", query: str = "") -> list[dict]:
        """Fetch unread emails from the specified label.

        Returns a list of dicts with: id, subject, from, date, body.
        """
        full_query = f"is:unread label:{label}"
        if query:
            full_query += f" {query}"

        results = (
            self.service.users()
            .messages()
            .list(userId="me", q=full_query)
            .execute()
        )
        messages = results.get("messages", [])
        emails = []
        for msg_ref in messages:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="full")
                .execute()
            )
            emails.append(self._parse_message(msg))
        return emails

    def get_replies_to(self, thread_id: str) -> list[dict]:
        """Get all messages in a thread."""
        thread = (
            self.service.users()
            .threads()
            .get(userId="me", id=thread_id)
            .execute()
        )
        messages = []
        for msg in thread.get("messages", []):
            messages.append(self._parse_message(msg))
        return messages

    def send_email(self, to: str, subject: str, body: str) -> dict:
        """Send an email and return the sent message metadata."""
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = (
            self.service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return sent

    def mark_as_read(self, message_id: str):
        """Mark a message as read by removing the UNREAD label."""
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    def add_label(self, message_id: str, label_name: str):
        """Add a label to a message. Creates the label if it doesn't exist."""
        label_id = self._get_or_create_label(label_name)
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    def _get_or_create_label(self, label_name: str) -> str:
        """Get a label ID by name, creating it if necessary."""
        results = self.service.users().labels().list(userId="me").execute()
        for label in results.get("labels", []):
            if label["name"] == label_name:
                return label["id"]

        created = (
            self.service.users()
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

    def _parse_message(self, msg: dict) -> dict:
        """Extract useful fields from a Gmail API message."""
        headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
        body = self._extract_body(msg["payload"])
        return {
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "subject": headers.get("subject", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "body": body,
        }

    def _extract_body(self, payload: dict) -> str:
        """Recursively extract the plain text body from a message payload."""
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get(
            "data"
        ):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="replace"
            )

        for part in payload.get("parts", []):
            text = self._extract_body(part)
            if text:
                return text

        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="replace"
            )

        return ""
