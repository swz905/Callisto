"""Speech-to-text endpoint + WAV handling, and the desktop pet's sprite and logic (no window, no mic)."""

import math
from array import array

import pytest

from kairos.pet.brain import PetBrain, VoiceActivity, bubble_for, shorten, status_text
from kairos.pet.sprite import FACE, H, MOODS, SCREEN_H, SCREEN_W, SKINS, W, bob_offset, frame, mix, text_bitmap, to_rows
from kairos.voice.stt import clean_transcript, is_wav, read_wav, write_wav


# --- audio ------------------------------------------------------------------------------------------
def tone(seconds=0.5, rate=16000, channels=1):
    n = int(seconds * rate)
    mono = [int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n)]
    return array("h", [s for s in mono for _ in range(channels)])


def stereo_wav(samples, rate):
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def test_wav_roundtrip_and_stereo_downmix():
    data = write_wav(tone(0.5))
    assert is_wav(data) and not is_wav(b"OggS....")
    pcm = read_wav(data)
    assert pcm.rate == 16000 and len(pcm.samples) == 8000 and abs(pcm.seconds - 0.5) < 1e-6
    st = read_wav(stereo_wav(tone(0.25, 48000, channels=2), 48000))
    assert st.rate == 48000 and len(st.samples) == 12000  # mono after averaging channels


def test_whisper_silence_hallucinations_are_dropped():
    assert clean_transcript(" Thank you. ") == ""
    assert clean_transcript("  where is   my office ") == "where is my office"


class FakeSTT:
    def __init__(self, text="", error=None):
        self.text, self.error, self.got = text, error, None

    def transcribe(self, data):
        self.got = data
        if self.error:
            raise self.error
        return self.text

    def status(self):
        return {"available": True, "model": "fake", "loaded": True, "error": None}


@pytest.fixture()
def hub(monkeypatch):
    from fastapi.testclient import TestClient

    from kairos.hub import app as hub_app

    replies = []

    class K:
        def chat(self, text, device, session):
            replies.append((text, device))
            return type("R", (), {"text": f"You said {text}", "actions": [], "planner": "rules"})()

    monkeypatch.setitem(hub_app.state, "k", K())
    yield TestClient(hub_app.app), hub_app, replies
    hub_app.state.pop("stt", None)


