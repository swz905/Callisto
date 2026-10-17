"""Speech-to-text for the hub: one local faster-whisper model shared by every voice client.

Clients (the dashboard mic, the desktop pet, phone) send a 16-bit PCM WAV; the hub turns it into text and runs the
normal chat turn. Audio never leaves the machine. WAV parsing is pure stdlib so clients don't need ffmpeg; other
containers (webm/ogg/mp3) are handed to faster-whisper's own decoder (PyAV).

Install (later): pip install faster-whisper numpy      Model: KAIROS_STT_MODEL (base.en, small.en, or a local path)
"""

from __future__ import annotations

import importlib.util
import io
import logging
import threading
import wave
from array import array
from dataclasses import dataclass

log = logging.getLogger(__name__)
TARGET_RATE = 16_000
MAX_SECONDS = 60


class STTUnavailable(RuntimeError):
    pass


@dataclass
class Pcm:
    samples: array  # int16, mono
    rate: int

    @property
    def seconds(self) -> float:
        return len(self.samples) / self.rate if self.rate else 0.0


def is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def read_wav(data: bytes) -> Pcm:
    """16-bit PCM WAV (any rate, mono or stereo) -> mono int16 samples."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        channels, rate = w.getnchannels(), w.getframerate()
        frames = w.readframes(min(w.getnframes(), rate * MAX_SECONDS))
    samples = array("h")
    samples.frombytes(frames)
    if channels > 1:  # average the channels
        samples = array("h", (sum(samples[i:i + channels]) // channels for i in range(0, len(samples), channels)))
    return Pcm(samples, rate)


def write_wav(samples, rate: int = TARGET_RATE) -> bytes:
    """int16 samples (array/list/numpy) -> WAV bytes. Used by clients that record raw audio."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(array("h", samples)) if not hasattr(samples, "tobytes") else samples.tobytes())
    return buf.getvalue()


def to_float32(pcm: Pcm):
    """Mono int16 at any rate -> float32 numpy array at 16 kHz (linear resampling)."""
    import numpy as np

    x = np.frombuffer(pcm.samples.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.rate != TARGET_RATE and len(x):
        n = int(len(x) * TARGET_RATE / pcm.rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


class SpeechToText:
    def __init__(self, model: str = "base.en", device: str = "cpu", language: str | None = "en"):
        self.model_name = model
        self.device = device
        self.language = language or None
        self._model = None
        self._lock = threading.Lock()  # one transcription at a time; the model isn't re-entrant
        self.last_error: str | None = None

    @staticmethod
    def installed() -> bool:
        return importlib.util.find_spec("faster_whisper") is not None and importlib.util.find_spec("numpy") is not None

    def status(self) -> dict:
        return {"available": self.installed(), "model": self.model_name, "loaded": self._model is not None,
                "error": self.last_error}

    def _load(self):
        if self._model is None:
            if not self.installed():
                raise STTUnavailable("speech-to-text needs: pip install faster-whisper numpy")
            from faster_whisper import WhisperModel

            log.info("loading speech model %s (first use downloads it)", self.model_name)
            try:
                self._model = WhisperModel(self.model_name, device=self.device,
                                           compute_type="int8" if self.device == "cpu" else "float16")
            except Exception as exc:  # no network for the first download, bad path, ...
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                raise STTUnavailable(f"could not load speech model {self.model_name}: {self.last_error}") from exc
        return self._model

    def transcribe(self, data: bytes) -> str:
        """Audio bytes (WAV preferred) -> text. Empty string for silence."""
        if self._model is None and not self.installed():
            if is_wav(data) and read_wav(data).seconds < 0.25:
                return ""
            raise STTUnavailable("speech-to-text needs: pip install faster-whisper numpy")
        if is_wav(data):
            pcm = read_wav(data)
            if pcm.seconds < 0.25:
                return ""
            audio = to_float32(pcm)
        else:
            audio = io.BytesIO(data)  # faster-whisper decodes other formats itself (needs PyAV)
        with self._lock:
            model = self._load()
            segments, _ = model.transcribe(audio, language=self.language, vad_filter=True, beam_size=1)
            text = " ".join(s.text.strip() for s in segments).strip()
        self.last_error = None
        return clean_transcript(text)


# Whisper hallucinates these on silence/noise.
_NOISE = {"", "you", "thank you.", "thanks for watching!", "thank you for watching.", ".", "bye.", "[blank_audio]",
          "(silence)", "[music]", "[ silence ]"}


def clean_transcript(text: str) -> str:
    t = " ".join((text or "").split())
    return "" if t.lower() in _NOISE else t
