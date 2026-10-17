"""KAIROS hub: device agents connect over WebSocket; REST API for the web dashboard, simulator and tests.

Run: kairos hub   (or: uvicorn kairos.hub.app:app --host 0.0.0.0 --port 8000)
Dashboard: http://localhost:8000/
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import re
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kairos.config import load_settings
from kairos.diag import clear_diagnostics, diag_log, get_diagnostic_events, get_diagnostic_text
from kairos.models import EventEnvelope, Outbound
from kairos.timeutil import MINUTE_MS, now_ms

log = logging.getLogger("kairos.hub")

STATIC = Path(__file__).parent / "static"
TICK_SECONDS = 5
CONSOLIDATE_SECONDS = 600
JOBS_SECONDS = 60


class HubGateway:
    """Delivers Outbound messages to connected devices (thread-safe: tools run in a threadpool).

    Devices that aren't connected can be played by a simulator (`fallback`) so the demo works without hardware.
    Every message is also kept in `outbox` for the dashboard."""

    def __init__(self) -> None:
        self.queues: dict[str, asyncio.Queue] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.fallback = None
        self.channels: dict[str, object] = {}  # device_id -> channel with .deliver(msg) (e.g. Telegram)
        self.outbox: deque = deque(maxlen=200)
        self._seq = itertools.count(1)

    def connect(self, device_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.queues[device_id] = q
        return q

    def disconnect(self, device_id: str) -> None:
        self.queues.pop(device_id, None)

    def send(self, msg: Outbound) -> bool:
        channel = self.channels.get(msg.device_id)
        if channel is not None:
            delivered = channel.deliver(msg)
        else:
            delivered = self.push(msg.device_id, msg.model_dump(), quiet=self.fallback is not None)
        simulated = not delivered and channel is None and self.fallback is not None
        if simulated:
            self.fallback.send(msg)
        self.outbox.append({"seq": next(self._seq), "ts": now_ms(), "device_id": msg.device_id, "kind": msg.kind,
                            "text": msg.text, "args": msg.args, "delivered": delivered, "simulated": simulated,
                            "action_id": msg.action_id})
        diag_log("GATEWAY_SEND", f"Dispatched {msg.kind} to {msg.device_id} (action_id={msg.action_id}, delivered={delivered})",
                 device_id=msg.device_id, kind=msg.kind, action_id=msg.action_id, text=msg.text,
                 args=msg.args, delivered=delivered, simulated=simulated)
        return delivered or simulated

    def push(self, device_id: str, payload: dict, quiet: bool = False) -> bool:
        q = self.queues.get(device_id)
        if q is None or self.loop is None:
            if not quiet:
                log.warning("device %s offline; dropped %s", device_id, payload.get("kind"))
            return False
        self.loop.call_soon_threadsafe(q.put_nowait, payload)
        return True


class ChatIn(BaseModel):
    device_id: str
    text: str
    session_id: str | None = None


class ConfirmIn(BaseModel):
    approve: bool


gateway = HubGateway()
state: dict = {}


async def _every(seconds: int, fn) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await run_in_threadpool(fn)
        except Exception:
            log.exception("periodic job %s failed", getattr(fn, "__name__", fn))


def _attach_to_default_desktop() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            u32 = ctypes.windll.user32
            hwinsta = u32.OpenWindowStationW("WinSta0", False, 0x037F)
            if hwinsta:
                u32.SetProcessWindowStation(hwinsta)
            hdesk = u32.OpenDesktopW("Default", 0, False, 0x01FF)
            if hdesk:
                u32.SetThreadDesktop(hdesk)
        except Exception:
            pass


def _spawn_pet_process() -> subprocess.Popen:
    si = None
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.lpDesktop = r"WinSta0\Default"
    return subprocess.Popen([sys.executable, "-m", "kairos.pet"], startupinfo=si)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from kairos.gateway import LocalGateway
    from kairos.runtime import Kairos

    settings = load_settings()
    gateway.loop = asyncio.get_running_loop()
    k = Kairos(settings, gateway=gateway)
    if not await run_in_threadpool(k.graph.ping):
        msg = (f"FalkorDB is not reachable at {settings.falkordb_host}:{settings.falkordb_port}. "
               f"Start it with `docker compose up -d falkordb`, then run `kairos doctor`.")
        log.error(msg)
        raise RuntimeError(msg)
    if settings.simulate_devices:
        gateway.fallback = LocalGateway(printer=lambda s: log.info("simulated device %s", s), simulate=True,
                                        emit=k.ingest, user_id=settings.user_id, on_result=k.device_result)
    await run_in_threadpool(k.graph.apply_schema)
    await run_in_threadpool(k.start_background)
    if settings.telegram_bot_token:
        from kairos.channels.telegram import TelegramChannel

        tg_id = settings.telegram_device_id or k.devices.get("chat") or f"telegram-{settings.user_id}"
        k.telegram = TelegramChannel(k, settings.telegram_bot_token, set(settings.telegram_allowed_chat_ids), tg_id)
        gateway.channels[tg_id] = k.telegram
        k.telegram.start()
    state["k"] = k
    tasks = [asyncio.create_task(_every(TICK_SECONDS, k.tick)),
             asyncio.create_task(_every(CONSOLIDATE_SECONDS, k.consolidate)),
             asyncio.create_task(_every(JOBS_SECONDS, k.background_jobs))]
    asyncio.get_running_loop().run_in_executor(None, k.background_jobs)  # first calendar/email pull now
    log.info("KAIROS hub up: graph %s, llm %s, dashboard http://localhost:8000/", k.settings.graph_name,
             k.llm_status())

    f9_stop = threading.Event()
    if sys.platform == "win32":
        def hub_f9_watcher():
            import ctypes
            _attach_to_default_desktop()
            was_down = False
            while not f9_stop.is_set():
                try:
                    # 0x78 is VK_F9; also check Ctrl+Shift+K (0x11, 0x10, 0x4B)
                    f9 = bool(ctypes.windll.user32.GetAsyncKeyState(0x78) & 0x8000)
                    ctrl = bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
                    shift = bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
                    k_key = bool(ctypes.windll.user32.GetAsyncKeyState(0x4B) & 0x8000)
                    is_down = f9 or (ctrl and shift and k_key)
                    if is_down and not was_down:
                        print("[HOTKEY] F9 / Ctrl+Shift+K triggered: activating pet avatar...", flush=True)
                        _attach_to_default_desktop()
                        hwnd = ctypes.windll.user32.FindWindowW(None, "KAIROS")
                        if not hwnd:
                            _spawn_pet_process()
                        else:
                            ctypes.windll.user32.ShowWindow(hwnd, 5)
                            ctypes.windll.user32.SetForegroundWindow(hwnd)
                    was_down = is_down
                except Exception:
                    pass
                time.sleep(0.06)

        threading.Thread(target=hub_f9_watcher, daemon=True).start()

    yield
    f9_stop.set()
    for t in tasks:
        t.cancel()


app = FastAPI(title="KAIROS hub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _k():
    return state["k"]


# --- core API (devices, simulator, tests) ------------------------------------------
@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/architecture")
async def architecture() -> FileResponse:
    return FileResponse(STATIC / "architecture.html")


@app.get("/diagram")
async def diagram() -> FileResponse:
    return FileResponse(STATIC / "diagram.html")


def _demo_pdf(title: str) -> bytes:
    """A tiny valid one-page PDF, so the demo's real download works without the internet."""
    text = title.replace("(", "").replace(")", "")[:60]
    stream = f"BT /F1 24 Tf 72 720 Td (KAIROS demo: {text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@app.get("/demo/{name}")
