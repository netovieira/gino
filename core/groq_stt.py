"""
Groq listening — the microphone goes to Whisper on Groq, and only the TEXT
reaches the Gemini Live session.

Why anyone would want this: Gemini Live bills audio input by the second, and
on a quiet day most of what it hears is room tone. Whisper on Groq is free up
to a generous daily limit and transcribes Portuguese very well. With this on,
the conversation stays on Gemini (voice out, tools, memory, everything), but
"what did the user say" is answered here, for free, and sent as text.

What changes for the user: speech is cut into utterances locally (start when
the room gets louder than its own floor, end after ~0.7 s of silence), so
JARVIS answers a beat after you stop talking rather than mid-sentence, and it
cannot be interrupted by voice while it speaks — ESC or the INTERRUPT button
still work.

Settings live in config/api_keys.json under plugin_config.groq, written by the
⚙ → PLUGIN SETTINGS → GROQ form that plugins/groq_listen.py declares:
    api_key   gsk_...            (https://console.groq.com/keys)
    model     whisper-large-v3-turbo | whisper-large-v3
    language  pt | en | ... | auto
    preroll_s 1.5                seconds of audio kept before the first phoneme
    max_utterance_s 15           voiced seconds before a live segment is cut
"""
from __future__ import annotations

import collections
import io
import math
import os
import queue
import threading
import time
import wave
from typing import Callable

import numpy as np
import requests

from core import duck as _duck

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODELS = ("whisper-large-v3-turbo", "whisper-large-v3")
NAMESPACE = "groq"

# Segmenter, in 64 ms blocks at 16 kHz (CHUNK_SIZE 1024 in main.py).
_START_BLOCKS = 3        # ~190 ms above threshold opens an utterance
_END_BLOCKS = 11         # ~700 ms below closes it
# Pre-roll: audio kept from BEFORE the first phoneme. ONE place sizes the ring
# (override: plugin_config.groq.preroll_s in config/api_keys.json).
_PREROLL_S = 1.5
_MIN_PREROLL_S = 0.0     # a disabled ring still keeps _START_BLOCKS to fire
_MAX_PREROLL_S = 5.0     # a small cap keeps a typo from eating memory
_BLOCK_SECONDS = 0.064   # one microphone frame: CHUNK_SIZE 1024 at 16 kHz
_MIN_SPEECH = 5          # ~320 ms of voiced blocks, else dropped as noise
_MAX_BLOCKS = 600        # ~38 s hard cap, then sent as-is
# A live utterance used to grow to that 38 s cap whenever nobody paused — a TV
# monologue, music, a fan — and 38 s is not transcribable: Whisper gave up on
# the whole blob ("não reconheceu nada no que ouvi") and the owner's words
# inside it went with it. Past ~15 s of VOICED audio the segment is handed
# over and a new one starts on the same breath, so a monologue yields chunks
# that transcribe while a normal owner turn (< 10 s) is never chopped.
# Override: plugin_config.groq.max_utterance_s in config/api_keys.json.
_UTTERANCE_CAP_S = 15.0
_MIN_UTTERANCE_S = 5.0
_MAX_UTTERANCE_S = _MAX_BLOCKS * _BLOCK_SECONDS   # never looser than the hard cap
# Known non-owner voice (a TV the gate can already identify) is cut tighter:
# none of it deserves a long turn, and short chunks keep an owner sentence
# spoken over the TV from hiding inside one blob.
_NON_OWNER_CAP_S = 8.0
# While the assistant is speaking only echo-guard-approved blocks reach this
# segmenter, but a stray one must not grow into a minute of self-audio: cut it
# short so a barge-in phrase still works and a false start costs 2.5 s, not 38.
_SPEAK_CAP_BLOCKS = 40
# Quiet, distant microphones: Whisper sees a whisper and gives up. Level is
# raised after the utterance is cut, never before the VAD, so the segmenter
# still tracks the real room floor.
_TARGET_RMS = 0.055
_MAX_GAIN = 10.0
# Asleep, the 25 s cap is worthless: it only closes on 700 ms of silence, and a
# room with music or a fan never gives that. The block grew to the full 25 s and
# was then thrown away for being "too long for a wake call" — every single time.
# Cutting on time instead gives one chance to hear the name every ~3.8 s, which
# is just under _SLEEP_MAX_S so the block is never discarded unseen.
_SLEEP_CAP_BLOCKS = 59
_FLOOR_MIN = 0.002       # the quietest floor we believe
_THRESH_MIN = 0.006      # the quietest threshold we accept (≈ -44 dBFS)
_FLOOR_MULT = 2.2        # speech must be this many times the room floor
_LEVEL_LOG_S = 5.0       # console line with the mic level, to tune the above
_SLEEP_MAX_S = 4.0        # asleep, longer "utterances" are music/TV, not a call

