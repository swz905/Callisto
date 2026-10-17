"""Calendar (ICS), email (IMAP parsing), places, Telegram, browser history, algorithms, handoff, plugins."""

import asyncio
import json
import sqlite3
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agents.browser_history import BrowserHistoryWatcher, clean_visits, from_epoch_ms, read_visits
from kairos.algorithms import important_people, interaction_score
from kairos.calendar_ics import build_prep, demo_events, parse_ics
from kairos.channels.telegram import TelegramChannel
from kairos.email_imap import message_to_envelope
from kairos.handoff import TransferStore, safe_name
from kairos.ingest.rules import extract_notification
from kairos.models import EventEnvelope, Outbound
from kairos.places import haversine_m, nearest_place, normalize_place
from kairos.plugins import PluginManager, PluginTool, result_text, tool_name
from kairos.runtime import Kairos
from kairos.status import StatusTracker, diff_status
from kairos.timeutil import now_ms

# ------------------------------------------------------------------ calendar
ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:abc@google.com
DTSTART;TZID=Asia/Kolkata:20261015T110000
DTEND;TZID=Asia/Kolkata:20261015T113000
SUMMARY:Q3 sync\\, budget
LOCATION:Conf Room 3
ORGANIZER;CN=Rahul Sharma:mailto:rahul@example.com
ATTENDEE;CN="Karan Gupta";ROLE=REQ-PARTICIPANT:mailto:karan@example.com
ATTENDEE:mailto:priya.mehta@example.com
DESCRIPTION:Review the Q3 report and this is a long line that is folded acros
 s two lines
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTART:20261012T043000Z
DTEND:20261012T050000Z
RRULE:FREQ=WEEKLY;COUNT=4
SUMMARY:Team standup
END:VEVENT
BEGIN:VEVENT
UID:allday
DTSTART;VALUE=DATE:20261016
SUMMARY:Holiday
END:VEVENT
END:VCALENDAR"""

WINDOW_START = 1_791_000_000_000  # ~ Oct 2026
WINDOW = (WINDOW_START, WINDOW_START + 40 * 86_400_000)


def test_parse_ics_timezones_folding_attendees_and_recurrence():
    events = {e.uid.split("#")[0]: e for e in parse_ics(ICS, "Asia/Kolkata", WINDOW)}
    q3 = events["abc@google.com"]
    assert q3.title == "Q3 sync, budget" and q3.location == "Conf Room 3"
    assert q3.attendees == ["Rahul Sharma", "Karan Gupta", "Priya Mehta"]
    assert "across two lines" in q3.description
    assert q3.end_ts - q3.start_ts == 30 * 60_000
    from datetime import datetime, timezone
    assert datetime.fromtimestamp(q3.start_ts / 1000, timezone.utc).strftime("%H:%M") == "05:30"  # 11:00 IST
    weekly = [e for e in parse_ics(ICS, "Asia/Kolkata", WINDOW) if e.uid.startswith("weekly")]
    assert len(weekly) == 4 and weekly[1].start_ts - weekly[0].start_ts == 7 * 86_400_000
    assert events["allday"].title == "Holiday"


def test_demo_calendar_and_prep_text():
    evs = demo_events(1_000_000)
    assert evs[0].title == "Q3 sync" and "Rahul Sharma" in evs[0].attendees
    text = build_prep({"title": "Q3 sync", "start_ts": now_ms(), "people": ["Rahul Sharma"]},
                      [{"person": "Rahul Sharma", "subjects": ["Q3 report for review"], "tasks": ["send the deck"],
                        "files": ["Q3_report.pdf"]}], ["Q3_report.pdf", "Q3_report.pdf"], "Asia/Kolkata")
    assert "Q3 report for review" in text and "send the deck" in text and text.count("Q3_report.pdf") == 1


# ------------------------------------------------------------------ email
def test_imap_message_becomes_a_linked_message_with_a_request():
    msg = EmailMessage()
    msg["From"] = "Rahul Sharma <rahul@example.com>"
    msg["Subject"] = "Budget deck"
    msg["Date"] = "Wed, 14 Oct 2026 10:00:00 +0530"
    msg["Message-ID"] = "<m1@example.com>"
    msg.set_content("plain fallback")
    msg.add_alternative("<p>Could you <b>share the deck</b> by Friday? https://x.example.com/deck.pptx</p>",
                        subtype="html")
    env = message_to_envelope(msg.as_bytes(), "arjun")
    assert env.source == "imap" and env.payload["title"] == "Rahul Sharma" and env.payload["key"] == "<m1@example.com>"
    ex = extract_notification(env.source, env.payload)
    assert ex.kind == "message" and ex.message["sender"] == "Rahul Sharma" and ex.message["subject"] == "Budget deck"
    assert ex.urls == ["https://x.example.com/deck.pptx"]


# ------------------------------------------------------------------ places
PLACES = [{"name": "Home", "lat": 28.4211, "lon": 77.0717, "radius_m": 250},
          {"name": "Office", "lat": 28.4595, "lon": 77.0950, "radius_m": 300}]


def test_geofences():
    assert 4000 < haversine_m(28.4211, 77.0717, 28.4595, 77.0950) < 5000
    assert nearest_place(28.4596, 77.0951, PLACES) == "Office"
    assert nearest_place(28.44, 77.08, PLACES) is None
    assert normalize_place("the office", ["Office", "Home"]) == "Office"
    assert normalize_place("work") == "Office" and normalize_place("gym") == "Gym"


def test_place_transitions():
    prev = {"status_ts": 1, "place_name": "Office"}
    assert diff_status(prev, {"place_name": ""}) == [("place.left", {"place": "Office"})]
    assert diff_status(prev, {"place_name": "Home"}) == [("place.left", {"place": "Office"}),
                                                          ("place.entered", {"place": "Home"})]
    assert diff_status(prev, {"battery": 50}) == []  # no location in this snapshot: no change


def test_tracker_maps_location_to_place_and_never_stores_coordinates():
    stored, ingested = {}, []
    graph = SimpleNamespace(
        one=lambda name, **p: {"status_ts": 1, "place_name": "Office"},
        run=lambda name, **p: PLACES if name == "geo_places" else stored.update(p.get("props") or {}) or [])
    k = SimpleNamespace(graph=graph, lock=threading.RLock(),
                        ingest=lambda env: ingested.append(env) or SimpleNamespace(alerts=[]))
    StatusTracker(k).apply(EventEnvelope(user_id="arjun", device_id="phone-arjun", type="device.status",
                                         payload={"location": {"lat": 28.4212, "lon": 77.0716}}))
    assert stored["place_name"] == "Home" and "lat" not in stored and "location" not in stored
    assert [e.type for e in ingested] == ["place.left", "place.entered"]


# ------------------------------------------------------------------ telegram
def make_tg(allowed=(42,)):
    sent = []

    def handler(req):
        sent.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": []})

    k = SimpleNamespace(chat=lambda text, device, session_id=None: SimpleNamespace(text=f"echo: {text}"),
                        graph=SimpleNamespace(run=lambda *a, **kw: []))
    return TelegramChannel(k, "TOKEN", set(allowed), "telegram-arjun", transport=httpx.MockTransport(handler)), sent


def test_telegram_allowed_chat_talks_to_the_agent():
    tg, sent = make_tg()
    assert tg.handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "brief me"}}) == "echo: brief me"
    assert sent == [("sendMessage", {"chat_id": 42, "text": "echo: brief me"})]


def test_telegram_strangers_are_refused():
    tg, sent = make_tg()
    reply = tg.handle_update({"update_id": 2, "message": {"chat": {"id": 7}, "text": "run rm -rf"}})
    assert "private" in reply and "7" in reply and len(sent) == 1


def test_telegram_delivers_alerts_but_not_device_commands():
    tg, sent = make_tg()
    assert tg.deliver(Outbound(kind="alert", device_id="telegram-arjun", text="Download done"))
    assert sent[-1][1]["text"] == "🔔 Download done"
    assert not tg.deliver(Outbound(kind="exec", device_id="telegram-arjun", args={"cmd": "x"}))


# ------------------------------------------------------------------ browser history
def _chromium_db(path: Path, rows):
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                      "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
    for i, (url, title, ts_ms) in enumerate(rows, 1):
        con.execute("INSERT INTO urls VALUES(?,?,?)", (i, url, title))
        con.execute("INSERT INTO visits VALUES(?,?,?)", (i, i, from_epoch_ms(ts_ms, "chromium")))
    con.commit()
    con.close()


def _firefox_db(path: Path, rows):
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                      "CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER);")
    for i, (url, title, ts_ms) in enumerate(rows, 1):
        con.execute("INSERT INTO moz_places VALUES(?,?,?)", (i, url, title))
        con.execute("INSERT INTO moz_historyvisits VALUES(?,?,?)", (i, i, ts_ms * 1000))
    con.commit()
    con.close()


def test_browser_history_reads_chromium_and_firefox(tmp_path):
    t0 = 1_791_000_000_000
    _chromium_db(tmp_path / "History", [("https://news.example.com/a", "Budget news", t0),
                                         ("chrome://settings", "Settings", t0 + 1000),
                                         ("https://news.example.com/a", "Budget news", t0 + 2000)])
    _firefox_db(tmp_path / "places.sqlite", [("https://docs.example.com/q3", "Q3 doc", t0 + 5000)])
    chrome = clean_visits(read_visits(tmp_path / "History", "chromium", 0), "chromium", "Chrome")
    assert [(v["url"], v["ts"]) for v in chrome] == [("https://news.example.com/a", t0)]  # internal + reload dropped
    ff = clean_visits(read_visits(tmp_path / "places.sqlite", "firefox", 0), "firefox", "Firefox")
    assert ff[0]["title"] == "Q3 doc" and ff[0]["ts"] == t0 + 5000


def test_browser_history_watcher_starts_from_now_then_streams_new_visits(tmp_path):
    db = tmp_path / "History"
    old = now_ms() - 3_600_000
    _chromium_db(db, [("https://old.example.com", "Old", old)])
    w = BrowserHistoryWatcher(state_file=tmp_path / "state.json")
    assert w.poll([("Chrome", db, "chromium")]) == []  # no backfill on first run
    con = sqlite3.connect(db)
    con.execute("INSERT INTO urls VALUES(9, 'https://new.example.com', 'New')")
    con.execute("INSERT INTO visits VALUES(9, 9, ?)", (from_epoch_ms(now_ms() + 1000, "chromium"),))
    con.commit()
    con.close()
    got = BrowserHistoryWatcher(state_file=tmp_path / "state.json").poll([("Chrome", db, "chromium")])
    assert [v["url"] for v in got] == ["https://new.example.com"]


# ------------------------------------------------------------------ algorithms
def test_important_people_blends_pagerank_and_interactions():
    now = now_ms()
    rows = [{"id": "p1", "name": "Rahul", "relation": "manager", "msgs": 3, "mentions": 1, "open_tasks": 1,
             "meetings": 1, "last_ts": now - 1000},
            {"id": "p2", "name": "Karan", "relation": "colleague", "msgs": 0, "mentions": 1, "open_tasks": 0,
             "meetings": 0, "last_ts": None}]

    def run(name, **p):
        if name == "pagerank_people":
            raise RuntimeError("procedure not found")  # older server: fall back
        return rows

    ranked = important_people(SimpleNamespace(run=run), "arjun", now)
    assert [r["name"] for r in ranked] == ["Rahul", "Karan"] and "open task" in ranked[0]["why"]
    assert interaction_score(rows[0], now) > interaction_score(rows[1], now)
    with_pr = important_people(SimpleNamespace(run=lambda n, **p: [{"id": "p2", "score": 1.0}] if n == "pagerank_people"
                                               else rows), "arjun", now)
    assert with_pr[1]["pagerank"] == 1.0


# ------------------------------------------------------------------ handoff
def test_transfer_store(tmp_path):
    store = TransferStore(str(tmp_path))
    t = store.create("act-1", r"C:\Users\x\Q3 report.pdf", "pc-main", "phone-arjun")
    assert t.name == "Q3 report.pdf" and len(t.id) == 32
    store.receive(t.id, b"%PDF data")
    assert (tmp_path / "transfers" / f"{t.id}_Q3 report.pdf").read_bytes() == b"%PDF data"
    with pytest.raises(ValueError):
        store.receive(t.id, b"again")
    with pytest.raises(KeyError):
        store.receive("nope", b"x")
    t.created -= 7200
    assert store.get(t.id) is None  # expired
    assert safe_name("../../etc/passwd") == "passwd"


def test_transfer_received_opens_on_target_and_reports_back(tmp_path):
    sent, results = [], []
    fake = SimpleNamespace(transfers=TransferStore(str(tmp_path)), gateway=SimpleNamespace(send=sent.append),
                           settings=SimpleNamespace(hub_public_url="http://192.168.1.5:8000"),
                           device_result=lambda aid, ok, out: results.append((aid, ok, out)))
    t = fake.transfers.create("act-7", "deck.pptx", "pc-main", "phone-arjun")
    out = Kairos.transfer_received(fake, t.id, b"bytes")
    assert sent[0].kind == "open" and sent[0].device_id == "phone-arjun"
    assert out["url"] == f"http://192.168.1.5:8000/api/transfer/{t.id}/deck.pptx"
    assert results == [("act-7", True, "deck.pptx → phone-arjun")]


def test_clipboard_result_is_forwarded_not_stored():
    sent, updates = [], []
    row = {"id": "a1", "tool": "send_clipboard", "status": "dispatched", "summary": "", "device_id": "pc-main",
           "origin_device": "pc-main", "args_json": json.dumps({"to_device": "phone-arjun"})}
    fake = SimpleNamespace(lock=threading.RLock(), graph=SimpleNamespace(one=lambda n, **p: dict(row)),
                           audit=SimpleNamespace(update=lambda aid, s, r=None: updates.append((aid, s, r))),
                           gateway=SimpleNamespace(send=sent.append), devices={"phone": "phone-arjun"},
                           _route=lambda d: d)
    Kairos.device_result(fake, "a1", True, "secret clipboard text")
    assert sent[0].kind == "clipboard_set" and sent[0].args["text"] == "secret clipboard text"
    assert updates[0][2] == "21 characters copied to phone-arjun"  # contents never written to the graph


# ------------------------------------------------------------------ plugins
def test_plugin_names_specs_and_results(tmp_path):
    assert tool_name("my files", "read.file") == "mcp__my_files__read_file"
    assert len(tool_name("s" * 50, "t" * 50)) == 64
    spec = PluginTool("files", "read", "mcp__files__read", "Read a file",
                      {"type": "object", "properties": {"path": {"type": "string"}}}).spec()
    assert spec["function"]["name"] == "mcp__files__read" and "[plugin files]" in spec["function"]["description"]
    content = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello"), SimpleNamespace(type="image")])
    assert result_text(content) == "hello\n[image]"
    assert PluginManager(None, tmp_path / "missing.json").config == {}


def test_plugin_call_runs_on_the_plugin_loop(tmp_path):
    pm = PluginManager(None, tmp_path / "none.json")

    class Session:
        async def call_tool(self, name, args):
            return SimpleNamespace(content=[SimpleNamespace(text=f"{name}:{args['x']}")], isError=False)

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    pm.loop, pm.sessions["s"] = loop, Session()
    pm.tools["mcp__s__t"] = PluginTool("s", "t", "mcp__s__t", "d")
    assert pm.has("mcp__s__t") and pm.capability("mcp__s__t") == "plugin.s"
    assert pm.call("mcp__s__t", {"x": 1}) == "t:1"
    loop.call_soon_threadsafe(loop.stop)
    time.sleep(0.05)
