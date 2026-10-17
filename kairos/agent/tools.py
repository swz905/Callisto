"""Agent tools. Each tool declares a capability (checked against the permission graph before it runs)
and a JSON schema (ready to hand to the LLM / MCP once that integration lands)."""

from __future__ import annotations

import inspect
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from kairos.algorithms import important_people
from kairos.memory import describe_habit
from kairos.models import Outbound, new_id
from kairos.places import normalize_place
from kairos.policy import command_capability
from kairos.status import describe_status
from kairos.tasks import Commitment, describe_task, parse_due
from kairos.trust import Taint
from kairos.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, PERIODS, fmt_time, period_range, start_of_day
from kairos.watchers import WatcherSpec

if TYPE_CHECKING:
    from kairos.runtime import Kairos


@dataclass
class ToolResult:
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    causes: list[tuple[str, str]] = field(default_factory=list)  # extra (label, id) for CAUSED_BY


@dataclass
class Turn:
    """Per-call context handed to a tool."""

    device_id: str
    utterance_id: str | None
    session: dict[str, Any]
    now: int
    action_id: str | None = None  # pre-allocated so device messages can report results back to it
    taint: Taint = field(default_factory=Taint)  # external content the LLM has read this turn


def _obj(required: list[str] | None = None, **props: Any) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _s(desc: str, enum: list[str] | None = None) -> dict:
    return {"type": "string", "description": desc, **({"enum": enum} if enum else {})}