async def demo_file(name: str) -> Response:
    if not re.fullmatch(r"[\w\-. ]{1,80}\.pdf", name):
        raise HTTPException(404)
    return Response(_demo_pdf(name[:-4]), media_type="application/pdf")


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "devices_connected": sorted(gateway.queues)}


@app.post("/events")
async def post_event(env: EventEnvelope) -> dict:
    out = await run_in_threadpool(_k().ingest, env)
    return {"event_id": out.event_id, "duplicate": out.duplicate, "alerts": out.alerts}


@app.post("/chat")
async def chat(body: ChatIn) -> dict:
    diag_log("USER_INPUT", f"User request from {body.device_id}: \"{body.text}\"",
             device_id=body.device_id, text=body.text, session_id=body.session_id)
    reply = await run_in_threadpool(_k().chat, body.text, body.device_id, body.session_id)
    diag_log("AGENT_REPLY", f"Replied to {body.device_id}: \"{reply.text[:140]}\"",
             reply=reply.text, actions=reply.actions, planner=reply.planner)
    return {"reply": reply.text, "actions": reply.actions, "data": reply.data, "planner": reply.planner}


# --- voice: audio in -> local speech-to-text -> the normal chat turn -------------------------
MAX_AUDIO_BYTES = 8 * 1024 * 1024


