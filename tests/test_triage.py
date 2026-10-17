"""Notification triage: rules + LLM decisions and their effects, with a fake graph and fake OpenRouter."""

import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from kairos.agent.intents import parse
from kairos.agent.llm import TurnContext, make_llm
from kairos.config import load_settings
from kairos.models import EventEnvelope
from kairos.timeutil import DAY_MS, now_ms
from kairos.triage import Triage, keyword_match, rules_triage

PEOPLE = [{"name": "Rahul Sharma", "relation": "manager"}, {"name": "Priya Mehta", "relation": "sister"},
          {"name": "Karan Gupta", "relation": "colleague"}]
BUDGET_WATCH = {"id": "watch-b1", "description": "Rahul replies about the budget", "notify_device": "phone-arjun"}


class FakeGraph:
    def __init__(self, watchers):
        self.watchers = watchers
        self.calls = []

    def run(self, name, **p):
        self.calls.append((name, p))
        return {"semantic_watchers": self.watchers, "list_people": PEOPLE, "recent_utterances": [
            {"text": "tell me when Rahul replies about the budget", "ts": now_ms() - 60_000}]}.get(name, [])

    def one(self, name, **p):
        self.calls.append((name, p))
        if name == "fire_watcher":
            w = next(w for w in self.watchers if w["id"] == p["watcher_id"])
            return {"watcher_id": w["id"], "description": w["description"], "notify_device": w["notify_device"],
                    "event_text": "Slack: Rahul Sharma — Budget numbers are final"}
        return None

    def names(self):
        return [n for n, _ in self.calls]


def make_k(monkeypatch, watchers=(), handler=None, triage="auto"):
    if handler:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    else:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("KAIROS_TRIAGE", triage)
    s = load_settings()
    sent, records = [], []
    k = SimpleNamespace(settings=s, graph=FakeGraph(list(watchers)), lock=threading.RLock(),
                        llm=make_llm(s, transport=httpx.MockTransport(handler) if handler else None),
                        gateway=SimpleNamespace(send=sent.append),
                        audit=SimpleNamespace(record=lambda *a, **kw: records.append(a) or f"act-{len(records)}"),
                        _route=lambda d: d or "phone-arjun")
    k.sent, k.records = sent, records
    return k


def note(app, title, body, ts=None):
    return EventEnvelope(user_id="arjun", device_id="phone-arjun", type="notification", source=app.lower(),
                         payload={"app": app, "title": title, "body": body}, ts=ts or now_ms())


# --- pure rules -------------------------------------------------------------------------
def test_keyword_match():
    assert keyword_match("Rahul replies about the budget", "Slack: Rahul Sharma — Budget numbers are final")
    assert not keyword_match("Rahul replies about the budget", "Slack: Rahul Sharma — lunch?")
    assert not keyword_match("tell me when", "anything")


def test_rules_importance():
    assert rules_triage("Amazon: Big sale — 70% off", [], PEOPLE).importance == 0
    assert rules_triage("WhatsApp: Priya Mehta — call me, urgent", [], PEOPLE).importance == 3
    assert rules_triage("Bank: OTP 482113 for login", [], PEOPLE).importance == 2
    assert rules_triage("WhatsApp: Priya Mehta — dinner?", [], PEOPLE).importance == 2
    assert rules_triage("Karan Gupta: urgent", [], PEOPLE).importance == 1  # colleague: not close, no request
    r = rules_triage("Slack: Rahul Sharma — Budget final, ASAP", [BUDGET_WATCH], PEOPLE)
    assert r.importance == 3 and r.fulfills == ["watch-b1"]


def test_open_ended_request_becomes_semantic_watcher():
    ctx = TurnContext(device_id="pc-main", devices={"phone": "phone-arjun", "pc": "pc-main"}, now=now_ms(),
                      tz="Asia/Kolkata")
    c = parse("Tell me on my phone when Rahul replies about the budget", ctx).calls[0]
    assert c.name == "watch_for" and c.args == {"description": "Rahul replies about the budget",
                                                "notify_device": "phone-arjun"}
    # specific watchers still win
    assert parse("tell me when the download finishes", ctx).calls[0].name == "watch_download"


