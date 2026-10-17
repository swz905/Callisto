"""Rule-based classification and entity extraction for notifications.

Rules run first and cover the demo apps; the LLM extractor (later) only handles what rules miss.
Pure functions: no graph, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
ETA_RE = re.compile(r"(\d+)\s*(?:min|mins|minute|minutes)\b", re.I)
DRIVER_RES = [
    re.compile(r"\b(?:driver|captain|partner)\s+([A-Z][a-z]+)"),
    re.compile(r"^([A-Z][a-z]+)\s+(?:is|has|will)\b"),
]
DEST_RE = re.compile(r"\bto\s+(?:the\s+)?([A-Z][\w]*(?:\s+[A-Z][\w]*)*)")
GROUP_SENDER_RE = re.compile(r"^([A-Z][\w.]*(?:\s+[A-Z][\w.]*)?):\s+(.*)$", re.S)
COUNT_SUFFIX_RE = re.compile(r"\s*\(\d+\s+messages?\)\s*$", re.I)

RIDE_APPS = {
    "uber": "Uber", "com.ubercab": "Uber",
    "ola": "Ola", "com.olacabs.customer": "Ola",
    "rapido": "Rapido", "com.rapido.passenger": "Rapido",
}
MESSAGE_APPS = {
    "gmail": ("Gmail", "email"), "com.google.android.gm": ("Gmail", "email"),
    "outlook": ("Outlook", "email"), "com.microsoft.office.outlook": ("Outlook", "email"),
    "whatsapp": ("WhatsApp", "whatsapp"), "com.whatsapp": ("WhatsApp", "whatsapp"),
    "messages": ("Messages", "sms"), "com.google.android.apps.messaging": ("Messages", "sms"),
    "slack": ("Slack", "slack"), "com.slack": ("Slack", "slack"),
    "teams": ("Teams", "teams"), "com.microsoft.teams": ("Teams", "teams"),
    "email": ("Email", "email"), "imap": ("Email", "email"),
    "telegram": ("Telegram", "telegram"), "org.telegram.messenger": ("Telegram", "telegram"),
}


@dataclass
class Extraction:
    kind: str  # "ride" | "message" | "other"
    app: str
    category: str
    people: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    trip: dict | None = None
    message: dict | None = None


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def file_parts(path: str) -> tuple[str, str, str]:
    """(name, ext without dot, folder) for a Windows or POSIX path."""
    p = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    return p.name, p.suffix.lower().lstrip("."), str(p.parent)


def _key(source: str, app: str | None) -> str:
    return (app or source or "").strip().lower()


def ride_status(text: str) -> str:
    t = text.lower()
    if re.search(r"trip (is )?(complete|completed|ended)|thanks for riding|rate your (trip|driver)|you've arrived", t):
        return "completed"
    if re.search(r"\b(has arrived|is here|is outside|arrived at your)", t):
        return "arrived"
    if ETA_RE.search(t) or re.search(r"arriving|on the way|en route|away", t):
        return "arriving"
    if re.search(r"looking for|finding|request|booked|confirmed", t):
        return "requested"
    return "update"


def extract_ride(app: str, title: str, body: str, payload: dict) -> Extraction:
    text = f"{title}. {body}"
    eta = ETA_RE.search(text)
    driver = payload.get("driver")
    if not driver:
        for rx in DRIVER_RES:
            if m := rx.search(body) or rx.search(title):
                driver = m.group(1)
                break
    dest = payload.get("destination")
    if not dest and (m := DEST_RE.search(body)):
        dest = m.group(1)
    status = ride_status(text)
    trip = {
        "provider": app,
        "trip_id": payload.get("trip_id"),
        "status": status,
        "eta_min": int(eta.group(1)) if eta and status == "arriving" else None,
        "driver": driver,
        "destination": dest,
    }
    return Extraction(kind="ride", app=app, category="travel", trip=trip)


def extract_message(app: str, channel: str, title: str, body: str, payload: dict) -> Extraction:
    sender = COUNT_SUFFIX_RE.sub("", payload.get("sender") or title).strip()
    text = body
    group = None
    if m := GROUP_SENDER_RE.match(body):  # group chats: title = group, body = "Name: text"
        group, sender, text = sender, m.group(1), m.group(2)
    subject = payload.get("subject") or (text.splitlines()[0][:120] if text else "")
    urls = URL_RE.findall(f"{body} {payload.get('url', '')}")
    message = {
        "channel": channel,
        "sender": sender,
        "group": group,
        "subject": subject,
        "snippet": text[:280],
        "urls": list(dict.fromkeys(urls)),
    }
    return Extraction(kind="message", app=app, category="communication", people=[sender] if sender else [],
                      urls=message["urls"], message=message)


def extract_notification(source: str, payload: dict) -> Extraction:
    app_key = _key(source, payload.get("app"))
    title, body = payload.get("title", "") or "", payload.get("body", "") or ""
    for key in (app_key, source.lower()):
        if key in RIDE_APPS:
            return extract_ride(RIDE_APPS[key], title, body, payload)
        if key in MESSAGE_APPS:
            app, channel = MESSAGE_APPS[key]
            return extract_message(app, channel, title, body, payload)
    app = payload.get("app") or source or "unknown"
    return Extraction(kind="other", app=app, category="other", urls=URL_RE.findall(body))
