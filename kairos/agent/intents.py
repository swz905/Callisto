"""Rule-based planner: maps the demo's spoken commands to tool calls.

This is the offline stand-in for the LLM planner. It is deliberately simple; once ClaudePlanner lands,
this stays as the fallback when the API is unreachable.
"""

from __future__ import annotations

import re

from kairos.agent.llm import Plan, ToolCall, TurnContext
from kairos.timeutil import period_range

EXT_WORDS = {"pdf": "pdf", "doc": "docx", "docx": "docx", "word": "docx", "ppt": "pptx", "deck": "pptx",
             "slides": "pptx", "sheet": "xlsx", "excel": "xlsx", "spreadsheet": "xlsx", "csv": "csv",
             "image": "png", "photo": "jpg", "zip": "zip"}
FILE_WORDS = r"(pdf|file|doc|docx|document|report|attachment|deck|slides|ppt|sheet|spreadsheet|excel|csv|image|photo|zip)"
PROVIDERS = {"uber": "Uber", "ola": "Ola", "rapido": "Rapido"}
FACT_KEYS = [
    (r"\boffice\b|\bwork(place)?\b", "office_location"),
    (r"\bhome\b|\bi live\b|\bflat\b|\bapartment\b", "home_location"),
    (r"\bmanager\b|\bboss\b", "manager"),
    (r"\bbirthday\b", "birthday"),
    (r"\bgym\b", "gym"),
]
STOP = {"me", "my", "the", "a", "that", "this", "yesterday", "today", "last", "week", "you", "it"}

YES = re.compile(r"^(yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|confirm|please do)\b", re.I)
NO = re.compile(r"^(no|nope|cancel|don't|do not|stop|never mind|nevermind)\b", re.I)


def confirmation(text: str) -> bool | None:
    """True/False for a yes/no reply, None otherwise. Kept deterministic: never left to the LLM."""
    t = _norm(text)
    return True if YES.match(t) else False if NO.match(t) else None


def _norm(text: str) -> str:
    return re.sub(r"[?!.]+$", "", text.strip()).strip()


def _device(text: str, ctx: TurnContext, default: str | None = None) -> str | None:
    t = text.lower()
    if re.search(r"\b(on|to) my (phone|mobile)\b", t):
        return ctx.devices.get("phone", default)
    if re.search(r"\b(on|to) my (laptop|pc|computer|desktop)\b", t):
        return ctx.devices.get("pc", default)
    if re.search(r"\b(on|to|via|over) (my )?telegram\b", t):
        return ctx.devices.get("chat", default)
    return default


def period_of(text: str) -> str | None:
    t = text.lower()
    if "yesterday" in t:
        return "yesterday_evening" if "evening" in t else "yesterday_morning" if "morning" in t else "yesterday"
    if "today" in t or "this morning" in t:
        return "today"
    if "last week" in t or "this week" in t:
        return "last_week"
    if "last month" in t:
        return "last_month"
    return None


def _device_word(t: str, ctx: TurnContext) -> str | None:
    """Bare device mention anywhere ('phone battery', 'laptop status')."""
    if re.search(r"\b(phone|mobile)\b", t):
        return ctx.devices.get("phone")
    if re.search(r"\b(laptop|pc|computer|desktop)\b", t):
        return ctx.devices.get("pc")
    return None


def _since(text: str, ctx: TurnContext, default_days: int = 30) -> tuple[int, int]:
    return period_range(period_of(text), ctx.tz, ctx.now, default_days)


def _ext(text: str) -> str | None:
    for word, ext in EXT_WORDS.items():
        if re.search(rf"\b{word}\b", text.lower()):
            return ext
    return None


def _person_before(verb_re: str, text: str) -> str | None:
    """'...PDF Rahul sent me...' -> 'Rahul'."""
    m = re.search(rf"\b([A-Za-z][\w']*)\s+(?:{verb_re})\b", text)
    if m and m.group(1).lower() not in STOP:
        return m.group(1).capitalize()
    m = re.search(rf"\bfrom\s+([A-Za-z][\w']*)", text)
    return m.group(1).capitalize() if m and m.group(1).lower() not in STOP else None


def fact_key(statement: str) -> str:
    s = statement.lower()
    for pattern, key in FACT_KEYS:
        if re.search(pattern, s):
            return key
    words = re.findall(r"[a-z]+", s)[:4]
    return "note_" + "_".join(words)


