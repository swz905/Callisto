"""KAIROS desktop pet: a tiny pixel robot that sits above the taskbar, shows status bubbles and talks.

  kairos pet [--hub http://localhost:8000] [--device pc-main] [--scale 4] [--voice-chat] [--no-tts]

- Click it to talk (mic -> hub speech-to-text -> KAIROS -> spoken reply). Without a mic library it opens a text box.
- Hover for status (brain online?, battery, foreground app...). Double-click opens the dashboard.
- Drag to move. Right-click for the menu (talk, type, voice chat mode, mute, dashboard, quit).
- Alerts and spoken messages KAIROS sends to this device pop up as speech bubbles.

Needs only the standard library (tkinter) + httpx. Voice input: pip install sounddevice numpy (STT runs on the hub:
pip install faster-whisper there). Speech output: pyttsx3 if installed, else Windows' built-in voice via PowerShell.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser

from kairos.pet.brain import Bubble, PetBrain, VoiceActivity, shorten, status_text
from kairos.pet.sprite import H, SKINS, W, frame, to_rows

log = logging.getLogger("kairos.pet")
MAGIC = "#ff00fe"  # painted = see-through (Windows transparent colour key)
SAMPLE_RATE, FRAME = 16_000, 1280


# --- hub ---------------------------------------------------------------------------------------
class HubClient:
    def __init__(self, base: str, device: str):
        import httpx

        self.base, self.device = base.rstrip("/"), device
        self.http = httpx.Client(base_url=self.base, timeout=httpx.Timeout(60, connect=3))

    def status(self) -> dict | None:
        try:
            return self.http.get("/api/status", timeout=5).json()
        except Exception:
            return None

    def outbox(self, after: int) -> list[dict]:
        return self.http.get("/api/outbox", params={"after": after}, timeout=5).json()

    def chat(self, text: str) -> str:
        return self.http.post("/chat", json={"device_id": self.device, "text": text}).json()["reply"]

    def voice(self, wav: bytes) -> dict:
        r = self.http.post("/api/voice", params={"device_id": self.device}, content=wav,
                           headers={"Content-Type": "audio/wav"})
        if r.status_code == 503:
            raise RuntimeError(r.json().get("detail") or "speech-to-text isn't installed on the hub")
        r.raise_for_status()
        return r.json()

    def recent_messages(self, limit: int = 5) -> list[dict]:
        try:
            return self.http.get("/api/recent_messages", params={"limit": limit}, timeout=5).json()
        except Exception:
            return []


# --- voice in/out ----------------------------------------------------------------------------------
class Mouth:
    """Text-to-speech: pyttsx3 if installed, else Windows' built-in System.Speech (no install). Interruptible."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.proc: subprocess.Popen | None = None

    def say(self, text: str) -> None:
        if not self.enabled or not text:
            return
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", 185)
            engine.say(text)
            engine.runAndWait()
            return
        except Exception:
            pass
        if sys.platform == "win32":  # the text goes in via an env var: never part of the command line
            script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                      " $s.Rate = 1; $s.Speak($env:KAIROS_SAY)")
            self.proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                         env={**os.environ, "KAIROS_SAY": text[:1500]},
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.proc.wait()
            self.proc = None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class Ears:
    """Microphone -> WAV bytes, ending on a pause. Needs sounddevice + numpy."""

    @staticmethod
    def available() -> bool:
        import importlib.util

        return all(importlib.util.find_spec(m) for m in ("sounddevice", "numpy"))

    def __init__(self):
        self.cancelled = False

    def listen(self) -> bytes | None:
        import numpy as np
        import sounddevice as sd

        from kairos.voice.stt import write_wav

        self.cancelled = False
        vad, frames = VoiceActivity(), []
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
            while not self.cancelled:
                data, _ = stream.read(FRAME)
                f = data.flatten()
                frames.append(f)
                verdict = vad.feed(float(np.sqrt(np.mean(f.astype("float32") ** 2))), FRAME / SAMPLE_RATE)
                if verdict != "listen":
                    break
        if self.cancelled or vad.speech < vad.min_speech_s:
            return None
        return write_wav(np.concatenate(frames).astype("int16"))


