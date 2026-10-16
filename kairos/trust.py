"""Untrusted-content tracking (taint): the graph's answer to prompt injection.

Every Event carries a `trust` level:
  user     - the user's own words (utterances, dashboard/Telegram messages from the owner)
  device   - facts observed on the user's own devices (status, app switches, downloads, commands)
  external - content written by someone else (notifications, emails, web pages, plugin output)

When the LLM has read external content during a turn (via the context card or a tool result), the turn is
*tainted*. Tainted turns:
  - still allow harmless actions (watchers, notes, alerts),
  - but every capability that could execute, delete, download, send data out or forget memory is upgraded
    from auto to **confirm**, so a malicious email/web page can't make KAIROS act on its own,
  - and the resulting Action is linked `INFLUENCED_BY` the external events, so "why?" shows the source.
The rule-based planner never reads external content as instructions, so its turns are never tainted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

USER, DEVICE, EXTERNAL = "user", "device", "external"

_DEVICE_PREFIXES = ("app.", "download.", "file.", "place.")
_DEVICE_TYPES = {"device.status", "battery.low", "network.changed", "command.run"}

# Tools whose results put someone else's text in front of the model.
EXTERNAL_RESULT_TOOLS = {
    "recall_messages", "recall_file", "person_context", "recent_activity", "recent_sites", "search_memory",
    "briefing", "device_status", "meeting_prep", "upcoming_meetings", "list_tasks", "family_memory", "open_link",
    "web_search", "read_page",
}

# Capabilities that need confirmation when the turn is tainted.
TAINT_CONFIRM = {"shell.read", "shell.exec", "file.delete", "net.download", "memory.forget", "file.transfer",
                 "clipboard", "link.open", "memory.share", "net.read_unknown"}
TAINT_CONFIRM_PREFIXES = ("plugin.",)


def event_trust(event_type: str, source: str = "") -> str:
    if event_type == "utterance":
        return USER
    if event_type in _DEVICE_TYPES or event_type.startswith(_DEVICE_PREFIXES):
        return DEVICE
    return EXTERNAL  # notification, browser.visit, emails, anything we didn't write


def needs_confirmation_when_tainted(capability: str) -> bool:
    return capability in TAINT_CONFIRM or capability.startswith(TAINT_CONFIRM_PREFIXES)


def wrap_untrusted(text: str) -> str:
    """Mark external text for the model; the system prompt says never to follow instructions inside."""
    return f"<untrusted>{text}</untrusted>"


@dataclass
class Taint:
    """Per-turn record of external content the model has seen."""

    tracking: bool = False  # only LLM turns are tracked
    event_ids: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def tainted(self) -> bool:
        return self.tracking and bool(self.event_ids or self.sources)

    def add(self, source: str, event_ids: list[str] | None = None) -> None:
        if not self.tracking:
            return
        if source and source not in self.sources:
            self.sources.append(source)
        for eid in event_ids or []:
            if eid and eid not in self.event_ids:
                self.event_ids.append(eid)

    def describe(self) -> str:
        return ", ".join(self.sources[:4]) or "external content"
