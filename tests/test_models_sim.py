from agents.phone_agent import parse_dumpsys
from agents.pc_agent import DownloadWatcher, strip_temp
from kairos.models import EventEnvelope
from kairos.seed import load_persona
from kairos.sim import history, load_scenario, scenario_events


def test_envelope_text_and_hash_stable():
    a = EventEnvelope(user_id="arjun", device_id="phone-arjun", type="notification", source="com.ubercab",
                      payload={"app": "Uber", "title": "Uber", "body": "4 min away", "key": "k1"}, ts=1)
    b = a.model_copy(update={"ts": 2, "event_id": "other"})
    assert a.text() == "Uber: Uber — 4 min away"
    assert a.content_hash() == b.content_hash()  # same notification key -> dedupe across polls


def test_scenarios_parse():
    for name in ("morning", "download"):
        events = list(scenario_events(load_scenario(name), "arjun", start_ms=0))
        assert events and all(isinstance(e, EventEnvelope) for _, e in events)


def test_history_has_weekday_rides():
    events = history("arjun", "Asia/Kolkata", weeks=3)
    rides = [e for e in events if e.source == "com.ubercab"]
    assert len(rides) >= 4 * 12  # >= 12 weekdays x 4 notifications


def test_persona_permissions_reference_known_devices_and_caps():
    p = load_persona()
    devices = {d["id"] for d in p["devices"]}
    caps = {c["name"] for c in p["capabilities"]}
    for device, perms in p["permissions"].items():
        assert device in devices
        assert set(perms) <= caps
        assert set(perms.values()) <= {"auto", "confirm", "deny"}


def test_pc_download_watcher_chrome_flow():
    events = []
    w = DownloadWatcher(folder=None, emit=lambda t, p: events.append((t, p)))
    w.created(r"C:\d\Unconfirmed 123.crdownload")
    w.moved(r"C:\d\Unconfirmed 123.crdownload", r"C:\d\Q3_report.pdf.crdownload")
    w.moved(r"C:\d\Q3_report.pdf.crdownload", r"C:\d\Q3_report.pdf")
    assert [t for t, _ in events] == ["download.started", "download.completed"]
    assert events[0][1]["download_id"] == events[1][1]["download_id"]
    assert events[1][1]["filename"] == "Q3_report.pdf"
    assert strip_temp("a.pdf.part") == "a.pdf"


def test_hub_initiated_download_keeps_hub_id():
    events = []
    w = DownloadWatcher(folder=None, emit=lambda t, p: events.append((t, p)))
    w.claims[r"C:\d\r.pdf.part"] = ("dl-hub", "https://x/r.pdf")
    w.created(r"C:\d\r.pdf.part")
    w.moved(r"C:\d\r.pdf.part", r"C:\d\r.pdf")
    assert {p["download_id"] for _, p in events} == {"dl-hub"}
    assert events[0][1]["url"] == "https://x/r.pdf"


def test_parse_dumpsys():
    text = """
  NotificationRecord(0x0abc: pkg=com.ubercab user=UserHandle{0} id=1 tag=null importance=4 key=0|com.ubercab|1)
      when=1760000000000
        android.title=String (Uber)
        android.text=String (Ramesh is 2 min away)
  NotificationRecord(0x0def: pkg=com.whatsapp user=UserHandle{0} id=2)
        android.title=String (Priya Mehta)
        android.text=String (hi)
"""
    out = parse_dumpsys(text)
    assert [n["package"] for n in out] == ["com.ubercab", "com.whatsapp"]
    assert out[0]["body"] == "Ramesh is 2 min away" and out[0]["title"] == "Uber"
