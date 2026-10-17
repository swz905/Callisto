"""Device results flow back to the Action that asked for them, and the user hears the outcome."""

import json
import threading
import time
from types import SimpleNamespace

from kairos.agent.tools import ToolResult
from kairos.gateway import LocalGateway
from kairos.models import Outbound
from kairos.runtime import Kairos, result_message
from test_agent_fallback import make_agent


def test_action_id_exists_before_tool_runs_and_status_is_dispatched(monkeypatch):
    agent, k, _ = make_agent(monkeypatch, key=False)
    seen = []

    def run(name, args, turn):
        seen.append(turn.action_id)
        return ToolResult("Running `Get-Process` on your PC.", {"dispatched": True})

    agent.toolbox.run = run
    reply = agent.handle("run Get-Process", "phone-arjun")
    assert seen[0] and seen[0] == reply.actions[0] == k.record_meta[0]["action_id"]
    assert k.records[0][:2] == ("run_command", "dispatched")
    assert k.record_meta[0]["origin_device"] == "phone-arjun"  # where the user asked, not where it runs


def test_confirmed_action_keeps_its_id(monkeypatch):
    pending = {"id": "act-77", "tool": "delete_path", "args_json": json.dumps({"path": "temp"}),
               "device_id": "pc-main"}
    agent, k, _ = make_agent(monkeypatch, key=False, pending=pending)
    seen = []
    agent.toolbox.run = lambda n, a, turn: seen.append(turn.action_id) or ToolResult("Deleting.", {"dispatched": True})
    agent.handle("yes", "pc-main")
    assert seen == ["act-77"] and k.updates == [("act-77", "dispatched")]


def fake_runtime(row):
    sent, updates = [], []
    fake = SimpleNamespace(
        lock=threading.RLock(),
        graph=SimpleNamespace(one=lambda name, **p: dict(row) if row else None),
        audit=SimpleNamespace(update=lambda aid, status, result=None: updates.append((aid, status, result))),
        gateway=SimpleNamespace(send=sent.append),
        agent=SimpleNamespace(followup=lambda row, ok, out: None),
        _route=lambda d: d or "phone-arjun")
    return fake, sent, updates


def test_device_result_updates_action_and_tells_origin_device():
    row = {"id": "act-1", "tool": "run_command", "status": "dispatched", "summary": "run `Get-Process` on your PC",
           "device_id": "pc-main", "origin_device": "phone-arjun"}
    fake, sent, updates = fake_runtime(row)
    out = Kairos.device_result(fake, "act-1", True, "\n  Handles  NPM(K)\n  ------\n")
    assert updates == [("act-1", "done", "\n  Handles  NPM(K)\n  ------\n")]
    assert out["device"] == "phone-arjun" and sent[0].device_id == "phone-arjun"
    assert sent[0].text == "Command finished: Handles  NPM(K) | ------" and sent[0].action_id == "act-1"


def test_device_failure_is_reported():
    row = {"id": "act-2", "tool": "delete_path", "status": "dispatched", "summary": "delete temp",
           "device_id": "pc-main", "origin_device": "pc-main"}
    fake, sent, updates = fake_runtime(row)
    Kairos.device_result(fake, "act-2", False, "refused: outside sandbox")
    assert updates[0][1] == "failed" and sent[0].text == "Couldn't delete it: refused: outside sandbox"


def test_successful_download_stays_quiet_and_unknown_or_cancelled_actions_ignored():
    fake, sent, _ = fake_runtime({"id": "a", "tool": "download_from_person", "status": "dispatched",
                                  "summary": "", "device_id": "pc-main", "origin_device": "pc-main"})
    Kairos.device_result(fake, "a", True, r"C:\x.pdf")
    assert not sent  # the download watcher already alerts
    fake, sent, updates = fake_runtime(None)
    assert Kairos.device_result(fake, "nope", True, "x") is None and not updates
    fake, sent, updates = fake_runtime({"id": "c", "tool": "run_command", "status": "cancelled", "summary": "",
                                        "device_id": "pc-main", "origin_device": "pc-main"})
    Kairos.device_result(fake, "c", True, "late output")
    assert updates == [("c", "cancelled", "late output")] and not sent


def test_result_messages():
    assert result_message("run_command", True, "", "x") == "Command finished."
    assert result_message("run_command", False, "boom\nmore", "x") == "The command failed: boom"
    assert result_message("delete_path", True, "", "x") == "Deleted."
    assert result_message("open_file", True, "", "x") is None


def test_simulated_devices_report_results():
    got = []
    gw = LocalGateway(printer=lambda s: None, on_result=lambda aid, ok, out: got.append((aid, ok, out)))
    gw.send(Outbound(kind="exec", device_id="pc-main", args={"cmd": "Get-Date"}, action_id="act-9"))
    gw.send(Outbound(kind="exec", device_id="pc-main", args={"cmd": "Get-Date"}))  # no id: nothing to report
    time.sleep(0.8)
    assert got == [("act-9", True, "(simulated) ran Get-Date")]


def test_phone_tools_and_ui_messaging(monkeypatch):
    agent, k, _ = make_agent(monkeypatch, key=False)
    from kairos.agent.tools import Toolbox
    toolbox = Toolbox(k)
    k.gateway = SimpleNamespace(send=lambda msg: setattr(k.gateway, "last", msg), last=None)
    turn = SimpleNamespace(device_id="pc-main", action_id="act-phone-1")

    # 1. Test PC send_ui_message
    res = toolbox.run("send_ui_message", {"message": "hello", "recipient": "Arjun", "app": "WhatsApp"}, turn)
    assert res.data["dispatched"] is True
    assert k.gateway.last.kind == "type_message"
    assert k.gateway.last.args["recipient"] == "Arjun"

    # 2. Test Phone send chat
    res = toolbox.run("phone_send_chat", {"recipient": "Rahul", "message": "hey", "channel": "whatsapp"}, turn)
    assert res.data["dispatched"] is True
    assert k.gateway.last.kind == "chat_send"
    assert k.gateway.last.args["recipient"] == "Rahul"

    # 3. Test Phone UI tap & key & screenshot & dump
    res = toolbox.run("phone_ui_tap", {"x": 100, "y": 200}, turn)
    assert k.gateway.last.kind == "tap"
    assert k.gateway.last.args == {"x": 100, "y": 200}

    res = toolbox.run("phone_ui_key", {"key": "home"}, turn)
    assert k.gateway.last.kind == "key"
    assert k.gateway.last.args == {"key": "home"}

    res = toolbox.run("phone_screenshot", {}, turn)
    assert k.gateway.last.kind == "screenshot"

    res = toolbox.run("phone_ui_dump", {}, turn)
    assert k.gateway.last.kind == "ui_dump"

    # 4. Test PC read_notifications
    res = toolbox.run("read_notifications", {"app": "Telegram", "query": "Anushka"}, turn)
    assert res.data["dispatched"] is True
    assert k.gateway.last.kind == "notifications_get"
    assert k.gateway.last.args["app"] == "Telegram"
    assert k.gateway.last.args["query"] == "Anushka"

    # 5. Test PC take_screenshot
    res = toolbox.run("take_screenshot", {}, turn)
    assert res.data["dispatched"] is True
    assert k.gateway.last.kind == "screenshot"