# --- the window -------------------------------------------------------------------------------------
class PetApp:
    def __init__(self, args):
        import tkinter as tk

        self.tk = tk
        self.args = args
        self.hub = HubClient(args.hub, args.device)
        self.brain = PetBrain(devices=() if args.all_devices else (args.device,))
        self.mouth = Mouth(enabled=not args.no_tts)
        self.ears = Ears() if Ears.available() else None
        self.voice_chat = args.voice_chat
        self.events: queue.Queue = queue.Queue()
        self.turn_running = False
        self.t0 = time.time()

        _attach_to_default_desktop()
        _dpi_aware()
        self.root = root = tk.Tk()
        root.title("KAIROS")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=MAGIC)
        try:
            root.attributes("-transparentcolor", MAGIC)
        except tk.TclError:  # not Windows: no colour-key transparency, use a dark square instead
            pass
        s = self.scale = args.scale
        self.canvas = tk.Canvas(root, width=W * s, height=H * s, bg=MAGIC, highlightthickness=0, bd=0,
                                cursor="hand2")
        self.canvas.pack()
        self.small = tk.PhotoImage(width=W, height=H)  # one frame at 1:1, zoomed up to the window size
        self.big = self.small.zoom(s)
        self.sprite_item = self.canvas.create_image(0, 0, anchor="nw", image=self.big)
        self.last_rows = None
        right, bottom = _work_area(root)
        root.geometry(f"{W * s}x{H * s}+{right - W * s - 24}+{bottom - H * s - 36}")
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)

        self.bubble = None
        self.bubble_after = None
        self._drag = None
        self._hover_after = None
        self._seq_after = None
        self._closing = False
        self._menu()
        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._move)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", lambda e: webbrowser.open(self.hub.base + "/"))
        c.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        c.bind("<Enter>", lambda e: self._hover(True))
        c.bind("<Leave>", lambda e: self._hover(False))
        root.bind("<F9>", lambda e: self.events.put(("hotkey_f9", None)))
        root.bind("<Control-Shift-K>", lambda e: self.events.put(("hotkey_f9", None)))
        root.bind("<Control-Shift-k>", lambda e: self.events.put(("hotkey_f9", None)))
        root.bind("<Control-k>", lambda e: self.events.put(("hotkey_f9", None)))

        self._start_hotkey_listener()
        threading.Thread(target=self._poll, daemon=True).start()
        self._tick()

    def _start_hotkey_listener(self) -> None:
        if sys.platform != "win32":
            return

        def listen():
            import ctypes
            _attach_to_default_desktop()
            was_down = False
            while not getattr(self, "_closing", False):
                try:
                    # 0x78 = VK_F9; also check Ctrl+Shift+K (0x11, 0x10, 0x4B)
                    f9 = bool(ctypes.windll.user32.GetAsyncKeyState(0x78) & 0x8000)
                    ctrl = bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
                    shift = bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
                    k_key = bool(ctypes.windll.user32.GetAsyncKeyState(0x4B) & 0x8000)
                    is_down = f9 or (ctrl and shift and k_key)
                    if is_down and not was_down:
                        print("[PET HOTKEY] Activated via F9 / Ctrl+Shift+K!", flush=True)
                        self.events.put(("hotkey_f9", None))
                    was_down = is_down
                except Exception:
                    pass
                time.sleep(0.06)

        threading.Thread(target=listen, daemon=True).start()

    # --- drawing ------------------------------------------------------------------------------
    def _tick(self) -> None:
        self._drain()
        t = time.time() - self.t0
        mood = self.brain.current()
        rows = to_rows(frame(mood, t, self._look(), self.args.skin), MAGIC)
        if rows != self.last_rows:  # only re-render when something moved
            self.small.put(" ".join("{" + " ".join(r) + "}" for r in rows), to=(0, 0))
            self.big = self.small.zoom(self.scale)
            self.canvas.itemconfigure(self.sprite_item, image=self.big)
            self.last_rows = rows
        if int(t * 10) % 25 == 0:  # periodically re-assert topmost
            try:
                self.root.attributes("-topmost", True)
            except Exception:
                pass
        self.root.after(80, self._tick)

    def _look(self) -> tuple[float, float]:
        """Where the mouse is relative to the pet's face, -1..1 on each axis (the eyes follow it)."""
        try:
            px, py = self.root.winfo_pointerxy()
        except Exception:
            return 0.0, 0.0
        cx = self.root.winfo_rootx() + W * self.scale / 2
        cy = self.root.winfo_rooty() + 32 * self.scale
        dx, dy = px - cx, py - cy
        dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
        if dist < 12 * self.scale:  # mouse on the pet: look at you
            return 0.0, 0.0
        return dx / dist * 1.4, dy / dist * 1.4

    # --- bubbles ------------------------------------------------------------------------------
    def say_bubble(self, text: str, seconds: float = 6.0, kind: str = "speak") -> None:
        tk = self.tk
        self.hide_bubble()
        b = self.bubble = tk.Toplevel(self.root)
        b.overrideredirect(True)
        b.attributes("-topmost", True)
        colour = {"alert": "#3a1d1d", "status": "#1c2430"}.get(kind, "#151b23")
        edge = {"alert": "#f85149", "status": "#7c9cff"}.get(kind, "#57f287")
        frame_ = tk.Frame(b, bg=edge, padx=1, pady=1)
        frame_.pack()
        label = tk.Label(frame_, text=text, bg=colour, fg="#e6edf3", justify="left", wraplength=260,
                         font=("Segoe UI", 10), padx=10, pady=7)
        label.pack()
        label.bind("<Button-1>", lambda e: self.hide_bubble())
        b.update_idletasks()
        x = self.root.winfo_x() - b.winfo_width() + 2 * self.scale  # up and to the left of the pet
        y = self.root.winfo_y() - b.winfo_height() + 16 * self.scale
        b.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.bubble_after = self.root.after(int(seconds * 1000), self.hide_bubble)

    def hide_bubble(self) -> None:
        if self.bubble_after:
            self.root.after_cancel(self.bubble_after)
            self.bubble_after = None
        if self.bubble is not None:
            self.bubble.destroy()
            self.bubble = None

    # --- hub polling (background thread) -> UI queue --------------------------------------------------
    def _poll(self) -> None:
        last_status = 0.0
        while True:
            now = time.time()
            if now - last_status > 10 or not self.brain.online:
                st = self.hub.status()
                self.events.put(("status", st))
                last_status = now
            if self.brain.online:
                try:
                    self.events.put(("outbox", self.hub.outbox(self.brain.seen_seq or 0)))
                except Exception:
                    self.events.put(("status", None))
            time.sleep(1.5 if self.brain.online else 5)

    def _drain(self) -> None:
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                was = self.brain.online
                self.brain.on_status(data)
                if data and not was:
                    self.say_bubble("Hi! I'm here. Press F9 for updates or click me to talk.", 4.5, "status")
            elif kind == "outbox":
                for b in self.brain.on_outbox(data):
                    self._show(b)
            elif kind == "bubble":
                self._show(data)
            elif kind == "busy":
                self.brain.set_busy(data)
            elif kind == "hotkey_f9":
                self.activate_with_messages()
            elif kind == "play_sequence":
                self._play_message_sequence(data)

    def _show(self, b: Bubble) -> None:
        self.say_bubble(b.text, b.seconds, "alert" if b.mood == "alert" else "speak")
        if self.args.speak_alerts and b.mood == "alert":
            threading.Thread(target=self.mouth.say, args=(b.text,), daemon=True).start()

    # --- F9 activation & message sequencer ----------------------------------------------------
    def activate_with_messages(self) -> None:
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
        except Exception:
            pass

        self._cancel_sequence()
        if self.turn_running:
            if self.ears:
                self.ears.cancelled = True
            self.mouth.stop()
            self.turn_running = False

        self.brain.set_busy("speaking")
        self.say_bubble("⚡ F9: Checking recent updates...", 2.5, "status")

        def fetch():
            msgs = self.hub.recent_messages(limit=5)
            self.events.put(("play_sequence", msgs))

        threading.Thread(target=fetch, daemon=True).start()

    def _cancel_sequence(self) -> None:
        if self._seq_after:
            self.root.after_cancel(self._seq_after)
            self._seq_after = None

    def _play_message_sequence(self, messages: list[dict], index: int = 0) -> None:
        if not messages:
            self.brain.set_busy(None)
            self.say_bubble("✨ You're all caught up! No recent messages.\nClick me to speak or ask anything.", 4.5, "speak")
            return

        if index >= len(messages):
            self.brain.set_busy(None)
            self.say_bubble(f"✨ All {len(messages)} updates shown!\nClick me to speak or ask anything.", 5.0, "speak")
            return

        msg = messages[index]
        sender = msg.get("sender") or msg.get("type") or "Notification"
        text = msg.get("text") or msg.get("raw") or ""
        bubble_text = f"[{index + 1}/{len(messages)}] {sender}:\n{shorten(text, 130)}"

        self.brain.set_busy("speaking")
        display_seconds = max(3.5, min(7.0, len(text.split()) * 0.35 + 2.0))
        self.say_bubble(bubble_text, display_seconds, "alert" if msg.get("type") == "notification" else "speak")

        self._seq_after = self.root.after(int(display_seconds * 1000),
                                          lambda: self._play_message_sequence(messages, index + 1))

    # --- talking -------------------------------------------------------------------------------
    def talk(self) -> None:
        self._cancel_sequence()
        if self.turn_running:  # click while busy = stop
            if self.ears:
                self.ears.cancelled = True
            self.mouth.stop()
            self.turn_running = False
            self.events.put(("busy", None))
            self.hide_bubble()
            return
        if not self.brain.online:
            self.say_bubble("I can't reach the hub. Start it with `kairos hub`.", 5, "alert")
            return
        if self.ears is None:
            return self.type_message()
        self.turn_running = True
        threading.Thread(target=self._voice_turns, daemon=True).start()

    def _voice_turns(self) -> None:
        try:
            while True:
                self.events.put(("busy", "listening"))
                self.events.put(("bubble", Bubble("🎙️ Listening... (speak now)", 10, "status")))
                wav = self.ears.listen()
                if wav is None:
                    if self.voice_chat:
                        self.events.put(("bubble", Bubble("Voice chat paused. Click me when you want me.", 4, "speaking")))
                    else:
                        self.events.put(("bubble", Bubble("No speech detected. Click me to speak.", 3, "speaking")))
                    return
                self.events.put(("busy", "thinking"))
                self.events.put(("bubble", Bubble("⏳ Transcribing & thinking...", 8, "status")))
                try:
                    r = self.hub.voice(wav)
                except Exception as exc:
                    self.events.put(("bubble", Bubble(shorten(f"Voice didn't work: {exc}", 200), 7, "alert")))
                    return
                if not r.get("text"):
                    self.events.put(("bubble", Bubble("Sorry, I couldn't make that out.", 3, "speaking")))
                    if not self.voice_chat:
                        return
                    continue
                reply = r.get("reply") or ""
                self.events.put(("bubble", Bubble(f"“{shorten(r['text'], 80)}”\n\n{shorten(reply, 260)}",
                                                  max(5, len(reply.split()) * 0.4 + 3), "speaking")))
                self.events.put(("busy", "speaking"))
                self.mouth.say(reply)
                if not self.voice_chat or reply.startswith("Just to confirm"):
                    return  # confirmations are answered deliberately: click again and say yes/no
        finally:
            self.events.put(("busy", None))
            self.turn_running = False

    def type_message(self) -> None:
        from tkinter import simpledialog

        text = simpledialog.askstring("KAIROS", "Say something to KAIROS:", parent=self.root)
        if not text:
            return
        self.brain.set_busy("thinking")

        def run():
            try:
                reply = self.hub.chat(text)
                self.events.put(("bubble", Bubble(shorten(reply, 300), max(5, len(reply.split()) * 0.4 + 3))))
                self.events.put(("busy", "speaking"))
                self.mouth.say(reply)
            except Exception as exc:
                self.events.put(("bubble", Bubble(f"Couldn't reach KAIROS: {exc}", 5, "alert")))
            finally:
                self.events.put(("busy", None))

        threading.Thread(target=run, daemon=True).start()

    # --- mouse ------------------------------------------------------------------------------------
    def _press(self, e) -> None:
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), False)

    def _move(self, e) -> None:
        if not self._drag:
            return
        x0, y0, wx, wy, _ = self._drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if abs(dx) + abs(dy) > 4:
            self._drag = (x0, y0, wx, wy, True)
            self.root.geometry(f"+{wx + dx}+{wy + dy}")
            self.hide_bubble()

    def _release(self, e) -> None:
        moved = self._drag and self._drag[4]
        self._drag = None
        if not moved:
            self.root.after(250, self._single_click)  # wait: it may be a double-click
            self._clicks = getattr(self, "_clicks", 0) + 1

    def _single_click(self) -> None:
        clicks, self._clicks = getattr(self, "_clicks", 0), 0
        if clicks == 1:
            self._cancel_sequence()
            self.talk()

    def _hover(self, inside: bool) -> None:
        if self._hover_after:
            self.root.after_cancel(self._hover_after)
            self._hover_after = None
        if inside and self.bubble is None and not self.turn_running:
            self._hover_after = self.root.after(
                700, lambda: self.say_bubble(status_text(self.brain.status), 5, "status"))

    def _menu(self) -> None:
        tk = self.tk
        m = self.menu = tk.Menu(self.root, tearoff=0)
        self.vc_var = tk.BooleanVar(value=self.voice_chat)
        self.mute_var = tk.BooleanVar(value=not self.mouth.enabled)
        m.add_command(label="Check updates (F9)", command=self.activate_with_messages)
        m.add_command(label="Talk" + ("" if self.ears else " (pip install sounddevice numpy)"), command=self.talk,
                      state="normal" if self.ears else "disabled")
        m.add_command(label="Type a message…", command=self.type_message)
        m.add_checkbutton(label="Voice chat (keep listening)", variable=self.vc_var,
                          command=lambda: setattr(self, "voice_chat", self.vc_var.get()))
        m.add_checkbutton(label="Mute", variable=self.mute_var,
                          command=lambda: setattr(self.mouth, "enabled", not self.mute_var.get()))
        m.add_command(label="Status", command=lambda: self.say_bubble(status_text(self.brain.status), 6, "status"))
        m.add_command(label="Open dashboard", command=lambda: webbrowser.open(self.hub.base + "/"))
        m.add_separator()
        m.add_command(label="Quit", command=self._quit)

    def _quit(self) -> None:
        self._closing = True
        self._cancel_sequence()
        if self.ears:
            self.ears.cancelled = True
        self.mouth.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# --- Windows helpers ----------------------------------------------------------------------------------
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


