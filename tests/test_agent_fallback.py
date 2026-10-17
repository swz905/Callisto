"""Agent planner selection: LLM when online, rules when offline, no double execution, deterministic yes/no."""

import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from kairos.agent.core import Agent
from kairos.agent.llm import make_llm
from kairos.agent.tools import ToolResult
from kairos.config import load_settings
from kairos.policy import Decision


class FakeK:
    def __init__(self, settings, pending=None):
        self.settings = settings
        self.devices = {"pc": "pc-main", "phone": "phone-arjun"}
        self.lock = threading.RLock()
        self.records = []
        self.record_meta = []
        self.updates = []
        self._pending = pending
        self.graph = SimpleNamespace(run=lambda *a, **k: [], one=lambda *a, **k: None,
                                     run_template=lambda *a, **k: [])
        check = lambda device, cap: (Decision.CONFIRM if cap in ("file.delete", "shell.exec") else Decision.AUTO, 0)  # noqa: E731
        self.policy = SimpleNamespace(check=check, check_pair=lambda target, origin, cap: check(target, cap))
        self.tasks = SimpleNamespace(open=lambda person=None: [], from_utterance=lambda text, eid: [])
        self.calendar = SimpleNamespace(upcoming=lambda since, until: [])
        self.plugins = SimpleNamespace(has=lambda name: False)
        self._ids = iter(f"act-{i}" for i in range(1, 100))
        self.audit = SimpleNamespace(pending=lambda: self._pending, record=self._record,
                                     new_action_id=lambda: next(self._ids),
                                     update=lambda aid, status, result=None: self.updates.append((aid, status)))
        self.watchers = SimpleNamespace(list_active=lambda: [])
        self.memory = SimpleNamespace(habits=lambda min_strength=0.3: [])
        self.status = SimpleNamespace(all=lambda: [])

    def ingest(self, env):
        return SimpleNamespace(event_id=env.event_id)

    def _record(self, tool, args, cap, status, summary, device, causes, result=None, action_id=None,
                origin_device=None, tainted=False, taint_sources=None, influenced_by=()):
        self.records.append((tool, status, args))
        self.record_meta.append({"action_id": action_id, "origin_device": origin_device, "tainted": tainted,
                                 "influenced_by": list(influenced_by), "summary": summary})
        return action_id


def make_agent(monkeypatch, handler=None, pending=None, key=True):
    if key:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    else:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("KAIROS_LLM", "auto")
    settings = load_settings()
    transport = httpx.MockTransport(handler) if handler else None
    k = FakeK(settings, pending)
    agent = Agent(k, make_llm(settings, transport=transport))
    ran = []
    agent.toolbox.run = lambda name, args, turn: ran.append((name, args)) or ToolResult(f"ran {name}")
    return agent, k, ran


def tool_call(name, args):
    return {"choices": [{"message": {"tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def test_online_uses_llm_and_normalises_device_kinds(monkeypatch):
    replies = iter([tool_call("watch_download", {"notify_device": "phone"}),
                    {"choices": [{"message": {"content": "I'll tell your phone."}}]}])
    agent, k, ran = make_agent(monkeypatch, lambda r: httpx.Response(200, json=next(replies)))
    reply = agent.handle("let me know on my phone once the download is done", "pc-main")
    assert reply.planner == "openrouter" and reply.text == "I'll tell your phone."
    assert ran == [("watch_download", {"notify_device": "phone-arjun"})]
    assert k.records[0][:2] == ("watch_download", "done")


def test_offline_falls_back_to_rules(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("offline")

    agent, k, ran = make_agent(monkeypatch, handler)
    reply = agent.handle("remind me when my Uber is 2 minutes away", "phone-arjun")
    assert reply.planner == "rules"
    assert ran == [("watch_ride", {"provider": "Uber", "eta_threshold": 2, "notify_device": "phone-arjun"})]
    # circuit is open now: the next turn doesn't even try the network
    assert not agent.llm.planner.available()


def test_no_key_runs_rules(monkeypatch):
    agent, k, ran = make_agent(monkeypatch, key=False)
    assert agent.handle("brief me", "pc-main").planner == "rules"
    assert ran == [("briefing", {})]


def test_failure_after_tool_ran_does_not_rerun_with_rules(monkeypatch):
    calls = iter([httpx.Response(200, json=tool_call("briefing", {})), None])

    def handler(req):
        r = next(calls)
        if r is None:
            raise httpx.ReadTimeout("slow")
        return r

    agent, k, ran = make_agent(monkeypatch, handler)
    reply = agent.handle("brief me", "pc-main")
    assert ran == [("briefing", {})]  # exactly once
    assert reply.planner == "openrouter(partial)" and reply.text == "ran briefing"


def test_risky_tool_from_llm_still_needs_confirmation(monkeypatch):
    replies = iter([tool_call("delete_path", {"path": "temp folder", "device": "pc"}),
                    {"choices": [{"message": {"content": "Just to confirm: delete temp folder on your PC?"}}]}])
    agent, k, ran = make_agent(monkeypatch, lambda r: httpx.Response(200, json=next(replies)))
    agent.handle("clean up my temp folder", "pc-main")
    assert ran == []  # not executed
    assert k.records[0][:2] == ("delete_path", "pending_confirmation")


def test_yes_is_deterministic_even_when_llm_online(monkeypatch):
    pending = {"id": "act-9", "tool": "delete_path", "args_json": json.dumps({"path": "temp"}), "device_id": "pc-main"}

    def handler(req):
        raise AssertionError("the LLM must not decide confirmations")

    agent, k, ran = make_agent(monkeypatch, handler, pending=pending)
    agent.handle("yes", "pc-main")
    assert ran == [("delete_path", {"path": "temp"})]
    assert k.updates == [("act-9", "done")]


def test_history_is_kept_for_follow_ups(monkeypatch):
    agent, k, ran = make_agent(monkeypatch, key=False)
    agent.handle("brief me", "pc-main")
    agent.handle("what are my habits", "pc-main")
    hist = agent.sessions["session-pc-main"]["history"]
    assert [m["role"] for m in hist] == ["user", "assistant", "user", "assistant"]
