r"""Commitments and open loops: "Rahul asked for the deck", "I'll send it by Friday".

Tasks are graph nodes linked to the person they involve and the event they came from:
  (User)-[:HAS_TASK]->(Task {title, direction, due_ts, status})-[:WITH]->(Person)
                                         \-[:DERIVED_FROM]->(Event)
direction: "they_asked" (someone asked the user for something) | "i_promised" (the user committed) | "todo".

Sources: rule extraction from incoming messages and the user's own words (offline), the LLM triage step
(optional `commitment` field), explicit "add a task" requests, and the dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from kairos.models import new_id
from kairos.timeutil import HOUR_MS, fmt_time, get_tz, now_ms, to_local

if TYPE_CHECKING:
    from kairos.runtime import Kairos

ACTION_VERBS = (r"send|share|review|check|look at|go through|read|call|ring|email|mail|reply|respond|finish|complete|"
                r"submit|prepare|draft|write|update|fix|book|pay|buy|order|sign|approve|forward|schedule|confirm|"
                r"bring|pick up|collect|upload|print")
REQUEST_RE = re.compile(
    rf"\b(?:can you|could you|would you|can u|pls|please|kindly|need you to|i need you to|make sure you)\s+"
    rf"((?:{ACTION_VERBS})\b[^.?!\n]{{0,140}})", re.I)
PROMISE_RE = re.compile(
    rf"\b(?:i'll|i will|i need to|i have to|i must|i promised(?: \w+)? (?:i'?d|to))\s+"
    rf"((?:{ACTION_VERBS})\b[^.?!\n]{{0,140}})", re.I)
PROMISED_TO_RE = re.compile(r"\bpromised (\w+)\b", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
STOP = {"the", "a", "an", "to", "for", "me", "my", "your", "it", "this", "that", "by", "on", "of", "and", "with", "i",
        "you", "please", "send", "share"}


def parse_due(text: str, tz: str, now: int | None = None) -> int | None:
    """Best-effort due date from phrases like 'by Friday', 'tomorrow morning', 'before 5pm', 'in 2 days'."""
    now = now or now_ms()
    t = text.lower()
    base = to_local(now, tz)
    day: datetime | None = None
    hour, minute = 18, 0  # default: end of working day

    if m := re.search(r"\bin (\d+) (hour|hr|day|week)s?\b", t):
        n, unit = int(m.group(1)), m.group(2)
        delta = {"hour": timedelta(hours=n), "hr": timedelta(hours=n), "day": timedelta(days=n),
                 "week": timedelta(weeks=n)}[unit]
        return int((base + delta).timestamp() * 1000)
    if re.search(r"\b(today|tonight|eod|end of (the )?day|this evening)\b", t):
        day = base
        if "tonight" in t:
            hour = 21
    elif "tomorrow" in t:
        day = base + timedelta(days=1)
        if "morning" in t:
            hour = 10
    elif "next week" in t:
        day = base + timedelta(days=7 - base.weekday())  # next Monday
        hour = 10
    else:
        for i, name in enumerate(WEEKDAYS):
            if re.search(rf"\b{name}\b", t):
                ahead = (i - base.weekday()) % 7 or 7
                day = base + timedelta(days=ahead)
                break
    if m := TIME_RE.search(t):
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        minute = int(m.group(2) or 0)
        day = day or base
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day is base and candidate.timestamp() * 1000 < now:
            day = base + timedelta(days=1)
    if day is None:
        return None
    due = day.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=get_tz(tz))
    return int(due.timestamp() * 1000)


def clean_title(raw: str) -> str:
    title = re.sub(r"\s+", " ", raw).strip(" ,;:-")
    title = re.sub(r"\s+(please|pls|thanks|thank you|asap)$", "", title, flags=re.I)
    return title[:120]


@dataclass
class Commitment:
    title: str
    direction: str  # they_asked | i_promised | todo
    person: str | None = None
    due_ts: int | None = None


def extract_requests(text: str, sender: str | None, tz: str, now: int | None = None) -> list[Commitment]:
    """Someone asked the user to do something."""
    out = []
    for m in REQUEST_RE.finditer(text or ""):
        title = clean_title(m.group(1))
        out.append(Commitment(title, "they_asked", sender, parse_due(title, tz, now) or parse_due(text, tz, now)))
    return out[:3]


def extract_promises(text: str, tz: str, now: int | None = None) -> list[Commitment]:
    """The user committed to something ("I'll send Rahul the deck by Friday")."""
    out = []
    for m in PROMISE_RE.finditer(text or ""):
        title = clean_title(m.group(1))
        to = PROMISED_TO_RE.search(text)
        person = to.group(1).capitalize() if to else None
        if not person and (m2 := re.search(r"\b(?:send|email|call|ring|reply to|forward)\s+([A-Z][a-z]+)\b", m.group(1))):
            person = m2.group(1)
        out.append(Commitment(title, "i_promised", person, parse_due(text, tz, now)))
    return out[:3]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", s.lower()) if len(w) > 2 and w not in STOP}


def similar(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, min(len(wa), len(wb))) if wa and wb else 0.0


def describe_task(t: dict, tz: str) -> str:
    who = t.get("person")
    lead = {"they_asked": f"{who} asked you to" if who else "You were asked to",
            "i_promised": f"You promised {who} to" if who else "You promised to"}.get(t.get("direction"), "To do:")
    due = f" (due {fmt_time(t['due_ts'], tz)})" if t.get("due_ts") else ""
    return f"{lead} {t['title']}{due}"


class TaskManager:
    def __init__(self, k: Kairos):
        self.k = k

    def create(self, c: Commitment, source: str, event_id: str | None = None, trust: str = "user") -> str | None:
        """Create unless an open task with the same person and a very similar title exists."""
        for t in self.open():
            if similar(t["title"], c.title) >= 0.7 and (not c.person or not t.get("person")
                                                        or c.person.lower() in (t["person"] or "").lower()):
                return None
        person_id = self.k.ingestor.resolve_person(c.person) if c.person else None
        tid = f"task-{new_id()[:8]}"
        self.k.graph.run("create_task", uid=self.k.settings.user_id, id=tid, title=c.title, direction=c.direction,
                         due_ts=c.due_ts, now=now_ms(), source=source, trust=trust, person_id=person_id,
                         event_id=event_id)
        return tid

    def open(self, person: str | None = None) -> list[dict]:
        return self.k.graph.run("open_tasks", uid=self.k.settings.user_id, person=person)

    def complete(self, task_id: str) -> bool:
        return bool(self.k.graph.run("complete_task", id=task_id, now=now_ms()))

    def complete_matching(self, text: str) -> dict | None:
        best, score = None, 0.0
        for t in self.open():
            s = similar(t["title"] + " " + (t.get("person") or ""), text)
            if s > score:
                best, score = t, s
        if best and score >= 0.5:
            self.complete(best["id"])
            return best
        return None

    def from_message(self, text: str, sender: str | None, event_id: str, trust: str) -> list[str]:
        made = []
        for c in extract_requests(text, sender, self.k.settings.tz):
            if tid := self.create(c, "message", event_id, trust):
                made.append(tid)
        return made

    def from_utterance(self, text: str, event_id: str) -> list[str]:
        made = []
        for c in extract_promises(text, self.k.settings.tz):
            if tid := self.create(c, "utterance", event_id, "user"):
                made.append(tid)
        return made

    def due_soon(self, now: int, window_ms: int = HOUR_MS) -> list[dict]:
        return self.k.graph.run("due_tasks", uid=self.k.settings.user_id, until=now + window_ms, now=now)