def _dpi_aware() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp pixels instead of blurry scaling
    except Exception:
        pass


def _work_area(root) -> tuple[int, int]:
    """Right/bottom of the desktop area not covered by the taskbar."""
    _attach_to_default_desktop()
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.right, rect.bottom
    except Exception:
        pass
    return root.winfo_screenwidth(), root.winfo_screenheight() - 48


def main(argv: list[str] | None = None) -> None:
    _attach_to_default_desktop()
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="kairos pet", description="KAIROS desktop pet")
    default_hub = os.environ.get("KAIROS_HUB_HTTP") or os.environ.get("KAIROS_HUB_PUBLIC_URL") or "http://localhost:8000"
    p.add_argument("--hub", default=default_hub)
    p.add_argument("--device", default="pc-main", help="device id this pet speaks for")
    p.add_argument("--scale", type=int, default=3, help="size of one pixel (2 = small, 3 = normal, 4 = big)")
    p.add_argument("--skin", default=os.environ.get("KAIROS_PET_SKIN", "slate"), choices=sorted(SKINS),
                   help="colour theme")
    p.add_argument("--voice-chat", action="store_true", help="keep listening after each reply")
    p.add_argument("--no-tts", action="store_true", help="don't speak replies aloud")
    p.add_argument("--speak-alerts", action="store_true", help="also read alerts aloud (off: the PC agent may)")
    p.add_argument("--all-devices", action="store_true", help="show bubbles for every device, not just this one")
    PetApp(p.parse_args(argv)).run()


if __name__ == "__main__":
    main()