def _stt():
    if "stt" not in state:
        from kairos.voice.stt import SpeechToText

        s = load_settings()
        state["stt"] = SpeechToText(s.stt_model, s.stt_device, s.stt_language)
    return state["stt"]


@app.get("/api/voice/status")
async def api_voice_status() -> dict:
    return _stt().status()


@app.post("/api/voice")
async def api_voice(request: Request, device_id: str = "pc-main", session_id: str | None = None,
                    reply: bool = True) -> dict:
    """Body = audio (16-bit WAV preferred). Returns what you said and, unless reply=false, KAIROS's answer."""
    from kairos.voice.stt import STTUnavailable

    data = await request.body()
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too long")
    try:
        text = await run_in_threadpool(_stt().transcribe, data)
    except STTUnavailable as exc:
        raise HTTPException(503, str(exc))
    except ValueError as exc:  # unsupported WAV
        raise HTTPException(400, str(exc))
    if not text or not reply:
        return {"text": text, "reply": None}
    r = await run_in_threadpool(_k().chat, text, device_id, session_id)
    return {"text": text, "reply": r.text, "actions": r.actions, "planner": r.planner}


@app.get("/watchers")
async def watchers() -> list[dict]:
    return await run_in_threadpool(_k().watchers.list_active)


@app.get("/explain/{action_id}")
async def explain(action_id: str) -> dict:
    exp = await run_in_threadpool(_k().explainer.explain, action_id)
    return {"text": exp.text, "chain": exp.chain}


@app.post("/jobs/consolidate")
async def consolidate() -> dict:
    return await run_in_threadpool(_k().consolidate)


@app.post("/jobs/digest")
async def digest(day: str = "today") -> dict:
    """File a day of conversation into memory now (normally runs by itself every night)."""
    from kairos.digest import parse_day

    k = _k()
    return await run_in_threadpool(k.digest.run, parse_day(day, k.settings.tz))


@app.get("/api/chatlog")
async def api_chatlog(day: str = "today") -> dict:
    """One day of the on-disk chat log, for the dashboard."""
    from kairos.digest import parse_day

    k = _k()
    d = parse_day(day, k.settings.tz)
    lines = await run_in_threadpool(k.journal.read_day, d)
    return {"day": d.isoformat(), "path": str(k.journal.path_for(d)), "enabled": k.journal.enabled,
            "lines": [ln.__dict__ for ln in lines[-200:]]}


# --- dashboard API --------------------------------------------------------------------
@app.get("/api/status")
async def api_status() -> dict:
    k = _k()

    def build():
        from kairos.status import describe_status

        devices = k.status.all()
        for d in devices:
            d["summary"] = describe_status(d)
        counts = {r["label"]: r["n"] for r in k.graph.run("graph_counts")}
        return devices, counts

    devices, counts = await run_in_threadpool(build)
    for d in devices:
        d["connected"] = d["id"] in gateway.queues
    return {"user": k.settings.user_id, "graph": k.settings.graph_name, "llm": k.llm_status(),
            "simulate_devices": gateway.fallback is not None, "devices": devices, "counts": counts}


@app.get("/api/events")
async def api_events() -> list[dict]:
    return await run_in_threadpool(_k().graph.run, "recent_events")