# --- effects (offline rules) -----------------------------------------------------------------
def test_rules_fulfil_watcher_alerts_once(monkeypatch):
    k = make_k(monkeypatch, watchers=[BUDGET_WATCH])
    res = Triage(k).process(note("Slack", "Rahul Sharma", "Budget numbers are final, need your sign-off ASAP"))
    assert res.by == "rules" and res.fulfills == ["watch-b1"] and res.importance == 3
    assert "fire_watcher" in k.graph.names() and "set_triage" in k.graph.names()
    assert len(k.sent) == 1 and k.sent[0].device_id == "phone-arjun"  # fulfilment alert only, no duplicate


def test_noise_is_recorded_not_alerted(monkeypatch):
    k = make_k(monkeypatch)
    res = Triage(k).process(note("Amazon", "Big Billion Sale", "Up to 70% off. Limited time offer!"))
    assert res.importance == 0 and not k.sent
    assert ("set_triage", res) and k.graph.calls[-1][0] == "set_triage"


def test_old_events_and_other_types_skipped(monkeypatch):
    k = make_k(monkeypatch)
    t = Triage(k)
    assert t.process(note("WhatsApp", "Priya Mehta", "urgent call me", ts=now_ms() - DAY_MS)) is None
    utter = EventEnvelope(user_id="arjun", device_id="pc-main", type="utterance", payload={"text": "hi"})
    assert not t.eligible(utter)
    assert not k.graph.calls


def test_triage_off(monkeypatch):
    k = make_k(monkeypatch, triage="off")
    assert Triage(k).process(note("WhatsApp", "Priya Mehta", "urgent call me")) is None


def test_already_alerted_by_exact_watcher_no_extra_alert(monkeypatch):
    k = make_k(monkeypatch)
    Triage(k).process(note("WhatsApp", "Priya Mehta", "urgent, call me"), already_alerted=True)
    assert not k.sent


# --- LLM path ------------------------------------------------------------------------------
def llm_reply(obj):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(obj)}}]})


def test_llm_fulfils_with_context_and_ignores_unknown_ids(monkeypatch):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return llm_reply({"importance": 3, "reason": "Rahul answered your budget question",
                          "fulfills": ["watch-b1", "watch-made-up"], "message": "Rahul replied about the budget."})

    k = make_k(monkeypatch, watchers=[BUDGET_WATCH], handler=handler)
    res = Triage(k).process(note("Teams", "Rahul S.", "numbers for Q3 spend are locked"))  # rules wouldn't match
    assert res.by == "llm" and res.fulfills == ["watch-b1"]
    assert k.sent[0].text == "Rahul replied about the budget."
    prompt = seen[0]["messages"][1]["content"]
    assert "watch-b1: Rahul replies about the budget" in prompt and "Rahul Sharma (manager)" in prompt
    assert "USER SAID RECENTLY" in prompt


def test_llm_important_without_request(monkeypatch):
    k = make_k(monkeypatch, handler=lambda r: llm_reply({"importance": 3, "reason": "flight cancelled",
                                                         "fulfills": [], "message": "Your flight was cancelled."}))
    Triage(k).process(note("IndiGo", "Flight 6E-201", "Your flight has been cancelled"))
    assert [m.text for m in k.sent] == ["Your flight was cancelled."]


def test_llm_offline_falls_back_to_rules(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("offline")

    k = make_k(monkeypatch, watchers=[BUDGET_WATCH], handler=handler)
    res = Triage(k).process(note("Slack", "Rahul Sharma", "Budget is final"))
    assert res.by == "rules" and res.fulfills == ["watch-b1"]


def test_rate_limit_switches_to_rules(monkeypatch):
    monkeypatch.setenv("KAIROS_TRIAGE_MAX_PER_MIN", "1")
    k = make_k(monkeypatch, handler=lambda r: llm_reply({"importance": 1, "reason": "x", "fulfills": []}))
    t = Triage(k)
    assert t.process(note("A", "t", "one")).by == "llm"
    assert t.process(note("A", "t", "two")).by == "rules"
