"""Always-listening voice: wake word -> record until silence -> local STT -> hub /chat -> spoken reply.

  python -m kairos.voice.wake --hub http://localhost:8000 --device pc-main [--wake-model hey_jarvis]

Uses openWakeWord (pre-trained "hey_jarvis", "alexa", "hey_mycroft"...; a custom "hey kairos" model can be
trained with openWakeWord's tooling and passed as a .onnx/.tflite path) + faster-whisper for STT.
Runs the same on a Raspberry Pi with a USB mic/speaker: that's the "dedicated hardware" satellite - it is
just one more Device talking to the same hub.
Install: pip install openwakeword faster-whisper sounddevice numpy (+ pyttsx3 or piper for speech).
"""

from __future__ import annotations

import argparse
import time

SAMPLE_RATE = 16_000
FRAME = 1280  # 80 ms, what openWakeWord expects


def rms(frame) -> float:
    import numpy as np

    return float(np.sqrt(np.mean(frame.astype("float32") ** 2))) if len(frame) else 0.0


def record_until_silence(read_frame, max_s: float = 12.0, silence_s: float = 0.9, min_speech_s: float = 0.3,
                         threshold: float = 450.0):
    """Collect int16 frames until `silence_s` of quiet after speech started (or `max_s`).

    `read_frame()` returns one int16 numpy frame; injectable for tests."""
    import numpy as np

    frames, speech, quiet = [], 0.0, 0.0
    step = FRAME / SAMPLE_RATE
    total = 0.0
    while total < max_s:
        f = read_frame()
        frames.append(f)
        total += step
        if rms(f) >= threshold:
            speech += step
            quiet = 0.0
        elif speech >= min_speech_s:
            quiet += step
            if quiet >= silence_s - 1e-5:
                break
    return np.concatenate(frames) if frames else np.zeros(0, dtype="int16"), speech >= min_speech_s


def main() -> None:
    import httpx
    import numpy as np
    import sounddevice as sd

    from kairos.voice.ptt import STT, say

    p = argparse.ArgumentParser()
    p.add_argument("--hub", default="http://localhost:8000")
    p.add_argument("--device", default="pc-main")
    p.add_argument("--wake-model", default="hey_jarvis", help="pre-trained name or path to a custom model")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--stt-model", default="base.en")
    args = p.parse_args()

    from openwakeword.model import Model

    try:
        from openwakeword.utils import download_models

        download_models()
    except Exception:
        pass  # already present, or offline with models cached
    oww = Model(wakeword_models=[args.wake_model])
    stt = STT(args.stt_model)
    print(f"Listening for '{args.wake_model}'… (Ctrl+C to quit)")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
        def read_frame():
            data, _ = stream.read(FRAME)
            return data.flatten()

        while True:
            scores = oww.predict(read_frame())
            if max(scores.values(), default=0) < args.threshold:
                continue
            print("(wake word) listening…")
            audio, spoke = record_until_silence(read_frame)
            if hasattr(oww, "reset"):
                oww.reset()
            if not spoke:
                continue
            text = stt.transcribe(audio.astype(np.float32) / 32768.0)
            if not text:
                continue
            print(f"you> {text}")
            try:
                r = httpx.post(f"{args.hub}/chat", json={"device_id": args.device, "text": text}, timeout=60)
                say(r.json()["reply"])
            except Exception as exc:
                print(f"hub unreachable: {exc}")
            time.sleep(0.3)  # don't re-trigger on our own reply


if __name__ == "__main__":
    main()
