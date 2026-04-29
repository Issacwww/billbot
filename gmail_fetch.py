"""Fetch bill emails from Gmail using the Gmail API + OAuth2."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger("billbot.gmail")

BILLBOT_DIR = Path.home() / ".billbot"
DOWNLOADS_DIR = BILLBOT_DIR / "downloads"
CREDENTIALS_FILE = BILLBOT_DIR / "credentials.json"
TOKEN_FILE = BILLBOT_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


@dataclass
class FetchedBill:
    provider: str  # "pge" or "city-service"
    email_message_id: str
    email_subject: str
    email_date: str
    amount_due: Optional[float]  # extracted from email body (PG&E)
    pdf_path: Optional[Path]  # local path to downloaded PDF (City)
    bill_period_start: Optional[str] = None
    bill_period_end: Optional[str] = None


def _build_service():
    """Build Gmail API service with OAuth2 credentials."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail OAuth credentials not found at {CREDENTIALS_FILE}. "
                    "See setup_guide.md for instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _detect_provider(subject: str, sender: str) -> Optional[str]:
    """Detect bill provider from email subject and sender."""
    combined = (subject + " " + sender).lower()
    if "pg&e" in combined or "pge" in combined or "pacific gas" in combined:
        return "pge"
    if "city" in combined or "statement" in combined:
        return "city-service"
    return None


def _extract_amount_from_body(body_text: str) -> Optional[float]:
    """Extract dollar amount from PG&E email body."""
    patterns = [
        r"total\s+amount\s+due[:\s]*\$?\s*([0-9,]+\.[0-9]{2})",
        r"amount\s+due[:\s]*\$?\s*([0-9,]+\.[0-9]{2})",
        r"total\s+due[:\s]*\$?\s*([0-9,]+\.[0-9]{2})",
        r"balance[:\s]*\$?\s*([0-9,]+\.[0-9]{2})",
        r"\$\s*([0-9,]+\.[0-9]{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, body_text, re.IGNORECASE)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _extract_due_date_from_body(body_text: str) -> Optional[str]:
    """Extract the due date (MM/DD/YYYY) from PG&E email body."""
    dates = re.findall(r"(\d{2}/\d{2}/\d{4})", body_text)
    return dates[0] if dates else None


def _get_message_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload."""
    parts = payload.get("parts", [])
    if not parts:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    for part in parts:
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if mime.startswith("multipart/"):
            result = _get_message_body(part)
            if result:
                return result
    return ""


def _download_pdf_attachment(
    service, message_id: str, msg_payload: dict, download_dir: Path
) -> Optional[Path]:
    """Download the first PDF attachment from a message."""
    parts = msg_payload.get("parts", [])
    for part in parts:
        filename = part.get("filename", "")
        mime = part.get("mimeType", "")
        if not filename.lower().endswith(".pdf") and "pdf" not in mime.lower():
            continue

        attachment_id = part.get("body", {}).get("attachmentId")
        if not attachment_id:
            continue

        att = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        data = base64.urlsafe_b64decode(att["data"])

        download_dir.mkdir(parents=True, exist_ok=True)
        msg_hash = hashlib.sha256(message_id.encode()).hexdigest()[:8]
        safe_filename = f"{download_dir.name}_{msg_hash}.pdf"
        pdf_path = download_dir / safe_filename
        pdf_path.write_bytes(data)
        LOGGER.info("Downloaded PDF: %s", pdf_path)
        return pdf_path

    return None


def fetch_new_bills(
    processed_message_ids: set[str],
    since_date: Optional[datetime] = None,
    since_days: int = 60,
) -> list[FetchedBill]:
    """Fetch new bill emails from Gmail label 'Bill'.

    Args:
        processed_message_ids: set of message IDs already in SQLite (skip these).
        since_date: only search emails after this date. Falls back to since_days.
        since_days: fallback — search emails from the last N days.

    Returns:
        List of FetchedBill objects for new, unprocessed bills.
    """
    service = _build_service()

    if since_date:
        after_str = since_date.strftime("%Y/%m/%d")
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        after_str = cutoff.strftime("%Y/%m/%d")

    query = f"label:Bill has:attachment after:{after_str}"
    # Also search PG&E emails without attachments
    query_no_attachment = f"label:Bill -has:attachment after:{after_str}"

    bills: list[FetchedBill] = []

    for q in [query, query_no_attachment]:
        LOGGER.info("Gmail search: %s", q)
        results = service.users().messages().list(userId="me", q=q).execute()
        messages = results.get("messages", [])

        while "nextPageToken" in results:
            results = (
                service.users()
                .messages()
                .list(userId="me", q=q, pageToken=results["nextPageToken"])
                .execute()
            )
            messages.extend(results.get("messages", []))

        for msg_meta in messages:
            msg_id = msg_meta["id"]
            if msg_id in processed_message_ids:
                LOGGER.debug("Skipping already-processed message %s", msg_id)
                continue

            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            date_str = headers.get("date", "")

            provider = _detect_provider(subject, sender)
            if not provider:
                LOGGER.debug("Skipping non-bill email: %s", subject)
                continue

            if provider == "pge":
                body_text = _get_message_body(msg["payload"])
                amount = _extract_amount_from_body(body_text)
                if amount is None:
                    LOGGER.warning("Could not extract amount from PG&E email: %s", subject)
                    continue
                # PG&E emails don't include billing period — use due date as
                # bill_period_end so we can exclude pre-move-in bills.
                due_date = _extract_due_date_from_body(body_text)
                bills.append(FetchedBill(
                    provider=provider,
                    email_message_id=msg_id,
                    email_subject=subject,
                    email_date=date_str,
                    amount_due=amount,
                    pdf_path=None,
                    bill_period_end=due_date,
                ))
            else:
                # City — download PDF
                date_folder = datetime.now(timezone.utc).strftime("%Y-%m")
                try:
                    # Try to parse email date for folder name
                    from email.utils import parsedate_to_datetime
                    email_dt = parsedate_to_datetime(date_str)
                    date_folder = email_dt.strftime("%Y-%m")
                except Exception:
                    pass

                download_dir = DOWNLOADS_DIR / date_folder
                pdf_path = _download_pdf_attachment(service, msg_id, msg["payload"], download_dir)
                if pdf_path is None:
                    LOGGER.warning("No PDF attachment in city bill email: %s", subject)
                    continue
                bills.append(FetchedBill(
                    provider=provider,
                    email_message_id=msg_id,
                    email_subject=subject,
                    email_date=date_str,
                    amount_due=None,
                    pdf_path=pdf_path,
                ))

    LOGGER.info("Found %d new bill(s)", len(bills))
    return bills


def send_notification_email(subject: str, body: str) -> None:
    """Send an email to yourself as a push notification."""
    import base64
    from email.mime.text import MIMEText

    service = _build_service()
    # Get your own email address
    profile = service.users().getProfile(userId="me").execute()
    my_email = profile["emailAddress"]

    msg = MIMEText(body)
    msg["To"] = my_email
    msg["From"] = my_email
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    LOGGER.info("Notification email sent: %s", subject)