def test_voice_endpoint_transcribes_then_chats(hub):
    client, hub_app, replies = hub
    hub_app.state["stt"] = stt = FakeSTT("where is my office")
    wav = write_wav(tone(0.5))
    r = client.post("/api/voice?device_id=phone-arjun", content=wav, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200
    assert r.json() == {"text": "where is my office", "reply": "You said where is my office", "actions": [],
                        "planner": "rules"}
    assert stt.got == wav and replies == [("where is my office", "phone-arjun")]
    assert client.get("/api/voice/status").json()["model"] == "fake"


def test_voice_endpoint_silence_and_transcribe_only(hub):
    client, hub_app, replies = hub
    hub_app.state["stt"] = FakeSTT("")
    assert client.post("/api/voice", content=b"x").json() == {"text": "", "reply": None}
    hub_app.state["stt"] = FakeSTT("hello")
    assert client.post("/api/voice?reply=false", content=b"x").json() == {"text": "hello", "reply": None}
    assert replies == []  # nothing was sent to the agent


def test_voice_endpoint_reports_missing_model_and_bad_audio(hub):
    from kairos.voice.stt import STTUnavailable

    client, hub_app, _ = hub
    hub_app.state["stt"] = FakeSTT(error=STTUnavailable("speech-to-text needs: pip install faster-whisper numpy"))
    r = client.post("/api/voice", content=b"x")
    assert r.status_code == 503 and "faster-whisper" in r.json()["detail"]
    hub_app.state["stt"] = FakeSTT(error=ValueError("only 16-bit PCM WAV is supported"))
    assert client.post("/api/voice", content=b"x").status_code == 400
    assert client.post("/api/voice", content=b"0" * (8 * 1024 * 1024 + 1)).status_code == 413


def test_stt_without_packages_says_what_to_install(monkeypatch):
    from kairos.voice.stt import SpeechToText, STTUnavailable

    stt = SpeechToText()
    monkeypatch.setattr(SpeechToText, "installed", staticmethod(lambda: False))
    assert stt.status()["available"] is False
    with pytest.raises(STTUnavailable, match="pip install faster-whisper"):
        stt.transcribe(write_wav(tone(0.5)))
    assert stt.transcribe(write_wav(tone(0.1))) == ""  # too short to be speech: no model needed


# --- pet sprite ------------------------------------------------------------------------------------------
def test_every_mood_and_skin_draws_inside_the_grid_with_its_face_colour():
    for skin in SKINS:
        for mood in MOODS:
            for t in (0.3, 1.9, 3.3):
                px = frame(mood, t, (0, 0), skin)
                assert all(0 <= x < W and 0 <= y < H for x, y in px)
                assert all(len(c) == 7 and c.startswith("#") for c in px.values())
                if mood != "sleep":  # sleep dims the screen
                    assert FACE[mood] in px.values(), (skin, mood)
    rows = to_rows(frame("idle", 0.5), "#ff00fe")
    assert len(rows) == H and all(len(r) == W for r in rows) and rows[0][0] == "#ff00fe"


def test_offline_screen_says_null_and_animations_move():
    rows = text_bitmap("NULL")
    assert len(rows) == SCREEN_H and all(len(r) == SCREEN_W for r in rows)
    assert any(r.startswith("#..#", r.index("#")) for r in rows if "#" in r)  # the N
    assert frame("speaking", 0.0) != frame("speaking", 0.12)  # mouth opens and closes
    assert frame("listening", 0.0) != frame("listening", 0.25)  # sound waves
    assert frame("happy", 0.0) != frame("happy", 0.3)  # waving + hearts
    assert frame("idle", 0.05) != frame("idle", 1.0)  # blink
    assert frame("idle", 1.0, look=(-1, 0)) != frame("idle", 1.0, look=(1, 0))  # eyes follow the mouse
    assert bob_offset("offline", 1.0) == 0 and {bob_offset("idle", t / 10) for t in range(40)} == {0, 1}
    assert mix("#000000", "#ffffff", .5) == "#808080"


# --- pet logic -----------------------------------------------------------------------------------------------
def test_brain_moods_and_bubbles():
    b = PetBrain(devices=("pc-main",))
    assert b.current(0) == "offline"
    b.on_status({"devices": []})
    assert b.current() == "happy"  # just came online
    assert b.on_outbox([{"seq": 5, "kind": "alert", "device_id": "pc-main", "text": "old"}]) == []  # watermark only
    out = b.on_outbox([
        {"seq": 5, "kind": "alert", "device_id": "pc-main", "text": "old"},
        {"seq": 6, "kind": "alert", "device_id": "phone-arjun", "text": "for the phone"},
        {"seq": 7, "kind": "exec", "device_id": "pc-main", "text": "dir"},
        {"seq": 8, "kind": "alert", "device_id": "pc-main", "text": "Heads up: your download finished"},
    ])
    assert [x.text for x in out] == ["Heads up: your download finished"] and out[0].mood == "alert"
    assert b.current() == "alert" and b.seen_seq == 8
    b.set_busy("listening")
    assert b.current() == "listening"
    b.set_busy(None)
    assert b.current(now=10**12) == "idle"  # transient mood expired
    b.on_status(None)
    assert b.current() == "offline"


def test_bubble_text_and_status():
    long = "word " * 100
    bub = bubble_for({"kind": "speak", "text": long})
    assert len(bub.text) <= 220 and bub.text.endswith("…") and bub.mood == "speaking" and bub.seconds == 14.0
    assert shorten("short", 10) == "short"
    assert "hub" in status_text({})
    st = status_text({"llm": {"online": True, "model": "x/y"}, "devices": [
        {"kind": "pc", "summary": "pc-main: Chrome, battery 71%"}, {"kind": "chat", "summary": "telegram"}]})
    assert st.splitlines() == ["Brain: online · x/y", "💻 pc-main: Chrome, battery 71%"]


def test_voice_activity_detector():
    vad = VoiceActivity(threshold=100, silence_s=0.5, min_speech_s=0.2, no_speech_s=2, max_s=5)
    steps = [vad.feed(500, 0.1) for _ in range(3)] + [vad.feed(10, 0.1) for _ in range(5)]
    assert steps[:7] == ["listen"] * 7 and steps[7] == "done"
    quiet = VoiceActivity(threshold=100, no_speech_s=1.0)
    assert [quiet.feed(10, 0.25) for _ in range(4)][-1] == "nothing"
    endless = VoiceActivity(threshold=100, max_s=1.0)
    assert [endless.feed(500, 0.25) for _ in range(4)][-1] == "done"  # talking non-stop still ends


def test_recent_messages_endpoint_and_client(hub):
    client, hub_app, _ = hub

    class FakeGraph:
        def run(self, q, **kw):
            if q == "recent_events":
                return [
                    {"id": "e1", "type": "notification", "text": "WhatsApp: Rahul — Are we meeting at 5?", "ts": 1000, "device": "phone-arjun", "importance": 3},
                    {"id": "e2", "type": "notification", "text": "Uber: Your ride has arrived", "ts": 950, "device": "phone-arjun", "importance": 2},
                ]
            return []

    hub_app.state["k"].graph = FakeGraph()
    r = client.get("/api/recent_messages?limit=5")
    assert r.status_code == 200
    msgs = r.json()
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "WhatsApp"
    assert "Rahul" in msgs[0]["text"]
    assert msgs[1]["sender"] == "Uber"
    assert "arrived" in msgs[1]["text"]

