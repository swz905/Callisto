"""Email ingestion over IMAP (stdlib only): new messages become `notification` events from app "Email".

They flow through the normal pipeline, so sender -> Message -> Link chains, commitments ("can you send...")
and triage work exactly as for phone notifications. Read-only: messages are fetched with BODY.PEEK (never
marked as read). Use an app password; settings come from .env (KAIROS_IMAP_*). The first poll only records
the newest UID (no backfill flood).
"""

from __future__ import annotations

import email
import email.policy
import html
import imaplib
import json
import logging
import re
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kairos.models import EventEnvelope
from kairos.timeutil import now_ms

if TYPE_CHECKING:
    from kairos.runtime import Kairos

log = logging.getLogger(__name__)
MAX_BYTES = 2_000_000
STATE_FILE = Path.home() / ".kairos" / "imap_state.json"


URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _content(part) -> str:
    if part is None:
        return ""
    try:
        return part.get_content()
    except Exception:
        return (part.get_payload(decode=True) or b"").decode("utf-8", "ignore")


def _text_of(msg: email.message.EmailMessage) -> str:
    """Readable text plus every link (HTML-only links usually hide in href attributes)."""
    plain = _content(msg.get_body(preferencelist=("plain",)))
    raw_html = _content(msg.get_body(preferencelist=("html",)))
    hrefs = re.findall(r"""href=["'](https?://[^"']+)""", raw_html, flags=re.I)
    html_text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.S | re.I)
    html_text = html.unescape(re.sub(r"<[^>]+>", " ", html_text))
    text = plain if len(plain.strip()) >= 80 or not html_text.strip() else html_text
    text = re.sub(r"\s+", " ", text).strip()
    links = list(dict.fromkeys(URL_RE.findall(plain) + URL_RE.findall(html_text) + hrefs))
    missing = [u for u in links if u not in text]
    return (text + (" " + " ".join(missing) if missing else "")).strip()


def message_to_envelope(raw: bytes, user_id: str, device_id: str = "email") -> EventEnvelope:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    sender = name or (addr.split("@")[0].replace(".", " ").title() if addr else "Unknown")
    subject = str(msg.get("Subject", "") or "(no subject)")
    text = _text_of(msg)
    try:
        ts = int(parsedate_to_datetime(str(msg.get("Date"))).timestamp() * 1000)
    except Exception:
        ts = now_ms()
    return EventEnvelope(user_id=user_id, device_id=device_id, type="notification", source="imap", ts=ts,
                         payload={"app": "Email", "title": sender, "sender": sender, "subject": subject,
                                  "body": f"{subject}. {text[:600]}", "key": str(msg.get("Message-ID") or ts)})


class ImapPoller:
    def __init__(self, k: Kairos):
        self.k = k
        s = k.settings
        self.host, self.user, self.password = s.imap_host, s.imap_user, s.imap_password
        self.folder, self.port = s.imap_folder, s.imap_port
        self.last_poll = 0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    def _state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}

    def _save(self, state: dict) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))

    def poll(self) -> int:
        if not self.configured:
            return 0
        self.last_poll = now_ms()
        key = f"{self.user}@{self.host}/{self.folder}"
        state = self._state()
        envs: list[EventEnvelope] = []
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self.password)
            conn.select(self.folder, readonly=True)
            typ, data = conn.uid("search", None, "ALL")
            uids = [int(u) for u in (data[0] or b"").split()] if typ == "OK" else []
            if not uids:
                return 0
            last = state.get(key)
            if last is None:  # first run: start from now, no backfill
                state[key] = max(uids)
                self._save(state)
                return 0
            for uid in [u for u in uids if u > last][:25]:
                typ, size = conn.uid("fetch", str(uid), "(RFC822.SIZE)")
                m = re.search(rb"RFC822\.SIZE (\d+)", size[0] if size and size[0] else b"")
                if m and int(m.group(1)) > MAX_BYTES:
                    continue
                typ, msg_data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
                if raw:
                    envs.append(message_to_envelope(raw, self.k.settings.user_id))
                state[key] = uid
            self._save(state)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        for env in envs:
            self.k.ingest(env)
        return len(envs)
