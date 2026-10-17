"""Daily chat log on disk: one plain-text file per local day, `<data_dir>/chatlog/YYYY-MM-DD.txt`.

Why a file as well as the graph:
- the nightly digest (kairos/digest.py) reads a whole day of conversation from it,
- a restarted hub restores each session's recent turns from it (the in-memory history is lost on restart),
- the user can open, read or delete it like any text file.

Line format (one message per line, newlines inside a message become " / "):
    09:15:02 | pc-main | session-pc-main | user | I'm flying to Goa next Friday
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from kairos.timeutil import DAY_MS, now_ms, to_local

log = logging.getLogger(__name__)
SEP = " | "


@dataclass
class LogLine:
    time: str  # HH:MM:SS local
    device: str
    session: str
    role: str  # user | assistant
    text: str


def _clean(text: str) -> str:
    return " / ".join(ln.strip() for ln in (text or "").splitlines() if ln.strip())


def parse_line(line: str) -> LogLine | None:
    if not line.strip() or line.startswith("#"):
        return None
    parts = line.rstrip("\n").split(SEP, 4)
    if len(parts) != 5 or parts[3] not in ("user", "assistant"):
        return None
    return LogLine(*parts)


class ChatJournal:
    def __init__(self, data_dir: str, tz: str, enabled: bool = True, keep_days: int = 30):
        self.dir = Path(data_dir) / "chatlog"
        self.tz = tz
        self.enabled = enabled
        self.keep_days = keep_days
        self._lock = threading.Lock()

    def path_for(self, day: date) -> Path:
        return self.dir / f"{day.isoformat()}.txt"

    def today(self, now: int | None = None) -> date:
        return to_local(now if now is not None else now_ms(), self.tz).date()

    # --- write -------------------------------------------------------------------
    def append(self, session: str, device: str, role: str, text: str, ts: int | None = None) -> None:
        if not self.enabled or not (text := _clean(text)):
            return
        ts = ts if ts is not None else now_ms()
        local = to_local(ts, self.tz)
        path = self.path_for(local.date())
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                new = not path.exists()
                with path.open("a", encoding="utf-8") as fh:
                    if new:
                        fh.write(f"# KAIROS chat log {local.date().isoformat()} (local time, {self.tz})\n")
                    fh.write(SEP.join([f"{local:%H:%M:%S}", device, session, role, text]) + "\n")
        except OSError as exc:  # a full disk must never break a conversation
            log.warning("chat log write failed: %s", exc)

    # --- read --------------------------------------------------------------------
    def read_day(self, day: date) -> list[LogLine]:
        path = self.path_for(day)
        if not path.exists():
            return []
        with self._lock:
            raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [ln for ln in map(parse_line, raw) if ln]

    def days(self) -> list[date]:
        out = []
        for p in self.dir.glob("*.txt") if self.dir.exists() else []:
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return sorted(out)

    def recent(self, session: str, n: int, now: int | None = None) -> list[dict]:
        """The last `n` messages of a session from today's and yesterday's logs, as chat history."""
        if not self.enabled:
            return []
        today = self.today(now)
        lines = self.read_day(today - timedelta(days=1)) + self.read_day(today)
        msgs = [{"role": ln.role, "content": ln.text} for ln in lines if ln.session == session]
        return msgs[-n:] if n else []

    def transcript(self, day: date, roles: tuple[str, ...] = ("user",), max_chars: int = 0) -> str:
        lines = [f"{ln.time} {'USER' if ln.role == 'user' else 'ASSISTANT'}: {ln.text}"
                 for ln in self.read_day(day) if ln.role in roles]
        text = "\n".join(lines)
        return text[-max_chars:] if max_chars else text

    # --- housekeeping ---------------------------------------------------------------
    def prune(self, digested: set[date], now: int | None = None) -> int:
        """Delete logs older than keep_days, but only days whose digest is already in the graph."""
        cutoff = self.today(now) - timedelta(days=self.keep_days)
        removed = 0
        for day in self.days():
            if day < cutoff and day in digested:
                try:
                    self.path_for(day).unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


def day_bounds(day: date, tz: str) -> tuple[int, int]:
    """Epoch ms of local midnight at the start and end of `day`."""
    from datetime import datetime

    from kairos.timeutil import get_tz

    start = datetime(day.year, day.month, day.day, tzinfo=get_tz(tz))
    s = int(start.timestamp() * 1000)
    return s, s + DAY_MS
