"""Memory lifecycle (design doc §8): versioned facts, habits that strengthen and decay, forgetting."""

from __future__ import annotations

from kairos.config import Settings
from kairos.embed import Embedder, NullEmbedder
from kairos.graph.client import Graph
from kairos.models import new_id
from kairos.timeutil import DAY_MS, now_ms, to_local

HABIT_LOOKBACK_DAYS = 28
HABIT_MIN_COUNT = 3
DECAY_LAMBDA = 0.05  # per day; a habit unused for ~2 weeks halves
ARCHIVE_THRESHOLD = 0.1


class Memory:
    def __init__(self, graph: Graph, settings: Settings, embedder: Embedder | None = None):
        self.g = graph
        self.s = settings
        self.embedder = embedder or NullEmbedder()

    # --- semantic: facts ----------------------------------------------------
    def remember(self, statement: str, key: str, source_event: str | None = None, confidence: float = 0.9) -> str:
        fid = f"fact-{new_id()[:8]}"
        self.g.run("assert_fact", uid=self.s.user_id, id=fid, key=key, statement=statement, now=now_ms(),
                   conf=confidence, source_event=source_event)
        if vec := self.embedder.embed(statement):
            self.g.run_template("set_embedding", "Fact", id=fid, embedding=vec)
        return fid

    def get_fact(self, key: str) -> dict | None:
        return self.g.one("current_fact", uid=self.s.user_id, key=key)

    def history(self, key: str) -> list[dict]:
        return self.g.run("fact_history", uid=self.s.user_id, key=key)

    def search(self, q: str) -> list[dict]:
        if vec := self.embedder.embed(q):
            try:
                return self.g.run("vector_facts", embedding=vec)
            except Exception:  # vector index missing/unsupported: fall back to text
                pass
        return self.g.run("search_facts_text", uid=self.s.user_id, q=q)

    def search_all(self, q: str, exclude: str | None = None) -> dict:
        """Hybrid recall: structured facts + notes + daily digests + events (incl. what the user said) by meaning
        (vectors) or words (full-text). `exclude` = the utterance asking the question, so it doesn't find itself."""
        events: list[dict] = []
        exclude = exclude or ""
        if vec := self.embedder.embed(q):
            try:
                events = self.g.run("vector_events", embedding=vec, exclude=exclude)
            except Exception:
                events = []
        if not events:
            try:
                events = self.g.run("search_events_fulltext", q=q, exclude=exclude)
            except Exception:  # full-text index missing: plain substring match
                events = self.g.run("search_events_contains", q=q, exclude=exclude)
        return {"facts": self.search(q), "notes": self.g.run("search_notes_text", q=q),
                "days": self.g.run("search_digests_text", q=q), "events": events}

    # --- free-text notes linked to the graph -------------------------------------
    def add_note(self, text: str, people_ids: list[str] = (), place_ids: list[str] = (),
                 source_event: str | None = None, trust: str = "user") -> str:
        nid = f"note-{new_id()[:8]}"
        self.g.run("add_note", uid=self.s.user_id, id=nid, text=text[:1000], now=now_ms(), trust=trust,
                   source_event=source_event)
        for pid in people_ids:
            self.g.run("note_about_person", note_id=nid, person_id=pid)
        for plid in place_ids:
            self.g.run("note_about_place", note_id=nid, place_id=plid)
        if vec := self.embedder.embed(text):
            self.g.run_template("set_embedding", "Note", id=nid, embedding=vec)
        return nid

    def notes_about(self, person: str) -> list[dict]:
        return self.g.run("notes_about_person", person=person)

    # --- memory editor (dashboard) ------------------------------------------------
    def archive_fact(self, fact_id: str) -> bool:
        return bool(self.g.run("archive_fact", id=fact_id, now=now_ms()))

    def archive_note(self, note_id: str) -> bool:
        return bool(self.g.run("archive_note", id=note_id, now=now_ms()))

    def people(self) -> list[dict]:
        return self.g.run("list_people_full", uid=self.s.user_id)

    def update_person(self, person_id: str, name: str | None = None, aliases: list[str] | None = None,
                      relation: str | None = None) -> bool:
        return bool(self.g.run("update_person", id=person_id, uid=self.s.user_id, name=name, aliases=aliases,
                               relation=relation))

    def merge_people(self, keep_id: str, old_id: str) -> dict | None:
        """Entity resolution: move every relationship of `old` onto `keep`, keep its name as an alias."""
        from kairos.graph.queries import PERSON_RELS_IN, PERSON_RELS_OUT

        if keep_id == old_id:
            return None
        for rel in PERSON_RELS_OUT:
            self.g.run_template("merge_person_out", rel=rel, old=old_id, keep=keep_id)
        for rel in PERSON_RELS_IN:
            self.g.run_template("merge_person_in", rel=rel, old=old_id, keep=keep_id)
        return self.g.one("merge_person_finish", old=old_id, keep=keep_id, uid=self.s.user_id)

    def forget(self, topic: str) -> int:
        row = self.g.one("forget_topic", topic=topic, now=now_ms())
        return row["n"] if row else 0

    # --- procedural: habits ---------------------------------------------------
    def detect_habits(self, now: int | None = None) -> list[dict]:
        now = now or now_ms()
        return self.g.run("detect_trip_habits", since=now - HABIT_LOOKBACK_DAYS * DAY_MS,
                          min_count=HABIT_MIN_COUNT, now=now)

    def decay(self, now: int | None = None) -> int:
        row = self.g.one("decay_habits", now=now or now_ms(), **{"lambda": DECAY_LAMBDA},
                         threshold=ARCHIVE_THRESHOLD)
        return row["archived"] if row else 0

    def habits(self, min_strength: float = 0.3) -> list[dict]:
        return self.g.run("list_habits", min_strength=min_strength)

    def due_suggestions(self, now: int | None = None, lead_min: int = 20) -> list[dict]:
        """Strong trip habits whose time is coming up and that haven't been suggested today."""
        now = now or now_ms()
        local = to_local(now, self.s.tz)
        is_weekday = local.weekday() < 5
        out = []
        for h in self.habits(min_strength=0.6):
            if h.get("kind") != "trip" or (h.get("days") == "weekdays") != is_weekday:
                continue
            minutes_to = (h["hour"] * 60) - (local.hour * 60 + local.minute)
            suggested_today = h.get("last_suggested") and to_local(h["last_suggested"], self.s.tz).date() == local.date()
            if 0 < minutes_to <= lead_min and not suggested_today:
                out.append(h)
        return out

    def mark_suggested(self, habit_id: str) -> None:
        self.g.run("mark_habit_suggested", id=habit_id, now=now_ms())


def describe_habit(h: dict) -> str:
    hour = h.get("hour", 0)
    when = f"{(hour % 12) or 12} {'AM' if hour < 12 else 'PM'}"
    return f"You usually take a {h.get('provider') or 'cab'} to {h.get('place')} around {when} on {h.get('days')}"
