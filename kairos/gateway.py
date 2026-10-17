"""How the brain reaches devices. The hub uses a WebSocket gateway; the CLI uses LocalGateway."""

from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

from kairos.models import EventEnvelope, Outbound


class Gateway(Protocol):
    def send(self, msg: Outbound) -> bool: ...


ResultCallback = Callable[[str, bool, str], object]  # (action_id, ok, output) -> Kairos.device_result


class LocalGateway:
    """Prints outbound messages. With `simulate=True` it also plays the devices: downloads complete, and
    exec/delete report a (simulated) result back to their Action, so the full demo runs without hardware."""

    def __init__(self, printer: Callable[[str], None] = print, simulate: bool = True,
                 emit: Callable[[EventEnvelope], None] | None = None, user_id: str = "arjun",
                 on_result: ResultCallback | None = None):
        self.printer = printer
        self.simulate = simulate
        self.emit = emit
        self.user_id = user_id
        self.on_result = on_result

    def send(self, msg: Outbound) -> bool:
        if msg.kind == "status_request":  # simulated devices have no live status to refresh
            return True
        tag = f"[-> {msg.device_id}] {msg.kind.upper()}"
        try:
            self.printer(f"{tag}: {msg.text or msg.args}")
        except Exception:
            try:
                text = str(msg.text or msg.args).encode("ascii", "replace").decode("ascii")
                self.printer(f"{tag}: {text}")
            except Exception:
                pass
        if self.simulate and self.emit and msg.kind == "download":
            threading.Thread(target=self._fake_download, args=(msg,), daemon=True).start()
        elif self.simulate and msg.kind in ("exec", "delete", "type_message", "chat_send", "file_list", "file_pull", "tap", "key", "screenshot", "ui_dump"):
            threading.Thread(target=self._fake_result, args=(msg,), daemon=True).start()
        return True

    def _report(self, msg: Outbound, ok: bool, output: str) -> None:
        # Runs on another thread: the caller records the Action (under the runtime lock) before this lands.
        if self.on_result and msg.action_id:
            self.on_result(msg.action_id, ok, output)

    def _fake_result(self, msg: Outbound, seconds: float = 0.5) -> None:
        time.sleep(seconds)
        if msg.kind == "exec":
            self._report(msg, True, f"(simulated) ran {msg.args.get('cmd')}")
        elif msg.kind == "type_message":
            self._report(msg, True, f"(simulated) typed message in {msg.args.get('app') or 'active window'}")
        elif msg.kind == "delete":
            self._report(msg, True, f"(simulated) deleted {msg.args.get('path')}")
        elif msg.kind == "chat_send":
            self._report(msg, True, f"(simulated) sent {msg.args.get('channel')} to {msg.args.get('recipient')}")
        elif msg.kind == "file_list":
            self._report(msg, True, f"(simulated) listed {msg.args.get('path')}: [photo.jpg, doc.pdf]")
        elif msg.kind == "file_pull":
            self._report(msg, True, f"(simulated) pulled {msg.args.get('remote_path')} to {msg.args.get('local_path')}")
        elif msg.kind == "tap":
            self._report(msg, True, f"(simulated) tapped ({msg.args.get('x')}, {msg.args.get('y')})")
        elif msg.kind == "key":
            self._report(msg, True, f"(simulated) pressed key {msg.args.get('key')}")
        elif msg.kind == "screenshot":
            self._report(msg, True, f"(simulated) captured screenshot to {msg.args.get('path')}")
        elif msg.kind == "notifications_get":
            self._report(msg, True, "(simulated) [WhatsApp] Anushka: Hey are you free? (5m ago)")
        elif msg.kind == "ui_dump":
            self._report(msg, True, "(simulated) UI dump: Button 'OK' @ [100,200][300,400]")
        else:
            self._report(msg, True, "Simulated: Done.")

    def _fake_download(self, msg: Outbound, seconds: float = 3.0) -> None:
        a = msg.args
        path = f"C:\\Users\\arjun\\Downloads\\{a['filename']}"
        base = {"user_id": self.user_id, "device_id": msg.device_id, "source": "chrome"}
        self.emit(EventEnvelope(type="download.started", payload={
            "download_id": a["download_id"], "url": a["url"], "filename": a["filename"]}, **base))
        time.sleep(seconds)
        self.emit(EventEnvelope(type="download.completed", payload={
            "download_id": a["download_id"], "url": a["url"], "filename": a["filename"], "path": path,
            "bytes": 482_133}, **base))
        self._report(msg, True, path)
