"""Offline phrases for the new features (work without the LLM)."""

import pytest

from kairos.agent.intents import parse
from kairos.agent.llm import TurnContext
from kairos.timeutil import now_ms


@pytest.fixture
def ctx():
    return TurnContext(device_id="pc-main", devices={"phone": "phone-arjun", "pc": "pc-main", "chat": "telegram-arjun"},
                       now=now_ms(), tz="Asia/Kolkata")


def first(text, ctx):
    plan = parse(text, ctx)
    assert plan.calls, f"no tool for {text!r}: {plan.reply}"
    return plan.calls[0]


@pytest.mark.parametrize("text,tool,args", [
    ("remind me to buy milk when I leave the office", "watch_place",
     {"place": "office", "transition": "left", "message": "buy milk"}),
    ("remind me to call mom when I get home", "watch_place", {"place": "home", "transition": "entered"}),
    ("when I get to the office, remind me to print the deck", "watch_place",
     {"place": "office", "transition": "entered", "message": "print the deck"}),
    ("tell me when I reach home", "watch_place", {"place": "home", "transition": "entered"}),
    ("what do I owe Rahul", "list_tasks", {"person": "Rahul"}),
    ("my tasks", "list_tasks", {"person": None}),
    ("add a task to book flights by Friday", "add_task", {"title": "book flights by Friday"}),
    ("I've sent the deck to Rahul", "complete_task", {"query": "the deck to rahul"}),
    ("mark the budget deck as done", "complete_task", {"query": "the budget deck"}),
    ("prep me for my next meeting", "meeting_prep", {"query": None}),
    ("prep me for the Q3 sync", "meeting_prep", {"query": "q3"}),
    ("what's on my calendar today", "upcoming_meetings", {"period": "today"}),
    ("who matters most to me right now", "important_people", {}),
    ("send it to my phone", "send_file", {"query": None, "to_device": "phone"}),
    ("send the Q3 report to my laptop", "send_file", {"query": "q3 report", "to_device": "laptop"}),
    ("send my clipboard to my phone", "send_clipboard", {"to_device": "phone"}),
    ("tell the family the plumber comes at 5", "share_with_family", {"statement": "the plumber comes at 5"}),
    ("what did the family share", "family_memory", {}),
    ("note that Rahul seemed stressed about the budget", "add_note",
     {"text": "Rahul seemed stressed about the budget"}),
    ("that thing about the budget", "search_memory", {"q": "budget"}),
    ("what site was I reading this morning", "recent_sites", {"period": "today"}),
])
def test_new_phrases(ctx, text, tool, args):
    c = first(text, ctx)
    assert c.name == tool, (text, c)
    for k, v in args.items():
        assert c.args.get(k) == v, (text, k, c.args)


def test_older_phrases_still_route_the_same(ctx):
    assert first("remind me when my Uber is 2 minutes away", ctx).name == "watch_ride"
    assert first("tell me on my phone when Rahul replies about the budget", ctx).name == "watch_for"
    assert first("brief me", ctx).name == "briefing"
    assert first("download the report Rahul sent and tell me on my phone when it's done", ctx).name \
        == "download_from_person"
    assert first("open the link I just got on WhatsApp on my laptop", ctx).name == "open_link"


def test_telegram_device_phrase(ctx):
    assert first("tell me on telegram when the download finishes", ctx).args["notify_device"] == "telegram-arjun"