@app.get("/api/recent_messages")
async def api_recent_messages(limit: int = 5) -> list[dict]:
    """Recent incoming notifications, chats, and alerts formatted for sequential display."""
    k = _k()

    def fetch() -> list[dict]:
        items: list[dict] = []
        try:
            events = k.graph.run("recent_events")
            for e in events:
                etype = e.get("type") or ""
                text = (e.get("text") or "").strip()
                if not text:
                    continue
                if etype in ("notification", "utterance", "message", "chat") or e.get("importance", 0) >= 2:
                    sender = "Notification"
                    body = text
                    if ":" in text:
                        parts = text.split(":", 1)
                        sender, body = parts[0].strip(), parts[1].strip()
                    elif " — " in text:
                        parts = text.split(" — ", 1)
                        sender, body = parts[0].strip(), parts[1].strip()
                    items.append({
                        "id": str(e.get("id")),
                        "type": etype,
                        "sender": sender,
                        "text": body,
                        "raw": text,
                        "ts": e.get("ts"),
                        "device": e.get("device") or "unknown"
                    })
                if len(items) >= limit:
                    break
        except Exception as exc:
            log.warning("error fetching recent events: %s", exc)

        if not items:
            for out in reversed(gateway.outbox):
                t = (out.get("text") or "").strip()
                if t:
                    items.append({
                        "id": str(out.get("seq")),
                        "type": out.get("kind"),
                        "sender": "KAIROS",
                        "text": t,
                        "raw": t,
                        "ts": out.get("ts"),
                        "device": out.get("device_id") or "pc-main"
                    })
                if len(items) >= limit:
                    break
        return items

    return await run_in_threadpool(fetch)


@app.api_route("/api/launch_pet", methods=["GET", "POST"])
async def api_launch_pet() -> dict:
    """Launch or bring to front the desktop KAIROS pet avatar."""
    if sys.platform == "win32":
        import ctypes
        _attach_to_default_desktop()
        hwnd = ctypes.windll.user32.FindWindowW(None, "KAIROS")
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 5)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return {"status": "focused", "hwnd": hwnd}
        else:
            proc = _spawn_pet_process()
            return {"status": "launched", "pid": proc.pid}
    return {"status": "not_windows"}


@app.get("/api/actions")
async def api_actions() -> list[dict]:
    return await run_in_threadpool(_k().graph.run, "recent_actions")


@app.post("/api/actions/{action_id}/confirm")
async def api_confirm(action_id: str, body: ConfirmIn) -> dict:
    return {"reply": await run_in_threadpool(_k().confirm_action, action_id, body.approve)}


@app.post("/api/watchers/{watcher_id}/cancel")
async def api_cancel_watcher(watcher_id: str) -> dict:
    return {"cancelled": await run_in_threadpool(_k().watchers.cancel, watcher_id)}


@app.get("/api/memory")
async def api_memory() -> dict:
    k = _k()

    def build():
        now = now_ms()
        return {"facts": k.graph.run("all_facts", uid=k.settings.user_id),
                "habits": k.memory.habits(min_strength=0.0),
                "people": k.memory.people(),
                "notes": k.graph.run("list_notes"),
                "tasks": k.tasks.open(),
                "meetings": k.calendar.upcoming(now - 60 * MINUTE_MS, now + 7 * 24 * 60 * MINUTE_MS),
                "digests": k.digest.recent(7)}

    return await run_in_threadpool(build)


# --- memory editor (the user is in control of their memory) ------------------------------
class FactIn(BaseModel):
    statement: str
    key: str | None = None


