"""Nightly memory digest: a day of conversation -> facts, notes, people and tasks in the graph.

Real-time extraction only catches phrasings it knows ("remember that ...", "I'll send it by Friday"). Everything
else the user says in passing ("I'm flying to Goa next Friday", "my sister Priya lives in Pune") would sit in the
chat log as words. Once a day, a small, cheap model (KAIROS_DIGEST_MODEL) reads the day's log and files what is
worth keeping into the right nodes; offline, simple rules do a smaller version of the same job.

Safety: the digest reads ONLY the user's own lines. Assistant replies can quote emails or web pages (other people's
text), so they are never fed in; everything the digest writes is therefore the user's own word (trust "user").
It only writes memory; it never acts.

Provenance: every Fact/Note/Task it creates has (n)-[:DERIVED_FROM]->(:Digest {day})-[:COVERS]->(:Session).
Incremental: the Digest stores how many user lines it has read, so re-running a day only reads the new lines.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from kairos.journal import day_bounds
from kairos.models import new_id
from kairos.tasks import Commitment, parse_due, similar
from kairos.timeutil import HOUR_MS, MINUTE_MS, now_ms, to_local

if TYPE_CHECKING:
    from kairos.runtime import Kairos

log = logging.getLogger(__name__)
CHUNK_CHARS = 12_000  # transcript characters per model call
MAX_ITEMS = 15
CHECK_EVERY_MS = 15 * MINUTE_MS
RULES_AFTER_DAYS = 3  # auto mode: wait this long for the LLM before settling for the rules digest

DIGEST_PROMPT = """You maintain the long-term memory of a personal assistant. Below is what the USER said to the
assistant on {day} ({weekday}). File away what is worth remembering after today. Reply with ONLY a JSON object:
{{"summary": "1-3 sentences: what the user did, planned or cared about",
 "topics": ["short topic"],
 "facts": [{{"key": "snake_case_key", "statement": "Full sentence about the user", "confidence": 0.0}}],
 "notes": [{{"text": "Self-contained sentence", "people": ["Name"]}}],
 "people": [{{"name": "Name", "relation": "colleague|manager|friend|family|sister|..."}}],
 "tasks": [{{"title": "What to do", "person": "Name or null", "due": "e.g. 'friday', '20 oct' or null"}}]}}
Rules:
- facts: stable things about the user that may change later (home, office, family, preferences, health, car,
  routines). Phrase as "Your ..." (e.g. "Your sister Priya lives in Pune"). REUSE an existing key below when
  the statement updates it; skip facts already stored with the same meaning.
- notes: plans, events, opinions and context (a trip, an appointment, how a meeting went). Turn relative dates
  ("next Friday", "tomorrow") into real dates using {day}.
- tasks: only things the user said they must do that are not in OPEN TASKS.
- Only what the user actually said or clearly implied. Ignore questions, commands to the assistant, greetings,
  and anything they asked to forget. Empty lists are fine. Never invent.

