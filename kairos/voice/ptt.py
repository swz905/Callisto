"""Push-to-talk voice loop: hold a key, speak, release -> text -> hub /chat -> spoken reply.

  python -m kairos.voice.ptt --hub http://localhost:8000 --device pc-main [--key f9]

Needs (install later): faster-whisper, sounddevice, numpy, keyboard; optional pyttsx3 or piper for speech.
Swap engines freely; only `transcribe()` and `say()` matter to the rest of the system.
"""

from __future__ import annotations

import argparse
import queue

SAMPLE_RATE = 16_000


class STT:
    def __init__(self, model: str = "base.en"):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, audio) -> str:
        segments, _ = self.model.transcribe(audio, language="en", vad_filter=True, beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()


def say(text: str) -> None:
    print(f"kairos> {text}")
    try:
        import pyttsx3

        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except Exception:
        pass


def record_while_held(key: str):
    import keyboard
    import numpy as np
    import sounddevice as sd

    chunks: queue.Queue = queue.Queue()
    keyboard.wait(key)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        callback=lambda data, *_: chunks.put(data.copy())):
        while keyboard.is_pressed(key):
            sd.sleep(30)
    frames = [chunks.get() for _ in range(chunks.qsize())]
    return np.concatenate(frames).flatten() if frames else np.zeros(0, dtype="float32")


def main() -> None:
    import httpx

    p = argparse.ArgumentParser()
    p.add_argument("--hub", default="http://localhost:8000")
    p.add_argument("--device", default="pc-main")
    p.add_argument("--key", default="f9")
    p.add_argument("--model", default="base.en")
    args = p.parse_args()

    stt = STT(args.model)
    print(f"Hold {args.key.upper()} and speak. Ctrl+C to quit.")
    while True:
        audio = record_while_held(args.key)
        if audio.size < SAMPLE_RATE * 0.3:
            continue
        text = stt.transcribe(audio)
        if not text:
            continue
        print(f"you> {text}")
        r = httpx.post(f"{args.hub}/chat", json={"device_id": args.device, "text": text}, timeout=30)
        say(r.json()["reply"])


if __name__ == "__main__":
    main()
