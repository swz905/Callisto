"""Wires the brain together. Used by the hub, the CLI, the simulator and the MCP server."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from kairos.agent.core import Agent, AgentReply
from kairos.agent.llm import make_llm
from kairos.agent.tools import openai_tools
from kairos.calendar_ics import CalendarSync
from kairos.config import Settings, load_settings
from kairos.digest import MemoryDigest
from kairos.email_imap import ImapPoller
from kairos.embed import make_embedder
from kairos.explain import Explainer
from kairos.family import FamilyMemory
from kairos.gateway import Gateway, LocalGateway
from kairos.graph.client import Graph
from kairos.handoff import TransferStore
from kairos.ingest.pipeline import Ingestor
from kairos.journal import ChatJournal
from kairos.memory import Memory, describe_habit
from kairos.models import EventEnvelope, Outbound
from kairos.plugins import PluginManager
from kairos.policy import AuditLog, Policy
from kairos.status import StatusTracker
from kairos.tasks import TaskManager, describe_task
from kairos.timeutil import MINUTE_MS, now_ms
from kairos.triage import Triage
from kairos.trust import EXTERNAL
from kairos.watchers import Fired, WatcherEngine
from kairos.web import WebService

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def result_message(tool: str, ok: bool, output: str, summary: str) -> str | None:
    """What to tell the user when a device finishes. Downloads already alert via watchers, so success is quiet."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    first = (lines[0] if lines else "")[:160]
    if tool == "run_command":
        brief = " | ".join(lines[:8])[:500]  # several lines, not just the header row
        return f"Command finished: {brief}" if ok and brief else "Command finished." if ok else \
            f"The command failed: {first or 'no output'}"
    if tool == "delete_path":
        return "Deleted." if ok else f"Couldn't delete it: {first}"
    if tool == "download_from_person":
        return None if ok else f"The download failed: {first}"
    if tool == "send_file":
        return f"Sent: {first}" if ok else f"Couldn't send the file: {first}"
    if tool == "send_clipboard":
        return "Clipboard sent." if ok else f"Couldn't read the clipboard: {first}"
    return None if ok else f"That didn't work ({summary}): {first}"


@dataclass
class IngestOutcome:
    event_id: str
    duplicate: bool
    alerts: list[dict] = field(default_factory=list)


