"""B: lane prioritária da boca (origem 'boca') sem furar playback em curso."""
from __future__ import annotations

import threading
import time

import main


def _obj():
    obj = object.__new__(main.JarvisLive)
    obj._speech_epoch = 0
    obj._turn_ref = 0
    return obj


def test_boca_line_jumps_ahead_of_queued_brain_sentences():
    obj = _obj()
    processed = []
    started = threading.Event()
    release = threading.Event()

    def fake_say(text, **_kw):
        if text == "n1":
            started.set()
            assert release.wait(timeout=2.0)
        processed.append(str(text))

    obj._say_blocking = fake_say
    obj._ensure_say_worker()
    obj._say_now("n1", log=True, origin="qoder")
    assert started.wait(timeout=2.0)      # worker ocupado na frase em curso
    obj._say_now("n2", log=True, origin="qoder")
    obj._say_now("b1", log=True, origin="boca")
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(processed) < 3:
        time.sleep(0.01)
    assert processed == ["n1", "b1", "n2"]


def test_boca_line_wakes_an_idle_worker():
    obj = _obj()
    processed = []
    obj._say_blocking = lambda text, **_kw: processed.append(str(text))
    obj._ensure_say_worker()
    obj._say_now("b1", log=True, origin="boca")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not processed:
        time.sleep(0.01)
    assert processed == ["b1"]
