import pytest

from kairos.agent.intents import fact_key, parse
from kairos.agent.llm import TurnContext
from kairos.timeutil import now_ms


@pytest.fixture
def ctx():
    return TurnContext(device_id="pc-main", devices={"phone": "phone-arjun", "pc": "pc-main"}, now=now_ms(),
                       tz="Asia/Kolkata")


def first(text, ctx):
    plan = parse(text, ctx)
    assert plan.calls, f"no tool for {text!r}: {plan.reply}"
    return plan.calls[0]


def test_download_watcher_routes_to_phone(ctx):
    c = first("Tell me on my phone when the download finishes", ctx)
    assert c.name == "watch_download" and c.args["notify_device"] == "phone-arjun"


def test_download_watcher_defaults_to_current_device(ctx):
    assert first("tell me when the download is done", ctx).args["notify_device"] == "pc-main"


def test_uber_watcher(ctx):
    c = first("Remind me when my Uber is 2 minutes away", ctx)
    assert c.name == "watch_ride" and c.args["provider"] == "Uber" and c.args["eta_threshold"] == 2


def test_recall_and_open(ctx):
    plan = parse("What was that PDF Rahul sent me yesterday? Open it.", ctx)
    assert [c.name for c in plan.calls] == ["recall_file", "open_file"]
    assert plan.calls[0].args["person"] == "Rahul" and plan.calls[0].args["ext"] == "pdf"
    assert plan.calls[0].args["since"] < ctx.now


def test_download_from_person_with_phone_alert(ctx):
    c = first("Download the report Rahul sent and tell me on my phone when it's done", ctx)
    assert c.name == "download_from_person"
    assert c.args["person"] == "Rahul" and c.args["device"] == "pc-main" and c.args["notify_device"] == "phone-arjun"


def test_office_move_is_a_versioned_fact(ctx):
    c = first("I've moved to the Cyber City office.", ctx)
    assert c.name == "remember" and c.args["key"] == "office_location" and "Cyber City" in c.args["statement"]


def test_remember_generic(ctx):
    c = first("Remember that my gym is Cult Sector 29", ctx)
    assert c.name == "remember" and c.args["key"] == "gym"


def test_explain_alert(ctx):
    c = first("Why did you remind me?", ctx)
    assert c.name == "explain" and c.args["which"] == "alert"


def test_delete_goes_to_pc(ctx):
    c = first("Delete my temp folder", ctx)
    assert c.name == "delete_path" and c.args["path"] == "temp folder" and c.args["device"] == "pc-main"


def test_confirmation_only_when_pending(ctx):
    assert parse("yes", ctx).calls == []
    ctx.pending_action = {"id": "act-1"}
    assert first("yes", ctx).name == "confirm_pending"
    assert first("no thanks", ctx).args["approve"] is False


def test_misc(ctx):
    assert first("what are my habits", ctx).name == "list_habits"
    assert first("brief me", ctx).name == "briefing"
    assert first("what was I working on yesterday evening", ctx).name == "recent_activity"
    assert first("remind me in 10 minutes to call Priya", ctx).name == "remind_in"
    assert first("open the link I just got on WhatsApp on my laptop", ctx).args == {"channel": "whatsapp",
                                                                                      "device": "pc-main"}
    assert first("where is my office", ctx).name == "get_fact"
    assert first("forget everything about the Goa trip", ctx).args["topic"] == "the goa trip"


def test_unknown_falls_back(ctx):
    plan = parse("sing me a song", ctx)
    assert not plan.calls and "language model" in plan.reply


def test_fact_key():
    assert fact_key("my office is in Cyber City") == "office_location"
    assert fact_key("I like oat milk") == "note_i_like_oat_milk"