EXISTING FACTS (key: statement):
{facts}
KNOWN PEOPLE: {people}
OPEN TASKS: {tasks}"""


@dataclass
class DigestResult:
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)

    def merge(self, other: DigestResult) -> None:
        self.summary = " ".join(s for s in (self.summary, other.summary) if s)
        for name in ("topics", "facts", "notes", "people", "tasks"):
            getattr(self, name).extend(getattr(other, name))


# --- offline rules ---------------------------------------------------------------------
# Lines the real-time planner already handled (or that are questions/commands) carry no new memory.
SKIP_RE = re.compile(r"^(remember|note|make a note|jot|remind|tell me|let me know|notify|alert|what|why|where|who|"
                     r"when|how|which|is|are|do|does|did|can|could|would|will|open|show|download|search|find|read|"
                     r"delete|remove|run|forget|watch|send|copy|share|yes|no|ok|okay|thanks|thank you|hi|hello)\b",
                     re.I)
MY_X_IS_RE = re.compile(r"\bmy ([a-z][a-z' ]{1,30}?)(?:'s name)? (?:is|are|lives in|works at) ([^.!?]{2,80})", re.I)
VOLATILE = {"download", "phone", "battery", "uber", "cab", "ride", "internet", "wifi", "laptop", "pc", "screen",
            "day", "meeting", "head", "back", "mood", "bad", "question", "point", "plan", "guess"}
PLAN_RE = re.compile(r"\bI(?:'m| am| will be|'ll be) (?:flying|going|travelling|traveling|driving|heading|moving|"
                     r"visiting|off) (?:to )?([A-Z][\w ]{1,30}?)(?=[ ,.!]|$)")
PREF_RE = re.compile(r"\bI (?:really )?(like|love|prefer|hate|dislike|don't like|can't stand) ([^.!?]{2,60})", re.I)
ALLERGY_RE = re.compile(r"\bI(?:'m| am) allergic to ([^.!?]{2,60})", re.I)
PERSON_REL_RE = re.compile(r"\bmy (sister|brother|mother|mom|mum|father|dad|wife|husband|son|daughter|friend|"
                           r"manager|boss|colleague|cousin|partner)(?:'s name is| is called| is named)? ([A-Z][a-z]+)\b")


def snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "note"


def rules_digest(lines: list[str]) -> DigestResult:
    out = DigestResult()
    said = []
    for text in lines:
        if SKIP_RE.match(text.strip()):
            continue
        said.append(text)
        for m in PERSON_REL_RE.finditer(text):
            out.people.append({"name": m.group(2), "relation": m.group(1).lower()})
        for m in MY_X_IS_RE.finditer(text):
            subject = m.group(1).strip()
            if subject.split()[-1].lower() in VOLATILE or len(subject.split()) > 4:
                continue
            verb = re.search(r"\b(is|are|lives in|works at)\b", m.group(0)).group(1)
            out.facts.append({"key": snake(subject), "statement": f"Your {subject} {verb} {m.group(2).strip()}",
                              "confidence": 0.6})
        if PLAN_RE.search(text):
            out.notes.append({"text": f"Plan: {text.strip()}", "people": []})
        for m in PREF_RE.finditer(text):
            out.notes.append({"text": f"Preference: you {m.group(1).lower()} {m.group(2).strip()}", "people": []})
        for m in ALLERGY_RE.finditer(text):
            out.notes.append({"text": f"Health: you are allergic to {m.group(1).strip()}", "people": []})
    if said:
        out.summary = "You mentioned: " + "; ".join(s[:80] for s in said[:3]) + ("…" if len(said) > 3 else "") + "."
    return out


def _clean_result(data: dict) -> DigestResult:
    """Tolerate whatever shape a small model returns."""
    def items(name: str) -> list[dict]:
        return [x for x in (data.get(name) or []) if isinstance(x, dict)][:MAX_ITEMS]

    return DigestResult(summary=str(data.get("summary") or "")[:600],
                        topics=[str(t)[:40] for t in (data.get("topics") or []) if isinstance(t, str)][:8],
                        facts=items("facts"), notes=items("notes"), people=items("people"), tasks=items("tasks"))


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


class MemoryDigest:
    def __init__(self, k: Kairos):
        self.k = k
        self.last_check = 0

    @property
    def mode(self) -> str:
        return self.k.settings.digest

    def _client(self):
        client = self.k.llm.client
        return client if client is not None and client.configured and self.mode != "rules" else None

    # --- scheduling ------------------------------------------------------------------
    def _state(self) -> dict[str, dict]:
        try:
            return {r["day"]: r for r in self.k.graph.run("recent_digests", uid=self.k.settings.user_id, limit=400)}
        except Exception:
            return {}

    def pending_days(self, now: int | None = None) -> list[date]:
        """Finished days (yesterday only after KAIROS_DIGEST_HOUR) whose log has user lines not yet digested."""
        now = now if now is not None else now_ms()
        local = to_local(now, self.k.settings.tz)
        latest = local.date() - timedelta(days=1 if local.hour >= self.k.settings.digest_hour else 2)
        state = self._state()
        out = []
        for day in self.k.journal.days():
            if day > latest:
                continue
            user_lines = sum(1 for ln in self.k.journal.read_day(day) if ln.role == "user")
            if user_lines > (state.get(day.isoformat(), {}).get("turns") or 0):
                out.append(day)
        return out

    def maybe_run(self, now: int | None = None) -> list[dict]:
        """Called every minute by background_jobs; actually checks every 15 minutes."""
        now = now if now is not None else now_ms()
        if self.mode == "off" or not self.k.journal.enabled or now - self.last_check < CHECK_EVERY_MS:
            return []
        self.last_check = now
        done = []
        for day in self.pending_days(now):
            age = (to_local(now, self.k.settings.tz).date() - day).days
            client = self._client()
            if self.mode == "auto" and client is not None and not client.available() and age < RULES_AFTER_DAYS:
                continue  # the model is briefly down; a better digest later beats a rules digest now
            done.append(self.run(day))
        if done:
            self.k.journal.prune({date.fromisoformat(d) for d in self._state()}, now)
        return done

    # --- one day -------------------------------------------------------------------------
    def run(self, day: date | None = None, force_rules: bool = False) -> dict:
        day = day or self.k.journal.today()
        prev = self._state().get(day.isoformat(), {})
        done_turns = prev.get("turns") or 0
        user_lines = [ln for ln in self.k.journal.read_day(day) if ln.role == "user"]
        new_lines = user_lines[done_turns:]
        if not new_lines:
            return {"day": day.isoformat(), "turns": len(user_lines), "method": prev.get("method"), "new": 0,
                    "summary": prev.get("summary") or "", "facts": 0, "notes": 0, "people": 0, "tasks": 0}

        result, method = None, "rules"
        client = None if force_rules else self._client()
        if client is not None and client.available():
            try:
                result, method = self._llm_digest(client, day, [f"{ln.time} {ln.text}" for ln in new_lines]), "llm"
            except Exception as exc:  # LLMUnavailable or a malformed answer: rules instead
                log.info("digest via LLM failed for %s (%s); using rules", day, exc)
        if result is None:
            result = rules_digest([ln.text for ln in new_lines])
        if prev.get("summary"):
            result.summary = f"{prev['summary']} {result.summary}".strip()
        counts = self._apply(day, result, len(user_lines), method)
        log.info("digested %s: %s", day, counts)
        return {"day": day.isoformat(), "turns": len(user_lines), "new": len(new_lines), "method": method,
                "summary": result.summary, **counts}

    def _llm_digest(self, client, day: date, lines: list[str]) -> DigestResult:
        from kairos.agent.llm import parse_json_object

        g, uid = self.k.graph, self.k.settings.user_id
        facts = g.run("current_facts", uid=uid)
        prompt = DIGEST_PROMPT.format(
            day=day.isoformat(), weekday=day.strftime("%A"),
            facts="\n".join(f"{f['key']}: {f['statement']}" for f in facts[:60]) or "(none)",
            people=", ".join(f"{p['name']} ({p['relation']})" for p in g.run("list_people", uid=uid)[:40]) or "(none)",
            tasks="; ".join(t["title"] for t in self.k.tasks.open()[:20]) or "(none)")
        chunks, cur = [], ""
        for ln in lines:
            if cur and len(cur) + len(ln) > CHUNK_CHARS:
                chunks.append(cur)
                cur = ""
            cur += ln[:2000] + "\n"
        chunks.append(cur)
        out = DigestResult()
        for chunk in chunks:
            msg = client.chat([{"role": "system", "content": prompt}, {"role": "user", "content": chunk}],
                              model=self.k.settings.digest_model, max_tokens=1200, temperature=0, timeout=60)
            data = parse_json_object(msg.get("content") or "")
            if data is None:
                raise ValueError("digest model did not return JSON")
            out.merge(_clean_result(data))
        return out

    def _apply(self, day: date, r: DigestResult, turns: int, method: str) -> dict:
        k, g, tz = self.k, self.k.graph, self.k.settings.tz
        since, until = day_bounds(day, tz)
        counts = {"facts": 0, "notes": 0, "people": 0, "tasks": 0}
        noon = since + 12 * HOUR_MS
        with k.lock:
            row = g.one("create_digest", uid=k.settings.user_id, id=f"digest-{new_id()[:8]}", day=day.isoformat(),
                        summary=r.summary[:2000], topics=r.topics, now=now_ms(), turns=turns, method=method,
                        since=since, until=until)
            if not row:
                return counts
            did = row["id"]
            known = {p["name"].lower(): p for p in k.memory.people()}

            def person_id(name: str, relation: str | None = None) -> str | None:
                name = (name or "").strip()
                if not name or name.lower() in ("i", "me", "you", "user", "null", "none"):
                    return None
                pid = k.ingestor.resolve_person(name)
                cur = known.get(name.lower())
                if relation and (not cur or (cur.get("relation") or "unknown") in ("", "unknown", "contact")):
                    k.memory.update_person(pid, relation=relation.lower()[:30])
                g.run("digest_mentions_person", digest_id=did, person_id=pid)
                return pid

            for p in r.people[:MAX_ITEMS]:
                if person_id(str(p.get("name") or ""), p.get("relation")):
                    counts["people"] += 1

            for f in r.facts[:MAX_ITEMS]:
                statement, key = str(f.get("statement") or "").strip(), snake(str(f.get("key") or ""))
                try:
                    conf = max(0.3, min(0.95, float(f.get("confidence") or 0.7)))
                except (TypeError, ValueError):
                    conf = 0.7
                if not statement or conf < 0.5:
                    continue
                cur = k.memory.get_fact(key)
                if cur and (_norm(cur["statement"]) == _norm(statement) or similar(cur["statement"], statement) >= 0.9):
                    continue  # already known: don't create a new version of the same thing
                fid = k.memory.remember(statement[:300], key, confidence=conf)
                g.run_template("derived_from_digest", "Fact", id=fid, digest_id=did)
                counts["facts"] += 1

            for n in r.notes[:MAX_ITEMS]:
                text = str(n.get("text") or "").strip()
                if not text:
                    continue
                existing = g.run("search_notes_text", q=text[:40])
                if any(similar(e["text"], text) >= 0.8 for e in existing):
                    continue
                pids = [pid for name in (n.get("people") or []) if isinstance(name, str)
                        if (pid := person_id(name))]
                nid = k.memory.add_note(text[:1000], pids, trust="user")
                g.run_template("derived_from_digest", "Note", id=nid, digest_id=did)
                counts["notes"] += 1

            for t in r.tasks[:MAX_ITEMS]:
                title = str(t.get("title") or "").strip()
                if not title:
                    continue
                due = parse_due(str(t.get("due") or ""), tz, noon) if t.get("due") else None
                person = t.get("person") if isinstance(t.get("person"), str) else None
                tid = k.tasks.create(Commitment(title[:120], "todo", person, due), "digest", None, "user")
                if tid:
                    g.run_template("derived_from_digest", "Task", id=tid, digest_id=did)
                    counts["tasks"] += 1
        return counts

    # --- reading back ---------------------------------------------------------------------
    def recent(self, limit: int = 7) -> list[dict]:
        try:
            return self.k.graph.run("recent_digests", uid=self.k.settings.user_id, limit=limit)
        except Exception:
            return []

    def conversation(self, day: date) -> str:
        """What we talked about on `day`: the digest summary, or (not digested yet) the user's own lines."""
        row = self._state().get(day.isoformat())
        lines = [ln for ln in self.k.journal.read_day(day) if ln.role == "user"]
        undigested = lines[(row or {}).get("turns") or 0:]
        parts = []
        if row and row.get("summary"):
            parts.append(row["summary"])
        if undigested:
            parts.append("You said: " + "; ".join(f"“{ln.text[:100]}” ({ln.time[:5]})" for ln in undigested[-6:]))
        return " ".join(parts)


def parse_day(text: str | None, tz: str, now: int | None = None) -> date:
    """'today' | 'yesterday' | 'YYYY-MM-DD' | weekday name -> a local date (default today)."""
    today = to_local(now if now is not None else now_ms(), tz).date()
    t = (text or "today").strip().lower()
    if t in ("", "today", "now"):
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, name in enumerate(days):
        if name.startswith(t[:3]) and t[:3] in name:
            back = (today.weekday() - i) % 7 or 7  # "on monday" = the last Monday before today
            return today - timedelta(days=back)
    try:
        return datetime.strptime(t, "%d %b").date().replace(year=today.year)
    except ValueError:
        return today
