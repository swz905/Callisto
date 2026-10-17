"""Windows PC agent: watches Downloads, runs actions the hub has already approved, shows alerts.

  python -m agents.pc_agent --hub ws://localhost:8000 --device pc-main --token dev-token [--chat]

Optional extras: win11toast (toasts), pyttsx3 (speech). Without them alerts print to the console.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path

from agents.common import HubClient, envelope

TEMP_SUFFIXES = (".crdownload", ".part", ".download", ".partial", ".opdownload")


def is_temp(path: str) -> bool:
    return path.lower().endswith(TEMP_SUFFIXES)


def strip_temp(name: str) -> str:
    for suffix in TEMP_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def zone_url(path: str) -> str | None:
    """Windows stores the source URL of downloads in the Zone.Identifier alternate data stream."""
    try:
        with open(path + ":Zone.Identifier", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("HostUrl="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def toast(text: str) -> None:
    print(f"\a[KAIROS] {text}")
    try:
        from win11toast import notify

        notify("KAIROS", text)
    except Exception:
        pass


def speak(text: str) -> None:
    print(f"[KAIROS says] {text}")
    try:
        import pyttsx3

        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except Exception:
        pass


class DownloadWatcher:
    """Turns browser temp-file renames in ~/Downloads into download.started / download.completed events."""

    def __init__(self, folder: Path, emit):
        self.folder = folder
        self.emit = emit
        self.ids: dict[str, tuple[str, str | None]] = {}  # temp path -> (download_id, url)
        self.claims: dict[str, tuple[str, str]] = {}  # temp path we are about to create -> (id, url)

    def start(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            outer = self

            class Handler(FileSystemEventHandler):
                def on_created(self, ev):
                    if not ev.is_directory:
                        outer.created(ev.src_path)

                def on_moved(self, ev):
                    if not ev.is_directory:
                        outer.moved(ev.src_path, ev.dest_path)

            observer = Observer()
            observer.schedule(Handler(), str(self.folder), recursive=False)
            observer.daemon = True
            observer.start()
            return observer
        except ImportError:
            print(f"[pc_agent] Notice: 'watchdog' package not installed in current Python environment. "
                  f"Downloads folder auto-watch is disabled. Run via .venv to enable all features.")
            return None

    def created(self, path: str) -> None:
        if not is_temp(path) or path in self.ids:
            return
        download_id, url = self.claims.pop(path, (f"dl-{uuid.uuid4().hex[:8]}", None))
        self.ids[path] = (download_id, url)
        self.emit("download.started", {"download_id": download_id, "filename": strip_temp(Path(path).name),
                                       "url": url})

    def moved(self, src: str, dest: str) -> None:
        if not is_temp(src):
            return
        download_id, url = self.ids.pop(src, None) or self.claims.pop(src, (f"dl-{uuid.uuid4().hex[:8]}", None))
        if is_temp(dest):  # Chrome: "Unconfirmed 123.crdownload" -> "report.pdf.crdownload"
            self.ids[dest] = (download_id, url)
            return
        size = os.path.getsize(dest) if os.path.exists(dest) else None
        self.emit("download.completed", {"download_id": download_id, "filename": Path(dest).name, "path": dest,
                                         "bytes": size, "url": url or zone_url(dest)})


class PCAgent:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = HubClient(args.hub, args.device, args.token, self.on_message)
        self.downloads = Path(args.downloads).expanduser()
        self.sandbox = Path(args.sandbox).expanduser().resolve()
        self.watcher = DownloadWatcher(self.downloads, self.emit)

    def emit(self, type_: str, payload: dict, source: str = "explorer") -> None:
        payload = {k: v for k, v in payload.items() if v is not None}
        if type_ == "device.status":
            payload["key"] = str(time.time())  # every snapshot is new (never deduplicated)
        self.client.send_threadsafe(envelope(self.args.user, self.args.device, type_, source, payload))

    def result(self, action_id: str | None, ok: bool, output: str) -> None:
        if action_id:
            print(f"[pc_agent] Sending result for action {action_id} (ok={ok}): {output[:60]}...", flush=True)
            self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

    async def on_message(self, msg: dict) -> None:
        kind, text, a = msg.get("kind"), msg.get("text", ""), msg.get("args", {})
        if kind == "ack":
            return
        if kind == "alert":
            toast(text)
        elif kind == "speak":
            threading.Thread(target=speak, args=(text,), daemon=True).start()
        elif kind == "open":
            if a.get("path") and os.path.exists(a["path"]):
                os.startfile(a["path"])  # type: ignore[attr-defined]  (Windows only)
            elif a.get("url"):
                webbrowser.open(a["url"])
            else:
                print(f"cannot open {a}")
        elif kind == "download":
            threading.Thread(target=self.download, args=(a, msg.get("action_id")), daemon=True).start()
        elif kind == "exec":
            threading.Thread(target=self.exec, args=(a["cmd"], msg.get("action_id")), daemon=True).start()
        elif kind == "delete":
            self.delete(a["path"], msg.get("action_id"))
        elif kind == "status_request":
            threading.Thread(target=self.send_status, daemon=True).start()
        elif kind == "upload":
            threading.Thread(target=self.upload, args=(a, msg.get("action_id")), daemon=True).start()
        elif kind == "clipboard_get":
            threading.Thread(target=self.clipboard_get, args=(msg.get("action_id"),), daemon=True).start()
        elif kind == "clipboard_set":
            self.clipboard_set(a.get("text", ""))
        elif kind == "type_message":
            threading.Thread(target=self.type_message, args=(a, msg.get("action_id")), daemon=True).start()
        elif kind == "screenshot":
            threading.Thread(target=self.take_screenshot, args=(a, msg.get("action_id")), daemon=True).start()
        elif kind == "notifications_get":
            print(f"[pc_agent] Processing notifications_get action_id={msg.get('action_id')}", flush=True)
            threading.Thread(target=self.get_notifications, args=(a, msg.get("action_id")), daemon=True).start()
        else:
            print(f"unknown message: {msg}", flush=True)

    def upload(self, a: dict, action_id: str | None) -> None:
        """Send a local file to the hub for a cross-device handoff (the hub reports success itself)."""
        path = Path(a["path"])
        try:
            if not path.is_file():
                raise FileNotFoundError(str(path))
            req = urllib.request.Request(a["upload_url"], data=path.read_bytes(), method="POST",
                                         headers={"X-Kairos-Token": self.args.token,
                                                  "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
        except Exception as exc:
            self.result(action_id, False, f"upload failed: {exc!r}")

    def clipboard_get(self, action_id: str | None) -> None:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], capture_output=True,
                                 text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.result(action_id, True, (out.stdout or "")[:10_000])
        except Exception as exc:
            self.result(action_id, False, repr(exc))

    def _set_clipboard_native(self, text: str) -> bool:
        """Sets text in Windows clipboard natively with full UTF-16 Unicode/emoji support without dependencies."""
        try:
            import ctypes
            import ctypes.wintypes
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
            user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
            user32.OpenClipboard.restype = ctypes.wintypes.BOOL
            user32.EmptyClipboard.restype = ctypes.wintypes.BOOL
            user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            user32.SetClipboardData.restype = ctypes.c_void_p
            user32.CloseClipboard.restype = ctypes.wintypes.BOOL

            for _ in range(5):
                if user32.OpenClipboard(None):
                    break
                time.sleep(0.05)
            else:
                return False
            try:
                user32.EmptyClipboard()
                data = text.encode("utf-16-le") + b"\x00\x00"
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not h_mem:
                    return False
                ptr = kernel32.GlobalLock(h_mem)
                if not ptr:
                    return False
                ctypes.memmove(ptr, data, len(data))
                kernel32.GlobalUnlock(h_mem)
                return bool(user32.SetClipboardData(CF_UNICODETEXT, h_mem))
            finally:
                user32.CloseClipboard()
        except Exception:
            return False

    def clipboard_set(self, text: str) -> None:
        try:
            if not self._set_clipboard_native(text):
                subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"], input=text, text=True,
                               timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            toast("Clipboard received from your other device")
        except Exception as exc:
            print(f"clipboard set failed: {exc!r}")

    def _focus_window(self, app_name: str) -> bool:
        """Bring target application window to foreground using native Win32 APIs."""
        if not app_name:
            return True
        try:
            import ctypes
            import ctypes.wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            hwinsta = user32.OpenWindowStationW("WinSta0", False, 0x037F)
            if hwinsta:
                user32.SetProcessWindowStation(hwinsta)
            hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            candidates = []

            def enum_cb(hwnd, lparam):
                pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
                    if h_proc:
                        buf = ctypes.create_unicode_buffer(1024)
                        size = ctypes.wintypes.DWORD(1024)
                        pname = ""
                        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                            pname = buf.value.lower()
                        kernel32.CloseHandle(h_proc)

                        tlen = user32.GetWindowTextLengthW(hwnd)
                        tbuf = ctypes.create_unicode_buffer(tlen + 1)
                        user32.GetWindowTextW(hwnd, tbuf, tlen + 1)
                        title = tbuf.value

                        target = app_name.lower()
                        if target in pname or (title and target in title.lower()):
                            if title and "tray" not in title.lower() and "ime" not in title.lower():
                                candidates.append((hwnd, pid.value, title))
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            user32.EnumDesktopWindows(hdesk, WNDENUMPROC(enum_cb), 0)

            if candidates:
                hwnd = candidates[0][0]
                # SW_RESTORE = 9 restores/unminimizes even from system tray
                user32.ShowWindow(hwnd, 9)
                # Bypass Windows focus stealing lock with Alt key toggle
                user32.keybd_event(0x12, 0, 0, 0)
                user32.SetForegroundWindow(hwnd)
                user32.keybd_event(0x12, 0, 2, 0)
                time.sleep(0.35)
                return True
        except Exception as e:
            print(f"[pc_agent] _focus_window error: {e}")
        return False

    def type_message(self, a: dict, action_id: str | None) -> None:
        """Simulate typing/pasting a message into WhatsApp, Telegram, or any chat application.
        If recipient is specified, automatically searches for and opens their chat first via Ctrl+F."""
        text = a.get("text", "")
        recipient = (a.get("recipient") or "").strip()
        app_name = (a.get("app") or "").strip()
        press_enter = bool(a.get("press_enter", True))

        try:
            # 1. Focus application window natively (handles minimized/tray state, Qt/Electron apps, Unicode titles)
            focused = self._focus_window(app_name)
            app_clean = app_name.replace("'", "''").strip()

            # 2. If recipient specified: ensure window is active, search contact (Ctrl+F), select chat (Enter)
            if recipient:
                self._set_clipboard_native(recipient)
                ps_search = f"""