class PersonIn(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    relation: str | None = None


class MergeIn(BaseModel):
    keep: str
    merge: str


class NoteIn(BaseModel):
    text: str
    about: str | None = None


class TaskIn(BaseModel):
    title: str
    person: str | None = None
    due: str | None = None


def _audit_edit(k, tool: str, args: dict, summary: str) -> None:
    k.audit.record(tool, args, "memory.write", "done", summary, "dashboard", [], origin_device="dashboard")


@app.post("/api/facts")
async def api_fact(body: FactIn) -> dict:
    from kairos.agent.intents import fact_key

    k = _k()
    key = body.key or fact_key(body.statement)

    def run():
        with k.lock:
            fid = k.memory.remember(body.statement, key)
            _audit_edit(k, "edit_fact", {"key": key, "statement": body.statement}, f"set {key} from the dashboard")
            return fid

    return {"id": await run_in_threadpool(run), "key": key}


@app.post("/api/facts/{fact_id}/archive")
async def api_fact_archive(fact_id: str) -> dict:
    k = _k()

    def run():
        with k.lock:
            ok = k.memory.archive_fact(fact_id)
            _audit_edit(k, "forget_fact", {"id": fact_id}, "forgot a fact from the dashboard")
            return ok

    return {"archived": await run_in_threadpool(run)}


@app.post("/api/people/merge")
async def api_people_merge(body: MergeIn) -> dict:
    k = _k()

    def run():
        with k.lock:
            out = k.memory.merge_people(body.keep, body.merge)
            _audit_edit(k, "merge_people", body.model_dump(), "merged two people from the dashboard")
            return out

    result = await run_in_threadpool(run)
    if result is None:
        raise HTTPException(400, "cannot merge a person into themselves")
    return result


@app.post("/api/people/{person_id}")
async def api_person(person_id: str, body: PersonIn) -> dict:
    k = _k()

    def run():
        with k.lock:
            ok = k.memory.update_person(person_id, body.name, body.aliases, body.relation)
            _audit_edit(k, "edit_person", {"id": person_id, **body.model_dump()}, "edited a person from the dashboard")
            return ok

    return {"updated": await run_in_threadpool(run)}


@app.post("/api/notes")
async def api_note(body: NoteIn) -> dict:
    from kairos.agent.tools import Turn

    k = _k()
    turn = Turn(device_id="dashboard", utterance_id=None, session={}, now=now_ms())

    def run():
        with k.lock:
            return k.agent.toolbox.t_add_note(turn, body.text, body.about).data

    return await run_in_threadpool(run)


@app.post("/api/notes/{note_id}/archive")
async def api_note_archive(note_id: str) -> dict:
    k = _k()
    return {"archived": await run_in_threadpool(k.memory.archive_note, note_id)}


@app.post("/api/tasks")
async def api_task(body: TaskIn) -> dict:
    from kairos.agent.tools import Turn

    k = _k()
    turn = Turn(device_id="dashboard", utterance_id=None, session={}, now=now_ms())

    def run():
        with k.lock:
            return k.agent.toolbox.t_add_task(turn, body.title, body.person, body.due).text

    return {"reply": await run_in_threadpool(run)}


@app.post("/api/tasks/{task_id}/done")
async def api_task_done(task_id: str) -> dict:
    k = _k()
    return {"done": await run_in_threadpool(k.tasks.complete, task_id)}


@app.get("/api/memory/export.md")
async def api_memory_export() -> Response:
    from kairos.export import memory_markdown

    k = _k()

    def build():
        m = {"facts": k.graph.run("all_facts", uid=k.settings.user_id), "people": k.memory.people(),
             "notes": k.graph.run("list_notes"), "tasks": k.tasks.open(), "habits": k.memory.habits(min_strength=0.0),
             "meetings": k.calendar.upcoming(now_ms(), now_ms() + 7 * 24 * 60 * MINUTE_MS)}
        return memory_markdown(k.settings.user_display_name or k.settings.user_id, tz=k.settings.tz, **m)

    md = await run_in_threadpool(build)
    return Response(md, media_type="text/markdown",
                    headers={"Content-Disposition": 'attachment; filename="kairos-memory.md"'})


# --- cross-device file handoff -------------------------------------------------------------
@app.post("/api/transfer/{transfer_id}")
async def api_transfer_upload(transfer_id: str, request: Request) -> dict:
    k = _k()
    if request.headers.get("x-kairos-token") != k.settings.device_token:
        raise HTTPException(401, "bad device token")
    data = await request.body()
    try:
        return await run_in_threadpool(k.transfer_received, transfer_id, data)
    except KeyError:
        raise HTTPException(404, "unknown or expired transfer")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/transfer/{transfer_id}/{name}")
async def api_transfer_download(transfer_id: str, name: str) -> FileResponse:
    t = _k().transfers.get(transfer_id)  # the 128-bit id is the capability; expires after an hour
    if t is None or t.path is None or not t.path.exists() or name != t.name:
        raise HTTPException(404)
    return FileResponse(t.path, filename=t.name)


@app.get("/api/graph")
async def api_graph(minutes: int = Query(24 * 60, ge=1, le=60 * 24 * 60)) -> dict:
    rows = await run_in_threadpool(_k().graph.run, "graph_snapshot", since=now_ms() - minutes * MINUTE_MS)
    nodes: dict[int, dict] = {}
    edges = []
    for r in rows:
        for side in ("src", "dst"):
            labels = [lbl for lbl in r[f"{side}_labels"] if lbl != "Event"] or ["Event"]
            nodes.setdefault(r[side], {"id": r[side], "label": labels[0], "name": str(r[f"{side}_name"] or "")[:80]})
        edges.append({"source": r["src"], "target": r["dst"], "rel": r["rel"]})
    return {"nodes": list(nodes.values()), "edges": edges}


@app.get("/api/outbox")
async def api_outbox(after: int = 0) -> list[dict]:
    return [m for m in list(gateway.outbox) if m["seq"] > after]


@app.post("/api/sim/{scenario}")
async def api_sim(scenario: str, speed: float = 2.0) -> dict:
    from kairos.sim import load_scenario, replay

    k = _k()
    try:
        sc = load_scenario(scenario)
    except FileNotFoundError:
        raise HTTPException(404, f"no scenario {scenario}")
    threading.Thread(target=replay, args=(sc, k.ingest, k.settings.user_id, speed),
                     kwargs={"log": lambda s: log.info(s)}, daemon=True).start()
    return {"started": scenario, "steps": len(sc["steps"])}


# --- device WebSocket -------------------------------------------------------------------
@app.websocket("/ws/{device_id}")
async def device_socket(ws: WebSocket, device_id: str, token: str = Query("")) -> None:
    k = _k()
    if token != k.settings.device_token:
        await ws.close(code=4401)
        return
    await ws.accept()
    q = gateway.connect(device_id)
    await run_in_threadpool(k.graph.run, "set_device_online", device_id=device_id, online=True, now=now_ms())
    await run_in_threadpool(k.refresh_devices)

    async def pump() -> None:  # the only task that writes to the socket
        while True:
            await ws.send_json(await q.get())

    sender = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("kind")
            if kind == "event":
                env = EventEnvelope(**msg["event"])
                out = await run_in_threadpool(k.ingest, env)
                gateway.push(device_id, {"kind": "ack", "event_id": out.event_id})
            elif kind == "chat":
                reply = await run_in_threadpool(k.chat, msg["text"], device_id, msg.get("session_id"))
                gateway.push(device_id, {"kind": "speak", "device_id": device_id, "text": reply.text})
            elif kind == "result" and msg.get("action_id"):
                diag_log("DEVICE_RESULT", f"Device {device_id} reported action {msg.get('action_id')} (ok={msg.get('ok')}): {str(msg.get('output', ''))[:140]}",
                         device_id=device_id, action_id=msg.get("action_id"), ok=bool(msg.get("ok")), output=str(msg.get("output", "")))
                await run_in_threadpool(k.device_result, msg["action_id"], bool(msg.get("ok")), str(msg.get("output", "")))
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        with contextlib.suppress(Exception):
            await sender
        gateway.disconnect(device_id)
        await run_in_threadpool(k.graph.run, "set_device_online", device_id=device_id, online=False, now=now_ms())


@app.get("/api/diagnostics")
async def api_diagnostics(limit: int = 100, stage: str | None = None) -> dict:
    """Retrieve structured diagnostic log events for real-time monitoring and debugging."""
    return {"events": get_diagnostic_events(limit=limit, stage=stage)}


@app.get("/api/diagnostics/text")
async def api_diagnostics_text(limit: int = 100) -> Response:
    """Retrieve plain text human-readable diagnostics log."""
    return Response(content=get_diagnostic_text(limit=limit), media_type="text/plain; charset=utf-8")


@app.post("/api/diagnostics/clear")
async def api_diagnostics_clear() -> dict:
    """Clear memory ring buffer and truncate diagnostics.log."""
    clear_diagnostics()
    return {"status": "cleared"}
