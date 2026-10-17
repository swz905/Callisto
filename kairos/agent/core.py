"""Flow B: utterance -> episodic memory -> plan -> policy -> tools -> audited actions -> reply.

Planner choice per turn:
  pending yes/no         -> deterministic (safety decisions are never left to a model)
  OpenRouter available   -> LLM tool loop (taint-tracked: see kairos/trust.py)
  otherwise / on failure -> offline rule planner
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kairos.agent.intents import confirmation
from kairos.agent.llm import LLMStack, TurnContext
from kairos.agent.openrouter import LLMUnavailable
from kairos.agent.tools import TOOL_SPECS, UNLOCKED_TOOLS, ToolResult, Toolbox, Turn, capability_for, summarize
from kairos.diag import diag_log
from kairos.memory import describe_habit
from kairos.models import EventEnvelope, new_id
from kairos.policy import Decision
from kairos.status import describe_status
from kairos.tasks import describe_task
from kairos.timeutil import DAY_MS, fmt_time, now_ms, to_local
from kairos.trust import EXTERNAL_RESULT_TOOLS, Taint, needs_confirmation_when_tainted, wrap_untrusted

if TYPE_CHECKING:
    from kairos.runtime import Kairos

log = logging.getLogger(__name__)
HISTORY_MESSAGES = 8
ALWAYS = re.compile(r"\b(always|all (future|further|next|such)|every time|from now on|don'?t (ask|need to ask))\b", re.I)
HISTORY_OUTPUT_CHARS = 1500  # how much command output stays in the chat history
MODEL_OUTPUT_CHARS = 4000  # how much the model reads when it explains a result
DEVICE_ARGS = ("device", "notify_device", "to_device", "from_device")


def _status(data: dict) -> str:
    """'dispatched' = sent to a device that will report the outcome; 'done' otherwise."""
    return "dispatched" if data.get("dispatched") else "done"


@dataclass
class AgentReply:
    text: str
    utterance_id: str
    actions: list[str] = field(default_factory=list)
    data: list[dict] = field(default_factory=list)
    planner: str = "rules"


class Agent:
    def __init__(self, k: Kairos, llm: LLMStack):
        self.k = k
        self.llm = llm
        self.toolbox = Toolbox(k)
        self.sessions: dict[str, dict] = {}

    def handle(self, text: str, device_id: str, session_id: str | None = None) -> AgentReply:
        session_id = session_id or f"session-{device_id}"
        session = self.sessions.get(session_id)
        if session is None:  # first turn since the hub started: pick the conversation up from today's chat log
            session = self.sessions[session_id] = {"id": session_id, "device": device_id}
            if journal := getattr(self.k, "journal", None):
                session["history"] = journal.recent(session_id, HISTORY_MESSAGES)
        session["last_utterance"] = text  # links the user typed themselves count as known (read without asking)
        now = now_ms()

        # 1. the utterance itself is episodic memory (and may contain a commitment: "I'll send it by Friday")
        env = EventEnvelope(user_id=self.k.settings.user_id, device_id=device_id, type="utterance", source="voice",
                            payload={"text": text, "speaker": "user", "session_id": session_id, "key": new_id()})
        utterance_id = self.k.ingest(env).event_id
        try:
            with self.k.lock:
                self.k.tasks.from_utterance(text, utterance_id)
        except Exception:
            log.exception("commitment extraction failed")
        turn = Turn(device_id=device_id, utterance_id=utterance_id, session=session, now=now)
        reply = AgentReply("", utterance_id)

        # 2. pending confirmation: deterministic
        pending = self.k.audit.pending()
        answer = confirmation(text) if pending else None
        if answer is not None:
            trusted = bool(answer and ALWAYS.search(text))
            reply.text, aid = self._resolve_pending(pending, answer, turn, trust=trusted)
            reply.actions += [aid] if aid else []
            return self._remember_turn(session, text, reply, now)

        ctx = TurnContext(device_id=device_id, devices=self.k.devices, now=now, tz=self.k.settings.tz,
                          session=session, pending_action=pending)

        # 3. LLM tool loop when available (taint-tracked)
        if self.llm.planner is not None and self.llm.planner.available():
            texts: list[str] = []
            turn.taint = Taint(tracking=True)

            def execute(name: str, args: dict) -> str:
                out, aid, data = self._execute(name, args, turn)
                texts.append(out)
                reply.actions += [aid] if aid else []
                reply.data.append(data)
                if name in EXTERNAL_RESULT_TOOLS or self.k.plugins.has(name):
                    turn.taint.add(f"tool {name}")
                    return wrap_untrusted(out)
                return out

            try:
                ctx.card = self.context_card(turn, text)
                reply.text = self.llm.planner.run(text, ctx, execute)
                reply.planner = "openrouter"
                return self._remember_turn(session, text, reply, now)
            except LLMUnavailable as exc:
                if texts:  # tools already ran; don't run them twice via the rule planner
                    reply.text = " ".join(texts)
                    reply.planner = "openrouter(partial)"
                    return self._remember_turn(session, text, reply, now)
                log.info("LLM unavailable (%s); falling back to rules", exc)
                turn.taint = Taint()  # rules read no external text as instructions

        # 4. offline rule planner
        plan = self.llm.rules.plan(text, ctx)
        if not plan.calls:
            reply.text = plan.reply or "Sorry, I didn't catch that."
            return self._remember_turn(session, text, reply, now)
        outs = []
        for call in plan.calls:
            out, aid, data = self._execute(call.name, call.args, turn)
            outs.append(out)
            reply.actions += [aid] if aid else []
            reply.data.append(data)
        reply.text = " ".join(outs)
        return self._remember_turn(session, text, reply, now)

    def _remember_turn(self, session: dict, user_text: str, reply: AgentReply, ts: int) -> AgentReply:
        hist = session.setdefault("history", [])
        hist += [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply.text}]
        del hist[:-HISTORY_MESSAGES]
        if journal := getattr(self.k, "journal", None):  # the day's log on disk: restarts + the nightly digest
            sid, dev = session.get("id", "session"), session.get("device", "unknown")
            journal.append(sid, dev, "user", user_text, ts)
            journal.append(sid, dev, "assistant", reply.text)
        return reply

    # --- tool execution through the permission graph ------------------------------
    def _normalize_devices(self, args: dict, turn: Turn) -> dict:
        args = dict(args)
        known = set(self.k.devices.values())
        for key in DEVICE_ARGS:
            v = args.get(key)
            if v is None:
                continue
            v = str(v).strip().lower()
            if v in ("this", "current", "here", "this device"):
                args[key] = turn.device_id
            elif v in self.k.devices:  # 'phone' / 'pc' / 'chat'
                args[key] = self.k.devices[v]
            elif v in ("laptop", "computer", "desktop"):
                args[key] = self.k.devices.get("pc", turn.device_id)
            elif v in ("mobile", "android"):
                args[key] = self.k.devices.get("phone", turn.device_id)
            elif v in ("telegram",):
                args[key] = self.k.devices.get("chat", turn.device_id)
            elif args[key] not in known:
                args[key] = None
        return args

    def _execute(self, name: str, args: dict, turn: Turn) -> tuple[str, str | None, dict]:
        is_plugin = self.k.plugins.has(name)
        if name not in TOOL_SPECS and not is_plugin:
            return f"Unknown tool {name}.", None, {}
        args = self._normalize_devices(args, turn)
        capability = self.k.plugins.capability(name) if is_plugin else capability_for(name, args)
        if name == "read_page":  # provenance: unknown URLs (not searched/sent/typed) need the human's OK
            args["url"] = self.toolbox.resolve_url_ref(args.get("url"), turn) or args.get("url")
            if not self.toolbox.url_is_known(args.get("url"), turn):
                capability = "net.read_unknown"
        target = args.get("device") or turn.device_id
        summary = (f"use the {name.split('__')[1]} plugin tool {name.split('__')[-1]}" if is_plugin
                   else summarize(name, args, self.toolbox._dev_name(target)))
        causes = [("Event", turn.utterance_id)]
        remote_origin = turn.device_id if self.k.devices.get("chat") == turn.device_id else None
        with self.k.lock:
            decision, _risk = self.k.policy.check_pair(target, remote_origin, capability)
            tainted = turn.taint.tainted
            if capability == "net.read_unknown" and decision == Decision.AUTO:
                decision = Decision.CONFIRM  # enforced in code too: a URL nobody gave us is how data gets exfiltrated
            if tainted and decision == Decision.AUTO and needs_confirmation_when_tainted(capability):
                decision = Decision.CONFIRM  # an email/web page may be steering the model: ask the human
                summary += " (requested while reading untrusted content)"
            # The id exists before the tool runs, so device messages carry it and results come back to it.
            aid = self.k.audit.new_action_id()
            meta = {"action_id": aid, "origin_device": turn.device_id, "tainted": tainted,
                    "taint_sources": turn.taint.describe() if tainted else None,
                    "influenced_by": turn.taint.event_ids if tainted else []}
            diag_log("POLICY_CHECK", f"Tool '{name}' -> decision: {decision.value} (target={target}, cap={capability}, aid={aid})",
                     tool=name, target=target, decision=decision.value, capability=capability, action_id=aid, args=args)
            if decision == Decision.DENY:
                diag_log("POLICY_DENIED", f"Tool '{name}' was DENIED by policy", tool=name, target=target, capability=capability)
                self.k.audit.record(name, args, capability, "denied", summary, target, causes, **meta)
                return f"I'm not allowed to {summary}.", aid, {}
            if decision == Decision.CONFIRM:
                diag_log("POLICY_CONFIRM", f"Tool '{name}' requires human confirmation", tool=name, summary=summary)
                self.k.audit.record(name, args, capability, "pending_confirmation", summary, target, causes, **meta)
                return f"Just to confirm: {summary}? Say yes or no.", aid, {}
        turn.action_id = aid
        diag_log("TOOL_START", f"Running tool '{name}' (action_id={aid})", tool=name, action_id=aid, args=args)
        try:
            result = self._run_tool(name, args, turn, is_plugin)
            diag_log("TOOL_RETURN", f"Tool '{name}' returned (len={len(result.text)}): \"{result.text[:120]}\"",
                     tool=name, action_id=aid, text=result.text, data=result.data)
        except Exception as exc:  # a tool failing must not kill the turn
            log.exception("tool %s failed", name)
            diag_log("TOOL_ERROR", f"Tool '{name}' FAILED: {exc!r}", tool=name, action_id=aid, error=repr(exc))
            with self.k.lock:
                self.k.audit.record(name, args, capability, "failed", summary, target, causes, result=str(exc), **meta)
            return f"Something went wrong while trying to {summary}.", aid, {}
        finally:
            turn.action_id = None
        with self.k.lock:
            self.k.audit.record(name, args, capability, _status(result.data), summary, target,
                                causes + result.causes, result=result.text[:500], **meta)
        return result.text, aid, result.data

    def _run_tool(self, name: str, args: dict, turn: Turn, is_plugin: bool) -> ToolResult:
        """Network-bound tools (web, plugins) run outside the graph lock so ingestion never waits on them."""
        if is_plugin:
            return ToolResult(self.k.plugins.call(name, args))
        if name in UNLOCKED_TOOLS:
            return self.toolbox.run(name, args, turn)
        with self.k.lock:
            return self.toolbox.run(name, args, turn)

    def _resolve_pending(self, pending: dict, approve: bool, turn: Turn, trust: bool = False) -> tuple[str, str | None]:
        aid = pending["id"]
        with self.k.lock:
            if turn.utterance_id:
                self.k.graph.run_template("link_cause", "Event", action_id=aid, cause_id=turn.utterance_id)
            if not approve:
                self.k.audit.update(aid, "cancelled")
                return "Okay, I won't.", aid
        args = json.loads(pending["args_json"] or "{}")
        note = ""
        if trust:  # "yes, always": remember it in the permission graph for this device + capability
            target = pending.get("device_id") or args.get("device") or turn.device_id
            cap = capability_for(pending["tool"], args)
            if self.k.policy.trust(target, cap):
                note = f" I'll run {cap} actions on that device without asking from now on."
        turn.action_id = aid  # the confirmed action keeps its id; the device reports back to it
        try:
            result = self._run_tool(pending["tool"], args, turn, self.k.plugins.has(pending["tool"]))
        except Exception as exc:
            log.exception("confirmed tool %s failed", pending["tool"])
            with self.k.lock:
                self.k.audit.update(aid, "failed", str(exc))
            return "That failed, sorry.", aid
        finally:
            turn.action_id = None
        with self.k.lock:
            self.k.audit.update(aid, _status(result.data), result.text[:500])
        return result.text + note, aid

    def confirm_by_id(self, action_id: str, approve: bool) -> str:
        """Approve/deny from the dashboard."""
        row = self.k.graph.one("get_action", id=action_id)
        if not row or row["status"] != "pending_confirmation":
            return "That action is no longer waiting for confirmation."
        device = row.get("origin_device") or row.get("device_id") or self.k.devices.get("pc", "pc-main")
        turn = Turn(device_id=device, utterance_id=None,
                    session=self.sessions.setdefault(f"session-{device}", {"id": f"session-{device}", "device": device}),
                    now=now_ms())
        return self._resolve_pending(row, approve, turn)[0]

    # --- device results go back to the model ------------------------------------------
    def followup(self, row: dict, ok: bool, output: str) -> str | None:
        """A device finished a command: put its output in the chat history (so later turns can see it) and, when
        the model is available, let it answer the user's original request from that output. Returns the text to
        tell the user, or None to fall back to the plain device message."""
        device = row.get("origin_device") or row.get("device_id") or self.k.devices.get("pc", "pc-main")
        session = self.sessions.setdefault(f"session-{device}", {})
        hist = session.setdefault("history", [])
        shown = (output or "").strip()
        status = "finished" if ok else "FAILED"
        summary = row.get("summary") or row.get("tool") or "a command"
        hist.append({"role": "assistant", "content":
                     f"[Command result: {summary} {status}]\n{shown[:HISTORY_OUTPUT_CHARS] or '(no output)'}"})
        del hist[:-HISTORY_MESSAGES]
        planner = self.llm.planner
        if planner is None or not planner.available():
            return None
        turn = Turn(device_id=device, utterance_id=None, session=session, now=now_ms())
        ctx = TurnContext(device_id=device, devices=self.k.devices, now=turn.now, tz=self.k.settings.tz,
                          session=session, card=self.context_card(turn))
        try:
            text = planner.explain_result(ctx, summary, shown[:MODEL_OUTPUT_CHARS], ok)
        except LLMUnavailable as exc:
            log.info("LLM unavailable for result follow-up (%s)", exc)
            return None
        if text:
            hist.append({"role": "assistant", "content": text})
            del hist[:-HISTORY_MESSAGES]
        return text or None

    # --- GraphRAG context card for the LLM (design doc §7.3) ------------------------
    def context_card(self, turn: Turn, utterance: str = "") -> str:
        """Compact graph context. Text written by other people is wrapped in <untrusted> and taints the turn."""
        s, g, tz = self.k.settings, self.k.graph, self.k.settings.tz
        lines = [f"Now: {to_local(turn.now, tz):%a %d %b %Y, %I:%M %p} ({tz})."]
        devs = ", ".join(f"{kind} = {did}{' (this device)' if did == turn.device_id else ''}"
                         for kind, did in self.k.devices.items())
        lines.append(f"Devices: {devs or turn.device_id}.")
        for st in self.k.status.all():  # live state from periodic snapshots
            if st.get("kind") == "chat":
                continue
            line = describe_status(st, turn.now)
            if st.get("foreground_title"):  # window titles can be web page titles: someone else's text
                line = wrap_untrusted(line)
            lines.append(f"Live status: {line}.")
        people = g.run("list_people", uid=s.user_id)
        if people:
            lines.append("People: " + "; ".join(f"{p['name']} ({p['relation']})" for p in people[:12]) + ".")
        facts = g.run("current_facts", uid=s.user_id)
        if facts:
            lines.append("Facts (key: statement): " + "; ".join(f"{f['key']}: {f['statement']}" for f in facts) + ".")
        tasks = self.k.tasks.open()
        if tasks:
            lines.append("Open commitments: " + "; ".join(describe_task(t, tz) for t in tasks[:5]) + ".")
        meetings = self.k.calendar.upcoming(turn.now, turn.now + DAY_MS)
        if meetings:
            lines.append("Upcoming meetings: " + "; ".join(
                f"{m['title']} {fmt_time(m['start_ts'], tz)} with {', '.join(m['people'][:3]) or 'no one listed'}"
                for m in meetings[:3]) + ".")
        watchers = self.k.watchers.list_active()
        if watchers:
            lines.append("Watching for: " + "; ".join(w["description"] for w in watchers[:5]) + ".")
        habits = self.k.memory.habits(min_strength=0.5)
        if habits:
            lines.append("Routines: " + "; ".join(f"{describe_habit(h)} (strength {h['strength']:.2f})"
                                                   for h in habits[:3]) + ".")
        # entity linking: people the user mentioned -> their neighbourhood in the graph
        mentioned = [p for p in people if any(re.search(rf"\b{re.escape(part)}\b", utterance, re.I)
                                              for part in p["name"].split() if len(part) > 2)][:3]
        for p in mentioned:
            notes = self.k.memory.notes_about(p["name"])
            msgs = g.run("recall_messages_from_person", person=p["name"], since=turn.now - 14 * DAY_MS)[:2]
            bits = [f"{p['name']} ({p['relation']})"]
            if notes:
                bits.append("notes: " + "; ".join(n["text"] for n in notes[:2]))
            if msgs:
                turn.taint.add(f"messages from {p['name']}")
                bits.append(wrap_untrusted("recent messages: " + "; ".join(
                    f"“{m['subject']}” {fmt_time(m['sent_at'], tz)}" for m in msgs)))
            lines.append("About " + " | ".join(bits) + ".")
        recent = [e for e in g.run("recent_events")[:10] if e["type"] != "utterance"][:8]
        if recent:
            lines.append("Recent events: " + wrap_untrusted(" | ".join(
                f"{fmt_time(e['ts'], tz)} {e['text'][:90]}" for e in recent)) + ".")
        if digest := getattr(self.k, "digest", None):  # the user's own words, filed away nightly
            days = [d for d in digest.recent(2) if d.get("summary")]
            if days:
                lines.append("Recent days (from your chats): " + " | ".join(
                    f"{d['day']}: {d['summary'][:240]}" for d in days) + ".")
        if f := turn.session.get("last_file"):
            lines.append(f"Last file found: {f['name']} on {f.get('device') or 'unknown device'}.")
        if u := turn.session.get("last_url"):
            lines.append(f"Last link mentioned: {u}.")
        return "\n".join(lines)