I, N, B = {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
DEVICE = _s("'phone', 'pc', or a device id. Omit for the device the user is speaking on.")
NOTIFY = _s("Where to send the alert: 'phone', 'pc', or a device id. Omit for the current device.")
PERIOD = _s("Time window", list(PERIODS))
PERSON = _s("Person's name as the user said it, e.g. 'Rahul'")

# name -> (capability, description for the LLM, JSON schema of arguments)
TOOL_SPECS: dict[str, tuple[str, str, dict]] = {
    "watch_download": ("watcher.create", "Alert the user when the current (or next) download finishes.",
                       _obj(notify_device=NOTIFY)),
    "watch_ride": ("watcher.create", "Alert when a cab (Uber/Ola/Rapido) is within N minutes, or has arrived (0).",
                   _obj(provider=_s("Uber, Ola or Rapido; omit for any"), eta_threshold=I, notify_device=NOTIFY)),
    "watch_app": ("watcher.create", "Alert when a notification from an app contains a keyword.",
                  _obj(["app"], app=_s("App name, e.g. WhatsApp, Gmail"), keyword=_s("Text to look for"),
                       notify_device=NOTIFY)),
    "watch_for": ("watcher.create",
                  "Open-ended 'tell me when...' request that no other watch_* tool covers, e.g. 'when Rahul replies "
                  "about the budget', 'when my passport status changes'. Every new notification is checked against it.",
                  _obj(["description"], description=_s("What to watch for, in the user's words"), notify_device=NOTIFY)),
    "remind_in": ("watcher.create", "Time-based reminder after N minutes.",
                  _obj(["minutes", "message"], minutes=N, message=_s("What to remind about"), notify_device=NOTIFY)),
    "download_from_person": ("net.download",
                             "Download the latest link a person sent (on the PC by default); optionally alert when done.",
                             _obj(["person"], person=PERSON, ext=_s("File extension filter, e.g. pdf"), device=DEVICE,
                                  notify_device=NOTIFY)),
    "recall_file": ("read", "Find a file a person sent: walks Person→Message→Link→Download→File in memory.",
                    _obj(["person"], person=PERSON, ext=_s("pdf, docx, pptx, xlsx, ... (optional)"), period=PERIOD)),
    "recall_messages": ("read", "Recent messages from a person.", _obj(["person"], person=PERSON, period=PERIOD)),
    "open_file": ("file.open", "Open a file on a device: the last file found, or one matching `query` by name.",
                  _obj(query=_s("Part of the file name (omit to open the last recalled file)"), device=DEVICE)),
    "open_link": ("link.open", "Open the latest link the user received (optionally from one channel) on a device.",
                  _obj(channel=_s("Channel", ["whatsapp", "email", "sms", "slack", "telegram"]), device=DEVICE)),
    "remember": ("memory.write",
                 "Store a fact about the user. Same key = update (the old version is kept as history).",
                 _obj(["statement", "key"], statement=_s("The fact, phrased to the user, e.g. 'Your office is at Cyber City'"),
                      key=_s("snake_case key, e.g. office_location, home_location, manager, gym, birthday"))),
    "get_fact": ("read", "Current value of a fact by key.", _obj(["key"], key=_s("e.g. office_location"))),
    "fact_history": ("read", "All past and current versions of a fact.", _obj(["key"], key=_s("e.g. office_location"))),
    "search_memory": ("read", "Fuzzy recall across facts, notes and past events (by meaning or words), e.g. "
                              "'that thing about the budget'.", _obj(["q"], q=_s("Search text"))),
    "recall_conversation": ("read", "What the user and KAIROS talked about on a day (from the daily chat log and its "
                                    "memory digest), e.g. 'what did we talk about yesterday?'.",
                            _obj(day=_s("today | yesterday | a weekday name | YYYY-MM-DD (default today)"))),
    "digest_memory": ("memory.write", "File the conversation of a day away into long-term memory now (facts, notes, "
                                      "people, tasks). Normally happens automatically every night.",
                      _obj(day=_s("today | yesterday | YYYY-MM-DD (default today)"))),
    "add_note": ("memory.write", "Save a free-text note, linked to the people/places it's about (nuance that isn't a "
                                 "simple fact, e.g. 'Rahul seemed stressed about the budget').",
                 _obj(["text"], text=_s("The note"), about=_s("Comma-separated names of people/places it's about"))),
    "list_tasks": ("read", "Open commitments: things people asked the user for and things the user promised.",
                   _obj(person=_s("Only tasks with this person (optional)"))),
    "add_task": ("memory.write", "Add a to-do / commitment.",
                 _obj(["title"], title=_s("What needs doing"), person=_s("Who it's for/with (optional)"),
                      due=_s("When, in words: 'by Friday', 'tomorrow 5pm' (optional)"))),
    "complete_task": ("memory.write", "Mark a commitment done (fuzzy match on its description).",
                      _obj(["query"], query=_s("Words from the task, e.g. 'deck for Rahul'"))),
    "upcoming_meetings": ("read", "Meetings from the user's calendar.",
                          _obj(period=_s("today | tomorrow | next (default: next 24 hours)", ["today", "tomorrow", "next"]))),
    "meeting_prep": ("read", "Prep pack for a meeting: who's in it, what they sent recently, related files, open tasks.",
                     _obj(query=_s("Part of the meeting title; omit for the next meeting"))),
    "important_people": ("read", "Who matters most to the user right now (graph PageRank + recent interactions, "
                                 "open tasks, meetings).", _obj(days=I)),
    "recent_sites": ("read", "Web pages the user visited (browser history), optionally filtered by words.",
                     _obj(query=_s("Words in the page title/URL (optional)"), period=PERIOD)),
    "watch_place": ("watcher.create", "Reminder when the user arrives at or leaves a place (from phone location).",
                    _obj(["place", "transition"], place=_s("Place name, e.g. Office, Home"),
                         transition=_s("entered or left", ["entered", "left"]), message=_s("What to remind about"),
                         notify_device=NOTIFY)),
    "send_file": ("file.transfer", "Send a file to another device (e.g. the last file found, to the phone).",
                  _obj(query=_s("Part of the file name; omit for the last file found"),
                       to_device=_s("'phone', 'pc', or a device id"))),
    "send_clipboard": ("clipboard", "Copy the clipboard from one device to another.",
                       _obj(from_device=_s("'pc' (default) or 'phone'"), to_device=_s("'phone' (default) or 'pc'"))),
    "send_ui_message": ("clipboard", "Type and send a message in WhatsApp, Telegram, web chats or any open chat window by simulating keystrokes. Can automatically search and open a recipient's chat.",
                        _obj(["message"], message=_s("Text of the message to send"),
                             recipient=_s("Name of the contact or group to search and open in the chat app (e.g. 'Rahul', 'Mom'); omit if chat is already open"),
                             app=_s("Target app or window title e.g. 'WhatsApp', 'Telegram', 'Chrome', or omit for currently active window"),
                             press_enter=B, device=DEVICE)),
    "read_notifications": ("read", "Read recent Windows PC notifications (from WhatsApp, Telegram, Phone Link SMS, OTPs, system alerts). Filter by app or query keyword.",
                           _obj(app=_s("App name to filter, e.g. 'Telegram', 'WhatsApp', 'Phone'"),
                                query=_s("Keyword or sender to filter in notification text"),
                                limit=I, device=DEVICE)),
    "take_screenshot": ("file.open", "Capture a screenshot of the PC desktop/screen.",
                        _obj(device=DEVICE)),
    "phone_send_chat": ("link.open", "Send a message (WhatsApp, SMS, or Telegram) to someone via the phone.",
                        _obj(["recipient", "message"], recipient=_s("Recipient phone number or name"),
                             message=_s("Message text to send"),
                             channel=_s("whatsapp | sms | telegram (default whatsapp)", ["whatsapp", "sms", "telegram"]),
                             device=DEVICE)),
    "phone_list_files": ("file.open", "List files on the phone in a directory (e.g. /sdcard/Download).",
                         _obj(path=_s("Directory on phone (default: /sdcard/Download)"), device=DEVICE)),
    "phone_pull_file": ("file.transfer", "Pull a file from the phone /sdcard to the PC sandbox.",
                        _obj(["remote_path"], remote_path=_s("Full path on phone, e.g. /sdcard/Download/doc.pdf"),
                             local_dest=_s("Local destination path (optional)"), device=DEVICE)),
    "phone_ui_tap": ("shell.exec", "Tap coordinates on the phone screen via ADB.",
                     _obj(["x", "y"], x=I, y=I, device=DEVICE)),
    "phone_ui_key": ("shell.exec", "Press a button on the phone (back, home, enter, power, app_switch).",
                     _obj(["key"], key=_s("back | home | enter | app_switch | power"), device=DEVICE)),
    "phone_screenshot": ("file.open", "Take a screenshot of the phone screen.", _obj(device=DEVICE)),
    "phone_ui_dump": ("read", "Inspect the on-screen UI elements, text, and buttons on the phone.", _obj(device=DEVICE)),
    "share_with_family": ("memory.share", "Share a note with the family's shared memory (other members can see it).",
                          _obj(["statement"], statement=_s("What to share"), key=_s("Optional key so updates replace it"))),
    "family_memory": ("read", "What family members have shared.", _obj(q=_s("Search words (optional)"))),
    "web_search": ("net.search", "Search the web (read-only) for current or public information. Returns numbered "
                                 "results (title, site, snippet). Follow with read_page on the most relevant 1-3.",
                   _obj(["query"], query=_s("Search query"), max_results=I)),
    "read_page": ("net.read", "Open a web page in the browser and read its text (read-only). `url` can be a full "
                              "URL or a result number from the last web_search ('1', '2'...).",
                  _obj(["url"], url=_s("URL or search result number"),
                       focus=_s("What you're looking for on the page, to pick the relevant parts (optional)"))),
    "forget": ("memory.forget", "Archive everything remembered about a topic (user asked to forget).",
               _obj(["topic"], topic=_s("Topic to forget"))),
    "explain": ("read", "Explain why KAIROS did its last action (or last alert) from the graph path behind it.",
                _obj(which=_s("'alert' for the last reminder/notification, 'any' otherwise", ["alert", "any"]))),
    "delete_path": ("file.delete", "Delete a folder/file inside the KAIROS sandbox on a device (always confirmed).",
                    _obj(["path"], path=_s("e.g. 'temp folder'"), device=DEVICE)),
    "run_command": ("shell.exec", "Run a PowerShell command on the PC (risky commands are confirmed first).",
                    _obj(["cmd"], cmd=_s("PowerShell command"), device=DEVICE)),
    "device_status": ("read",
                      "Live status of the user's devices: foreground app and window, open apps, battery, network, "
                      "CPU/RAM, idle time. Also asks the device for a fresh snapshot.",
                      _obj(device=_s("'phone', 'pc', or a device id; omit for all devices"))),
    "list_habits": ("read", "Routines KAIROS has learned, with strength.", _obj()),
    "briefing": ("read", "Summary of today: activity, active watchers, routines.", _obj()),
    "recent_activity": ("read", "What the user was doing in a time window (files, downloads, notifications).",
                        _obj(period=PERIOD)),
    "person_context": ("read", "Who someone is to the user and recent messages from them.",
                       _obj(["person"], person=PERSON)),
    "find_path": ("read", "How two people/places/files/apps are connected in memory.",
                  _obj(["a", "b"], a=_s("First name/thing"), b=_s("Second name/thing"))),
    "list_watchers": ("read", "What KAIROS is currently watching for.", _obj()),
    "cancel_watchers": ("watcher.create", "Cancel all active watchers/reminders.", _obj()),
    "help": ("read", "What KAIROS can do.", _obj()),
}
ACTION_TOOLS = [n for n, (cap, _, _) in TOOL_SPECS.items() if cap != "read"] + ["notify"]


def openai_tools() -> list[dict]:
    """TOOL_SPECS in OpenAI/OpenRouter `tools` format."""
    return [{"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
            for name, (_cap, desc, schema) in TOOL_SPECS.items()]

HELP = ("Try: \"tell me when the download finishes\", \"remind me when my Uber is 2 minutes away\", "
        "\"what was that PDF Rahul sent me yesterday? open it\", \"I've moved to the Cyber City office\", "
        "\"why did you remind me\", \"delete my temp folder\", \"what do I owe Rahul\", \"prep me for my next "
        "meeting\", \"remind me to buy milk when I leave the office\", \"send it to my phone\", "
        "\"who matters most to me right now\", \"brief me\".")


def capability_for(name: str, args: dict) -> str:
    if name == "run_command":
        return command_capability(args.get("cmd", ""))
    return TOOL_SPECS[name][0]


# Tools that do slow network I/O: run outside the graph lock (they take it themselves for their writes).
UNLOCKED_TOOLS = {"web_search", "read_page", "digest_memory"}


def summarize(name: str, args: dict, device_name: str) -> str:
    if name == "delete_path":
        return f"delete {args.get('path')} on {device_name}"
    if name == "run_command":
        return f"run `{args.get('cmd')}` on {device_name}"
    if name == "forget":
        return f"forget everything about {args.get('topic')}"
    if name == "download_from_person":
        return f"download the file {args.get('person')} sent, on {device_name}"
    if name == "send_file":
        return f"send {args.get('query') or 'the file'} to {args.get('to_device') or 'your other device'}"
    if name == "send_clipboard":
        return f"copy the clipboard from {args.get('from_device') or 'your PC'} to {args.get('to_device') or 'your phone'}"
    if name == "send_ui_message":
        app = args.get("app") or "the active window"
        who = f" to {args.get('recipient')}" if args.get("recipient") else ""
        return f"type message in {app}{who} on {device_name}"
    if name == "open_link":
        return f"open the latest {args.get('channel') or ''} link on {device_name}".replace("  ", " ")
    if name == "share_with_family":
        return f"share “{(args.get('statement') or '')[:60]}” with the family"
    if name == "read_page":
        return f"read {args.get('url')}"
    if name == "web_search":
        return f"search the web for “{args.get('query')}”"
    if name == "phone_send_chat":
        return f"send {args.get('channel') or 'whatsapp'} to {args.get('recipient')} on {device_name}"
    if name == "phone_pull_file":
        return f"pull {args.get('remote_path')} from {device_name}"
    if name == "phone_ui_tap":
        return f"tap ({args.get('x')}, {args.get('y')}) on {device_name}"
    return name.replace("_", " ")


def _filename_from_url(url: str) -> str:
    name = os.path.basename(unquote(urlparse(url).path)) or f"download-{new_id()[:6]}"
    return re.sub(r"[^\w.\- ]", "_", name)


class Toolbox:
    def __init__(self, k: Kairos):
        self.k = k

    def run(self, name: str, args: dict, turn: Turn) -> ToolResult:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return ToolResult(f"Unknown tool {name}.")
        accepted = inspect.signature(fn).parameters  # LLMs sometimes invent extra arguments
        return fn(turn, **{a: v for a, v in args.items() if v is not None and a in accepted})

    def _range(self, turn: Turn, period: str | None, since: int | None, until: int | None,
               default_days: int) -> tuple[int, int]:
        if since is not None:
            return int(since), int(until) if until is not None else turn.now + 1
        return period_range(period, self.k.settings.tz, turn.now, default_days)

    # --- helpers --------------------------------------------------------------
    def _dev(self, device: str | None, turn: Turn) -> str:
        return device or turn.device_id

    def _dev_name(self, device_id: str | None) -> str:
        kind = next((k for k, v in self.k.devices.items() if v == device_id), None)
        return {"phone": "your phone", "pc": "your PC", "chat": "Telegram"}.get(kind, device_id or "this device")

    def _resolve_device(self, spoken: str | None, default: str | None) -> str | None:
        if not spoken:
            return default
        s = spoken.strip().lower()
        if s in self.k.devices.values():
            return s
        for kind, words in (("phone", ("phone", "mobile", "android")), ("pc", ("pc", "laptop", "computer", "desktop")),
                            ("chat", ("telegram", "chat"))):
            if s in words or s == kind:
                return self.k.devices.get(kind, default)
        return default

    def _watch(self, turn: Turn, spec: WatcherSpec, notify_device: str | None) -> str:
        return self.k.watchers.create(spec, turn.utterance_id, self._dev(notify_device, turn))

    # --- watchers ---------------------------------------------------------------
    def t_watch_download(self, turn: Turn, notify_device: str | None = None) -> ToolResult:
        row = self.k.graph.one("latest_open_download", since=turn.now - HOUR_MS)
        if row:
            spec = WatcherSpec(description=f"tell me when {row['filename'] or 'the download'} finishes",
                               condition_type="entity_state", target_label="Download", target_id=row["id"],
                               target_status="completed")
            what = row["filename"] or "the download"
        else:
            spec = WatcherSpec(description="tell me when the next download finishes", condition_type="event_type",
                               event_type="download.completed")
            what = "the next download"
        wid = self._watch(turn, spec, notify_device)
        return ToolResult(f"Okay, I'll tell you on {self._dev_name(self._dev(notify_device, turn))} when {what} finishes.",
                          {"watcher_id": wid}, [("Watcher", wid)])

    def t_watch_ride(self, turn: Turn, provider: str | None = None, eta_threshold: int = 2,
                     notify_device: str | None = None) -> ToolResult:
        name = provider or "ride"
        eta_threshold = int(eta_threshold)
        when = "arrives" if eta_threshold == 0 else f"is {eta_threshold} minutes away"
        spec = WatcherSpec(description=f"remind me when my {name} {when}", condition_type="trip_eta",
                           provider=provider, eta_threshold=eta_threshold)
        wid = self._watch(turn, spec, notify_device)
        return ToolResult(f"Got it. I'll remind you when your {name} {when}.", {"watcher_id": wid}, [("Watcher", wid)])

    def t_watch_app(self, turn: Turn, app: str, keyword: str | None = None,
                    notify_device: str | None = None) -> ToolResult:
        spec = WatcherSpec(description=f"tell me when {app} mentions {keyword}", condition_type="app_text",
                           app=app, keyword=keyword)
        wid = self._watch(turn, spec, notify_device)
        return ToolResult(f"Okay, watching {app} for “{keyword}”.", {"watcher_id": wid}, [("Watcher", wid)])

    def t_watch_for(self, turn: Turn, description: str, notify_device: str | None = None) -> ToolResult:
        desc = re.sub(r"^(please\s+)?(tell|notify|alert|ping|let|remind)\s+me(\s+know)?\s+(when|if)\s+", "",
                      description.strip(), flags=re.I).rstrip(".?!")
        spec = WatcherSpec(description=desc, condition_type="semantic")
        wid = self._watch(turn, spec, notify_device)
        return ToolResult(f"Okay, I'll keep an eye out and tell you on "
                          f"{self._dev_name(self._dev(notify_device, turn))} when {desc}.",
                          {"watcher_id": wid}, [("Watcher", wid)])

    def t_remind_in(self, turn: Turn, minutes: float, message: str, notify_device: str | None = None) -> ToolResult:
        due = turn.now + int(float(minutes) * MINUTE_MS)
        spec = WatcherSpec(description=f"remind me to {message}", condition_type="time", due_ts=due,
                           message=f"Reminder: {message}")
        wid = self._watch(turn, spec, notify_device)
        return ToolResult(f"I'll remind you {fmt_time(due, self.k.settings.tz)}.", {"watcher_id": wid},
                          [("Watcher", wid)])

    def t_list_watchers(self, turn: Turn) -> ToolResult:
        rows = self.k.watchers.list_active()
        if not rows:
            return ToolResult("I'm not watching for anything right now.")
        return ToolResult("I'm watching for: " + "; ".join(r["description"] for r in rows) + ".", {"watchers": rows})

    def t_cancel_watchers(self, turn: Turn) -> ToolResult:
        n = self.k.watchers.cancel()
        return ToolResult(f"Cancelled {n} reminder{'s' if n != 1 else ''}.")

    # --- cross-device -------------------------------------------------------------
    def t_download_from_person(self, turn: Turn, person: str | None = None, ext: str | None = None,
                               device: str | None = None, notify_device: str | None = None) -> ToolResult:
        if not person:
            return ToolResult("Who sent it?")
        links = self.k.graph.run("recall_links_from_person", person=person, since=turn.now - 30 * DAY_MS)
        if ext:
            links = [lk for lk in links if lk["url"].lower().split("?")[0].endswith("." + ext)] or links
        if not links:
            return ToolResult(f"I couldn't find any link from {person} in the last 30 days.")
        link = links[0]
        device = device or self.k.devices.get("pc", turn.device_id)
        download_id = f"dl-{new_id()[:8]}"
        filename = _filename_from_url(link["url"])
        self.k.graph.run("create_download_request", download_id=download_id, url=link["url"], filename=filename,
                         ts=turn.now)
        causes: list[tuple[str, str]] = []
        tail = ""
        if notify_device:
            spec = WatcherSpec(description=f"tell me when {filename} finishes downloading",
                               condition_type="entity_state", target_label="Download", target_id=download_id,
                               target_status="completed")
            wid = self._watch(turn, spec, notify_device)
            causes.append(("Watcher", wid))
            tail = f" I'll tell you on {self._dev_name(notify_device)} when it's done."
        self.k.gateway.send(Outbound(kind="download", device_id=device, text=f"Downloading {filename}",
                                     args={"url": link["url"], "filename": filename, "download_id": download_id},
                                     action_id=turn.action_id))
        turn.session["last_url"] = link["url"]
        return ToolResult(f"Downloading {filename} from {link['person']}'s message “{link['subject']}” "
                          f"on {self._dev_name(device)}.{tail}",
                          {"download_id": download_id, "dispatched": True, **link}, causes)

    def t_open_file(self, turn: Turn, query: str | None = None, device: str | None = None) -> ToolResult:
        if query:
            rows = self.k.graph.run("find_file_by_name", q=query)
            if not rows:
                return ToolResult(f"I couldn't find a file called {query}.")
            f = rows[0]
        else:
            f = turn.session.get("last_file")
            if not f:
                return ToolResult("Which file? Ask me to find it first.")
        target = device or f.get("device") or self.k.devices.get("pc", turn.device_id)
        self.k.gateway.send(Outbound(kind="open", device_id=target, text=f"Opening {f['name']}",
                                     args={"path": f["path"]}, action_id=turn.action_id))
        return ToolResult(f"Opening {f['name']} on {self._dev_name(target)}.", {"path": f["path"]})

    def t_open_link(self, turn: Turn, channel: str | None = None, device: str | None = None) -> ToolResult:
        row = self.k.graph.one("latest_link", channel=channel)
        if not row:
            return ToolResult("I haven't seen any links yet.")
        target = self._dev(device, turn)
        self.k.gateway.send(Outbound(kind="open", device_id=target, text="Opening link", args={"url": row["url"]},
                                     action_id=turn.action_id))
        who = f" from {row['person']}" if row.get("person") else ""
        return ToolResult(f"Opening the link{who} on {self._dev_name(target)}.", row)

    # --- recall -------------------------------------------------------------------
    def t_recall_file(self, turn: Turn, person: str | None = None, ext: str | None = None,
                      since: int | None = None, period: str | None = None) -> ToolResult:
        if not person:
            return ToolResult("Who sent it?")
        ext = ext.lower().lstrip(".") if ext else None
        since, _ = self._range(turn, period, since, None, 30)
        rows = self.k.graph.run("recall_file_from_person", person=person, ext=ext, since=since)
        if not rows and ext:
            rows = self.k.graph.run("recall_file_from_person", person=person, ext=None, since=since)
        if not rows:
            links = self.k.graph.run("recall_links_from_person", person=person, since=since)
            if links:
                lk = links[0]
                turn.session["last_url"] = lk["url"]
                return ToolResult(f"{lk['person']} sent a link in “{lk['subject']}” "
                                  f"{fmt_time(lk['sent_at'], self.k.settings.tz)}, but it hasn't been downloaded yet. "
                                  f"Say “download the file {person} sent” and I'll get it.", lk)
            return ToolResult(f"I don't see anything from {person} in that time.")
        f = rows[0]
        turn.session["last_file"] = {"path": f["path"], "name": f["name"], "device": f["device"]}
        return ToolResult(f"{f['person']} sent {f['name']} {fmt_time(f['sent_at'], self.k.settings.tz)} "
                          f"(“{f['subject']}”). It's saved on {self._dev_name(f['device'])}.",
                          {"file": f, "evidence_path": f["evidence_path"]})

    def t_recall_messages(self, turn: Turn, person: str, since: int | None = None,
                          period: str | None = None) -> ToolResult:
        since, _ = self._range(turn, period, since, None, 7)
        rows = self.k.graph.run("recall_messages_from_person", person=person, since=since)
        if not rows:
            return ToolResult(f"Nothing from {person} in that time.")
        tz = self.k.settings.tz
        items = [f"“{r['subject']}” on {r['channel']} {fmt_time(r['sent_at'], tz)}" for r in rows[:3]]
        if rows[0]["urls"]:
            turn.session["last_url"] = rows[0]["urls"][0]
        return ToolResult(f"{rows[0]['person']} sent: " + "; ".join(items) + ".", {"messages": rows})

    def t_recent_activity(self, turn: Turn, since: int | None = None, until: int | None = None,
                          period: str | None = None) -> ToolResult:
        since, until = self._range(turn, period, since, until, 1)
        rows = self.k.graph.run("recent_activity", since=since, until=until)
        if not rows:
            return ToolResult("I don't have any activity recorded for then.")
        lines = [r["text"] for r in rows[:6]]
        return ToolResult("Here's what you were doing: " + "; ".join(lines) + ".", {"events": rows})

    def t_person_context(self, turn: Turn, person: str) -> ToolResult:
        row = self.k.graph.one("person_context", person=person, uid=self.k.settings.user_id)
        if not row:
            return ToolResult(f"I don't know anyone called {person}.")
        rel = f", your {row['relation']}" if row.get("relation") else ""
        recent = f" Recently: {', '.join(s for s in row['recent_subjects'] if s)}." if row["recent_subjects"] else ""
        notes = self.k.memory.notes_about(person)
        tasks = self.k.tasks.open(person)
        extra = ""
        if notes:
            extra += " Notes: " + "; ".join(n["text"] for n in notes[:2]) + "."
        if tasks:
            extra += " Open: " + "; ".join(describe_task(t, self.k.settings.tz) for t in tasks[:2]) + "."
        return ToolResult(f"{row['name']}{rel}. {row['messages']} messages so far.{recent}{extra}",
                          {**row, "notes": notes, "tasks": tasks})

    def t_find_path(self, turn: Turn, a: str, b: str) -> ToolResult:
        row = self.k.graph.one("find_path", a=a, b=b)
        if not row:
            return ToolResult(f"I can't find a connection between {a} and {b}.")
        steps = [f"{n}" for n in row["names"]]
        chain = steps[0] + "".join(f" →({r})→ {n}" for r, n in zip(row["rels"], steps[1:]))
        return ToolResult(chain, row)

    # --- memory -------------------------------------------------------------------
    def t_remember(self, turn: Turn, statement: str, key: str) -> ToolResult:
        old = self.k.memory.get_fact(key)
        fid = self.k.memory.remember(statement, key, source_event=turn.utterance_id)
        if old and old["statement"] != statement:
            return ToolResult(f"Updated. {statement}. (I still remember that before, {old['statement'].lower()}.)",
                              {"fact_id": fid, "superseded": old["id"]}, [("Fact", fid)])
        return ToolResult(f"Got it, I'll remember that {statement}.", {"fact_id": fid}, [("Fact", fid)])

    def t_get_fact(self, turn: Turn, key: str) -> ToolResult:
        row = self.k.memory.get_fact(key)
        if not row:
            return ToolResult("I don't know that yet. Tell me and I'll remember it.")
        return ToolResult(f"{row['statement']} (since {fmt_time(row['valid_from'], self.k.settings.tz)}).", row,
                          [("Fact", row["id"])])

    def t_fact_history(self, turn: Turn, key: str) -> ToolResult:
        rows = self.k.memory.history(key)
        if not rows:
            return ToolResult("I don't have any history for that.")
        tz = self.k.settings.tz
        items = [f"{r['statement']} ({'current' if not r['valid_to'] else 'until ' + fmt_time(r['valid_to'], tz)})"
                 for r in rows]
        return ToolResult("; ".join(items) + ".", {"history": rows})

    def t_search_memory(self, turn: Turn, q: str) -> ToolResult:
        """Hybrid recall: structured facts, linked notes, then events by meaning/words."""
        found = self.k.memory.search_all(q, exclude=turn.utterance_id)
        person = self.k.graph.one("person_context", person=q, uid=self.k.settings.user_id)
        tz = self.k.settings.tz
        parts = [f["statement"] + "." for f in found["facts"][:3]]
        parts += [f"Note: {n['text']}" for n in found["notes"][:2]]
        parts += [f"On {d['day']}: {d['summary'][:200]}" for d in found.get("days", [])[:1]]
        parts += [f"{fmt_time(e['ts'], tz)}: " + (f"you said “{e['text'][:140]}”" if e.get("type") == "utterance"
                                                    else e["text"][:140]) for e in found["events"][:3]]
        if person:
            parts.append(self.t_person_context(turn, q).text)
        return ToolResult(" ".join(parts) if parts else f"I don't know anything about {q} yet.", found)

    def t_recall_conversation(self, turn: Turn, day: str | None = None) -> ToolResult:
        from kairos.digest import parse_day

        d = parse_day(day, self.k.settings.tz, turn.now)
        text = self.k.digest.conversation(d)
        label = "today" if d == self.k.journal.today(turn.now) else f"on {d:%a %d %b}"
        return ToolResult(f"{label.capitalize()}: {text}" if text else f"I have no conversation logged {label}.",
                          {"day": d.isoformat()})

    def t_digest_memory(self, turn: Turn, day: str | None = None) -> ToolResult:
        from kairos.digest import parse_day

        r = self.k.digest.run(parse_day(day, self.k.settings.tz, turn.now))
        if not r["new"]:
            return ToolResult("Nothing new to file away from that day.", r)
        made = ", ".join(f"{r[n]} {n if r[n] != 1 else n[:-1]}" for n in ("facts", "notes", "people", "tasks") if r[n])
        return ToolResult(f"Filed away {r['new']} messages: {made or 'nothing worth keeping'}.", r)

    def t_forget(self, turn: Turn, topic: str) -> ToolResult:
        n = self.k.memory.forget(topic)
        return ToolResult(f"Done. I've forgotten {n} thing{'s' if n != 1 else ''} about {topic}.")

    def t_device_status(self, turn: Turn, device: str | None = None) -> ToolResult:
        rows = self.k.status.all()
        if device:
            rows = [r for r in rows if r["id"] == device] or rows
        for r in rows:  # ask for a fresh snapshot; the answer below uses the latest one we have
            self.k.gateway.send(Outbound(kind="status_request", device_id=r["id"]))
        if not rows:
            return ToolResult("I don't have any devices registered yet.")
        return ToolResult(" ".join(describe_status(r, turn.now) + "." for r in rows), {"devices": rows})

    def t_list_habits(self, turn: Turn) -> ToolResult:
        rows = self.k.memory.habits()
        if not rows:
            return ToolResult("I haven't picked up any routines yet.")
        items = [f"{describe_habit(h)} (strength {h['strength']:.2f}, seen {h['count']} times)" for h in rows]
        return ToolResult(". ".join(items) + ".", {"habits": rows}, [("Habit", h["id"]) for h in rows])

    def t_briefing(self, turn: Turn) -> ToolResult:
        tz = self.k.settings.tz
        counts = {r["type"]: r["n"] for r in self.k.graph.run("event_counts_since", since=start_of_day(tz, 0, turn.now))}
        watchers = self.k.watchers.list_active()
        habits = self.k.memory.habits(min_strength=0.6)
        parts = [f"Today: {counts.get('notification', 0)} notifications, "
                 f"{counts.get('download.completed', 0)} downloads finished."]
        if watchers:
            parts.append(f"I'm watching for {len(watchers)} thing{'s' if len(watchers) != 1 else ''}: "
                         + "; ".join(w["description"] for w in watchers[:3]) + ".")
        if habits:
            parts.append(describe_habit(habits[0]) + ".")
        meetings = self.k.calendar.upcoming(turn.now, start_of_day(tz, -1, turn.now))
        if meetings:
            parts.append(f"{len(meetings)} meeting(s) left today, next: “{meetings[0]['title']}” "
                         f"{fmt_time(meetings[0]['start_ts'], tz)}.")
        tasks = self.k.tasks.open()
        if tasks:
            parts.append(f"{len(tasks)} open commitment(s): " +
                         "; ".join(describe_task(t, tz) for t in tasks[:3]) + ".")
        return ToolResult(" ".join(parts), {"counts": counts, "tasks": tasks, "meetings": meetings})

    def t_explain(self, turn: Turn, which: str = "any") -> ToolResult:
        tools = ["notify"] if which == "alert" else ACTION_TOOLS
        aid = self.k.audit.last(tools)
        if not aid:
            return ToolResult("I haven't done anything yet that needs explaining.")
        exp = self.k.explainer.explain(aid)
        return ToolResult(exp.text, {"action_id": aid, "chain": exp.chain})

    # --- risky device actions (reach here only after policy/confirmation) -------------
    def t_delete_path(self, turn: Turn, path: str, device: str | None = None) -> ToolResult:
        target = self._dev(device, turn)
        resolved = self.sandbox_path(path)
        if not resolved:
            return ToolResult(f"I can only delete things inside the KAIROS sandbox, not {path}.")
        self.k.gateway.send(Outbound(kind="delete", device_id=target, text=f"Deleting {resolved}",
                                     args={"path": resolved}, action_id=turn.action_id))
        return ToolResult(f"Deleting {path} on {self._dev_name(target)}.", {"path": resolved, "dispatched": True})

    def t_run_command(self, turn: Turn, cmd: str, device: str | None = None) -> ToolResult:
        target = self._dev(device, turn)
        self.k.gateway.send(Outbound(kind="exec", device_id=target, text=f"Running {cmd}", args={"cmd": cmd},
                                     action_id=turn.action_id))
        return ToolResult(f"Running `{cmd}` on {self._dev_name(target)}. I'll tell you the result.",
                          {"cmd": cmd, "dispatched": True})

    # --- phone actions -------------------------------------------------------------
    def t_phone_send_chat(self, turn: Turn, recipient: str, message: str, channel: str = "whatsapp",
                           device: str | None = None) -> ToolResult:
        target = device or self.k.devices.get("phone", turn.device_id)
        self.k.gateway.send(Outbound(kind="chat_send", device_id=target, text=f"Sending {channel} to {recipient}",
                                     args={"recipient": recipient, "message": message, "channel": channel},
                                     action_id=turn.action_id))
        return ToolResult(f"Sending {channel} to {recipient} on {self._dev_name(target)}.",
                          {"recipient": recipient, "channel": channel, "dispatched": True})

    def t_phone_list_files(self, turn: Turn, path: str = "/sdcard/Download", device: str | None = None) -> ToolResult:
        target = device or self.k.devices.get("phone", turn.device_id)
        self.k.gateway.send(Outbound(kind="file_list", device_id=target, text=f"Listing {path}",
                                     args={"path": path}, action_id=turn.action_id))
        return ToolResult(f"Listing files in {path} on {self._dev_name(target)}.", {"path": path, "dispatched": True})

    def t_phone_pull_file(self, turn: Turn, remote_path: str, local_dest: str | None = None,
                          device: str | None = None) -> ToolResult:
        target = device or self.k.devices.get("phone", turn.device_id)
        local_path = local_dest or os.path.join(self.k.settings.sandbox_dir, os.path.basename(remote_path))
        self.k.gateway.send(Outbound(kind="file_pull", device_id=target, text=f"Pulling {remote_path}",
                                     args={"remote_path": remote_path, "local_path": local_path},
                                     action_id=turn.action_id))
        return ToolResult(f"Pulling {remote_path} to {local_path} from {self._dev_name(target)}.",
                          {"remote_path": remote_path, "local_path": local_path, "dispatched": True})

    def t_phone_ui_tap(self, turn: Turn, x: int, y: int, device: str | None = None) -> ToolResult:
        target = device or self.k.devices.get("phone", turn.device_id)
        self.k.gateway.send(Outbound(kind="tap", device_id=target, text=f"Tapping ({x}, {y})",
                                     args={"x": x, "y": y}, action_id=turn.action_id))
        return ToolResult(f"Tapping ({x}, {y}) on {self._dev_name(target)}.", {"x": x, "y": y, "dispatched": True})

    def t_phone_ui_key(self, turn: Turn, key: str, device: str | None = None) -> ToolResult:
        target = device or self.k.devices.get("phone", turn.device_id)
        self.k.gateway.send(Outbound(kind="key", device_id=target, text=f"Pressing {key}",
                                     args={"key": key}, action_id=turn.action_id))
        return ToolResult(f"Pressing {key} on {self._dev_name(target)}.", {"key": key, "dispatched": True})

    def t_phone_screenshot(self, turn: Turn, device: str | None = None) -> ToolResult:
        target = device or self.k.devices.get("phone", turn.device_id)
        dest = os.path.join(self.k.settings.sandbox_dir, f"phone_screenshot_{int(time.time())}.png")
        self.k.gateway.send(Outbound(kind="screenshot", device_id=target, text="Taking screenshot",
                                     args={"path": dest}, action_id=turn.action_id))
        return ToolResult(f"Capturing phone screenshot to {dest} on {self._dev_name(target)}.",
                          {"path": dest, "dispatched": True})

    def t_phone_ui_dump(self, turn: Turn, device: str | None = None) -> ToolResult:
        target = device or self.k.devices.get("phone", turn.device_id)
        self.k.gateway.send(Outbound(kind="ui_dump", device_id=target, text="Inspecting phone screen",
                                     args={}, action_id=turn.action_id))
        return ToolResult(f"Inspecting on-screen elements on {self._dev_name(target)}.", {"dispatched": True})

    def sandbox_path(self, spoken: str) -> str | None:
        root = os.path.abspath(self.k.settings.sandbox_dir)
        name = re.sub(r"\b(folder|directory|dir|files?)\b", "", spoken).strip() or "temp"
        candidate = os.path.abspath(os.path.join(root, name.replace(" ", "_")))
        return candidate if os.path.commonpath([root, candidate]) == root and candidate != root else None

    def t_help(self, turn: Turn) -> ToolResult:
        return ToolResult(HELP)

    # --- notes & tasks (commitments) ------------------------------------------------
    def t_add_note(self, turn: Turn, text: str, about: str | None = None) -> ToolResult:
        names = [n.strip() for n in (about or "").split(",") if n.strip()]
        if not names:  # link to anyone the note mentions by name
            names = [p["name"] for p in self.k.graph.run("list_people", uid=self.k.settings.user_id)
                     if any(part.lower() in text.lower() for part in p["name"].split() if len(part) > 2)]
        people_ids = [self.k.ingestor.resolve_person(n) for n in names[:5]]
        nid = self.k.memory.add_note(text, people_ids, source_event=turn.utterance_id)
        who = f" about {', '.join(names)}" if names else ""
        return ToolResult(f"Noted{who}.", {"note_id": nid, "about": names}, [("Note", nid)])

    def t_list_tasks(self, turn: Turn, person: str | None = None) -> ToolResult:
        tasks = self.k.tasks.open(person)
        if not tasks:
            return ToolResult(f"Nothing open{' with ' + person if person else ''}.")
        tz = self.k.settings.tz
        return ToolResult(f"{len(tasks)} open: " + "; ".join(describe_task(t, tz) for t in tasks[:5]) + ".",
                          {"tasks": tasks}, [("Task", t["id"]) for t in tasks[:5]])

    def t_add_task(self, turn: Turn, title: str, person: str | None = None, due: str | None = None) -> ToolResult:
        due_ts = parse_due(due or title, self.k.settings.tz, turn.now)
        tid = self.k.tasks.create(Commitment(title, "i_promised" if person else "todo", person, due_ts), "user",
                                  turn.utterance_id)
        if not tid:
            return ToolResult("You already have that on your list.")
        when = f" (due {fmt_time(due_ts, self.k.settings.tz)})" if due_ts else ""
        return ToolResult(f"Added: {title}{when}.", {"task_id": tid}, [("Task", tid)])

    def t_complete_task(self, turn: Turn, query: str) -> ToolResult:
        t = self.k.tasks.complete_matching(query)
        if not t:
            return ToolResult("I couldn't find an open task matching that.")
        return ToolResult(f"Marked done: {t['title']}.", {"task": t}, [("Task", t["id"])])

    # --- calendar --------------------------------------------------------------------
    def t_upcoming_meetings(self, turn: Turn, period: str | None = None) -> ToolResult:
        tz = self.k.settings.tz
        if period == "today":
            since, until = turn.now, start_of_day(tz, -1, turn.now)
        elif period == "tomorrow":
            since, until = start_of_day(tz, -1, turn.now), start_of_day(tz, -2, turn.now)
        else:
            since, until = turn.now, turn.now + DAY_MS
        meetings = self.k.calendar.upcoming(since, until)
        if not meetings:
            return ToolResult("No meetings in that window." if self.k.settings.calendar_urls
                              else "No calendar connected yet (set KAIROS_CALENDAR_URLS).")
        items = [f"“{m['title']}” {fmt_time(m['start_ts'], tz)}" +
                 (f" with {', '.join(m['people'][:3])}" if m["people"] else "") for m in meetings[:5]]
        return ToolResult("; ".join(items) + ".", {"meetings": meetings})

    def t_meeting_prep(self, turn: Turn, query: str | None = None) -> ToolResult:
        meetings = self.k.calendar.upcoming(turn.now - 30 * MINUTE_MS, turn.now + 7 * DAY_MS)
        if query:
            meetings = [m for m in meetings if query.lower() in m["title"].lower()] or meetings
        if not meetings:
            return ToolResult("I don't see an upcoming meeting.")
        m = meetings[0]
        return ToolResult(self.k.calendar.prep(m, turn.now), {"meeting": m}, [("Meeting", m["id"])])

    # --- graph algorithms ------------------------------------------------------------
    def t_important_people(self, turn: Turn, days: int = 14) -> ToolResult:
        ranked = important_people(self.k.graph, self.k.settings.user_id, turn.now, days=int(days))
        if not ranked:
            return ToolResult("I don't have enough interactions yet to tell.")
        return ToolResult("Right now: " + "; ".join(f"{r['name']} ({r['why']})" for r in ranked) + ".",
                          {"people": ranked})

    def t_recent_sites(self, turn: Turn, query: str | None = None, period: str | None = None) -> ToolResult:
        since, until = self._range(turn, period, None, None, 1)
        rows = self.k.graph.run("recent_browser_visits", since=since, until=until, q=query or "")
        if not rows:
            return ToolResult("I don't see any matching pages.")
        tz = self.k.settings.tz
        return ToolResult("; ".join(f"{r['title'] or r['url']} ({fmt_time(r['ts'], tz)})" for r in rows[:5]) + ".",
                          {"visits": rows})

    # --- location ----------------------------------------------------------------------
    def t_watch_place(self, turn: Turn, place: str, transition: str = "entered", message: str | None = None,
                      notify_device: str | None = None) -> ToolResult:
        known = [p["name"] for p in self.k.graph.run("list_places")]
        name = normalize_place(place, known)
        transition = "left" if transition.lower().startswith(("left", "leave", "exit")) else "entered"
        verb = "leave" if transition == "left" else "get to"
        spec = WatcherSpec(description=f"when I {verb} {name}: {message or 'remind me'}", condition_type="place",
                           place=name, transition=transition, message=f"Reminder: {message}" if message else None)
        wid = self._watch(turn, spec, notify_device or self.k.devices.get("phone"))
        hint = "" if name in known else f" (I don't have {name}'s location yet, so add it to your places)"
        return ToolResult(f"Okay, I'll remind you when you {verb} {name}{hint}.", {"watcher_id": wid},
                          [("Watcher", wid)])

    # --- cross-device handoff ------------------------------------------------------------
    def t_send_file(self, turn: Turn, query: str | None = None, to_device: str | None = None) -> ToolResult:
        if query:
            rows = self.k.graph.run("find_file_by_name", q=query)
            f = rows[0] if rows else None
        else:
            f = turn.session.get("last_file")
        if not f:
            return ToolResult("Which file? Ask me to find it first.")
        source = f.get("device") or self.k.devices.get("pc", turn.device_id)
        target = self._resolve_device(to_device, self.k.devices.get("phone", turn.device_id))
        if target == source:
            return ToolResult(f"{f['name']} is already on {self._dev_name(target)}.")
        t = self.k.transfers.create(turn.action_id, f["name"], source, target)
        self.k.gateway.send(Outbound(kind="upload", device_id=source, text=f"Sending {f['name']}",
                                     args={"path": f["path"], "transfer_id": t.id,
                                           "upload_url": f"{self.k.settings.hub_public_url}/api/transfer/{t.id}"},
                                     action_id=turn.action_id))
        return ToolResult(f"Sending {f['name']} from {self._dev_name(source)} to {self._dev_name(target)}.",
                          {"transfer_id": t.id, "dispatched": True})

    def t_send_clipboard(self, turn: Turn, from_device: str | None = None, to_device: str | None = None) -> ToolResult:
        source = self._resolve_device(from_device, self.k.devices.get("pc", turn.device_id))
        target = self._resolve_device(to_device, self.k.devices.get("phone", turn.device_id))
        self.k.gateway.send(Outbound(kind="clipboard_get", device_id=source, text="Reading clipboard",
                                     args={"to_device": target}, action_id=turn.action_id))
        return ToolResult(f"Copying the clipboard from {self._dev_name(source)} to {self._dev_name(target)}.",
                          {"dispatched": True, "to_device": target})

    def t_send_ui_message(self, turn: Turn, message: str, recipient: str | None = None, app: str | None = None,
                           press_enter: bool = True, device: str | None = None) -> ToolResult:
        target = self._dev(device, turn)
        who = f" to {recipient}" if recipient else ""
        self.k.gateway.send(Outbound(
            kind="type_message",
            device_id=target,
            text=f"Typing message{who} in {app or 'active window'}",
            args={"text": message, "recipient": recipient, "app": app, "press_enter": bool(press_enter)},
            action_id=turn.action_id
        ))
        dest = f"{app} on {self._dev_name(target)}" if app else self._dev_name(target)
        action_verb = "Sent message" if press_enter else "Typed draft"
        return ToolResult(f"{action_verb}{who} in {dest}.",
                          {"text": message, "recipient": recipient, "app": app, "press_enter": press_enter, "dispatched": True})

    def t_read_notifications(self, turn: Turn, app: str | None = None, query: str | None = None,
                              limit: int | None = 10, device: str | None = None) -> ToolResult:
        target = self._resolve_device(device, self.k.devices.get("pc", turn.device_id))
        filter_parts = []
        if app:
            filter_parts.append(f"app '{app}'")
        if query:
            filter_parts.append(f"matching '{query}'")
        desc = (" for " + " and ".join(filter_parts)) if filter_parts else ""

        if turn.action_id and hasattr(self.k, "register_action_waiter"):
            self.k.register_action_waiter(turn.action_id)

        self.k.gateway.send(Outbound(
            kind="notifications_get",
            device_id=target,
            text=f"Reading PC notifications{desc}",
            args={"app": app, "query": query, "limit": limit or 10},
            action_id=turn.action_id
        ))

        if turn.action_id and hasattr(self.k, "wait_for_device_result"):
            res = self.k.wait_for_device_result(turn.action_id, timeout=4.0)
            if res:
                ok, output = res
                return ToolResult(output, {"app": app, "query": query, "output": output})

        return ToolResult(f"Checking PC notifications{desc} on {self._dev_name(target)}...",
                          {"app": app, "query": query, "dispatched": True})

    def t_take_screenshot(self, turn: Turn, device: str | None = None) -> ToolResult:
        target = self._resolve_device(device, self.k.devices.get("pc", turn.device_id))
        if turn.action_id and hasattr(self.k, "register_action_waiter"):
            self.k.register_action_waiter(turn.action_id)

        self.k.gateway.send(Outbound(
            kind="screenshot",
            device_id=target,
            text="Capturing PC desktop screenshot",
            args={},
            action_id=turn.action_id
        ))

        if turn.action_id and hasattr(self.k, "wait_for_device_result"):
            res = self.k.wait_for_device_result(turn.action_id, timeout=4.0)
            if res:
                ok, output = res
                return ToolResult(output, {"output": output})

        return ToolResult(f"Capturing screenshot on {self._dev_name(target)}...", {"dispatched": True})

    # --- read-only web --------------------------------------------------------------------
    def resolve_url_ref(self, ref: str | None, turn: Turn) -> str | None:
        """'2' / 'result 2' / 'last' -> a URL from this session; anything else is returned as-is."""
        if ref is None:
            return None
        s = str(ref).strip()
        m = re.fullmatch(r"(?:result\s*)?#?(\d{1,2})", s, re.I)
        results = turn.session.get("search_results") or []
        if m and results:
            i = int(m.group(1)) - 1
            return results[i]["url"] if 0 <= i < len(results) else None
        if s.lower() in ("last", "it", "that", "this", "the page", "that page"):
            return turn.session.get("last_url")
        if not s.startswith(("http://", "https://")) and re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", s, re.I):
            return "https://" + s
        return s

    def url_is_known(self, url: str | None, turn: Turn) -> bool:
        """Provenance check: reading is automatic only for URLs we already have a reason to trust the user wants -
        a search result, something the user typed, or a link already in the graph (messages, history, reads)."""
        if not url:
            return False
        if url in [r["url"] for r in turn.session.get("search_results") or []]:
            return True
        if url in (turn.session.get("last_utterance") or "") or url == turn.session.get("last_url"):
            return True
        try:
            return bool(self.k.graph.one("link_known", url=url))
        except Exception:
            return False

    def _record_web(self, type_: str, payload: dict, turn: Turn) -> None:
        from kairos.models import EventEnvelope

        try:
            self.k.ingest(EventEnvelope(user_id=self.k.settings.user_id, device_id=turn.device_id, type=type_,
                                        source="web", payload=payload))
        except Exception:  # memory is best-effort; never fail the answer because of it
            pass

    def t_web_search(self, turn: Turn, query: str, max_results: int = 5) -> ToolResult:
        from kairos.web import WebError

        try:
            results = self.k.web.search(query, max(1, min(int(max_results), 8)))
        except WebError as exc:
            return ToolResult(f"I couldn't search the web: {exc}")
        except Exception as exc:
            return ToolResult(f"Web search failed ({type(exc).__name__}). Am I online?")
        if not results:
            return ToolResult(f"No web results for “{query}”.")
        rows = [{"rank": r.rank, "title": r.title, "url": r.url, "snippet": r.snippet, "domain": r.domain}
                for r in results]
        turn.session["search_results"] = rows
        turn.session["last_query"] = query
        self._record_web("web.search", {"query": query, "results": rows, "key": f"{query}|{turn.now}"}, turn)
        lines = [f"{r['rank']}. {r['title']} ({r['domain']}): {r['snippet']}" for r in rows]
        return ToolResult("Web results:\n" + "\n".join(lines), {"results": rows})

    def t_read_page(self, turn: Turn, url: str, focus: str | None = None) -> ToolResult:
        from kairos.web import WebError, focus_text

        target = self.resolve_url_ref(url, turn)
        if not target:
            return ToolResult("Which page? Search first, or give me a link.")
        try:
            page = self.k.web.read(target)
        except WebError as exc:
            return ToolResult(f"I couldn't read that page: {exc}")
        except Exception as exc:
            return ToolResult(f"Reading the page failed ({type(exc).__name__}).")
        text = focus_text(page.text, focus or turn.session.get("last_query"), self.k.settings.web_max_chars)
        turn.session["last_url"] = page.url
        self._record_web("web.read", {"url": page.url, "title": page.title, "domain": page.domain,
                                      "excerpt": text[:1500], "key": f"{page.url}|{turn.now}"}, turn)
        return ToolResult(f"{page.title} ({page.domain}, via {page.backend}):\n{text}",
                          {"url": page.url, "title": page.title, "backend": page.backend})

    # --- family -----------------------------------------------------------------------
    def t_share_with_family(self, turn: Turn, statement: str, key: str | None = None) -> ToolResult:
        if not self.k.family.enabled:
            return ToolResult("Family sharing isn't set up (set KAIROS_FAMILY_GRAPH).")
        fid = self.k.family.share(statement, key)
        return ToolResult("Shared with the family.", {"shared_id": fid})

    def t_family_memory(self, turn: Turn, q: str | None = None) -> ToolResult:
        if not self.k.family.enabled:
            return ToolResult("Family sharing isn't set up (set KAIROS_FAMILY_GRAPH).")
        rows = self.k.family.search(q)
        if not rows:
            return ToolResult("Nothing shared by the family yet.")
        tz = self.k.settings.tz
        return ToolResult("; ".join(f"{r['member']}: {r['statement']} ({fmt_time(r['ts'], tz)})" for r in rows[:5])
                          + ".", {"shared": rows})
