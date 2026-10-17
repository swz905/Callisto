"""What the pet shows, decided without any UI (testable): mood, speech bubbles, status line."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

BUBBLE_KINDS = {"alert", "speak"}  # outbound messages worth a bubble (not open/exec/download/...)


@dataclass
class Bubble:
    text: str
    seconds: float
    mood: str = "speaking"


@dataclass
class PetBrain:
    """Mood state machine. Transient moods (alert, happy, speaking) fall back to idle after a while."""

    devices: tuple[str, ...] = ()  # device ids whose messages the pet announces ("" = all)
    online: bool = False
    mood: str = "sleep"
    until: float = 0.0
    busy: str | None = None  # listening / thinking while a voice turn is running
    seen_seq: int | None = None
    status: dict = field(default_factory=dict)

    def set_busy(self, mood: str | None) -> None:
        self.busy = mood

    def flash(self, mood: str, seconds: float, now: float | None = None) -> None:
        self.mood, self.until = mood, (now if now is not None else time.time()) + seconds

    def current(self, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        if self.busy:
            return self.busy
        if not self.online:
            return "offline"
        if self.mood not in ("idle", "sleep") and now < self.until:
            return self.mood
        return "idle"

    # --- hub data in ---------------------------------------------------------------------
    def on_status(self, status: dict | None) -> None:
        was = self.online
        self.online = status is not None
        self.status = status or {}
        if self.online and not was:
            self.flash("happy", 2.5)

    def on_outbox(self, msgs: list[dict]) -> list[Bubble]:
        """New hub messages -> bubbles. The first batch only sets the watermark (no flood of old alerts)."""
        if not msgs:
            return []
        latest = max(m.get("seq", 0) for m in msgs)
        if self.seen_seq is None:
            self.seen_seq = latest
            return []
        out = []
        for m in sorted(msgs, key=lambda m: m.get("seq", 0)):
            if m.get("seq", 0) <= self.seen_seq:
                continue
            if m.get("kind") in BUBBLE_KINDS and m.get("text") and \
                    (not self.devices or m.get("device_id") in self.devices):
                out.append(bubble_for(m))
        self.seen_seq = max(self.seen_seq, latest)
        if out:
            self.flash(out[-1].mood, min(6.0, out[-1].seconds))
        return out


class VoiceActivity:
    """Decides when a spoken turn is over from per-frame loudness (RMS of int16 audio).

    feed() returns "listen" (keep going), "done" (you spoke, then paused) or "nothing" (no speech at all)."""

    def __init__(self, threshold: float = 450.0, silence_s: float = 0.9, min_speech_s: float = 0.3,
                 no_speech_s: float = 6.0, max_s: float = 20.0):
        self.threshold, self.silence_s, self.min_speech_s = threshold, silence_s, min_speech_s
        self.no_speech_s, self.max_s = no_speech_s, max_s
        self.speech = self.quiet = self.total = 0.0

    def feed(self, rms: float, dt: float) -> str:
        self.total += dt
        if rms >= self.threshold:
            self.speech += dt
            self.quiet = 0.0
        elif self.speech >= self.min_speech_s:
            self.quiet += dt
        if self.speech >= self.min_speech_s and (self.quiet >= self.silence_s or self.total >= self.max_s):
            return "done"
        if self.speech < self.min_speech_s and self.total >= self.no_speech_s:
            return "nothing"
        return "listen"


def bubble_for(msg: dict) -> Bubble:
    text = " ".join(str(msg.get("text") or "").split())
    mood = "alert" if msg.get("kind") == "alert" else "speaking"
    return Bubble(shorten(text, 220), reading_seconds(text), mood)


def reading_seconds(text: str) -> float:
    return max(4.0, min(14.0, 1.5 + len(text.split()) * 0.35))


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def status_text(status: dict) -> str:
    """Hover text: who's online, battery, LLM."""
    if not status:
        return "I can't reach the KAIROS hub. Is `kairos hub` running?"
    llm = status.get("llm") or {}
    lines = [f"Brain: {'online · ' + str(llm.get('model') or '') if llm.get('online') else 'offline rules'}".strip(" ·")]
    for d in status.get("devices") or []:
        if d.get("kind") == "chat":
            continue
        name = {"pc": "💻", "phone": "📱"}.get(d.get("kind"), "•")
        summary = d.get("summary") or d.get("id")
        lines.append(f"{name} {shorten(str(summary), 90)}")
    return "\n".join(lines[:4])
