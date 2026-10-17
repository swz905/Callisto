"""Calendar ingestion from iCalendar (ICS) feeds, plus meeting prep packs.

No OAuth: Google Calendar ("Secret address in iCal format") and Outlook ("Publish calendar -> ICS") both give a
private ICS URL. Put one or more in KAIROS_CALENDAR_URLS (comma-separated; local .ics file paths work too).
`demo` generates two meetings relative to now, so the prep-pack demo works without any account.

Graph: (User)-[:ATTENDS]->(Meeting {title, start_ts, end_ts, location})-[:WITH]->(Person), -[:AT]->(Place)
Prep pack (15 min before): who's in it, what they recently sent, files from them, open tasks with them -
all found by walking the graph from the Meeting node.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from kairos.timeutil import DAY_MS, MINUTE_MS, fmt_time, get_tz, now_ms

if TYPE_CHECKING:
    from kairos.runtime import Kairos

log = logging.getLogger(__name__)
WINDOW_PAST_MS = DAY_MS
WINDOW_FUTURE_MS = 14 * DAY_MS
PREP_LEAD_MS = 15 * MINUTE_MS


@dataclass
class IcsEvent:
    uid: str
    title: str
    start_ts: int
    end_ts: int
    location: str = ""
    description: str = ""
    attendees: list[str] = field(default_factory=list)  # display names (or email local parts)


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _split(line: str) -> tuple[str, dict, str]:
    """'DTSTART;TZID=Asia/Kolkata:20261015T110000' -> ('DTSTART', {'TZID': 'Asia/Kolkata'}, '2026...')."""
    head, _, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def parse_dt(value: str, params: dict, default_tz: str) -> int:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        dt = datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=get_tz(params.get("TZID") or default_tz))
        return int(dt.timestamp() * 1000)
    fmt = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) == 15 else "%Y%m%dT%H%M"
    dt = datetime.strptime(value.rstrip("Z"), fmt)
    tz = timezone.utc if value.endswith("Z") else get_tz(params.get("TZID") or default_tz)
    return int(dt.replace(tzinfo=tz).timestamp() * 1000)


def _attendee_name(params: dict, value: str) -> str:
    if params.get("CN"):
        return params["CN"]
    m = re.search(r"mailto:([^@]+)@", value, re.I)
    return m.group(1).replace(".", " ").title() if m else ""


def _expand(ev: IcsEvent, rrule: str, start: int, end: int) -> list[IcsEvent]:
    """Minimal RRULE support: FREQ=DAILY|WEEKLY with COUNT/UNTIL/INTERVAL, within [start, end]."""
    rule = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    step = {"DAILY": DAY_MS, "WEEKLY": 7 * DAY_MS}.get(rule.get("FREQ", ""), 0) * int(rule.get("INTERVAL", "1"))
    if not step:
        return [ev]
    count = int(rule.get("COUNT", "500"))
    until = parse_dt(rule["UNTIL"], {}, "UTC") if "UNTIL" in rule else end
    dur, out, ts = ev.end_ts - ev.start_ts, [], ev.start_ts
    for i in range(count):
        if ts > min(until, end):
            break
        if ts + dur >= start:
            out.append(IcsEvent(f"{ev.uid}#{i}", ev.title, ts, ts + dur, ev.location, ev.description, ev.attendees))
        ts += step
    return out


def parse_ics(text: str, default_tz: str, window: tuple[int, int] | None = None) -> list[IcsEvent]:
    start, end = window or (now_ms() - WINDOW_PAST_MS, now_ms() + WINDOW_FUTURE_MS)
    events, cur, rrule = [], None, ""
    for line in _unfold(text):
        name, params, value = _split(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur, rrule = {"attendees": []}, ""
        elif name == "END" and value.upper() == "VEVENT" and cur is not None:
            if cur.get("start"):
                ev = IcsEvent(cur.get("uid") or f"ics-{cur['start']}", cur.get("title") or "(no title)",
                              cur["start"], cur.get("end") or cur["start"] + 30 * MINUTE_MS,
                              cur.get("location", ""), cur.get("description", ""), cur["attendees"])
                for occ in (_expand(ev, rrule, start, end) if rrule else [ev]):
                    if occ.end_ts >= start and occ.start_ts <= end:
                        events.append(occ)
            cur = None
        elif cur is not None:
            if name == "UID":
                cur["uid"] = value
            elif name == "SUMMARY":
                cur["title"] = _unescape(value)
            elif name == "LOCATION":
                cur["location"] = _unescape(value)
            elif name == "DESCRIPTION":
                cur["description"] = _unescape(value)[:500]
            elif name == "DTSTART":
                cur["start"] = parse_dt(value, params, default_tz)
            elif name == "DTEND":
                cur["end"] = parse_dt(value, params, default_tz)
            elif name == "RRULE":
                rrule = value
            elif name in ("ATTENDEE", "ORGANIZER"):
                n = _attendee_name(params, value)
                if n and n not in cur["attendees"]:
                    cur["attendees"].append(n)
    return sorted(events, key=lambda e: e.start_ts)


def demo_events(now: int | None = None) -> list[IcsEvent]:
    """Times are relative to `now` (the first sync), so the prep pack fires about a minute after startup."""
    now = now or now_ms()
    soon = now + 16 * MINUTE_MS
    return [
        IcsEvent("demo-q3-sync", "Q3 sync", soon, soon + 30 * MINUTE_MS, "Conf Room 3",
                 "Review Q3 report and budget", ["Rahul Sharma", "Karan Gupta"]),
        IcsEvent("demo-1on1-karan", "1:1 with Karan", now + DAY_MS, now + DAY_MS + 30 * MINUTE_MS, "",
                 "", ["Karan Gupta"]),
    ]


def build_prep(meeting: dict, ctx_rows: list[dict], files: list[str], tz: str) -> str:
    """One spoken-length summary of what matters for the meeting."""
    people = ", ".join(meeting.get("people") or []) or "no one listed"
    parts = [f"Your meeting “{meeting['title']}” with {people} starts {fmt_time(meeting['start_ts'], tz)}."]
    for r in ctx_rows:
        if not r.get("person"):
            continue
        bits = []
        if r.get("subjects"):
            bits.append("recently sent: " + "; ".join(s for s in r["subjects"] if s))
        if r.get("tasks"):
            bits.append("open with them: " + "; ".join(r["tasks"]))
        if bits:
            parts.append(f"{r['person']} - " + " | ".join(bits) + ".")
    if files:
        parts.append("Related files: " + ", ".join(dict.fromkeys(files)) + ".")
    return " ".join(parts)


class CalendarSync:
    def __init__(self, k: Kairos):
        self.k = k
        self.last_sync = 0
        self._demo_base: int | None = None

    def fetch(self, source: str) -> list[IcsEvent]:
        if source == "demo":
            self._demo_base = self._demo_base or now_ms()  # fixed on first sync so meetings don't drift
            return demo_events(self._demo_base)
        if source.startswith(("http://", "https://", "webcal://")):
            import httpx

            from kairos.net import ssl_context

            r = httpx.get(source.replace("webcal://", "https://"), timeout=20, follow_redirects=True,
                          verify=ssl_context())
            r.raise_for_status()
            text = r.text
        else:
            text = Path(source).read_text(encoding="utf-8", errors="ignore")
        return parse_ics(text, self.k.settings.tz)

    def sync(self) -> int:
        sources = self.k.settings.calendar_urls
        if not sources:
            return 0
        events: list[IcsEvent] = []
        for src in sources:  # network outside the graph lock
            try:
                events += self.fetch(src)
            except Exception as exc:
                log.warning("calendar %s failed: %s", src if src == "demo" else "<url>", exc)
        with self.k.lock:
            for ev in events:
                self.upsert(ev)
        self.last_sync = now_ms()
        return len(events)

    def upsert(self, ev: IcsEvent) -> None:
        g = self.k.graph
        mid = f"meeting-{re.sub(r'[^A-Za-z0-9#_-]', '', ev.uid)[:60]}"
        g.run("upsert_meeting", uid=self.k.settings.user_id, id=mid, title=ev.title, start=ev.start_ts,
              end=ev.end_ts, location=ev.location, description=ev.description, now=now_ms())
        for name in ev.attendees[:15]:
            if name.lower() in (self.k.settings.user_display_name or "").lower():
                continue
            g.run("link_meeting_person", meeting_id=mid, person_id=self.k.ingestor.resolve_person(name))
        if ev.location:
            g.run("link_meeting_place", meeting_id=mid, place=ev.location,
                  place_id=f"place-{re.sub(r'[^a-z0-9]+', '-', ev.location.lower())[:40]}")

    def upcoming(self, since: int, until: int) -> list[dict]:
        return self.k.graph.run("upcoming_meetings", uid=self.k.settings.user_id, since=since, until=until)

    def prep(self, meeting: dict, now: int | None = None) -> str:
        now = now or now_ms()
        g = self.k.graph
        rows = g.run("meeting_context", meeting_id=meeting["id"], since=now - 14 * DAY_MS)
        files = [f for r in rows for f in (r.get("files") or [])]
        for word in [w for w in re.findall(r"[A-Za-z0-9]{3,}", meeting["title"]) if w.lower() not in
                     {"sync", "meeting", "call", "with", "the", "and", "weekly", "daily"}][:2]:
            files += [f["name"] for f in g.run("find_file_by_name", q=word)[:2]]
        return build_prep(meeting, rows, files, self.k.settings.tz)

    def due_preps(self, now: int) -> list[dict]:
        return [m for m in self.upcoming(now, now + PREP_LEAD_MS) if not m.get("prepped") and m["start_ts"] >= now]
