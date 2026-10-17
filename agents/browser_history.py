"""Browser history -> `browser.visit` events (Chrome, Edge, Brave, Firefox) for "the site I was reading
before the meeting". Browsers lock their history DB, so it's copied to a temp file and read read-only (stdlib
sqlite3). Only visits newer than the saved watermark are sent; the first run starts from *now* (no backfill)
unless a backfill window is given. Internal pages (chrome://, about:, localhost, ...) are skipped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

CHROMIUM_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000  # 1601-01-01 -> 1970-01-01
IGNORE = re.compile(r"^(chrome|edge|brave|about|file|chrome-extension|moz-extension|devtools|view-source|data):"
                    r"|^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?/", re.I)
STATE_FILE = Path.home() / ".kairos" / "browser_state.json"
MAX_PER_POLL = 100


def find_history_dbs() -> list[tuple[str, Path, str]]:
    """(browser, path, kind) for every profile found on this machine."""
    local, roaming = Path(os.environ.get("LOCALAPPDATA", "")), Path(os.environ.get("APPDATA", ""))
    out: list[tuple[str, Path, str]] = []
    for browser, base in (("Chrome", local / "Google" / "Chrome" / "User Data"),
                          ("Edge", local / "Microsoft" / "Edge" / "User Data"),
                          ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data")):
        if base.exists():
            for prof in [base / "Default", *sorted(base.glob("Profile *"))]:
                if (prof / "History").exists():
                    out.append((browser, prof / "History", "chromium"))
    ff = roaming / "Mozilla" / "Firefox" / "Profiles"
    if ff.exists():
        for prof in ff.iterdir():
            if (prof / "places.sqlite").exists():
                out.append(("Firefox", prof / "places.sqlite", "firefox"))
    return out


def to_epoch_ms(raw: int, kind: str) -> int:
    return int((raw - CHROMIUM_EPOCH_OFFSET_US) / 1000) if kind == "chromium" else int(raw / 1000)


def from_epoch_ms(ms: int, kind: str) -> int:
    return ms * 1000 + CHROMIUM_EPOCH_OFFSET_US if kind == "chromium" else ms * 1000


def read_visits(db: Path, kind: str, since_raw: int, limit: int = MAX_PER_POLL) -> list[dict]:
    tmp = Path(tempfile.mkdtemp(prefix="kairos_hist_")) / "h.sqlite"
    try:
        shutil.copy2(db, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            if kind == "chromium":
                sql = ("SELECT u.url, u.title, v.visit_time FROM visits v JOIN urls u ON u.id = v.url "
                       "WHERE v.visit_time > ? ORDER BY v.visit_time LIMIT ?")
            else:
                sql = ("SELECT p.url, p.title, v.visit_date FROM moz_historyvisits v JOIN moz_places p "
                       "ON p.id = v.place_id WHERE v.visit_date > ? ORDER BY v.visit_date LIMIT ?")
            rows = con.execute(sql, (since_raw, limit)).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return [{"url": url, "title": title or "", "raw": raw} for url, title, raw in rows if url]


def clean_visits(rows: list[dict], kind: str, browser: str) -> list[dict]:
    out, last_url, last_ts = [], None, 0
    for r in rows:
        ts = to_epoch_ms(r["raw"], kind)
        if IGNORE.search(r["url"]):
            continue
        if r["url"] == last_url and ts - last_ts < 5 * 60_000:  # reloads / redirects
            continue
        out.append({"url": r["url"][:500], "title": r["title"][:200], "browser": browser, "ts": ts})
        last_url, last_ts = r["url"], ts
    return out


class BrowserHistoryWatcher:
    def __init__(self, backfill_hours: float = 0, state_file: Path = STATE_FILE):
        self.state_file = state_file
        self.backfill_ms = int(backfill_hours * 3_600_000)
        self.state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except Exception:
            return {}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state))

    def poll(self, dbs: list[tuple[str, Path, str]] | None = None) -> list[dict]:
        now = int(time.time() * 1000)
        visits: list[dict] = []
        for browser, path, kind in (dbs if dbs is not None else find_history_dbs()):
            key = str(path)
            since = self.state.get(key)
            if since is None:
                since = from_epoch_ms(now - self.backfill_ms, kind)
            try:
                rows = read_visits(path, kind, since)
            except Exception:
                continue  # profile in use in an unexpected way, permissions, corrupt copy...
            if rows:
                self.state[key] = rows[-1]["raw"]
            elif key not in self.state:
                self.state[key] = since
            visits += clean_visits(rows, kind, browser)
        self._save()
        return sorted(visits, key=lambda v: v["ts"])
