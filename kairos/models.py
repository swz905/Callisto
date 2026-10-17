"""Wire contracts shared by the hub, device agents and the simulator (design doc §5)."""

from __future__ import annotations

import hashlib
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from kairos.timeutil import now_ms

EventType = Literal[
    "notification",
    "download.started",
    "download.completed",
    "download.failed",
    "file.created",
    "file.modified",
    "file.moved",
    "file.deleted",
    "browser.visit",
    "command.run",
    "utterance",
    "device.status",  # periodic snapshot; not stored as an event, it updates the Device + RUNNING edges
    "app.opened",
    "app.closed",
    "app.focused",
    "battery.low",
    "network.changed",
    "place.entered",
    "place.left",
    "web.search",
    "web.read",
]


def new_id() -> str:
    return str(uuid4())


class EventEnvelope(BaseModel):
    event_id: str = Field(default_factory=new_id)
    user_id: str
    device_id: str
    type: EventType
    ts: int = Field(default_factory=now_ms)
    source: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    def text(self) -> str:
        """One human-readable line describing the event (stored as Event.text)."""
        p = self.payload
        if self.type == "notification":
            title, body = p.get("title", ""), p.get("body", "")
            app = p.get("app") or self.source
            return f"{app}: {title} — {body}".strip(" —:")
        if self.type.startswith("download."):
            state = self.type.split(".", 1)[1]
            return f"Download {state}: {p.get('filename') or p.get('path') or p.get('url', '')}"
        if self.type.startswith("file."):
            return f"File {self.type.split('.', 1)[1]}: {p.get('path', '')}"
        if self.type == "browser.visit":
            return f"Visited {p.get('title') or ''} {p.get('url', '')}".strip()
        if self.type == "command.run":
            return f"Ran `{p.get('cmd', '')}` (exit {p.get('exit_code')})"
        if self.type == "utterance":
            return p.get("text", "")
        if self.type == "app.opened":
            return f"Opened {p.get('app', '')} on {self.device_id}"
        if self.type == "app.closed":
            return f"Closed {p.get('app', '')} on {self.device_id}"
        if self.type == "app.focused":
            title = f" — {p['title']}" if p.get("title") else ""
            return f"Switched to {p.get('app', '')}{title} on {self.device_id}"
        if self.type == "battery.low":
            return f"Battery low on {self.device_id}: {p.get('battery')}%"
        if self.type == "network.changed":
            return f"Network on {self.device_id} changed: {p.get('from') or 'none'} → {p.get('to') or 'none'}"
        if self.type == "web.search":
            return f"Searched the web: {p.get('query', '')} ({len(p.get('results') or [])} results)"
        if self.type == "web.read":
            excerpt = (p.get("excerpt") or "")[:300].replace("\n", " ")
            return f"Read: {p.get('title') or p.get('url', '')} ({p.get('domain', '')}) - {excerpt}"
        if self.type == "place.entered":
            return f"Arrived at {p.get('place')} ({self.device_id})"
        if self.type == "place.left":
            return f"Left {p.get('place')} ({self.device_id})"
        if self.type == "device.status":
            return f"Status of {self.device_id}: using {p.get('foreground_app') or 'unknown'}"
        return str(p)

    def content_hash(self) -> str:
        # Polling agents resend the same notification; they pass a stable `key`.
        stamp = self.payload.get("key") or self.ts
        basis = f"{self.device_id}|{self.type}|{self.source}|{self.text()}|{stamp}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()


OutboundKind = Literal["alert", "speak", "open", "download", "exec", "delete", "status_request",
                       "upload", "clipboard_get", "clipboard_set", "type_message",
                       "chat_send", "file_list", "file_pull", "file_push", "tap", "swipe",
                       "type", "key", "keyevent", "screenshot", "ui_dump", "app_launch", "notifications_get"]


class Outbound(BaseModel):
    """Hub → device message."""

    kind: OutboundKind
    device_id: str
    text: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    action_id: str | None = None
    requires_confirmation: bool = False
