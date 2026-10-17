"""End-to-end against a real FalkorDB. Skipped unless one is reachable (docker compose up -d falkordb).

Uses a throwaway graph `kairos:test_<random>`.
"""

import os
import time
import uuid

import pytest

from kairos.config import load_settings


def _settings():
    os.environ["KAIROS_USER"] = f"test_{uuid.uuid4().hex[:6]}"
    return load_settings()


def _reachable(s) -> bool:
    try:
        from falkordb import FalkorDB

        FalkorDB(**{**s.falkordb_kwargs(), "socket_connect_timeout": 1}).connection.ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def k():
    s = _settings()
    if not _reachable(s):
        pytest.skip("FalkorDB not reachable")
    from kairos.gateway import LocalGateway
    from kairos.runtime import Kairos
    from kairos.seed import load_persona, seed

    sent = []
    gw = LocalGateway(printer=sent.append, simulate=False)
    kai = Kairos(s, gateway=gw)
    kai.sent = sent
    kai.graph.apply_schema(vectors=False)
    persona = load_persona()
    persona["user"]["id"] = s.user_id
    seed(kai.graph, persona, memory=kai.memory)
    kai.graph.run("set_all_devices_online", uid=s.user_id, online=True)
    kai.refresh_devices()
    yield kai
    kai.graph.wipe()


def _ev(k, type_, source, payload, device="phone-arjun"):
    from kairos.models import EventEnvelope

    return k.ingest(EventEnvelope(user_id=k.settings.user_id, device_id=device, type=type_, source=source,
                                  payload=payload))


def test_uber_watcher_fires_at_threshold(k):
    r = k.chat("Remind me when my Uber is 2 minutes away", "phone-arjun")
    assert "remind you" in r.text
    assert not _ev(k, "notification", "com.ubercab", {"title": "Uber", "body": "Ramesh is 5 min away",
                                                      "trip_id": "t1", "destination": "Office"}).alerts
    out = _ev(k, "notification", "com.ubercab", {"title": "Uber", "body": "Ramesh is 2 min away",
                                                 "trip_id": "t1", "destination": "Office"})
    assert len(out.alerts) == 1 and out.alerts[0]["device"] == "phone-arjun"
    exp = k.chat("why did you remind me", "phone-arjun").text
    assert "Uber" in exp and "2 min" in exp


def test_cross_device_download_recall_and_open(k):
    _ev(k, "notification", "com.google.android.gm", {
        "title": "Rahul Sharma", "subject": "Q3 report", "body": "https://files.example.com/Q3_report.pdf"})
    r = k.chat("Download the report Rahul sent and tell me on my phone when it's done", "pc-main")
    assert "Downloading Q3_report.pdf" in r.text
    download_id = r.data[0]["download_id"]
    _ev(k, "download.started", "chrome", {"download_id": download_id, "filename": "Q3_report.pdf"}, "pc-main")
    out = _ev(k, "download.completed", "chrome", {"download_id": download_id, "filename": "Q3_report.pdf",
                                                  "path": r"C:\Users\arjun\Downloads\Q3_report.pdf"}, "pc-main")
    assert [a["device"] for a in out.alerts] == ["phone-arjun"]
    r = k.chat("What was that PDF Rahul sent me today? Open it.", "phone-arjun")
    assert "Q3_report.pdf" in r.text and "your PC" in r.text
    assert any("OPEN" in s and "pc-main" in s for s in k.sent)


def test_fact_supersede_keeps_history(k):
    assert "Golf Course Road" in k.chat("where is my office", "pc-main").text
    r = k.chat("I've moved to the Cyber City office", "pc-main")
    assert "Updated" in r.text
    assert "Cyber City" in k.chat("where is my office", "pc-main").text
    hist = k.memory.history("office_location")
    assert len(hist) == 2 and hist[0]["valid_to"] is None and hist[1]["valid_to"] is not None


def test_risky_action_needs_confirmation_and_phone_is_denied(k):
    r = k.chat("Delete my temp folder", "pc-main")
    assert r.text.startswith("Just to confirm")
    assert "Deleting" in k.chat("yes", "pc-main").text
    r = k.chat("run Remove-Item foo on my phone", "phone-arjun")
    assert "not allowed" in r.text


def test_device_result_links_back_to_action(k):
    r = k.chat("run Get-Date", "phone-arjun")  # allowlisted: runs without confirmation, on the PC by default
    aid = r.actions[0]
    assert k.graph.one("get_action", id=aid)["status"] == "dispatched"
    before = len(k.sent)
    k.device_result(aid, True, "Saturday, 11 October 2026 10:00:00")
    row = k.graph.one("get_action", id=aid)
    assert row["status"] == "done" and row["origin_device"] == "phone-arjun"
    assert any("phone-arjun" in s and "Command finished: Saturday" in s for s in k.sent[before:])


def test_time_watcher(k):
    k.chat("remind me in 1 second to stretch", "pc-main")
    time.sleep(1.2)
    alerts = k.tick()
    assert any("stretch" in a["text"] for a in alerts)


