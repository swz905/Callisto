"""Cross-device handoff: "send this file to my phone", "send my clipboard to my phone".

File flow (the hub never reaches into a device's disk):
  send_file tool -> Outbound `upload` {path, upload_url} to the device that has the file
  device POSTs the bytes to /api/transfer/<id> (device token required)
  hub stores it under data_dir/transfers, sends Outbound `open` {url} to the target device, and reports
  the result back to the original Action (so "why?" and the dashboard show it).
Transfer ids are random 128-bit tokens, single-file, and expire after an hour.

Clipboard flow: Outbound `clipboard_get` to the source device -> its result (the clipboard text) arrives via
the normal device-result path -> hub forwards `clipboard_set` to the target device.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

MAX_TRANSFER_BYTES = 100 * 1024 * 1024
TTL_S = 3600


@dataclass
class Transfer:
    id: str
    action_id: str | None
    name: str
    source: str
    target: str
    created: float
    path: Path | None = None


def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    return re.sub(r"[^\w.\- ]", "_", name)[:120] or "file"


class TransferStore:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir) / "transfers"
        self.items: dict[str, Transfer] = {}

    def create(self, action_id: str | None, name: str, source: str, target: str) -> Transfer:
        self.cleanup()
        t = Transfer(secrets.token_hex(16), action_id, safe_name(name), source, target, time.time())
        self.items[t.id] = t
        return t

    def get(self, transfer_id: str) -> Transfer | None:
        t = self.items.get(transfer_id)
        if t and time.time() - t.created > TTL_S:
            self.items.pop(transfer_id, None)
            return None
        return t

    def receive(self, transfer_id: str, data: bytes) -> Transfer:
        t = self.get(transfer_id)
        if t is None:
            raise KeyError("unknown or expired transfer")
        if t.path is not None:
            raise ValueError("transfer already received")
        if len(data) > MAX_TRANSFER_BYTES:
            raise ValueError("file too large")
        self.dir.mkdir(parents=True, exist_ok=True)
        t.path = self.dir / f"{t.id}_{t.name}"
        t.path.write_bytes(data)
        return t

    def cleanup(self) -> None:
        now = time.time()
        for tid, t in list(self.items.items()):
            if now - t.created > TTL_S:
                if t.path and t.path.exists():
                    try:
                        t.path.unlink()
                    except OSError:
                        pass
                self.items.pop(tid, None)