$ws = New-Object -ComObject WScript.Shell
$act = {'$true' if focused else '$false'}
if (-not $act -and '{app_clean}') {{
    $act = $ws.AppActivate('{app_clean}')
    if (-not $act) {{
        $p = Get-Process | Where-Object {{ $_.ProcessName -match '{app_clean}' -or $_.MainWindowTitle -match '{app_clean}' }} | Select-Object -First 1
        if ($p) {{
            $act = $ws.AppActivate($p.Id)
        }}
    }}
    Start-Sleep -Milliseconds 350
}}
if (-not $act -and '{app_clean}') {{
    [Console]::Error.WriteLine("Could not find or focus window matching '{app_clean}'")
    exit 2
}}
# Clear any modal / search focus
$ws.SendKeys('{{ESC}}')
Start-Sleep -Milliseconds 200
# Focus search bar (universal across WhatsApp, Telegram, Slack)
$ws.SendKeys('^f')
Start-Sleep -Milliseconds 350
# Paste contact name
$ws.SendKeys('^v')
Start-Sleep -Milliseconds 700
# Press Enter to open the top matched conversation
$ws.SendKeys('{{ENTER}}')
Start-Sleep -Milliseconds 500
"""
                res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_search],
                                     capture_output=True, text=True, timeout=15,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if res.returncode != 0:
                    err = res.stderr.strip() or f"Failed to activate '{app_name}' or select '{recipient}'"
                    self.result(action_id, False, err)
                    return

            # 3. Paste message into the chat and optionally hit Enter
            if text:
                if not self._set_clipboard_native(text):
                    subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                                   input=text, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

                ps_type = f"""
