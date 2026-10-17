"""Android phone agent for KAIROS.

Mode A (default) runs ON the phone in Termux:
  pkg update && pkg install python termux-api git
  pip install websockets
  (install the Termux:API app from F-Droid and grant it notification access)
  python phone_agent.py --hub ws://<pc-ip>:8000 --device phone-arjun

Mode B (--adb) runs ON the PC and controls the phone over ADB (USB or Wireless ADB `adb connect <ip>:<port>`).
Provides deep control: notifications, files (/sdcard), chat sending (WhatsApp/SMS/Telegram),
screen capture, and full UI automation (tap, swipe, type, key events, UI dump).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import quote

try:  # inside the repo
    from agents.common import HubClient, envelope
except ImportError:  # copied next to common.py on the phone (Termux)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import HubClient, envelope  # type: ignore[no-redef]

POLL_SECONDS = 2.0

KEYCODE_MAP: dict[str, int] = {
    "home": 3,
    "back": 4,
    "call": 5,
    "endcall": 6,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "camera": 27,
    "tab": 61,
    "space": 62,
    "enter": 66,
    "delete": 67,
    "backspace": 67,
    "app_switch": 187,
    "recent": 187,
    "recents": 187,
    "wake": 224,
}


def find_adb(custom_path: str | None = None) -> str:
    """Find the best available adb executable."""
    if custom_path and os.path.isfile(custom_path):
        return custom_path
    if found := shutil.which("adb"):
        return found
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        r"C:\platform-tools\adb.exe",
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "adb"


def sh(*cmd: str, timeout: int = 15) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return res.stdout or ""
    except Exception as exc:
        print(f"command failed {cmd[0]}: {exc!r}")
        return ""


# --- sources -----------------------------------------------------------------
def termux_notifications() -> list[dict]:
    try:
        items = json.loads(sh("termux-notification-list") or "[]")
    except json.JSONDecodeError:
        return []
    return [{"package": n.get("packageName", ""), "title": n.get("title", ""), "body": n.get("content", ""),
             "key": f"{n.get('key')}:{n.get('when')}:{n.get('content', '')[:40]}"} for n in items]


_RECORD = re.compile(r"NotificationRecord\([^)]*pkg=(\S+)")
_FIELD = re.compile(r"android\.(title|text|bigText)=\w+ \((.*)\)\s*$")
_WHEN = re.compile(r"\bwhen=(\d+)")


def parse_dumpsys(text: str) -> list[dict]:
    """Best-effort parser for `dumpsys notification --noredact`."""
    out, cur = [], None
    for line in text.splitlines():
        if m := _RECORD.search(line):
            if cur and (cur["title"] or cur["body"]):
                out.append(cur)
            cur = {"package": m.group(1), "title": "", "body": "", "when": ""}
        elif cur is not None:
            if m := _FIELD.search(line):
                key = "title" if m.group(1) == "title" else "body"
                if key == "title" or not cur["body"] or m.group(1) == "bigText":
                    cur[key] = m.group(2)
            elif m := _WHEN.search(line):
                cur["when"] = m.group(1)
    if cur and (cur["title"] or cur["body"]):
        out.append(cur)
    for n in out:
        n["key"] = f"{n['package']}:{n.pop('when')}:{n['body'][:40]}"
    return out


# --- live status ---------------------------------------------------------------------
def parse_adb_battery(text: str) -> dict:
    out: dict = {}
    if m := re.search(r"^\s*level:\s*(\d+)", text, re.M):
        out["battery"] = int(m.group(1))
    plugged = re.findall(r"^\s*(AC|USB|Wireless) powered:\s*true", text, re.M)
    status = re.search(r"^\s*status:\s*(\d+)", text, re.M)
    out["charging"] = bool(plugged) or (status is not None and status.group(1) in ("2", "5"))
    return out


def parse_adb_focus(text: str) -> str:
    """Package of the focused window from `dumpsys window` (mCurrentFocus / mFocusedApp)."""
    m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/", text) or re.search(r"mFocusedApp=.*? ([\w.]+)/", text)
    return m.group(1) if m else ""


def parse_adb_wifi(text: str | None) -> str:
    if not text:
        return ""
    m = re.search(r'SSID: "?([^",\r\n]+)"?,?', text) or re.search(r'connected to "([^"]+)"', text)
    return f"Wi-Fi {m.group(1).strip()}" if m and "unknown ssid" not in m.group(1).lower() else ""


def termux_status() -> dict:
    snap: dict = {}
    try:
        b = json.loads(sh("termux-battery-status") or "{}")
        if "percentage" in b:
            snap["battery"] = int(b["percentage"])
            snap["charging"] = b.get("status") in ("CHARGING", "FULL") or b.get("plugged", "UNPLUGGED") != "UNPLUGGED"
    except json.JSONDecodeError:
        pass
    try:
        w = json.loads(sh("termux-wifi-connectioninfo") or "{}")
        ssid = (w.get("ssid") or "").strip('"')
        snap["network"] = f"Wi-Fi {ssid}" if ssid and "unknown" not in ssid.lower() else "mobile data/offline"
    except json.JSONDecodeError:
        pass
    return snap


def termux_location() -> dict | None:
    """One network-based fix (fast, low power). The hub maps it to a Place and never stores coordinates."""
    try:
        loc = json.loads(sh("termux-location", "-p", "network", "-r", "once", timeout=30) or "{}")
    except json.JSONDecodeError:
        return None
    if "latitude" in loc and "longitude" in loc:
        return {"lat": float(loc["latitude"]), "lon": float(loc["longitude"]), "accuracy": loc.get("accuracy")}
    return None


# --- agent ---------------------------------------------------------------------
class PhoneAgent:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.adb_bin = find_adb(args.adb_path)
        self.client = HubClient(args.hub, args.device, args.token, self.on_message)
        self.seen: set[str] = set()
        self.source = self.adb_notifications if args.adb else termux_notifications

    def adb(self, *subcmd: str, timeout: int = 15) -> str:
        cmd = [self.adb_bin]
        if self.args.serial:
            cmd.extend(["-s", self.args.serial])
        cmd.extend(subcmd)
        return sh(*cmd, timeout=timeout)

    def adb_notifications(self) -> list[dict]:
        return parse_dumpsys(self.adb("shell", "dumpsys", "notification", "--noredact"))

    def adb_status(self) -> dict:
        snap = parse_adb_battery(self.adb("shell", "dumpsys", "battery"))
        focus = parse_adb_focus(self.adb("shell", "dumpsys", "window"))
        if focus:
            snap["foreground_app"] = focus
        wifi_text = self.adb("shell", "cmd", "wifi", "status") or self.adb("shell", "dumpsys", "wifi")
        snap["network"] = parse_adb_wifi(wifi_text) or "mobile data/offline"
        return snap

    def poll(self) -> None:
        first = True
        while True:
            try:
                for n in self.source():
                    if n["key"] in self.seen or n["package"] in ("com.termux", "com.termux.api"):
                        continue
                    self.seen.add(n["key"])
                    if first and not self.args.backfill:
                        continue  # don't replay what is already in the shade at startup
                    self.client.send_threadsafe(envelope(self.args.user, self.args.device, "notification", n["package"],
                                                         {"app": n["package"], "title": n["title"], "body": n["body"],
                                                          "key": n["key"]}))
            except Exception as exc:
                print(f"[poll error] {exc!r}")
            first = False
            time.sleep(POLL_SECONDS)

    def alert(self, text: str) -> None:
        print(f"[KAIROS] {text}")
        if self.args.adb:
            self.adb("shell", "cmd", "notification", "post", "-S", "bigtext", "-t", "KAIROS", "kairos", text)
        else:
            sh("termux-notification", "--title", "KAIROS", "--content", text, "--priority", "high")
            sh("termux-vibrate", "-d", "400")

    # --- high-level actions -----------------------------------------------------
    def send_chat(self, recipient: str, text: str, channel: str = "whatsapp", auto_send: bool = True) -> tuple[bool, str]:
        """Send a message via WhatsApp, SMS, or Telegram."""
        clean_phone = re.sub(r"[^\d+]", "", recipient)
        if channel.lower() == "whatsapp":
            encoded = quote(text)
            url = f"https://api.whatsapp.com/send?phone={clean_phone}&text={encoded}"
            if self.args.adb:
                self.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)
                if auto_send:
                    time.sleep(2.0)
                    # Trigger enter or tap on send button
                    self.adb("shell", "input", "keyevent", "66")
                return True, f"WhatsApp intent dispatched for {clean_phone}"
            else:
                sh("termux-open-url", url)
                return True, f"Opened WhatsApp for {clean_phone} via Termux"

        elif channel.lower() == "sms":
            if not self.args.adb:
                out = sh("termux-sms-send", "-n", clean_phone, text)
                return True, f"SMS dispatched to {clean_phone}: {out.strip()}"
            else:
                self.adb("shell", "am", "start", "-a", "android.intent.action.SENDTO", "-d", f"sms:{clean_phone}",
                         "--es", "sms_body", text)
                if auto_send:
                    time.sleep(1.5)
                    self.adb("shell", "input", "keyevent", "66")
                return True, f"SMS intent dispatched for {clean_phone}"

        elif channel.lower() == "telegram":
            encoded = quote(text)
            url = f"tg://msg?to={clean_phone}&text={encoded}"
            if self.args.adb:
                self.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)
            else:
                sh("termux-open-url", url)
            return True, f"Telegram intent dispatched for {clean_phone}"

        return False, f"Unsupported channel: {channel}"

    def list_files(self, path: str = "/sdcard/Download") -> tuple[bool, str]:
        if self.args.adb:
            out = self.adb("shell", "ls", "-la", path)
            return True, out
        out = sh("ls", "-la", path)
        return True, out

    def pull_file(self, remote_path: str, local_path: str) -> tuple[bool, str]:
        if not self.args.adb:
            return False, "pull_file is only supported in ADB mode"
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        out = self.adb("pull", remote_path, local_path)
        return os.path.exists(local_path), out.strip() or f"Pulled to {local_path}"

    def push_file(self, local_path: str, remote_path: str = "/sdcard/Download/") -> tuple[bool, str]:
        if not self.args.adb:
            return False, "push_file is only supported in ADB mode"
        if not os.path.exists(local_path):
            return False, f"Local file not found: {local_path}"
        out = self.adb("push", local_path, remote_path)
        return True, out.strip() or f"Pushed {local_path} -> {remote_path}"

    def ui_tap(self, x: int, y: int) -> tuple[bool, str]:
        if not self.args.adb:
            return False, "UI automation requires ADB mode"
        self.adb("shell", "input", "tap", str(x), str(y))
        return True, f"Tapped ({x}, {y})"

    def ui_swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> tuple[bool, str]:
        if not self.args.adb:
            return False, "UI automation requires ADB mode"
        self.adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))
        return True, f"Swiped ({x1}, {y1}) -> ({x2}, {y2})"

    def ui_type(self, text: str) -> tuple[bool, str]:
        if not self.args.adb:
            return False, "UI automation requires ADB mode"
        # Escape spaces and quotes for adb shell input text
        escaped = text.replace(" ", "%s").replace('"', '\\"').replace("'", "\\'")
        self.adb("shell", "input", "text", escaped)
        return True, f"Typed text"

    def ui_key(self, key_name: str) -> tuple[bool, str]:
        if not self.args.adb:
            return False, "UI automation requires ADB mode"
        code = KEYCODE_MAP.get(key_name.lower())
        if code is None:
            if key_name.isdigit():
                code = int(key_name)
            else:
                return False, f"Unknown key name: {key_name}"
        self.adb("shell", "input", "keyevent", str(code))
        return True, f"Pressed key {key_name} (code {code})"

    def ui_screenshot(self, dest_path: str | None = None) -> tuple[bool, str]:
        if not self.args.adb:
            return False, "Screenshot requires ADB mode"
        target = dest_path or os.path.join(os.getcwd(), f"screenshot_{int(time.time())}.png")
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        try:
            cmd = [self.adb_bin]
            if self.args.serial:
                cmd.extend(["-s", self.args.serial])
            cmd.extend(["exec-out", "screencap", "-p"])
            with open(target, "wb") as f:
                res = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=15)
            if res.returncode == 0 and os.path.getsize(target) > 0:
                return True, target
            return False, f"Screenshot failed: {res.stderr.decode('utf-8', errors='ignore')}"
        except Exception as exc:
            return False, f"Screenshot error: {exc!r}"

    def ui_dump(self) -> tuple[bool, str]:
        """Extract all interactable text elements and buttons on the screen."""
        if not self.args.adb:
            return False, "UI dump requires ADB mode"
        self.adb("shell", "uiautomator", "dump", "/sdcard/window_dump.xml")
        xml = self.adb("shell", "cat", "/sdcard/window_dump.xml")
        # Extract node texts and bounds
        nodes = []
        for m in re.finditer(r'<node [^>]*text="([^"]*)"[^>]*content-desc="([^"]*)"[^>]*bounds="([^"]*)"', xml):
            text, desc, bounds = m.group(1), m.group(2), m.group(3)
            if text or desc:
                nodes.append(f"{text or desc} @ {bounds}")
        summary = "\n".join(nodes[:30]) if nodes else "No visible text nodes found."
        return True, summary

    async def on_message(self, msg: dict) -> None:
        kind, text, a = msg.get("kind"), msg.get("text", ""), msg.get("args", {})
        action_id = msg.get("action_id")

        if kind == "alert":
            self.alert(text)
        elif kind == "speak":
            print(f"[KAIROS says] {text}")
            if not self.args.adb:
                sh("termux-tts-speak", text)
        elif kind == "open":
            if a.get("url"):
                if not self.args.adb:
                    sh("termux-open-url", a["url"])
                else:
                    self.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", a["url"])
                if action_id:
                    self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": True, "output": f"Opened {a['url']}"})
            elif a.get("path"):
                if self.args.adb:
                    self.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", f"file://{a['path']}", "-t", "*/*")
                else:
                    sh("termux-open", a["path"])
                if action_id:
                    self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": True, "output": f"Opened {a['path']}"})

        elif kind == "chat_send":
            ok, output = self.send_chat(
                recipient=a.get("recipient") or a.get("to") or a.get("phone", ""),
                text=a.get("message") or a.get("text") or text,
                channel=a.get("channel", "whatsapp"),
                auto_send=a.get("auto_send", True),
            )
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind == "file_list":
            ok, output = self.list_files(a.get("path", "/sdcard/Download"))
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output[:8000]})

        elif kind == "file_pull":
            ok, output = self.pull_file(a.get("remote_path", ""), a.get("local_path", ""))
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind == "file_push":
            ok, output = self.push_file(a.get("local_path", ""), a.get("remote_path", "/sdcard/Download/"))
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind == "tap":
            ok, output = self.ui_tap(int(a.get("x", 0)), int(a.get("y", 0)))
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind == "swipe":
            ok, output = self.ui_swipe(int(a.get("x1", 0)), int(a.get("y1", 0)), int(a.get("x2", 0)), int(a.get("y2", 0)), int(a.get("duration", 300)))
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind == "type":
            ok, output = self.ui_type(a.get("text", ""))
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind in ("key", "keyevent"):
            ok, output = self.ui_key(a.get("key", "home"))
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind == "screenshot":
            ok, output = self.ui_screenshot(a.get("path"))
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind == "ui_dump":
            ok, output = self.ui_dump()
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": ok, "output": output})

        elif kind == "app_launch":
            pkg = a.get("package", "")
            if self.args.adb:
                self.adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
                if action_id:
                    self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": True, "output": f"Launched {pkg}"})

        elif kind == "status_request":
            threading.Thread(target=self.send_status, daemon=True).start()

        elif kind == "clipboard_set":
            if not self.args.adb:
                sh("termux-clipboard-set", a.get("text", ""))
            else:
                self.adb("shell", "cmd", "clipboard", "set", a.get("text", ""))
            self.alert("Clipboard received from your other device")

        elif kind == "clipboard_get":
            text = self.adb("shell", "cmd", "clipboard", "get") if self.args.adb else sh("termux-clipboard-get")
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": True,
                                             "output": text[:10_000]})

        elif kind == "exec" and self.args.adb:
            cmd = a.get("cmd", "")
            out = self.adb("shell", cmd)
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": True, "output": out[:5000]})

        elif kind != "ack":
            print(f"unsupported on phone: {kind}")
            if action_id:
                self.client.send_threadsafe({"kind": "result", "action_id": action_id, "ok": False,
                                             "output": f"{kind} is not supported on this phone configuration"})

    def send_status(self) -> None:
        snap = self.adb_status() if self.args.adb else termux_status()
        if self.args.location and not self.args.adb and (loc := termux_location()):
            snap["location"] = loc
        snap["key"] = str(time.time())
        self.client.send_threadsafe(envelope(self.args.user, self.args.device, "device.status", "status", snap))

    def status_loop(self) -> None:
        while True:
            try:
                self.send_status()
            except Exception as exc:
                print(f"status snapshot failed: {exc!r}")
            time.sleep(self.args.status_interval)

    def stdin_chat(self) -> None:
        for line in sys.stdin:
            if line.strip():
                self.client.send_threadsafe({"kind": "chat", "text": line.strip()})

    async def run(self) -> None:
        task = asyncio.create_task(self.client.run_forever())
        while self.client.loop is None:
            await asyncio.sleep(0.05)
        threading.Thread(target=self.poll, daemon=True).start()
        if self.args.status_interval > 0:
            threading.Thread(target=self.status_loop, daemon=True).start()
        if self.args.chat:
            threading.Thread(target=self.stdin_chat, daemon=True).start()
        await task


def main() -> None:
    p = argparse.ArgumentParser(description="KAIROS Android Phone Agent")
    p.add_argument("--hub", default=os.getenv("KAIROS_HUB_URL", "ws://localhost:8000"))
    p.add_argument("--device", default="phone-arjun")
    p.add_argument("--user", default=os.getenv("KAIROS_USER", "arjun"))
    p.add_argument("--token", default=os.getenv("KAIROS_DEVICE_TOKEN", "dev-token"))
    p.add_argument("--adb", action="store_true", help="run on the PC and control the phone via ADB")
    p.add_argument("--adb-path", default=os.getenv("ADB_PATH"), help="custom path to adb executable")
    p.add_argument("--serial", "-s", default=os.getenv("ANDROID_SERIAL"), help="specific adb device serial")
    p.add_argument("--backfill", action="store_true", help="also send notifications already present at startup")
    p.add_argument("--chat", action="store_true", help="enable interactive chat input via stdin")
    p.add_argument("--status-interval", type=int, default=int(os.getenv("KAIROS_STATUS_INTERVAL", "120")),
                   help="seconds between status snapshots (0 = off)")
    p.add_argument("--location", action="store_true",
                   help="include a network location fix in snapshots (Termux; enables place watchers)")
    asyncio.run(PhoneAgent(p.parse_args()).run())


if __name__ == "__main__":
    main()
