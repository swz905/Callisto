"""OpenRouter planner/extractor against a fake HTTP server: no API key or network needed."""

import json

import httpx
import pytest

from kairos.agent.llm import OpenRouterExtractor, OpenRouterPlanner, TurnContext, make_llm
from kairos.agent.openrouter import LLMUnavailable, OpenRouterClient
from kairos.agent.tools import TOOL_SPECS, openai_tools
from kairos.config import load_settings
from kairos.timeutil import now_ms


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODELS", "test/backup")
    monkeypatch.setenv("KAIROS_LLM", "auto")
    return load_settings()


def ctx():
    return TurnContext(device_id="pc-main", devices={"pc": "pc-main", "phone": "phone-arjun"}, now=now_ms(),
                       tz="Asia/Kolkata", card="Devices: pc = pc-main, phone = phone-arjun")


def tool_call(name, args, cid="c1"):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def text(content):
    return {"choices": [{"message": {"content": content}}]}


def test_tool_loop_executes_and_feeds_results_back(settings):
    requests = []
    replies = iter([tool_call("watch_ride", {"provider": "Uber", "eta_threshold": 2, "notify_device": "phone"}),
                    text("Okay, I'll ping your phone when the Uber is two minutes away.")])

    def handler(req):
        requests.append(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer sk-or-test"
        return httpx.Response(200, json=next(replies))

    client = OpenRouterClient(settings, transport=httpx.MockTransport(handler))
    executed = []
    out = OpenRouterPlanner(client, openai_tools()).run(
        "remind me on my phone when my uber is 2 min away", ctx(),
        lambda name, args: executed.append((name, args)) or "Got it. I'll remind you when your Uber is 2 minutes away.")

    assert executed == [("watch_ride", {"provider": "Uber", "eta_threshold": 2, "notify_device": "phone"})]
    assert out.startswith("Okay")
    first, second = requests
    assert first["model"] == "test/model" and first["models"] == ["test/model", "test/backup"]
    assert {t["function"]["name"] for t in first["tools"]} == set(TOOL_SPECS)
    assert "CONTEXT" in first["messages"][0]["content"]
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "c1",
                                      "content": "Got it. I'll remind you when your Uber is 2 minutes away."}


def test_bad_tool_arguments_do_not_crash(settings):
    replies = iter([{"choices": [{"message": {"tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "briefing", "arguments": "{not json"}}]}}]},
        text("Here's your day.")])
    client = OpenRouterClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(replies))))
    seen = []
    assert OpenRouterPlanner(client, openai_tools()).run("brief me", ctx(),
                                                         lambda n, a: seen.append((n, a)) or "ok") == "Here's your day."
    assert seen == [("briefing", {})]


def test_offline_raises_and_opens_circuit(settings):
    def handler(req):
        raise httpx.ConnectError("no internet")

    client = OpenRouterClient(settings, transport=httpx.MockTransport(handler))
    assert client.available()
    with pytest.raises(LLMUnavailable):
        client.chat([{"role": "user", "content": "hi"}])
    assert not client.available()  # next turns go straight to the rule planner
    assert client.status()["online"] is False and "network" in client.status()["reason"]


def test_auth_error_long_cooldown(settings):
    client = OpenRouterClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(LLMUnavailable, match="401"):
        client.chat([{"role": "user", "content": "hi"}])
    assert client.status()["retry_in_s"] > 200


def test_not_configured_means_rules(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("KAIROS_LLM", "auto")
    stack = make_llm(load_settings())
    assert stack.planner is not None and not stack.planner.available()
    assert stack.status()["online"] is False and "OPENROUTER_API_KEY" in stack.status()["reason"]


def test_rules_mode_never_builds_a_client(monkeypatch):
    monkeypatch.setenv("KAIROS_LLM", "rules")
    stack = make_llm(load_settings())
    assert stack.client is None and stack.planner is None


def test_extractor_uses_rules_first_and_llm_for_unknown_apps(settings):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=text('{"category": "work", "is_message": true, "sender": "Meera Iyer", '
                                             '"subject": "Standup moved", "people": ["Meera Iyer"]}'))

    ex = OpenRouterExtractor(OpenRouterClient(settings, transport=httpx.MockTransport(handler)), "test/cheap")
    uber = ex.extract_notification("com.ubercab", {"title": "Uber", "body": "Ramesh is 3 min away"})
    assert uber.kind == "ride" and not calls  # rules handled it; no LLM call
    other = ex.extract_notification("com.jira", {"app": "Jira", "title": "Meera Iyer", "body": "Standup moved to 10"})
    assert calls and other.kind == "message"
    assert other.message["sender"] == "Meera Iyer" and other.category == "work"


def test_extractor_offline_falls_back_to_rules(settings):
    def handler(req):
        raise httpx.ConnectTimeout("timeout")

    ex = OpenRouterExtractor(OpenRouterClient(settings, transport=httpx.MockTransport(handler)), None)
    out = ex.extract_notification("com.jira", {"app": "Jira", "title": "x", "body": "y"})
    assert out.kind == "other"