class Kairos:
    def __init__(self, settings: Settings | None = None, gateway: Gateway | None = None, graph: Graph | None = None,
                 llm_transport=None):
        self.settings = settings or load_settings()
        self.graph = graph or Graph(self.settings)
        self.gateway: Gateway = gateway or LocalGateway()
        embedder = make_embedder(self.settings.embeddings)
        self.llm = make_llm(self.settings, transport=llm_transport)
        self.ingestor = Ingestor(self.graph, self.settings, self.llm.extractor, embedder)
        self.watchers = WatcherEngine(self.graph)
        self.policy = Policy(self.graph)
        self.audit = AuditLog(self.graph)
        self.memory = Memory(self.graph, self.settings, embedder)
        self.explainer = Explainer(self.graph, self.settings.tz)
        self.tasks = TaskManager(self)
        self.calendar = CalendarSync(self)
        self.imap = ImapPoller(self)
        self.family = FamilyMemory(self)
        self.transfers = TransferStore(self.settings.data_dir)
        self.web = WebService(self.settings)
        self.journal = ChatJournal(self.settings.data_dir, self.settings.tz, self.settings.chatlog,
                                   self.settings.chatlog_days)
        self.digest = MemoryDigest(self)
        self.plugins = PluginManager(self, Path(self.settings.plugins_file or ROOT / "plugins.json"))
        self.agent = Agent(self, self.llm)
        self.triage = Triage(self)
        self.status = StatusTracker(self)
        self.telegram = None  # started by the hub when TELEGRAM_BOT_TOKEN is set
        self.devices: dict[str, str] = {}
        # Serialises graph writes (WS handlers, sim threads, ticks). LLM network calls happen outside it.
        self.lock = threading.RLock()
        self.refresh_devices()
        if self.llm.planner is not None:  # built-in tools + whatever MCP plugins are connected
            self.llm.planner.set_tools_provider(lambda: openai_tools() + self.plugins.specs())

    def refresh_devices(self) -> None:
        try:
            rows = self.graph.run("list_devices", uid=self.settings.user_id)
        except Exception:  # graph not seeded yet
            rows = []
        self.devices = {r["kind"]: r["id"] for r in rows if r.get("kind")}

    def llm_status(self) -> dict:
        return self.llm.status()

    # --- Flow A + C: events in, watchers fire, alerts out ------------------------
    def ingest(self, env: EventEnvelope) -> IngestOutcome:
        if env.type == "device.status":  # snapshot: update live state, ingest only the changes
            return self.status.apply(env)
        with self.lock:
            res = self.ingestor.ingest(env)
            if res.duplicate:
                return IngestOutcome(res.event_id, True)
            alerts = [self._alert(f) for f in self.watchers.on_event(res.event_id)]
            if res.linked.get("message"):  # "can you send me the deck by Friday?" -> open commitment
                try:
                    self.tasks.from_message(res.linked.get("text") or env.text(), res.linked.get("sender"),
                                            res.event_id, EXTERNAL)
                except Exception:
                    log.exception("commitment extraction failed")
        # Triage (importance + fuzzy requests) runs after exact watchers, outside the lock.
        if self.triage.eligible(env):
            self.triage.submit(env, already_alerted=bool(alerts))
        return IngestOutcome(res.event_id, False, alerts)

    def start_background(self) -> None:
        """Start workers (hub / interactive CLI): triage queue, MCP plugins. Without them triage runs inline."""
        self.triage.start()
        try:
            self.plugins.start()
        except Exception:
            log.exception("plugins failed to start")

    def background_jobs(self) -> dict:
        """Every minute: pull integrations that are due (network I/O happens outside the graph lock)."""
        now, done = now_ms(), {}
        if self.settings.calendar_urls and now - self.calendar.last_sync >= self.settings.calendar_interval_s * 1000:
            done["calendar"] = self.calendar.sync()
        if self.imap.configured and now - self.imap.last_poll >= self.settings.imap_interval_s * 1000:
            try:
                done["email"] = self.imap.poll()
            except Exception as exc:
                log.warning("IMAP poll failed: %s", exc)
        try:
            if digested := self.digest.maybe_run(now):
                done["digest"] = digested
        except Exception:
            log.exception("memory digest failed")
        return done

    def _route(self, preferred: str | None) -> str:
        if preferred:
            return preferred
        row = self.graph.one("route_device", uid=self.settings.user_id, since=now_ms() - 5 * MINUTE_MS)
        return row["device_id"] if row else self.devices.get("phone") or next(iter(self.devices.values()), "unknown")

    def _alert(self, f: Fired) -> dict:
        device = self._route(f.notify_device)
        text = f"Heads up: {f.event_text}" if f.event_text else f"Heads up: {f.description}"
        causes = [("Watcher", f.watcher_id)] + ([("Event", f.event_id)] if f.event_id else [])
        aid = self.audit.record("notify", {"device": device, "text": text}, "notify", "done",
                                f"alerted you on {device}: {text}", device, causes)
        self.gateway.send(Outbound(kind="alert", device_id=device, text=text, action_id=aid))
        return {"action_id": aid, "device": device, "text": text, "watcher_id": f.watcher_id}

    def _notify(self, text: str, summary: str, causes: list[tuple[str, str]], kind: str = "alert",
                device: str | None = None) -> dict:
        device = self._route(device)
        aid = self.audit.record("notify", {"device": device, "text": text}, "notify", "done", summary, device, causes)
        self.gateway.send(Outbound(kind=kind, device_id=device, text=text, action_id=aid))
        return {"action_id": aid, "device": device, "text": text}

    # --- Flow B: voice / chat ------------------------------------------------------
    def chat(self, text: str, device_id: str, session_id: str | None = None) -> AgentReply:
        if not self.devices:
            self.refresh_devices()
        return self.agent.handle(text, device_id, session_id)

    def confirm_action(self, action_id: str, approve: bool) -> str:
        return self.agent.confirm_by_id(action_id, approve)

    # --- Flow E: periodic jobs --------------------------------------------------------
    def tick(self) -> list[dict]:
        """Every few seconds: time watchers, habit suggestions, task due reminders, meeting prep packs."""
        with self.lock:
            now = now_ms()
            alerts = [self._alert(f) for f in self.watchers.due(now)]
            for h in self.memory.due_suggestions(now):
                text = f"{describe_habit(h)}. Want me to remind you when your ride is close?"
                alerts.append(self._notify(text, "suggested a routine", [("Habit", h["id"])], kind="speak"))
                self.memory.mark_suggested(h["id"])
            for t in self.tasks.due_soon(now):
                text = f"Reminder: {describe_task(t, self.settings.tz)}."
                alerts.append(self._notify(text, f"reminded you about: {t['title']}", [("Task", t["id"])]))
            for m in self.calendar.due_preps(now):
                text = self.calendar.prep(m, now)
                self.graph.run("mark_meeting_prepped", id=m["id"], now=now)
                alerts.append(self._notify(text, f"prepped you for {m['title']}", [("Meeting", m["id"])],
                                           kind="speak"))
            return alerts

    def consolidate(self) -> dict:
        """Every N minutes / nightly: strengthen habits, decay stale ones."""
        with self.lock:
            habits = self.memory.detect_habits()
            archived = self.memory.decay()
            return {"habits": habits, "archived": archived}

    def device_result(self, action_id: str, ok: bool, output: str) -> dict | None:
        """A device reports the outcome of an exec/delete/download/upload/clipboard read: update the original
        Action and tell the user on the device the request came from (`origin_device`)."""
        with self.lock:
            row = self.graph.one("get_action", id=action_id)
            if not row:
                log.warning("result for unknown action %s", action_id)
                return None
            keep = 4000 if row["tool"] == "run_command" else 500  # commands keep their full output in the audit
            if row["status"] not in ("dispatched", "done"):  # cancelled/denied/... : record, don't re-announce
                self.audit.update(action_id, row["status"], output[:keep])
                return None
            if row["tool"] == "send_clipboard" and ok:  # forward the text; never store clipboard contents
                target = json.loads(row.get("args_json") or "{}").get("to_device") or self.devices.get("phone")
                self.gateway.send(Outbound(kind="clipboard_set", device_id=target, args={"text": output},
                                           action_id=action_id))
                output = f"{len(output)} characters copied to {target}"
            self.audit.update(action_id, "done" if ok else "failed", output[:keep])
            text = result_message(row["tool"], ok, output, row.get("summary") or row["tool"])
            if not text:
                return None
            device = self._route(row.get("origin_device") or row.get("device_id"))
        # The model reads the real output and answers the user's request (LLM call, so outside the lock).
        if row["tool"] == "run_command":
            try:
                text = self.agent.followup(row, ok, output) or text
            except Exception:
                log.exception("result follow-up failed")
        self.gateway.send(Outbound(kind="alert", device_id=device, text=text, action_id=action_id))
        return {"action_id": action_id, "device": device, "text": text, "ok": ok}

    def transfer_received(self, transfer_id: str, data: bytes) -> dict:
        """A device uploaded a file for a handoff: store it, open it on the target, report to the Action."""
        t = self.transfers.receive(transfer_id, data)
        url = f"{self.settings.hub_public_url}/api/transfer/{t.id}/{t.name}"
        self.gateway.send(Outbound(kind="open", device_id=t.target, text=f"File from your other device: {t.name}",
                                   args={"url": url}, action_id=t.action_id))
        if t.action_id:
            self.device_result(t.action_id, True, f"{t.name} → {t.target}")
        return {"name": t.name, "target": t.target, "url": url}
