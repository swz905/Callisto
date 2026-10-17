"""Windows status snapshot via ctypes (no installs): foreground window, open apps, battery, idle, RAM, Wi-Fi.

`psutil` is used for CPU % if it happens to be installed. Every probe is best-effort: a failure leaves the
field out instead of breaking the snapshot. On non-Windows systems only the portable fields are returned.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

APP_NAMES = {
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
    "code.exe": "VS Code", "cursor.exe": "Cursor", "pycharm64.exe": "PyCharm", "idea64.exe": "IntelliJ",
    "winword.exe": "Word", "excel.exe": "Excel", "powerpnt.exe": "PowerPoint", "outlook.exe": "Outlook",
    "olk.exe": "Outlook", "onenote.exe": "OneNote", "ms-teams.exe": "Teams", "teams.exe": "Teams",
    "slack.exe": "Slack", "zoom.exe": "Zoom", "spotify.exe": "Spotify", "whatsapp.exe": "WhatsApp",
    "explorer.exe": "File Explorer", "notepad.exe": "Notepad", "windowsterminal.exe": "Terminal",
    "powershell.exe": "PowerShell", "cmd.exe": "Command Prompt", "acrobat.exe": "Acrobat",
    "acrord32.exe": "Acrobat Reader", "vlc.exe": "VLC", "discord.exe": "Discord", "obsidian.exe": "Obsidian",
    "notion.exe": "Notion", "figma.exe": "Figma", "claude.exe": "Claude", "postman.exe": "Postman",
}
IGNORE = {"textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
          "lockapp.exe", "searchapp.exe", "systemsettingsbroker.exe", "nvidia share.exe", "widgets.exe",
          "applicationframehost.exe"}
TEMP_SUFFIXES = (".crdownload", ".part", ".download", ".partial", ".opdownload")


def friendly_app(exe: str, title: str = "") -> str | None:
    """Readable app name from an executable (None = ignore). UWP apps report their host; use the title."""
    e = exe.lower()
    if e == "applicationframehost.exe":
        return title.split(" - ")[-1].strip() or None if title else None
    if not e or e in IGNORE:
        return None
    return APP_NAMES.get(e) or Path(exe).stem.replace("_", " ").title()


def _windows_snapshot(include_titles: bool) -> dict:
    import ctypes
    from ctypes import wintypes as wt

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    out: dict = {}

    def proc_name(pid: int) -> str:
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf, size = ctypes.create_unicode_buffer(512), wt.DWORD(512)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(h)
        return ""

    def window_pid(hwnd) -> int:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def title_of(hwnd) -> str:
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def cloaked(hwnd) -> bool:
        try:
            val = ctypes.c_int(0)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
            return bool(val.value)
        except Exception:
            return False

    # open apps: visible, titled, un-owned, non-tool, non-cloaked top-level windows
    apps: list[str] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def on_window(hwnd, _):
        try:
            if (user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0
                    and not user32.GetWindow(hwnd, 4)                     # GW_OWNER
                    and not user32.GetWindowLongW(hwnd, -20) & 0x80      # WS_EX_TOOLWINDOW
                    and not cloaked(hwnd)):
                name = friendly_app(proc_name(window_pid(hwnd)), title_of(hwnd))
                if name and name not in apps:
                    apps.append(name)
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(on_window), 0)
    out["apps"] = apps

    fg = user32.GetForegroundWindow()
    if fg:
        title = title_of(fg)
        out["foreground_app"] = friendly_app(proc_name(window_pid(fg)), title) or ""
        out["foreground_title"] = title[:80] if include_titles else ""

    class POWER(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]

    ps = POWER()
    if kernel32.GetSystemPowerStatus(ctypes.byref(ps)) and ps.BatteryLifePercent != 255 and ps.BatteryFlag != 128:
        out["battery"] = int(ps.BatteryLifePercent)
        out["charging"] = ps.ACLineStatus == 1

    class LASTINPUT(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

    li = LASTINPUT(ctypes.sizeof(LASTINPUT), 0)
    if user32.GetLastInputInfo(ctypes.byref(li)):
        out["idle_s"] = max(0, (kernel32.GetTickCount() - li.dwTime) // 1000)

    class MEM(ctypes.Structure):
        _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD)] + \
                   [(f, ctypes.c_ulonglong) for f in ("t", "a", "tp", "ap", "tv", "av", "ae")]

    mem = MEM()
    mem.dwLength = ctypes.sizeof(MEM)
    if kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        out["mem"] = float(mem.dwMemoryLoad)
    return out


def parse_netsh(text: str) -> str:
    """SSID from `netsh wlan show interfaces` ('' when not on Wi-Fi)."""
    state = re.search(r"^\s*State\s*:\s*(.+)$", text, re.M)
    ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", text, re.M)
    if state and "connected" in state.group(1).lower() and "disconnected" not in state.group(1).lower() and ssid:
        return f"Wi-Fi {ssid.group(1).strip()}"
    return ""


def wifi() -> str:
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return parse_netsh(out) or "wired/offline"
    except Exception:
        return ""


def collect(downloads: Path | None = None, include_titles: bool = True) -> dict:
    snap: dict = {}
    if sys.platform == "win32":
        try:
            snap.update(_windows_snapshot(include_titles))
        except Exception as exc:  # never let a probe kill the agent
            snap["error"] = repr(exc)[:120]
        net = wifi()
        if net:
            snap["network"] = net
    try:
        import psutil  # optional

        snap["cpu"] = float(psutil.cpu_percent(interval=0.3))
    except Exception:
        pass
    if downloads and downloads.exists():
        snap["downloads_active"] = sum(1 for f in downloads.iterdir() if f.name.lower().endswith(TEMP_SUFFIXES))
    return snap


if __name__ == "__main__":  # quick manual check:  python -m agents.pc_status
    import json

    print(json.dumps(collect(Path.home() / "Downloads"), indent=1))