$ws = New-Object -ComObject WScript.Shell
$act = {'$true' if (focused or recipient) else '$false'}
if (-not $act -and '{app_clean}') {{
    $act = $ws.AppActivate('{app_clean}')
    if (-not $act) {{
        $p = Get-Process | Where-Object {{ $_.ProcessName -match '{app_clean}' -or $_.MainWindowTitle -match '{app_clean}' }} | Select-Object -First 1
        if ($p) {{
            $act = $ws.AppActivate($p.Id)
        }}
    }}
    Start-Sleep -Milliseconds 350
}}
if (-not $act -and '{app_clean}') {{
    [Console]::Error.WriteLine("Could not find or focus window matching '{app_clean}'")
    exit 2
}}
$ws.SendKeys('^v')
if ({'$true' if press_enter else '$false'}) {{
    Start-Sleep -Milliseconds 200
    $ws.SendKeys('{{ENTER}}')
}}
"""
                proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_type],
                                      capture_output=True, text=True, timeout=15,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode != 0:
                    err = proc.stderr.strip() or f"Failed to paste into '{app_name}'"
                    self.result(action_id, False, err)
                    return

            dest = f"{app_name}" if app_name else "active window"
            who = f" to {recipient}" if recipient else ""
            self.result(action_id, True, f"Typed and {'sent' if press_enter else 'pasted'} message{who} in {dest}")
        except Exception as exc:
            self.result(action_id, False, repr(exc))

    def get_notifications(self, a: dict, action_id: str | None) -> None:
        """Read recent Windows toast notifications from wpndatabase.db with optional filtering."""
        import sqlite3
        import tempfile
        import xml.etree.ElementTree as ET

        target_app = (a.get("app") or "").strip().lower()
        query = (a.get("query") or "").strip().lower()
        limit = int(a.get("limit") or 10)

        db_path = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db")
        if not os.path.exists(db_path):
            self.result(action_id, False, "Windows notification database not found")
            return

        fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            shutil.copy2(db_path, tmp_path)
            con = sqlite3.connect(tmp_path)
            cur = con.cursor()
            sql = """
            SELECT n.ArrivalTime, h.PrimaryId, n.Payload
            FROM Notification n
            LEFT JOIN NotificationHandler h ON n.HandlerId = h.RecordId
            ORDER BY n.ArrivalTime DESC
            LIMIT 40
            """
            cur.execute(sql)
            rows = cur.fetchall()
            con.close()

            results = []
            for arr_time, app_id, payload in rows:
                app_name = (app_id or "System").split("!")[0].split(".")[-1]
                if "telegram" in (app_id or "").lower():
                    app_name = "Telegram"
                elif "whatsapp" in (app_id or "").lower():
                    app_name = "WhatsApp"
                elif "yourphone" in (app_id or "").lower():
                    app_name = "Phone Link"
                elif "screensketch" in (app_id or "").lower():
                    app_name = "Snipping Tool"

                texts = []
                if isinstance(payload, bytes):
                    try:
                        root = ET.fromstring(payload.decode("utf-8", errors="ignore"))
                        texts = [e.text.strip() for e in root.iter() if e.text and e.text.strip()]
                    except Exception:
                        pass
                elif isinstance(payload, str):
                    texts = [payload.strip()]

                combined = " — ".join(texts)
                if not combined:
                    continue

                if target_app and target_app not in app_name.lower() and target_app not in (app_id or "").lower():
                    continue

                if query and query not in combined.lower():
                    continue

                results.append(f"• [{app_name}] {combined}")
                if len(results) >= limit:
                    break

            if not results:
                filter_info = f" matching '{query or target_app}'" if (query or target_app) else ""
                self.result(action_id, True, f"No recent PC notifications found{filter_info}.")
            else:
                formatted = f"Found {len(results)} notification(s):\n" + "\n".join(results)
                self.result(action_id, True, formatted)
        except Exception as exc:
            self.result(action_id, False, f"Failed reading notifications: {exc!r}")
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def take_screenshot(self, a: dict, action_id: str | None) -> None:
        """Capture the PC desktop screen and save to the sandbox folder."""
        try:
            ts = int(time.time())
            filename = f"screenshot_{ts}.png"
            dest_dir = Path(a.get("dest") or self.sandbox)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / filename

            ps_code = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save('{dest_path.as_posix()}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()
"""
            res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_code],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if res.returncode == 0 and dest_path.is_file():
                kb = round(dest_path.stat().st_size / 1024, 1)
                self.result(action_id, True, f"Screenshot captured and saved to {dest_path} ({kb} KB)")
                self.emit("file.created", {"path": str(dest_path), "name": filename, "type": "screenshot", "size": dest_path.stat().st_size})
            else:
                err = res.stderr.strip() or "Failed to capture screenshot"
                self.result(action_id, False, err)
        except Exception as exc:
            self.result(action_id, False, repr(exc))

    def history_loop(self) -> None:
        from agents.browser_history import BrowserHistoryWatcher

        watcher = BrowserHistoryWatcher(backfill_hours=self.args.history_backfill_hours)
        while True:
            try:
                for v in watcher.poll():
                    self.client.send_threadsafe(envelope(
                        self.args.user, self.args.device, "browser.visit", v["browser"].lower(),
                        {"url": v["url"], "title": v["title"], "browser": v["browser"], "key": f"{v['url']}|{v['ts']}"},
                        ts=v["ts"]))
            except Exception as exc:
                print(f"browser history poll failed: {exc!r}")
            time.sleep(self.args.history_interval)

    def download(self, a: dict, action_id: str | None) -> None:
        final = self.downloads / a["filename"]
        tmp = str(final) + ".part"
        self.watcher.claims[tmp] = (a["download_id"], a["url"])
        try:
            with urllib.request.urlopen(a["url"], timeout=60) as r, open(tmp, "wb") as fh:
                shutil.copyfileobj(r, fh)
            os.replace(tmp, final)
            self.result(action_id, True, str(final))
        except Exception as exc:
            self.emit("download.failed", {"download_id": a["download_id"], "url": a["url"], "filename": a["filename"]})
            self.result(action_id, False, repr(exc))
            if os.path.exists(tmp):
                os.remove(tmp)

    def exec(self, cmd: str, action_id: str | None) -> None:
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                                  timeout=60)
            out, code = (proc.stdout or proc.stderr)[-2000:], proc.returncode
        except Exception as exc:
            out, code = repr(exc), -1
        print(f"$ {cmd}\n{out}")
        self.emit("command.run", {"cmd": cmd, "exit_code": code, "stdout": out}, source="shell")
        self.result(action_id, code == 0, out)

    def delete(self, path: str, action_id: str | None) -> None:
        target = Path(path).resolve()
        if self.sandbox not in target.parents:  # second line of defence; the hub already checked
            self.result(action_id, False, f"refused: {target} is outside {self.sandbox}")
            print(f"refused delete outside sandbox: {target}")
            return
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        self.emit("file.deleted", {"path": str(target)})
        self.result(action_id, True, f"deleted {target}")

    def stdin_chat(self) -> None:
        for line in sys.stdin:
            if line.strip():
                self.client.send_threadsafe({"kind": "chat", "text": line.strip()})

    def send_status(self) -> None:
        from agents.pc_status import collect

        snap = collect(self.downloads, include_titles=not self.args.no_titles)
        self.emit("device.status", snap, source="status")

    def status_loop(self) -> None:
        while True:
            try:
                self.send_status()
            except Exception as exc:
                print(f"status snapshot failed: {exc!r}")
            time.sleep(self.args.status_interval)

    async def run(self) -> None:
        self.sandbox.mkdir(parents=True, exist_ok=True)
        task = asyncio.create_task(self.client.run_forever())
        while self.client.loop is None:
            await asyncio.sleep(0.05)
        self.watcher.start()
        if self.args.chat:
            threading.Thread(target=self.stdin_chat, daemon=True).start()
        if self.args.status_interval > 0:
            threading.Thread(target=self.status_loop, daemon=True).start()
        if not self.args.no_history:
            threading.Thread(target=self.history_loop, daemon=True).start()
        print(f"watching {self.downloads}; sandbox {self.sandbox}; status every {self.args.status_interval}s; "
              f"browser history {'off' if self.args.no_history else 'every %ss' % self.args.history_interval}")
        await task


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("--hub", default=os.getenv("KAIROS_HUB_URL", "ws://localhost:8000"))
    p.add_argument("--device", default="pc-main")
    p.add_argument("--user", default=os.getenv("KAIROS_USER", "arjun"))
    p.add_argument("--token", default=os.getenv("KAIROS_DEVICE_TOKEN", "dev-token"))
    p.add_argument("--downloads", default=str(Path.home() / "Downloads"))
    p.add_argument("--sandbox", default=os.getenv("KAIROS_SANDBOX", str(Path.home() / "kairos_sandbox")))
    p.add_argument("--chat", action="store_true", help="type commands to the hub from this terminal")
    p.add_argument("--status-interval", type=int, default=int(os.getenv("KAIROS_STATUS_INTERVAL", "120")),
                   help="seconds between status snapshots (0 = off)")
    p.add_argument("--no-titles", action="store_true", help="send app names only, never window titles")
    p.add_argument("--no-history", action="store_true", help="don't read browser history")
    p.add_argument("--history-interval", type=int, default=60, help="seconds between browser history polls")
    p.add_argument("--history-backfill-hours", type=float, default=0,
                   help="on first run, also send visits from the last N hours")
    asyncio.run(PCAgent(p.parse_args()).run())


if __name__ == "__main__":
    main()