def parse(utterance: str, ctx: TurnContext) -> Plan:
    text = _norm(utterance)
    t = text.lower()
    here = ctx.device_id
    call = lambda name, **args: Plan([ToolCall(name, args)])  # noqa: E731

    # confirmations for a pending risky action
    if ctx.pending_action:
        if YES.match(t):
            return call("confirm_pending", approve=True)
        if NO.match(t):
            return call("confirm_pending", approve=False)

    # explainability
    if re.search(r"\bwhy (did|do|were|are) you\b", t):
        which = "alert" if re.search(r"remind|alert|notif|tell|ping|warn", t) else "any"
        return call("explain", which=which)

    # cross-device download + watcher: "download the report Rahul sent and tell me on my phone when it's done"
    if t.startswith("download") and re.search(r"\b(sent|shared|emailed|forwarded)\b", t):
        person = _person_before(r"sent|shared|emailed|forwarded", text)
        notify = _device(text, ctx, here) if re.search(r"tell|notify|let me know|ping|alert", t) else None
        return call("download_from_person", person=person, ext=_ext(text), device=ctx.devices.get("pc", here),
                    notify_device=notify)

    # watchers
    if re.search(r"\b(tell|notify|ping|alert|let) me( know)? when\b.*\bdownload", t) or \
            re.search(r"\bwhen (the |my |this )?download (is )?(done|finish|complete)", t):
        return call("watch_download", notify_device=_device(text, ctx, here))
    if m := re.search(r"\b(remind|tell|notify|alert|ping|let) me( know)? when (my |the )?(uber|ola|rapido|cab|taxi|ride|driver)\b", t):
        eta = re.search(r"(\d+)\s*(min|minute)", t)
        provider = PROVIDERS.get(m.group(4))
        threshold = int(eta.group(1)) if eta else (0 if re.search(r"arriv|here", t) else 2)
        return call("watch_ride", provider=provider, eta_threshold=threshold, notify_device=_device(text, ctx, here))
    if m := re.search(r"\bremind me in (\d+) (second|sec|minute|min|hour)s? (to |that |about )?(.+)", t):
        unit = {"second": 1 / 60, "sec": 1 / 60, "minute": 1, "min": 1, "hour": 60}[m.group(2)]
        return call("remind_in", minutes=float(m.group(1)) * unit, message=m.group(4),
                    notify_device=_device(text, ctx, here))
    if m := re.search(r"\b(tell|notify|alert) me when (i get |there'?s )?(an? )?(whatsapp|gmail|email|slack|uber) (message |notification )?(about|with|saying) (.+)", t):
        app = {"email": "Gmail"}.get(m.group(4), m.group(4).capitalize()).replace("Whatsapp", "WhatsApp")
        return call("watch_app", app=app, keyword=m.group(7), notify_device=_device(text, ctx, here))
    if m := re.search(r"\bremind me to (.+?) when i (leave|get to|reach|arrive at|get back to|am at|get) (the )?(.+)$", t):
        transition = "left" if m.group(2) == "leave" else "entered"
        return call("watch_place", place=m.group(4), transition=transition, message=m.group(1),
                    notify_device=_device(text, ctx, ctx.devices.get("phone", here)))
    if m := re.search(r"\bremind me to (.+?) when i get (home|back home)$", t):
        return call("watch_place", place="home", transition="entered", message=m.group(1),
                    notify_device=ctx.devices.get("phone", here))
    if m := re.search(r"^when i (leave|get to|reach|arrive at|get) (the )?(.+?),? remind me to (.+)$", t):
        return call("watch_place", place=m.group(3), transition="left" if m.group(1) == "leave" else "entered",
                    message=m.group(4), notify_device=ctx.devices.get("phone", here))
    if m := re.search(r"\b(tell|notify|alert|ping|let) me( know)? when i (leave|get to|reach|arrive at|get) (the )?(.+)$", t):
        return call("watch_place", place=m.group(5), notify_device=_device(text, ctx, here),
                    transition="left" if m.group(3) == "leave" else "entered")
    if m := re.search(r"\b(tell|notify|alert|ping|let|remind) me( know)?( (on|to) my \w+)? (when|if|once|as soon as) (.+)",
                      text, re.I):
        what = re.sub(r"\s+(on|to) my (phone|mobile|laptop|pc|computer|desktop)\b", "", m.group(6), flags=re.I)
        return call("watch_for", description=what.strip(), notify_device=_device(text, ctx, here))

    # read-only web (search, then read the top result)
    if m := re.search(r"^(?:open|read|show|summari[sz]e) (?:search )?result (?:number )?#?(\d{1,2})$", t):
        return call("read_page", url=m.group(1))
    if m := re.search(r"^(?:read|summari[sz]e|open and read|what does) (https?://\S+)", text, re.I):
        return call("read_page", url=m.group(1))
    if re.search(r"^(?:read|summari[sz]e) (?:that|the|this) (?:page|article|link|site)$", t):
        return call("read_page", url="last")
    if m := (re.search(r"^(?:search|google|bing)(?! my\b)(?: the web| online| the internet)?(?: for)? (.+)$", t)
             or re.search(r"^(?:look up|look online for|search online for|find online|find on the web) (.+)$", t)
             or re.search(r"\b(?:search the web|search online|on the web|on the internet|online) for (.+)$", t)):
        q = m.group(1).strip()
        return Plan([ToolCall("web_search", {"query": q}), ToolCall("read_page", {"url": "1", "focus": q})])

    # cross-device handoff
    if m := re.search(r"\b(send|copy|share|move) (my |the )?clipboard( from my (\w+))?( to my (\w+))?", t):
        return call("send_clipboard", from_device=m.group(4), to_device=m.group(6))
    if m := re.search(r"^(send|share|move|push) (it|this|that|the file|that file|(.+?)) to my "
                      r"(phone|mobile|laptop|pc|computer|desktop)$", t):
        query = None if m.group(2) in ("it", "this", "that", "the file", "that file") else re.sub(r"^the ", "", m.group(3))
        return call("send_file", query=query, to_device=m.group(4))

    # commitments / open loops
    if re.search(r"\b(what do i owe|pending with|what did i promise|open (tasks|loops|commitments)|"
                 r"my (tasks|to-?dos?|commitments|open loops)|to-?do list)\b", t):
        pm = re.search(r"\b(?:owe|with|promise|promised)\s+(\w+)", t)
        person = pm.group(1).capitalize() if pm and pm.group(1) not in STOP | {"anyone", "people"} else None
        return call("list_tasks", person=person)
    if m := re.search(r"^(add (a )?(task|to-?do)( to)?|to-?do:?|note to self:?)\s+(.+)", text, re.I):
        return call("add_task", title=m.group(5), due=m.group(5))
    if m := re.search(r"^(?:mark (.+?) (?:as )?(?:done|complete|completed|finished)|"
                      r"i(?:'ve| have)? (?:sent|finished|done|completed|submitted|shared|paid|called|emailed|reviewed)"
                      r" (.+))$", t):
        return call("complete_task", query=m.group(1) or m.group(2))

    # calendar
    if m := re.search(r"\b(prep|prepare|brief) me for (my |the )?(next )?(.+?)?\s*(meeting|call|sync)?$", t):
        q = (m.group(4) or "").strip()
        return call("meeting_prep", query=None if not q or q in ("next", "meeting", "my meeting") else q)
    if re.search(r"\b(what('s| is) on )?my (calendar|schedule|agenda)\b|\bmy meetings\b|\bnext meeting\b|"
                 r"\bmeetings? (today|tomorrow)\b|\bdo i have (any )?meetings\b", t):
        return call("upcoming_meetings", period="tomorrow" if "tomorrow" in t else "today" if "today" in t else "next")

    # graph algorithms
    if re.search(r"\bwho (matters|is important|should i (focus on|reply to|prioriti[sz]e))\b|"
                 r"\bwho('s| is) most important\b|\bwho do i (talk|interact) (to|with) (the )?most\b", t):
        return call("important_people")

    # family (shared graph)
    if m := re.search(r"^(?:tell|let) (?:the )?family(?: know)?(?: that)?:?\s+(.+)", text, re.I) or \
            re.search(r"^share (.+?) with (?:the )?family$", text, re.I):
        return call("share_with_family", statement=m.group(1))
    if re.search(r"\bwhat (did|has) (the )?family (share|shared|say|post)|\bfamily (board|notes|memory|updates)\b", t):
        return call("family_memory")

    # notes (nuance that isn't a simple fact) and fuzzy recall
    if m := re.search(r"^(?:note|make a note|jot down)(?: that)?:?\s+(.+)", text, re.I):
        return call("add_note", text=m.group(1))
    if m := re.search(r"\b(?:that|the) (?:thing|stuff|message|email|note|conversation|chat) about (.+)", t):
        return call("search_memory", q=re.sub(r"^(the|my|a|an|our) ", "", m.group(1)))
    # past conversations (daily chat log + nightly digest)
    if m := re.search(r"\bwhat (?:did|have) (?:we|i) (?:talk(?:ed)?|chat(?:ted)?|discuss(?:ed)?|say|said|tell you|told you|"
                      r"mention(?:ed)?)(?: about)?(?: on)?\s*(today|yesterday|monday|tuesday|wednesday|thursday|friday|"
                      r"saturday|sunday|\d{4}-\d\d-\d\d)?\??$", t):
        return call("recall_conversation", day=m.group(1) or "today")
    if m := re.search(r"\bwhat (?:did|have) (?:i|we) (?:say|said|tell you|told you|mention(?:ed)?|talk(?:ed)?|"
                      r"discuss(?:ed)?|chat(?:ted)?)(?: about)? (.+?)\??$", t):
        return call("search_memory", q=re.sub(r"^(the|my|a|an|our) ", "", m.group(1)))
    if re.search(r"^(?:digest|file away|save|consolidate) (?:my |the |today'?s? |yesterday'?s? )*"
                 r"(?:day|chats?|conversations?|memory)\b", t):
        return call("digest_memory", day="yesterday" if "yesterday" in t else "today")
    if m := re.search(r"\bwhat (?:site|page|website|article|link)s? (?:was|were|did) i (?:reading|on|looking at|visit|open)"
                      r"(?:.*?\babout (.+))?", t):
        return call("recent_sites", query=m.group(1), period=period_of(text) or "today")

    # recall
    if re.search(rf"\b{FILE_WORDS}\b", t) and re.search(r"\b(sent|shared|emailed|forwarded|from)\b", t) and \
            re.search(r"\b(what|which|find|show|where|get|the)\b", t):
        start, _ = _since(text, ctx)
        person = _person_before(r"sent|shared|emailed|forwarded", text)
        plan = Plan([ToolCall("recall_file", {"person": person, "ext": _ext(text), "since": start})])
        if re.search(r"\bopen it\b|\band open\b", t):
            plan.calls.append(ToolCall("open_file", {"device": _device(text, ctx, None)}))
        return plan
    if m := re.search(r"\bwhat did (\w+) (send|say|message|email|write)", t):
        start, _ = _since(text, ctx)
        return call("recall_messages", person=m.group(1).capitalize(), since=start)
    if re.search(r"^(open|show) (it|that|the file|that file)\b", t):
        return call("open_file", device=_device(text, ctx, None))
    if re.search(r"\bopen (the |that )?(last |latest )?link\b", t):
        channel = next((c for c in ("whatsapp", "email", "slack", "sms", "telegram") if c in t), None)
        return call("open_link", channel=channel, device=_device(text, ctx, here))
    if m := re.search(r"^open (the )?(.+?)( on my \w+)?$", t):
        return call("open_file", query=m.group(2), device=_device(text, ctx, None))
    if re.search(r"\bwhat (was|were) i (working on|doing|reading)\b", t):
        start, end = _since(text, ctx, default_days=1)
        return call("recent_activity", since=start, until=end)
    if m := re.search(r"\bwho is (\w+)", t):
        return call("person_context", person=m.group(1).capitalize())
    if m := re.search(r"\bhow (is|am) (\w+) (connected|related|linked) to (\w[\w ]*)", t):
        return call("find_path", a=m.group(2), b=m.group(4))

    # memory: remember / update / query / forget
    if m := re.search(r"\b(i('ve| have)? moved|we('ve| have)? moved|my office (has )?moved) to (the )?(.+?)( office)?$", t):
        place = m.group(6).strip().title()
        return call("remember", statement=f"Your office is at {place}", key="office_location")
    if m := re.search(r"^remember (that )?(.+)", text, re.I):
        statement = m.group(2)
        return call("remember", statement=statement, key=fact_key(statement))
    if re.search(r"\bwhere (did|have) i (work|worked) (before|earlier|previously|last)", t):
        return call("fact_history", key="office_location")
    if re.search(r"\bwhere (is my office|do i work)\b|\bmy commute\b|\bhow (do|should) i get to (the )?office\b", t):
        return call("get_fact", key="office_location")
    if re.search(r"\bwhere do i live\b|\bmy home address\b", t):
        return call("get_fact", key="home_location")
    if m := re.search(r"^forget (everything |all )?(about |that )?(.+)", t):
        return call("forget", topic=m.group(3))
    if m := re.search(r"\bwhat do you know about (.+)", t):
        return call("search_memory", q=m.group(1))

    # device actions (risky ones are gated by the permission graph)
    if m := re.search(r"^(delete|remove|clear|clean) (my |the )?(.+?)( on my \w+)?$", t):
        return call("delete_path", path=m.group(3), device=_device(text, ctx, ctx.devices.get("pc", here)))
    if m := re.search(r"^(?:run\s+)?(?:get-childitem|get\s+children|list\s+files?|show\s+files?|dir|ls)(?:\s+(?:in|of|from|at)\s+(.+))?$", t):
        path = m.group(1).strip() if m.group(1) else ""
        cmd = f"Get-ChildItem {path}".strip() if path else "Get-ChildItem"
        return call("run_command", cmd=cmd, device=_device(text, ctx, ctx.devices.get("pc", here)))
    if m := re.search(r"^(run|execute) (.+?)( on my \w+)?$", text, re.I):
        return call("run_command", cmd=m.group(2), device=_device(text, ctx, ctx.devices.get("pc", here)))

    # simulated typing / UI messaging in WhatsApp, Telegram, or chat apps
    if m := re.search(r"^(?:send\s+(?:a\s+)?message|type\s+(?:a\s+)?message|message|tell)\s+(?:to\s+)?(\w+)\s+(?:on|in)\s+(whatsapp|telegram|chat|slack|discord)(?:\s+(?:saying|that))?:?\s+[\"“']?(.+?)[\"”']?$", text, re.I):
        recipient, target_app, msg_text = m.group(1), m.group(2), m.group(3)
        draft = bool(re.search(r"\b(without sending|as draft|don't send|do not send)\b", t))
        return call("send_ui_message", message=msg_text.strip(), recipient=recipient.strip(), app=target_app.strip(),
                    press_enter=not draft, device=_device(text, ctx, ctx.devices.get("pc", here)))

    if m := re.search(r"^(?:send\s+(?:a\s+)?message|type\s+(?:a\s+)?message)\s+(?:saying\s+|that\s+)?[\"“']?(.+?)[\"”']?\s+to\s+(\w+)\s+(?:on|in)\s+(whatsapp|telegram|chat|slack|discord)(?:\s+(?:without\s+sending|as\s+draft))?$", text, re.I):
        msg_text, recipient, target_app = m.group(1), m.group(2), m.group(3)
        draft = bool(re.search(r"\b(without sending|as draft|don't send|do not send)\b", t))
        return call("send_ui_message", message=msg_text.strip(), recipient=recipient.strip(), app=target_app.strip(),
                    press_enter=not draft, device=_device(text, ctx, ctx.devices.get("pc", here)))

    if m := (re.search(r"^(?:send\s+(?:a\s+)?message|type\s+(?:a\s+)?message|post\s+(?:a\s+)?message)\s+(?:saying\s+|that\s+)?[\"“']?(.+?)[\"”']?\s+(?:on|in|to)\s+(whatsapp|telegram|chat|slack|discord|browser|chrome|edge)(?:\s+(?:without\s+sending|as\s+draft))?$", text, re.I) or
             re.search(r"^(?:type|send)\s+(?:in|on)\s+(whatsapp|telegram|chat|slack|discord|browser|chrome|edge):?\s+(.+)$", text, re.I)):
        if m.re.pattern.startswith("^(?:send\\s+(?:a\\s+)?message|type\\s+(?:a\\s+)?message"):
            msg_text, target_app = m.group(1), m.group(2)
        else:
            target_app, msg_text = m.group(1), m.group(2)
        draft = bool(re.search(r"\b(without sending|as draft|don't send|do not send)\b", t))
        return call("send_ui_message", message=msg_text.strip(), app=target_app.strip(), press_enter=not draft,
                    device=_device(text, ctx, ctx.devices.get("pc", here)))

    # routines and status
    if re.search(r"\bwhat('s| is| apps are| am i)? ?(open|running|doing) on my\b|\b(device|phone|pc|laptop) status\b"
                 r"|\bstatus of my\b|\bbattery\b|\bis my (phone|pc|laptop) (charging|online|connected)\b"
                 r"|\bwhich (wi-?fi|network)\b", t):
        return call("device_status", device=_device(text, ctx) or _device_word(t, ctx))
    if re.search(r"\b(habits?|routines?|what do i usually)\b", t):
        return call("list_habits")
    if re.search(r"\b(briefing|brief me|what('s| is) (pending|up|new)|catch me up|summary)\b", t):
        return call("briefing")
    if re.search(r"\b(what are you watching|active (watchers|reminders)|my reminders|what reminders)\b", t):
        return call("list_watchers")
    if re.search(r"\bcancel (all |my |the )?(reminders?|watchers?|alerts?|that)\b", t):
        return call("cancel_watchers")
    if re.search(r"^(help|what can you do)\b", t):
        return call("help")

    return Plan(reply="I can't handle that yet (the language model isn't connected). Say \"help\" to hear what I can do.")
