"""Commitments (parse, extract, dedupe), notes, memory export, person merge templates."""

from datetime import datetime
from types import SimpleNamespace

from kairos.export import memory_markdown
from kairos.graph.queries import QUERIES, render
from kairos.memory import Memory
from kairos.tasks import describe_task, extract_promises, extract_requests, parse_due, similar
from kairos.timeutil import get_tz

TZ = "Asia/Kolkata"
# Wednesday 14 Oct 2026, 10:00 IST
NOW = int(datetime(2026, 10, 14, 10, 0, tzinfo=get_tz(TZ)).timestamp() * 1000)


def local(ts):
    return datetime.fromtimestamp(ts / 1000, get_tz(TZ))


def test_parse_due():
    assert local(parse_due("by Friday", TZ, NOW)).strftime("%a %H:%M") == "Fri 18:00"
    assert local(parse_due("tomorrow morning", TZ, NOW)).strftime("%d %H:%M") == "15 10:00"
    assert local(parse_due("before 5pm", TZ, NOW)).strftime("%d %H:%M") == "14 17:00"
    assert local(parse_due("at 9am", TZ, NOW)).strftime("%d %H:%M") == "15 09:00"  # 9am already passed today
    assert local(parse_due("in 2 days", TZ, NOW)).strftime("%d %H:%M") == "16 10:00"
    assert local(parse_due("eod", TZ, NOW)).strftime("%d %H:%M") == "14 18:00"
    assert local(parse_due("on wednesday", TZ, NOW)).strftime("%d") == "21"  # next week's Wednesday
    assert parse_due("whenever you can", TZ, NOW) is None


def test_extract_requests_from_messages():
    c = extract_requests("Could you share the updated budget deck by Friday? Thanks", "Rahul Sharma", TZ, NOW)
    assert len(c) == 1 and c[0].title == "share the updated budget deck by Friday"
    assert c[0].direction == "they_asked" and c[0].person == "Rahul Sharma" and c[0].due_ts
    assert extract_requests("Lunch at 1?", "Priya", TZ, NOW) == []
    assert extract_requests("Please review the Q3 report before the 11am sync", "Rahul", TZ, NOW)[0].title \
        .startswith("review the Q3 report")


def test_extract_promises_from_user():
    c = extract_promises("I promised Rahul I'd send the deck by Friday", TZ, NOW)
    assert c and c[0].person == "Rahul" and c[0].title.startswith("send the deck") and c[0].direction == "i_promised"
    assert extract_promises("I'll be late today", TZ, NOW) == []  # no action verb
    assert extract_promises("remind me to call Priya in 10 minutes", TZ, NOW) == []  # that's a reminder, not a task


def test_similarity_and_description():
    assert similar("share the updated budget deck by Friday", "send the budget deck") >= 0.5
    assert similar("book flights", "pay rent") == 0
    assert describe_task({"title": "send the deck", "direction": "they_asked", "person": "Rahul"}, TZ) \
        == "Rahul asked you to send the deck"


def test_person_merge_templates_are_allowlisted():
    q = render(QUERIES["merge_person_in"], rel="MENTIONS")
    assert "[r:MENTIONS]" in q and "{rel}" not in q
    try:
        render(QUERIES["merge_person_in"], rel="MENTIONS]->() DETACH DELETE (x")
        raise AssertionError("should reject")
    except ValueError:
        pass


def test_merge_people_moves_every_relationship_type():
    calls = []
    g = SimpleNamespace(run_template=lambda name, label=None, rel=None, **p: calls.append((name, rel)) or [],
                        one=lambda name, **p: calls.append((name, None)) or {"id": p["keep"]})
    mem = Memory(g, SimpleNamespace(user_id="arjun"))
    assert mem.merge_people("p1", "p2") == {"id": "p1"}
    assert ("merge_person_out", "SENT") in calls and ("merge_person_in", "WITH") in calls
    assert calls[-1][0] == "merge_person_finish"
    assert mem.merge_people("p1", "p1") is None


def test_memory_markdown():
    md = memory_markdown(
        "Arjun", tz=TZ,
        facts=[{"key": "office_location", "statement": "Your office is at Cyber City", "valid_to": None},
               {"key": "office_location", "statement": "Your office is at Golf Course Road", "valid_to": NOW}],
        people=[{"name": "Rahul Sharma", "aliases": ["Rahul"], "relation": "manager"}],
        notes=[{"text": "Rahul prefers short decks", "about": ["Rahul Sharma"]}],
        tasks=[{"title": "send the deck", "direction": "they_asked", "person": "Rahul", "due_ts": None}],
        habits=[], meetings=[])
    assert "**office_location**: Your office is at Cyber City" in md
    assert "~~Your office is at Golf Course Road~~" in md
    assert "Rahul Sharma (aka Rahul): manager" in md and "- [ ] Rahul asked you to send the deck" in md