# Endpointing: a pause is not necessarily the end of a turn. Smart Turn v3.2
# looks at the last 8 s and decides; if it says "incomplete", the next segment
# is merged with this one before transcription. This is what prevents
# "…mas" from becoming a whole turn while the user is still talking.
_ENDPOINT_THRESHOLD = 0.5
_ENDPOINT_MIN_S = 0.6      # very short commands close immediately
_CONTINUE_WINDOW_S = 3.0   # how long to wait for the rest of the sentence
_MAX_PENDING_S = 18.0      # never hold a fragment forever

# Whisper hallucinates on silence, and does so with a short list of
# favourites. Anything that is only one of these is dropped.
_NOISE = {
    "obrigado", "obrigado.", "obrigada", "obrigada.", "tchau", "tchau.", "legendas",
    "thank you", "thank you.", "thanks for watching", "thanks for watching.",
    "you", "bye", "bye.", "legendas pela comunidade amara.org",
    "legendas pela comunidade amara.org.", "amara.org",
}


def _boost_quiet(pcm16: bytes) -> bytes:
    """Raise a quiet, distant utterance to a level Whisper can decode.

    Applied only to the clip sent to transcription; the segmenter keeps seeing
    the real room level, so a loud room does not become a loud false positive.
    """
    x = np.frombuffer(pcm16, dtype=np.int16)
    if x.size == 0:
        return pcm16
    f = x.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(f * f)))
    if rms >= 0.03 or rms < 0.004:
        return pcm16
    gain = min(_MAX_GAIN, _TARGET_RMS / rms)
    if gain <= 1.2:
        return pcm16
    y = f * gain
    # Soft ceiling instead of hard clipping: a loud consonant on top of the
    # boost is scaled down, so the extra input gain never turns into distortion.
    peak = float(np.max(np.abs(y)))
    if peak > 0.95:
        y *= 0.95 / peak
    return (y * 32767.0).astype(np.int16).tobytes()


class _ForcedChunk(bytes):
    """A chunk closed by the time boundary instead of by a pause.

    Same bytes, one extra fact for the worker: Smart Turn must not merge it
    with the continuation — two 15 s pieces glued back together are the same
    untranscribable blob this boundary exists to prevent.
    """


def preroll_from_config(cfg: dict | None) -> float:
    """Seconds of pre-roll, clamped. One key: plugin_config.groq.preroll_s.

    A bad value (text, negative, huge) must never leave the microphone
    unbounded or crash the listener: fall back to the default.
    """
    try:
        value = float((cfg or {}).get("preroll_s", _PREROLL_S))
    except (TypeError, ValueError):
        value = _PREROLL_S
    return min(_MAX_PREROLL_S, max(_MIN_PREROLL_S, value))


def ring_blocks(preroll_s: float) -> int:
    """How many 64 ms frames the ring holds.

    The detector needs `_START_BLOCKS` voiced blocks before it fires, and those
    blocks are part of the ring; the extra room is what keeps the full
    `preroll_s` of honest pre-speech audio even after that detection lag.
    """
    frames = int(math.ceil(max(0.0, float(preroll_s)) / _BLOCK_SECONDS))
    return frames + _START_BLOCKS


def utterance_cap_from_config(cfg: dict | None) -> float:
    """Seconds of voiced audio allowed in one live segment, clamped.

    One key: plugin_config.groq.max_utterance_s. The floor keeps a typo from
    chopping a normal sentence; the ceiling keeps the old 38 s blob from
    coming back through configuration.
    """
    try:
        value = float((cfg or {}).get("max_utterance_s", _UTTERANCE_CAP_S))
    except (TypeError, ValueError):
        value = _UTTERANCE_CAP_S
    return min(_MAX_UTTERANCE_S, max(_MIN_UTTERANCE_S, value))


