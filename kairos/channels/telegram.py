"""Telegram channel: talk to KAIROS from anywhere; alerts can be routed to Telegram too.

Telegram is just another Device (kind "chat", id from TELEGRAM_DEVICE_ID, default "telegram-<user>"), so the
permission graph, audit log, watchers and "why?" all work unchanged. Only chat IDs in
TELEGRAM_ALLOWED_CHAT_IDS may talk to it; anyone else is told their chat ID (so the owner can allow-list it)
and nothing else. Uses the Bot API over httpx long-polling: no webhook or public URL needed.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

import httpx

from kairos.net import ssl_context

from kairos.models import Outbound
from kairos.timeutil import now_ms

if TYPE_CHECKING:
    from kairos.runtime import Kairos

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000


class TelegramChannel:
    def __init__(self, k: Kairos, token: str, allowed_chat_ids: set[int], device_id: str,
                 transport: httpx.BaseTransport | None = None):
        self.k = k
        self.token = token
        self.allowed = allowed_chat_ids
        self.device_id = device_id
        self.http = httpx.Client(timeout=40, transport=transport, verify=ssl_context())
        self.offset = 0
        self.chat_id: int | None = next(iter(allowed_chat_ids), None)  # where alerts go
        self.thread: threading.Thread | None = None
        self.running = False

    def _call(self, method: str, **payload) -> dict:
        r = self.http.post(API.format(token=self.token, method=method), json=payload)
        r.raise_for_status()
        return r.json()

    def send_text(self, chat_id: int, text: str) -> None:
        for i in range(0, max(1, len(text)), MAX_LEN):
            self._call("sendMessage", chat_id=chat_id, text=text[i:i + MAX_LEN] or "…")

    # --- gateway side: hub -> Telegram ---------------------------------------------
    def deliver(self, msg: Outbound) -> bool:
        if self.chat_id is None:
            return False
        if msg.kind in ("alert", "speak"):
            text = f"🔔 {msg.text}" if msg.kind == "alert" else msg.text
        elif msg.kind == "open" and msg.args.get("url"):
            text = f"{msg.text or 'Link'}: {msg.args['url']}"
        elif msg.kind == "status_request":
            return True
        else:
            return False  # exec/delete/download etc. are device-only
        try:
            self.send_text(self.chat_id, text)
            return True
        except Exception as exc:
            log.warning("telegram delivery failed: %s", exc)
            return False

    # --- polling side: Telegram -> agent -----------------------------------------------
    def handle_update(self, update: dict) -> str | None:
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            return None
        if chat_id not in self.allowed:
            reply = (f"This KAIROS is private. Your chat id is {chat_id}; the owner can add it to "
                     f"TELEGRAM_ALLOWED_CHAT_IDS.")
            self.send_text(chat_id, reply)
            return reply
        self.chat_id = chat_id
        if text in ("/start", "/help"):
            text = "help"
        reply = self.k.chat(text, self.device_id, session_id=f"telegram-{chat_id}").text
        self.send_text(chat_id, reply)
        return reply

    def poll_once(self) -> int:
        data = self._call("getUpdates", offset=self.offset, timeout=30, allowed_updates=["message"])
        updates = data.get("result") or []
        for u in updates:
            self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
            try:
                self.handle_update(u)
            except Exception:
                log.exception("telegram update failed")
        return len(updates)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.running = True
        self.k.graph.run("set_device_online", device_id=self.device_id, online=True, now=now_ms())
        self.thread = threading.Thread(target=self._loop, name="kairos-telegram", daemon=True)
        self.thread.start()
        log.info("Telegram channel started (%d allowed chat(s))", len(self.allowed))

    def _loop(self) -> None:
        backoff = 2
        while self.running:
            try:
                self.poll_once()
                backoff = 2
            except Exception as exc:
                log.warning("telegram poll failed (%s); retrying in %ss", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