def test_triage_fulfils_open_ended_request(k):
    r = k.chat("Tell me on my phone when Rahul replies about the budget", "pc-main")
    assert "keep an eye out" in r.text
    before = len(k.sent)
    _ev(k, "notification", "com.slack", {"app": "Slack", "title": "Rahul Sharma",
                                          "body": "Budget numbers are final, need your sign-off ASAP"})
    assert any("phone-arjun" in s and "ALERT" in s for s in k.sent[before:])
    ev = k.graph.run("recent_events")[0]
    assert ev["importance"] == 3


def test_live_status_state_and_changes(k):
    snap = {"apps": ["Outlook", "Chrome"], "foreground_app": "Outlook", "battery": 40, "network": "Wi-Fi Home"}
    _ev(k, "device.status", "status", dict(snap), "pc-main")
    _ev(k, "device.status", "status", {**snap, "apps": ["Chrome", "Acrobat"], "foreground_app": "Chrome"}, "pc-main")
    reply = k.chat("what's open on my laptop", "pc-main").text
    assert "using Chrome" in reply and "Acrobat" in reply and "Outlook" not in reply
    types = [e["type"] for e in k.graph.run("recent_events")[:6]]
    assert {"app.opened", "app.closed", "app.focused"} <= set(types)


def test_request_email_becomes_a_task_and_completes(k):
    _ev(k, "notification", "com.google.android.gm", {"title": "Rahul Sharma", "subject": "Deck",
                                                       "body": "Could you share the updated budget deck by Friday?"})
    reply = k.chat("what do I owe Rahul", "pc-main").text
    assert "share the updated budget deck" in reply
    assert "Marked done" in k.chat("I've shared the budget deck with Rahul", "pc-main").text
    assert "Nothing open" in k.chat("what do I owe Rahul", "pc-main").text


def test_notes_and_hybrid_search(k):
    assert "Noted" in k.chat("note that Rahul seemed stressed about the budget", "pc-main").text
    assert "stressed" in k.chat("who is Rahul", "pc-main").text
    assert "budget" in k.chat("that thing about the budget", "pc-main").text.lower()


def test_meeting_prep_from_calendar(k):
    from kairos.calendar_ics import demo_events

    with k.lock:
        for ev in demo_events():
            k.calendar.upsert(ev)
    text = k.chat("prep me for the Q3 sync", "pc-main").text
    assert "Q3 sync" in text and "Rahul" in text


def test_place_watcher_fires_when_leaving(k):
    k.graph.run("seed_place", id="place-office", name="Office", kind="office", lat=28.4595, lon=77.0950, radius_m=300)
    assert "leave Office" in k.chat("remind me to buy milk when I leave the office", "phone-arjun").text
    _ev(k, "device.status", "status", {"battery": 50, "location": {"lat": 28.4596, "lon": 77.0951}})
    before = len(k.sent)
    _ev(k, "device.status", "status", {"battery": 49, "location": {"lat": 28.43, "lon": 77.08}})
    assert any("buy milk" in s for s in k.sent[before:])


def test_merge_people(k):
    dup = k.ingestor.resolve_person("R. Sharma")
    keep = k.ingestor.resolve_person("Rahul Sharma")
    k.memory.merge_people(keep, dup)
    names = [p["name"] for p in k.memory.people()]
    assert "R. Sharma" not in names and k.ingestor.resolve_person("R. Sharma") == keep


def test_habits_from_history(k):
    from kairos.sim import history

    for env in history(k.settings.user_id, k.settings.tz, weeks=3):
        k.ingest(env)
    res = k.consolidate()
    assert any(h["id"].startswith("trip_to:Office:weekdays:9") for h in res["habits"])
    assert "Office" in k.chat("what are my habits", "pc-main").text


def test_said_in_passing_is_remembered_after_restart_via_the_digest(k, tmp_path):
    """Tell it something casually, 'close the session', digest the day, ask again from a fresh session."""
    from kairos.agent.core import Agent
    from kairos.journal import ChatJournal

    k.journal = ChatJournal(str(tmp_path), k.settings.tz)
    k.chat("my sister Priya lives in Pune", "pc-main")
    k.chat("I'm flying to Goa next Friday", "pc-main")
    assert "Goa" in k.chat("what did I say about Goa", "phone-arjun").text  # own words are searchable right away

    r = k.digest.run(k.journal.today(), force_rules=True)
    assert r["new"] == 3 and r["facts"] >= 1 and r["notes"] >= 1
    assert k.memory.get_fact("sister_priya")["statement"] == "Your sister Priya lives in Pune"
    assert k.graph.run("recent_digests", uid=k.settings.user_id, limit=1)[0]["items"] >= 2  # DERIVED_FROM links

    k.agent = Agent(k, k.llm)  # "restart": no in-memory sessions
    assert "Goa" in k.chat("what did we talk about today", "pc-main").text
    assert k.agent.sessions["session-pc-main"]["history"]  # picked up from the day's log