def settings(get_plugin_config: Callable[[str], dict]) -> dict:
    cfg = get_plugin_config(NAMESPACE)
    model = str(cfg.get("model") or MODELS[0]).strip()
    lang = str(cfg.get("language") or "pt").strip().lower()
    return {
        "api_key": str(cfg.get("api_key") or "").strip(),
        "model": model if model in MODELS else MODELS[0],
        "language": "" if lang in ("auto", "") else lang,
        "preroll_s": preroll_from_config(cfg),
        "max_utterance_s": utterance_cap_from_config(cfg),
    }


def transcribe(pcm16: bytes, api_key: str, model: str, language: str = "pt",
               sample_rate: int = 16000, timeout: float = 20.0,
               prompt: str = "") -> str:
    """One utterance of int16 mono PCM → text. Raises on HTTP/network errors.

    `prompt` is Whisper's vocabulary hint: a short text in the user's
    language that contains the words it should know how to spell — above all
    the assistant's name, which it otherwise renders as Gervis or Charles."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    data = {"model": model, "response_format": "json", "temperature": "0"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt[:200]
    r = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": ("fala.wav", buf.getvalue(), "audio/wav")},
        data=data, timeout=timeout,
    )
    if r.status_code in (400, 404) and model != MODELS[1]:
        # The turbo model can be renamed or paused; the full model is the
        # fallback Ares uses too.
        return transcribe(pcm16, api_key, MODELS[1], language, sample_rate, timeout, prompt)
    r.raise_for_status()
    return str(r.json().get("text") or "").strip()


def test_connection(values: dict):
    """⚙ TEST CONNECTION: can this key list Groq's models?"""
    key = str(values.get("api_key") or "").strip()
    if not key:
        return False, "Enter a Groq API key (gsk_...) first."
    try:
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if r.status_code == 401:
            return False, "Groq rejected the key (401)."
        r.raise_for_status()
        ids = {m.get("id") for m in r.json().get("data", [])}
        have = [m for m in MODELS if m in ids]
        if not have:
            return True, "Connected, but no Whisper model is listed for this key."
        return True, "Connected. Whisper available: " + ", ".join(have)
    except Exception as e:
        return False, f"Connection failed: {e}"


