"""Live device status: diffing, state props, descriptions, collectors' parsers, tracker effects."""

import threading
from types import SimpleNamespace

from agents.pc_status import friendly_app, parse_netsh
from agents.phone_agent import parse_adb_battery, parse_adb_focus, parse_adb_wifi
from kairos.agent.intents import parse
from kairos.agent.llm import TurnContext
from kairos.models import EventEnvelope
from kairos.status import StatusTracker, describe_status, diff_status, state_props
from kairos.timeutil import MINUTE_MS, now_ms

PREV = {"status_ts": 1, "apps": ["Outlook", "Chrome", "Teams"], "foreground_app": "Outlook", "battery": 25,
        "network": "Wi-Fi Home"}


def test_first_snapshot_creates_no_events():
    assert diff_status(None, {"apps": ["Chrome"], "battery": 10}) == []
    assert diff_status({"status_ts": None}, {"apps": ["Chrome"]}) == []


def test_diff_apps_focus_battery_network():
    new = {"apps": ["Chrome", "Teams", "Acrobat"], "foreground_app": "Chrome", "foreground_title": "Q3.pdf",
           "battery": 18, "charging": False, "network": "mobile data/offline"}
    assert diff_status(PREV, new) == [
        ("app.opened", {"app": "Acrobat"}),
        ("app.closed", {"app": "Outlook"}),
        ("app.focused", {"app": "Chrome", "title": "Q3.pdf"}),
        ("battery.low", {"battery": 18}),
        ("network.changed", {"from": "Wi-Fi Home", "to": "mobile data/offline"}),
    ]


def test_battery_low_only_once_and_not_while_charging():
    assert not [c for c in diff_status({**PREV, "battery": 15}, {"battery": 12}) if c[0] == "battery.low"]
    assert not [c for c in diff_status(PREV, {"battery": 12, "charging": True}) if c[0] == "battery.low"]


def test_snapshot_without_apps_key_doesnt_close_everything():
    assert diff_status(PREV, {"battery": 24}) == []  # phone snapshots have no app list


def test_state_props():
    p = state_props({"foreground_app": None, "battery": 50, "charging": 1, "apps": ["x"], "junk": 1}, 99)
    assert p == {"status_ts": 99, "foreground_app": "", "battery": 50, "charging": True}


def test_describe_status():
    now = now_ms()
    line = describe_status({"name": "Arjun's laptop", "status_ts": now - 2 * MINUTE_MS, "foreground_app": "VS Code",
                            "foreground_title": "core.py", "apps": ["VS Code", "Chrome", "Slack"], "battery": 80,
                            "charging": True, "network": "Wi-Fi Home", "idle_s": 600, "mem": 61.0}, now)
    assert line.startswith("Arjun's laptop (2 min ago)")
    for part in ("using VS Code", "core.py", "also open: Chrome, Slack", "battery 80% charging", "idle 10 min"):
        assert part in line
    assert "[stale]" in describe_status({"id": "x", "status_ts": now - 30 * MINUTE_MS, "battery": 5}, now)
    assert "no status" in describe_status({"id": "x"}, now)


def test_friendly_app_names():
    assert friendly_app("chrome.exe") == "Chrome" and friendly_app("WINWORD.EXE") == "Word"
    assert friendly_app("TextInputHost.exe") is None
    assert friendly_app("ApplicationFrameHost.exe", "Inbox - Mail") == "Mail"
    assert friendly_app("mytool.exe") == "Mytool"


def test_parsers():
    assert parse_netsh("    State                  : connected\n    SSID                   : Home-5G\n    BSSID : x") \
        == "Wi-Fi Home-5G"
    assert parse_netsh("    State : disconnected\n") == ""
    assert parse_adb_battery("  AC powered: false\n  USB powered: true\n  status: 2\n  level: 76\n") \
        == {"battery": 76, "charging": True}
    assert parse_adb_focus("  mCurrentFocus=Window{1a2b u0 com.whatsapp/com.whatsapp.HomeActivity}") == "com.whatsapp"
    assert parse_adb_wifi('mWifiInfo SSID: "Home-5G", BSSID: aa') == "Wi-Fi Home-5G"


def test_status_intents():
    ctx = TurnContext(device_id="pc-main", devices={"phone": "phone-arjun", "pc": "pc-main"}, now=now_ms(),
                      tz="Asia/Kolkata")
    assert parse("what's open on my laptop", ctx).calls[0].args == {"device": "pc-main"}
    assert parse("how much battery does my phone have", ctx).calls[0].args == {"device": "phone-arjun"}
    assert parse("device status", ctx).calls[0].name == "device_status"
    # a request about battery is a watcher, not a status query
    assert parse("tell me when my phone battery is low", ctx).calls[0].name == "watch_for"


def test_tracker_updates_state_and_ingests_changes():
    calls, ingested = [], []
    graph = SimpleNamespace(
        one=lambda name, **p: calls.append(name) or dict(PREV),
        run=lambda name, **p: calls.append((name, p)) or [])
    k = SimpleNamespace(graph=graph, lock=threading.RLock(), settings=SimpleNamespace(user_id="arjun"),
                        ingest=lambda env: ingested.append(env) or SimpleNamespace(alerts=[]))
    env = EventEnvelope(user_id="arjun", device_id="pc-main", type="device.status",
                        payload={"apps": ["Chrome", "Teams"], "foreground_app": "Chrome", "battery": 60})
    out = StatusTracker(k).apply(env)
    names = [c[0] if isinstance(c, tuple) else c for c in calls]
    assert names == ["get_device_status", "set_device_status", "stop_running_apps", "start_running_apps"]
    assert [e.type for e in ingested] == ["app.closed", "app.focused"]
    assert out.duplicate is False
