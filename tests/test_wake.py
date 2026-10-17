"""Wake-word recorder logic (needs numpy; skipped when it isn't installed)."""

import pytest

np = pytest.importorskip("numpy")

from kairos.voice.wake import FRAME, record_until_silence  # noqa: E402


def frames(pattern):
    loud = (np.ones(FRAME) * 3000).astype("int16")
    quiet = np.zeros(FRAME, dtype="int16")
    seq = iter([loud if c == "L" else quiet for c in pattern])
    return lambda: next(seq, quiet)


def test_stops_after_trailing_silence():
    audio, spoke = record_until_silence(frames("LLLLLL" + "." * 20), silence_s=0.8)
    assert spoke and len(audio) == FRAME * (6 + 10)  # 0.8 s of quiet = 10 frames of 80 ms


def test_silence_only_is_not_speech():
    audio, spoke = record_until_silence(frames("." * 200), max_s=2.0)
    assert not spoke and len(audio) == FRAME * 25