class GroqListener:
    """Feed it microphone blocks; it calls `on_text(text)` per utterance.

    `feed()` runs on the audio callback thread and only does arithmetic and a
    queue push. Transcription happens on one worker thread, in order, so two
    quick sentences never arrive swapped.
    """

    def __init__(self, api_key: str, model: str, language: str,
                 on_text: Callable[[str], None],
                 on_status: Callable[[str], None] | None = None,
                 sample_rate: int = 16000, prompt: str = "",
                 transcribe_fn: Callable[[bytes], str] | None = None,
                 engine_label: str = "Groq",
                 usage_key: str = "groq",
                 fallback_transcribe: Callable[[bytes], str] | None = None,
                 on_utterance: Callable[[str, bytes], None] | None = None,
                 precheck: Callable[[bytes], bool] | None = None,
                 preroll_s: float | None = None,
                 max_utterance_s: float | None = None) -> None:
        self._key, self._model, self._lang = api_key, model, language
        self.prompt = prompt
        # Optional local transcriber (core.local_stt): same segmentation and
        # worker thread, no network call. None keeps the Groq HTTP path.
        self._transcribe_fn = transcribe_fn
        # The rung behind a local transcriber: if faster-whisper fails (model
        # gone, a package updated, the machine loaded) the same utterance goes
        # up to Groq instead of being lost. None means "no plan B".
        self._fallback_fn = fallback_transcribe
        self._fallback_used = False
        self.label = engine_label

        self._usage_key = usage_key
        self._on_text = on_text
        # Optional richer sink: receives the text AND the int16 PCM of the same
        # utterance, so the caller can diarize/identify the voice before the
        # turn reaches the brain. When None (the default), behavior is exactly
        # on_text(text) as before — main.py and core.local_stt keep working.
        self._on_utterance = on_utterance
        # Optional veto that runs on the STT worker BEFORE any transcription.
        # It receives the exact int16 PCM of the utterance and returns False to
        # drop it silently (the "modo série" gate: a TV line must cost no paid
        # transcription and must never reach the brain).
        self._precheck = precheck
        self._on_status = on_status or (lambda m: None)
        self._sr = sample_rate
        self._q: queue.Queue = queue.Queue()
        self._floor = 0.02
        self._active = False
        self._above = 0
        self._below = 0
        self._voiced = 0
        # The pre-roll ring. It is written on EVERY block that reaches feed()
        # and discards the oldest frame by itself; the microphone callback
        # keeps feeding it while asleep (and during our own playback), so the
        # 1.5 s before the first phoneme is never lost. Bounded by construction.
        self._preroll_s = preroll_from_config({"preroll_s": preroll_s}) \
            if preroll_s is not None else _PREROLL_S
        self._ring_blocks = ring_blocks(self._preroll_s)
        self._pre: collections.deque[bytes] = collections.deque(
            maxlen=self._ring_blocks)
        self._seg: list[bytes] = []
        # Forced boundary for a live segment, in voiced 64 ms blocks. Voiced,
        # not elapsed: the pre-roll silence and the pauses inside a sentence do
        # not count against the owner, a TV that never stops talking does.
        self._utterance_s = utterance_cap_from_config(
            {"max_utterance_s": max_utterance_s}) \
            if max_utterance_s is not None else _UTTERANCE_CAP_S
        self._owner_cap_blocks = int(round(self._utterance_s / _BLOCK_SECONDS))
        self._non_owner_cap_blocks = int(round(
            min(self._utterance_s, _NON_OWNER_CAP_S) / _BLOCK_SECONDS))
        # Optional speaker awareness, set from outside ONCE (main.py): a
        # callable returning True while the voice on the microphone is the
        # owner's. Default None = owner-agnostic, only the time cap applies.
        # It runs on the audio callback thread: keep it cheap, no I/O.
        self.current_speaker_is_owner: Callable[[], bool] | None = None
        self._cap_hook_warned = False
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="groq-stt", daemon=True)
        self._worker.start()
        self.speaking = False          # True while an utterance is open
        self.assistant_speaking = False  # True while a reply is playing
        self._drop_segment = False
        # True while the assistant sleeps: utterances are still cut and
        # transcribed (locally only) so a sentence that calls the assistant by
        # name can wake it — but nothing leaves the machine, nothing reaches
        # the panel and the speakers are not ducked for room noise.
        self.sleeping = False
        self._peak = 0.0
        self._peak_t = time.monotonic()

    @property
    def local(self) -> bool:
        """True when transcription happens on this machine (core.local_stt)."""
        return self._transcribe_fn is not None

    @property
    def preroll_seconds(self) -> float:
        """Configured pre-roll, seconds before the first voiced frame."""
        return self._preroll_s

    @property
    def preroll_blocks(self) -> int:
        """Ring capacity in frames; also the hard ceiling tests assert on."""
        return self._ring_blocks

    @property
    def preroll_bytes(self) -> int:
        """Upper bound of the ring in bytes (one channel, int16)."""
        return self._ring_blocks * int(round(_BLOCK_SECONDS * self._sr)) * 2

    @property
    def utterance_seconds(self) -> float:
        """Voiced seconds allowed in one live segment before a forced cut."""
        return self._utterance_s

    def set_assistant_speaking(self, value: bool) -> None:
        """Called by the playback path: a reply is (not) coming out now."""
        value = bool(value)
        if value == self.assistant_speaking:
            # The playback loop calls this once per ~200 ms batch. Re-arming
            # the pending drop on every call wiped whatever the owner had
            # started to say during the reply: the segment could never grow
            # into an utterance, so voice barge-in was effectively deaf.
            # Only the START of a reply invalidates a half-open segment.
            return
        self.assistant_speaking = value
        if value:
            # Drop any stale segment on the next mic block instead of touching
            # the segment list from the playback thread. A reply only starts
            # after a turn, but no mixed-voice segment may survive into it.
            self._drop_segment = True

    # ── audio thread ────────────────────────────────────────────────────────

    def feed(self, block, preroll_only: bool = False) -> None:
        x = np.asarray(block, dtype=np.float32).ravel()
        if x.size == 0:
            return
        raw = np.asarray(block, dtype=np.int16).tobytes()
        # Record FIRST, before any gate below can return: the ring is the only
        # copy of the audio that precedes a detection, and it must keep running
        # while the stream is open (asleep, echo tail, our own playback).
        self._pre.append(raw)
        if preroll_only:
            # Only the ring — used while the assistant's own voice is in the
            # monitor, so the segmenter does not open on our echo, but the
            # owner's first phoneme still survives for a barge-in.
            return
        level = float(np.sqrt(np.mean(x * x))) / 32768.0

        if self._drop_segment:
            # The reply started: whatever was half-open belongs to the previous
            # turn, not to a mixed utterance with the assistant's own voice.
            self._seg, self._active, self.speaking = [], False, False
            self._above = self._below = self._voiced = 0
            self._drop_segment = False

        if not self._active:
            # Track the room while nobody speaks; follow drops quickly, rises slowly.
            if level < self._floor:
                self._floor = 0.90 * self._floor + 0.10 * level
            else:
                self._floor = 0.995 * self._floor + 0.005 * level
            self._floor = max(_FLOOR_MIN, self._floor)
        thresh = max(_THRESH_MIN, self._floor * _FLOOR_MULT)

        # A level line every few seconds on the console: the one number that
        # says whether the microphone is even reaching us, and how it compares
        # to the threshold, so sensitivity can be tuned from a log instead of
        # a guess.
        self._peak = max(self._peak, level)
        now = time.monotonic()
        if now - self._peak_t >= _LEVEL_LOG_S:
            print(f"[Groq] mic peak {self._peak:.4f}  floor {self._floor:.4f}  "
                  f"threshold {thresh:.4f}  {'SPEECH' if self._active else 'quiet'}")
            self._peak, self._peak_t = 0.0, now

        if not self._active:
            if level > thresh:
                self._above += 1
                if self._above >= _START_BLOCKS:
                    self._active = True
                    self.speaking = True
                    # The ring already holds the full pre-roll plus the blocks
                    # the detector needed to fire: hand the whole thing to the
                    # utterance, then start a fresh ring for the continuation.
                    self._seg = list(self._pre)
                    self._pre.clear()
                    self._voiced = self._above
                    self._below = 0
                    # The room includes the speakers: while a sentence is open
                    # the music drops, and comes back when it closes — without
                    # this, Whisper transcribes the lyrics along with the user.
                    # Asleep, every cough would pump the volume: don't.
                    if not self.sleeping:
                        _duck.down()
            else:
                self._above = 0
            return

        self._seg.append(raw)
        if level > thresh:
            self._voiced += 1
            self._below = 0
            # The duck opened with this utterance; a block with actual voice
            # in it is what says the owner is speaking RIGHT NOW (a float
            # assignment — never a dropout). Silence inside an open segment
            # and the assistant's own tail must not refill the beat: the
            # watchdog in core/duck.py gives the volumes back when nothing is
            # voiced, and _close() releases them the moment the sentence ends.
            _duck.heartbeat()
        else:
            self._below += 1
        if self.assistant_speaking:
            cap = _SPEAK_CAP_BLOCKS
        else:
            cap = _SLEEP_CAP_BLOCKS if self.sleeping else _MAX_BLOCKS
        if self._below >= _END_BLOCKS or len(self._seg) >= cap:
            self._close(capped=len(self._seg) >= cap)
            return
        if not self.assistant_speaking and not self.sleeping:
            # Nobody paused: hand over a transcribable chunk instead of letting
            # the utterance run to the 38 s hard cap (see _cut_segment).
            voiced_cap = self._soft_cap_blocks()
            if self._voiced >= voiced_cap:
                self._cut_segment(voiced_cap)

    def _soft_cap_blocks(self) -> int:
        """Voiced blocks this live segment may hold before a forced boundary.

        `current_speaker_is_owner` is the injected hook (main.py may set it):
        when it says the voice is NOT the owner's, the cap tightens from the
        configured `max_utterance_s` (15 s) to `_NON_OWNER_CAP_S` (8 s). The
        hook is only asked once the segment is already long enough to matter,
        so short turns never pay for it.

        A hook that raises fails OPEN: the owner/agnostic cap stays, which
        costs longer chunks at worst — never a chopped owner sentence.
        """
        if self.current_speaker_is_owner is None:
            return self._owner_cap_blocks
        if self._voiced < self._non_owner_cap_blocks:
            return self._owner_cap_blocks
        try:
            is_owner = bool(self.current_speaker_is_owner())
        except Exception as e:
            if not self._cap_hook_warned:
                self._cap_hook_warned = True
                print(f"[{self.label}] hook de locutor falhou ({e!r}) — "
                      f"valendo só o teto de tempo")
            return self._owner_cap_blocks
        return self._owner_cap_blocks if is_owner else self._non_owner_cap_blocks

    def _cut_segment(self, cap_blocks: int) -> None:
        """Soft boundary: hand the captured audio over and keep listening.

        Unlike `_close()`, the utterance stays OPEN — VAD, duck, `_below` and
        `speaking` are untouched — and the next block simply starts a fresh
        segment. That is what guarantees no frame is dropped, repeated or
        reordered at the boundary: every block fed belongs to exactly one
        emitted chunk, in the order it arrived.
        """
        seg, voiced = self._seg, self._voiced
        self._seg, self._voiced = [], 0
        print(f"[{self.label}] fala contínua de {len(seg) * _BLOCK_SECONDS:.1f}s — "
              f"cortando em trechos transcrevíveis (teto de "
              f"{cap_blocks * _BLOCK_SECONDS:.0f}s de voz)")
        self._emit(seg, voiced, forced=True)

    def _close(self, capped: bool = False) -> None:
        seg, voiced = self._seg, self._voiced
        self._seg, self._active, self.speaking = [], False, False
        self._above = self._below = self._voiced = 0
        # The closed utterance is already going to the STT: its frames must not
        # become the pre-roll of the next sentence (that would duplicate words).
        self._pre.clear()
        # Cut by time while asleep: the tail becomes the opening of the next
        # window, so "Gino" ending at the boundary is still heard once.
        if capped and self.sleeping and len(seg) > self._ring_blocks:
            self._pre.extend(seg[-self._ring_blocks:])
        # Owner's spec (31/10): the volumes come back when the SPEECH stops,
        # never waiting on the reply or on background jobs. If a reply is
        # already playing the assistant is still talking — its playback keeps
        # the duck alive (each batch re-asserts the beat) and the drain
        # release in main.py closes the exchange when the speakers go quiet.
        # The next sentence starting to play ducks again from fresh originals.
        if not self.assistant_speaking:
            _duck.up()
        if capped and not self.sleeping:
            # A long answer is transcribed, but the tail may be missing. Say so
            # on the console: the user's request was cut mid-sentence once and
            # the model answered the fragment as if it were complete.
            print(f"[{self.label}] utterance hit the {_MAX_BLOCKS}-block cap — "
                  f"transcribing the captured part only")
        self._emit(seg, voiced)

    def _emit(self, seg: list[bytes], voiced: int,
              forced: bool = False) -> None:
        """Send one finished chunk to the STT worker, or drop it as noise."""
        if voiced >= _MIN_SPEECH:
            pcm = b"".join(seg)
            secs = len(pcm) / (2 * self._sr)
            if self.sleeping and secs > _SLEEP_MAX_S:
                # Asleep, a wake call is a short sentence; eight seconds of
                # "speech" is the song or the TV, and transcribing it would burn
                # CPU to learn that nobody called her.
                print(f"[Groq] asleep — ignored a {secs:.0f}s utterance "
                      f"(wake calls are short)")
            else:
                if not self.sleeping:
                    self._on_status(f"Ouvi {secs:.1f}s — transcrevendo no {self.label}…")
                payload = _boost_quiet(pcm)
                self._q.put(_ForcedChunk(payload) if forced else payload)
        else:
            print(f"[Groq] dropped a {voiced}-block blip as noise")
            _duck.up()          # it opened an utterance (and ducked) for nothing

    def _is_prompt_echo(self, text: str) -> bool:
        """True when the transcription is really a piece of the vocabulary hint.

        Compared fragment by fragment: the hint is a paragraph, so a whole-text
        ratio would dilute a single regurgitated sentence below any threshold.

        ASLEEP THIS FILTER IS OFF, on purpose. The hint is built from the phrases
        people use to call ("<name>, tá aí?") — so an honest wake call is
        character-for-character a fragment of the prompt and was dropped as an
        echo. She could not answer to her own name. While asleep the worst case
        of letting an echo through is one unnecessary "estou aqui"; while awake
        it is a whole invented turn, which is what this filter exists to stop.
        """
        import difflib
        import re as _re
        if self.sleeping:
            return False
        hint = (self.prompt or "").lower()
        if not hint or len(text) < 8:
            return False
        norm = " ".join(text.lower().split())
        for frag in _re.split(r"[.?!]+", hint):
            frag = " ".join(frag.split())
            if len(frag) < 8:
                continue
            if difflib.SequenceMatcher(None, norm, frag).ratio() > 0.8:
                return True
        return False

    def flush(self) -> None:
        """Push-to-talk released: whatever is open is an utterance."""
        if self._active:
            self._close()

    def transcribe_pcm(self, pcm: bytes) -> str:
        """Transcribe one PCM slice without segmentation or echo filtering.

        Used by full diarization to re-transcribe each speaker segment. It runs
        on the same worker thread that called ``on_utterance``, so it never
        races the listener's own model.
        """
        try:
            if self._transcribe_fn is not None:
                return str(self._transcribe_fn(pcm) or "").strip()
            if self._key:
                return str(transcribe(pcm, self._key, self._model, self._lang,
                                      self._sr, prompt=self.prompt) or "").strip()
            if self._fallback_fn is not None:
                return str(self._fallback_fn(pcm) or "").strip()
        except Exception as e:
            print(f"[{self.label}] segment transcription failed: {e}")
        return ""

    # ── worker thread ───────────────────────────────────────────────────────

    def _endpoint_complete(self, pcm: bytes) -> bool:
        """True when the utterance is a complete turn, not a mid-sentence pause."""
        secs = len(pcm) / (2 * self._sr)
        if self.sleeping or secs < _ENDPOINT_MIN_S or secs >= _MAX_PENDING_S:
            return True
        try:
            from core import smart_turn
            if not smart_turn.available():
                return True
            result = smart_turn.predict_endpoint(pcm)
            probability = float(result.get("probability", 1.0))
            if probability < _ENDPOINT_THRESHOLD:
                print(f"[{self.label}] pausa no meio da fala "
                      f"(smart turn {probability:.2f}) — aguardando continuação")
                return False
        except Exception as e:
            # A broken endpoint model must not make the assistant deaf: fall
            # back to the fixed silence rule.
            print(f"[{self.label}] smart turn falhou "
                  f"({e.__class__.__name__}) — fechando o turno")
        return True

    def _run(self) -> None:
        pending = b""
        pending_at = 0.0
        while not self._stop.is_set():
            try:
                pcm = self._q.get(timeout=0.25)
            except queue.Empty:
                if pending and time.monotonic() - pending_at >= _CONTINUE_WINDOW_S:
                    print(f"[{self.label}] continuação não veio — transcrevendo o trecho")
                    self._process_utterance(pending)
                    pending, pending_at = b"", 0.0
                continue
            if self._stop.is_set():
                break
            if self.sleeping:
                self._process_utterance(pcm)
                continue
            if isinstance(pcm, _ForcedChunk):
                # Closed by the time boundary, not by a pause: it is already a
                # complete piece, and gluing it to the next one would rebuild
                # exactly the long blob the boundary exists to avoid.
                if pending:
                    self._process_utterance(pending)
                    pending, pending_at = b"", 0.0
                self._process_utterance(pcm)
                continue

            now = time.monotonic()
            if pending:
                age = now - pending_at
                if age <= _CONTINUE_WINDOW_S:
                    merged = (pending + (b"\x00\x00" * int(0.20 * self._sr)) + pcm)
                    pending = b""
                    if (len(merged) < int(_MAX_PENDING_S * 2 * self._sr)
                            and not self._endpoint_complete(merged)):
                        pending, pending_at = merged, now
                        continue
                    self._process_utterance(merged)
                    continue
                # The user waited too long to continue: the old fragment is a
                # real turn. Send it before evaluating the new one.
                self._process_utterance(pending)
                pending, pending_at = b"", 0.0

            if (len(pcm) < int(_MAX_PENDING_S * 2 * self._sr)
                    and not self._endpoint_complete(pcm)):
                pending, pending_at = pcm, now
                continue
            self._process_utterance(pcm)

    def _process_utterance(self, pcm: bytes) -> None:
        t0 = time.monotonic()
        # Debug aid: keep the last asleep utterance on disk so a misheard
        # wake call can be re-transcribed offline with and without hints.
        if self.sleeping and os.environ.get("JARVIS_DUMP_SLEEP_UTTERANCE"):
            try:
                import wave as _wave
                from pathlib import Path as _Path
                p = _Path(__file__).resolve().parents[1] / "logs" / "ultima-fala-dormindo.wav"
                with _wave.open(str(p), "wb") as _w:
                    _w.setnchannels(1)
                    _w.setsampwidth(2)
                    _w.setframerate(self._sr)
                    _w.writeframes(pcm)
            except Exception:
                pass
        # Awake only: asleep, the local path is the wake word's ear and must
        # keep transcribing name calls. Awake, this is the first and cheapest
        # place to drop a TV line — before Whisper and before the brain.
        if self._precheck is not None and not self.sleeping:
            try:
                keep = bool(self._precheck(pcm))
            except Exception as e:
                # A broken gate must not make the assistant deaf: fail open,
                # but say so on the console.
                print(f"[{self.label}] pré-portão falhou ({e!r}) — seguindo")
                keep = True
            if not keep:
                _duck.up()
                return
        try:
            if self._transcribe_fn is not None:
                try:
                    text = self._transcribe_fn(pcm)
                except Exception as e:
                    # Asleep, room audio neither leaves the machine nor
                    # reaches the panel: a local failure drops quietly.
                    if self.sleeping:
                        print(f"[{self.label}] asleep — local STT failed "
                              f"({e.__class__.__name__}), utterance dropped")
                        return
                    # Local model failed. Say so once, then keep listening
                    # through the network rung instead of dropping speech.
                    if self._fallback_fn is None:
                        raise
                    if not self._fallback_used:
                        self._fallback_used = True
                        self._on_status(f"{self.label} falhou ({e.__class__.__name__}) — "
                                        "transcrevendo no Groq até resolver.")
                    text = self._fallback_fn(pcm)
            else:
                text = transcribe(pcm, self._key, self._model, self._lang, self._sr,
                                  prompt=self.prompt)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            self._on_status(f"{self.label}: transcrição falhou ({code}).")
            _duck.up()
            return
        except Exception as e:
            if self.sleeping:
                print(f"[{self.label}] asleep — utterance dropped "
                      f"({e.__class__.__name__})")
                return
            self._on_status(f"{self.label}: transcrição falhou: {e.__class__.__name__}.")
            _duck.up()
            return
        clean = text.strip()
        secs = len(pcm) / (2 * self._sr)
        # A real sentence the user spoke is never a prompt echo. Whisper
        # regurgitates the hint on silence/tail blips, which are short; a
        # 3-second utterance that happens to match a hint fragment is the
        # user talking to us (that case was being thrown away).
        if secs < 1.5 and self._is_prompt_echo(clean):
            # Whisper regurgitates its conditioning prompt when the audio is
            # silence or our own reverberant tail: the "utterance" comes back
            # as a sentence of the name hint, over and over. Drop it before
            # it reaches the blocklist, the gate or the brain.
            print(f"[{self.label}] discarded a prompt echo: {clean[:60]!r}")
            _duck.up()
            return
        if len(clean) < 2 or clean.lower() in _NOISE:
            if not self.sleeping:
                self._on_status(f"{self.label} não reconheceu nada nisso.")
                _duck.up()  # no text reaches main: close the exchange here
            return
        print(f"[{self.label}] {secs:.1f}s of speech → {len(clean)} chars in "
              f"{time.monotonic() - t0:.2f}s")
        try:
            from core import usage as _usage
            _usage.note(self._usage_key, utterances=1, audio_s=secs)
        except Exception:
            pass
        # The handler runs the whole conversation (gates, brain, tools).
        # An exception there used to end this thread, and a dead consumer
        # means the assistant keeps hearing audio forever and answers
        # nothing — the microphone meters in the log stay alive, so it looks
        # like "it only listens". One bad turn must cost one turn.
        try:
            if self._on_utterance is not None:
                self._on_utterance(clean, pcm)
            else:
                self._on_text(clean)
        except Exception as e:
            print(f"[{self.label}] handler falhou ({e!r}) — seguindo vivo")
            self._on_status("Tive um erro neste turno (detalhes no terminal).")
            # The exchange died inside the handler: nobody downstream will
            # close the duck the segmenter opened. Release it here unless a
            # reply is actively playing — in that case the playback heartbeat
            # and the host speech probe keep the apps ducked until the audio
            # really ends (main._duck_check_drain / set_speaking give it back).
            if not self.assistant_speaking:
                _duck.up()

    def stop(self) -> None:
        self._stop.set()
        _duck.up()
