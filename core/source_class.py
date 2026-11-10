"""core/source_class.py — what is the microphone hearing right now?

Numpy only (scipy, when present, just accelerates the high-pass below), no
model files, no network, no GPU. One chunk of mono PCM goes in;
one of ``silencio``, ``ruido``, ``pessoa``, ``tv`` or ``musica`` comes out,
together with a confidence and the numbers that produced it.

The features are deliberately cheap enough to run beside the capture callback:
RMS, the 300–3400 Hz voice band (and what falls outside it), spectral flatness
and entropy, zero-crossing rate, envelope variation, spectral flux/stability,
peak concentration (tonality) and autocorrelation periodicity.

    from core import source_class
    reading = source_class.classify(block, sample_rate=16000)
    reading.label, reading.confidence, reading.features

A single 10–25 ms block is too short to see syllables or pauses. For a stream,
keep one :class:`SourceClassifier` and feed every block — it classifies the
trailing 0.5 s at most every 100 ms and returns the cached reading in between.

The block that reaches this module is analysed after a 2nd-order high-pass
(:class:`HighPass`, corte em :data:`HIGHPASS_CUTOFF_HZ`, default 120 Hz) that
only exists HERE: it strips the fixed 31/120 Hz hum of the room, which used to
draw a "music" spectrum over the owner's voice. The audio sent to STT/Whisper
never goes through it — the filter lives on the classifier's own copy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SILENCE = "silencio"
NOISE = "ruido"
PERSON = "pessoa"
TV = "tv"
MUSIC = "musica"
CLASSES = (SILENCE, NOISE, PERSON, TV, MUSIC)

SAMPLE_RATES = (16000, 48000)
DEFAULT_SAMPLE_RATE = 16000
VOICE_LOW_HZ = 300.0
VOICE_HIGH_HZ = 3400.0
FRAME_SECONDS = 0.032
SILENCE_DBFS = -58.0

# ── passa-altas de análise ─────────────────────────────────────────────────
# A sala do dono tem dois picos FIXOS em 31 Hz e 120 Hz — medidos nos clipes
# do ledger de 10/11/2026, presentes tanto na sala calada quanto na fala, o
# que descarta a voz como fonte. Eles ficam abaixo da banda de voz
# (300–3400 Hz) e mesmo assim derrubam ``voice_ratio`` para baixo de 0,42 (que
# zera ``score_pessoa``) enquanto sobem ``tonal`` a ~0,72 e ``stability`` a
# ~0,91: o desenho de ``musica``. O resultado medido é a fala do dono virar
# "música" e ser descartada pelo filtro de fantasmas. Ganho não resolve:
# ``voice_ratio`` é razão de bandas, invariante ao nível.
#
# O remédio é um passa-altas de 2ª ordem (Butterworth, ~12 dB por oitava)
# aplicado SÓ ao sinal que alimenta este classificador. O áudio que vai para o
# STT/Whisper não passa por aqui: ele já transcreve bem e o corte poderia
# piorar a voz grave do dono.
#
# Frequência de corte nomeada, default 120 Hz. Pode ser mudada pela chave
# ``plugin_config.source_class.hp_cutoff_hz`` em config/api_keys.json; nenhuma
# outra chave existe, nenhuma outra muda de comportamento. 0 desliga o filtro.
HIGHPASS_CUTOFF_HZ = 120.0
#: Chão e teto do valor configurado. 0 desliga; um corte abaixo de ~60 Hz
#: deixaria o próprio pico de 120 Hz passar. Acima de 300 Hz o corte entraria
#: na banda de voz e começaria a comer a fala grave do dono.
HIGHPASS_MIN_HZ = 0.0
HIGHPASS_MAX_HZ = 300.0
#: Q de Butterworth em 2ª ordem: resposta mais plana possível sem realce.
HIGHPASS_Q = 1.0 / math.sqrt(2.0)
#: Namespace da chave de config (só ela) no plugin_config.
HIGHPASS_CONFIG_NAMESPACE = "source_class"
HIGHPASS_CONFIG_KEY = "hp_cutoff_hz"

_EPS = 1e-10

#: ``scipy.signal.lfilter``, resolvido uma vez, no primeiro filtro criado.
#: É só um acelerador: os coeficientes e o estado são os mesmos do laço
#: numpy abaixo, e sem scipy o laço numpy faz o serviço sozinho.
_LFILTER = None
_LFILTER_TRIED = False
#: Corte efetivo depois de ler o config uma vez (:func:`configured_highpass_hz`).
_HIGHPASS_HZ_CACHE: float | None = None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _ramp(value: float, low: float, high: float) -> float:
    """0 below ``low``, 1 above ``high``, linear in between."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return _clamp((value - low) / (high - low))


def _as_mono(chunk: object) -> np.ndarray:
    """Accept int16, int32, float, bytes or a 2-D block; return float32 mono."""
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        raw = np.frombuffer(chunk, dtype=np.int16)
        arr = raw.astype(np.float32) / 32768.0
    else:
        arr = np.asarray(chunk)
        if arr.dtype == np.int16:
            arr = arr.astype(np.float32) / 32768.0
        elif arr.dtype.kind in "iu":
            bits = np.iinfo(arr.dtype).max
            arr = arr.astype(np.float32) / float(bits)
        elif arr.dtype.kind == "f":
            arr = arr.astype(np.float32, copy=False)
        else:
            raise ValueError(f"tipo de amostra não suportado: {arr.dtype}")
    if arr.ndim == 2:
        arr = arr[:, 0]
    elif arr.ndim > 2:
        arr = arr.reshape(-1)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    return np.ascontiguousarray(arr, dtype=np.float32)


def clamp_highpass_hz(cutoff_hz: object) -> float:
    """Valor do passa-altas, saneado: texto/None volta ao default, fora da
    faixa é encostado no chão ou no teto. 0 desliga o filtro."""
    try:
        value = float(cutoff_hz)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return HIGHPASS_CUTOFF_HZ
    if not math.isfinite(value):
        return HIGHPASS_CUTOFF_HZ
    return max(HIGHPASS_MIN_HZ, min(HIGHPASS_MAX_HZ, value))


def highpass_hz_from_config(cfg: dict | None) -> float:
    """A única chave nova: ``hp_cutoff_hz`` de ``plugin_config.source_class``.

    Ausente, quebrada ou vazia → :data:`HIGHPASS_CUTOFF_HZ`. Nenhuma outra
    chave do config é lida nem alterada por esta mudança.
    """
    stored = cfg if isinstance(cfg, dict) else {}
    if HIGHPASS_CONFIG_KEY not in stored:
        return HIGHPASS_CUTOFF_HZ
    return clamp_highpass_hz(stored[HIGHPASS_CONFIG_KEY])


def configured_highpass_hz() -> float:
    """Corte do passa-altas com o config do dono, lido UMA vez por processo.

    O custo é um disco, por isso o resultado fica na memória: mexer na chave
    só vale no próximo reinício do assistente (que é o que o dono já sabe).
    Nunca é chamada no callback de áudio.
    """
    global _HIGHPASS_HZ_CACHE
    if _HIGHPASS_HZ_CACHE is not None:
        return _HIGHPASS_HZ_CACHE
    try:
        from memory import config_manager

        stored = config_manager.get_plugin_config(HIGHPASS_CONFIG_NAMESPACE)
    except Exception:
        stored = {}
    _HIGHPASS_HZ_CACHE = highpass_hz_from_config(
        stored if isinstance(stored, dict) else {})
    return _HIGHPASS_HZ_CACHE


def _highpass_coefficients(cutoff_hz: float,
                           sample_rate: int) -> tuple[float, ...] | None:
    """(b0, b1, b2, a1, a2) de um passa-altas de Butterworth de 2ª ordem.

    Transformada bilinear do protótipo ``H(s) = s² / (s² + s/Q + 1)`` — a
    receita clássica do biquad, sem dependência nova. ``None`` quando o corte
    está desligado ou não existe abaixo do Nyquist.
    """
    rate = float(sample_rate)
    fc = float(cutoff_hz)
    if fc <= 0.0 or rate <= 0.0 or fc >= rate / 2.0:
        return None
    w0 = 2.0 * math.pi * fc / rate
    cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
    alpha = sin_w0 / (2.0 * HIGHPASS_Q)
    a0 = 1.0 + alpha
    b0 = (1.0 + cos_w0) / 2.0 / a0
    b1 = -(1.0 + cos_w0) / a0
    b2 = b0
    a1 = -2.0 * cos_w0 / a0
    a2 = (1.0 - alpha) / a0
    return b0, b1, b2, a1, a2


def _resolve_lfilter() -> object | None:
    """``scipy.signal.lfilter`` se o scipy JÁ estiver no ambiente; senão None.

    Tentada uma única vez, na construção de um filtro — nunca no callback de
    áudio. Não é dependência nova: scipy já está em requirements.txt e é
    usado por core/brain.py.
    """
    global _LFILTER, _LFILTER_TRIED
    if not _LFILTER_TRIED:
        _LFILTER_TRIED = True
        try:
            from scipy.signal import lfilter as _lf

            _LFILTER = _lf
        except Exception:
            _LFILTER = None
    return _LFILTER


class HighPass:
    """Passa-altas IIR de 2ª ordem (Butterworth, ~12 dB/oitava) COM ESTADO.

    Um filtro recursivo só é suave se as duas amostras de histórico
    sobreviverem entre os blocos que chegam do microfone: reiniciar a cada
    bloco injeta um degrau em cada borda, e o degrau reaparecendo a cada 64 ms
    é exatamente o pico espectral que se queria apagar. Por isso o estado mora
    aqui — um objeto por sessão de captura — e :meth:`process` espera blocos
    CONSECUTIVOS da mesma stream.

    Aplicado apenas ao sinal que alimenta o classificador. O áudio do STT
    segue cru por onde sempre seguiu.
    """

    __slots__ = ("_cutoff_hz", "sample_rate", "_coeffs", "_x1", "_x2",
                 "_y1", "_y2")

    def __init__(self, cutoff_hz: float = HIGHPASS_CUTOFF_HZ,
                 sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        self.sample_rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
        # O setter calcula os coeficientes: não existe cutoff sem filtro atrás.
        self.cutoff_hz = cutoff_hz

    @property
    def cutoff_hz(self) -> float:
        return self._cutoff_hz

    @cutoff_hz.setter
    def cutoff_hz(self, value: object) -> None:
        self._cutoff_hz = clamp_highpass_hz(value)
        self._coeffs = _highpass_coefficients(self._cutoff_hz, self.sample_rate)
        if self._coeffs is not None:
            _resolve_lfilter()
        self.reset()

    @property
    def enabled(self) -> bool:
        return self._coeffs is not None

    def reset(self) -> None:
        """Desfaz o histórico (microfone trocado, stream reaberta)."""
        self._x1 = self._x2 = self._y1 = self._y2 = 0.0

    def process(self, chunk: object) -> np.ndarray:
        """Filtra um bloco contínuo e devolve float32; o estado continua."""
        x = _as_mono(chunk)
        if self._coeffs is None or x.size == 0:
            return x
        b0, b1, b2, a1, a2 = self._coeffs
        samples = x.astype(np.float64, copy=False)
        if _LFILTER is not None:
            # Estado DF1 convertido para as condições iniciais do lfilter:
            # z1 = b1·x[-1] + b2·x[-2] − a1·y[-1] − a2·y[-2], z2 = b2·x[-1]
            # − a2·y[-1]. É a mesma recursão, só resolvida em C.
            zi = np.array([b1 * self._x1 + b2 * self._x2
                           - a1 * self._y1 - a2 * self._y2,
                           b2 * self._x1 - a2 * self._y1])
            y = _LFILTER((b0, b1, b2), (1.0, a1, a2), samples, zi=zi)[0]
        else:
            y = np.empty(samples.size, dtype=np.float64)
            x1, x2, y1, y2 = self._x1, self._x2, self._y1, self._y2
            for i, value in enumerate(samples.tolist()):
                out = b0 * value + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
                y[i] = out
                x2, x1 = x1, value
                y2, y1 = y1, out
        # O estado volta das caudas reais do sinal: uma definição só, e as
        # duas rotas (scipy e laço numpy) ficam numericamente iguais.
        self._x1 = float(samples[-1])
        self._x2 = float(samples[-2]) if samples.size > 1 else self._x1
        self._y1 = float(y[-1])
        self._y2 = float(y[-2]) if y.size > 1 else self._y1
        return np.asarray(y, dtype=np.float32)

    def __repr__(self) -> str:  # diagnóstico curto para logs
        return (f"HighPass(cutoff_hz={self.cutoff_hz}, "
                f"sample_rate={self.sample_rate}, enabled={self.enabled})")


def make_highpass(cutoff_hz: float | None = None,
                  sample_rate: int = DEFAULT_SAMPLE_RATE) -> HighPass | None:
    """Um filtro pronto, ou ``None`` quando o corte está desligado.

    ``cutoff_hz=None`` pede o valor do config (default 120 Hz).
    """
    hz = configured_highpass_hz() if cutoff_hz is None else clamp_highpass_hz(
        cutoff_hz)
    if hz <= 0.0:
        return None
    return HighPass(hz, sample_rate)


def _frame_matrix(x: np.ndarray, frame: int, hop: int) -> np.ndarray:
    """View of consecutive frames; a short block is zero-padded to one frame."""
    if x.size <= frame:
        padded = np.zeros(frame, dtype=np.float32)
        padded[: x.size] = x
        return padded[None, :]
    count = 1 + (x.size - frame) // hop
    starts = np.arange(count)[:, None] * hop
    return x[starts + np.arange(frame)[None, :]]


def _autocorrelation_peak(x: np.ndarray, sample_rate: int) -> float:
    """Largest normalized autocorrelation in the 60–400 Hz pitch range."""
    usable = x[: min(x.size, sample_rate * 2)]
    if usable.size <= 64:
        return 0.0
    usable = usable - float(usable.mean())
    nfft = 1 << int(math.ceil(math.log2(max(2, usable.size * 2))))
    spectrum = np.fft.rfft(usable, nfft)
    acf = np.fft.irfft(spectrum * np.conjugate(spectrum), nfft)[: usable.size]
    if float(acf[0]) <= 0.0:
        return 0.0
    acf = acf / float(acf[0])
    low = max(1, int(sample_rate / 400.0))
    high = min(usable.size - 1, int(sample_rate / 60.0))
    if high <= low:
        return 0.0
    return _clamp(float(np.max(acf[low: high + 1])), -1.0, 1.0)


def _level(x: np.ndarray) -> dict[str, float]:
    """O NÍVEL do sinal, medido no que o microfone realmente capturou.

    Separado de propósito: o passa-altas de análise mexe na FORMA espectral,
    nunca no nível. ``rms_db`` manda no portão de silêncio e um filtro que o
    abaixe faria material baixo virar "silencio" — e silencio o GhostFilter
    aceita. É exatamente a regressão que a guarda de 04/11 mediu.
    """
    n = int(x.size)
    rms = float(np.sqrt(np.mean(x * x))) if n else 0.0
    peak = float(np.max(np.abs(x))) if n else 0.0
    return {
        "rms": rms,
        "rms_db": 20.0 * math.log10(max(rms, 1e-9)),
        "peak": peak,
        "crest": peak / (rms + _EPS),
    }


def _features(x: np.ndarray, sample_rate: int,
              level: dict[str, float] | None = None) -> dict[str, float]:
    """As features de um bloco.

    ``x`` é o sinal que desenha o espectro (filtrado, quando há passa-altas);
    ``level`` é o nível medido fora dele — ver :func:`_level`.
    """
    n = int(x.size)
    duration = n / float(sample_rate)
    power_level = level if level is not None else _level(x)
    rms = power_level["rms"]
    rms_db = power_level["rms_db"]
    peak = power_level["peak"]
    crest = power_level["crest"]

    frame = max(64, int(round(FRAME_SECONDS * sample_rate)))
    hop = max(1, frame // 2)
    frames = _frame_matrix(x, frame, hop)
    window = np.hanning(frame).astype(np.float32)
    spectra = np.fft.rfft(frames * window, axis=1)
    power = spectra.real ** 2 + spectra.imag ** 2 + _EPS
    freqs = np.fft.rfftfreq(frame, 1.0 / sample_rate)

    frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
    mean_rms = float(np.mean(frame_rms))
    env_cv = float(np.std(frame_rms) / (mean_rms + _EPS))
    env_db = 20.0 * np.log10(np.maximum(frame_rms, 1e-9))
    dyn_range = (float(np.percentile(env_db, 90) - np.percentile(env_db, 10))
                 if frame_rms.size > 2 else 0.0)
    threshold = max(float(np.max(frame_rms)) * 0.1, 1e-6)
    active_ratio = float(np.mean(frame_rms >= threshold))
    # Pauses are part of speech, not part of its spectrum: spectral shape is
    # measured only on frames that carry sound.
    active = frame_rms >= max(float(np.max(frame_rms)) * 0.08, 1e-4)
    if not bool(np.any(active)):
        active = np.ones_like(active, dtype=bool)

    total = np.sum(power[:, 1:], axis=1)
    voice = np.sum(power[:, (freqs >= VOICE_LOW_HZ) & (freqs <= VOICE_HIGH_HZ)], axis=1)
    low = np.sum(power[:, (freqs > 0) & (freqs < VOICE_LOW_HZ)], axis=1)
    high = np.sum(power[:, freqs > VOICE_HIGH_HZ], axis=1)
    band_total = total + 3.0 * _EPS
    voice_ratio = float(np.mean((voice / band_total)[active]))
    low_ratio = float(np.mean((low / band_total)[active]))
    high_ratio = float(np.mean((high / band_total)[active]))

    bins = power[:, 1:][active]
    flatness = float(np.mean(
        np.exp(np.mean(np.log(bins), axis=1)) / np.mean(bins, axis=1)))
    norm = bins / (np.sum(bins, axis=1, keepdims=True) + _EPS)
    entropy = float(np.mean(
        -np.sum(norm * np.log(norm + _EPS), axis=1) / math.log(bins.shape[1])))

    sign = np.signbit(x)
    zcr = (float(np.count_nonzero(sign[1:] != sign[:-1]) / (n - 1))
           if n > 1 else 0.0)

    magnitude = np.sqrt(power)
    magnitude /= (np.linalg.norm(magnitude, axis=1, keepdims=True) + _EPS)
    pairs = active[1:] & active[:-1]
    flux = (float(np.mean(
        1.0 - np.sum(magnitude[1:] * magnitude[:-1], axis=1)[pairs]))
        if bool(np.any(pairs)) else 0.0)
    stability = _clamp(1.0 - flux)

    top = np.sort(bins, axis=1)[:, -5:]
    tonal = float(np.mean(np.sum(top, axis=1) / (np.sum(bins, axis=1) + _EPS)))
    dominant = np.argmax(bins, axis=1)
    median_bin = float(np.median(dominant))
    peak_stability = float(np.mean(np.abs(dominant - median_bin) <= 2.0))

    syllabic = 0.0
    if frame_rms.size >= 8:
        envelope = frame_rms - mean_rms
        env_power = np.abs(np.fft.rfft(envelope)) ** 2
        env_freqs = np.fft.rfftfreq(frame_rms.size, hop / float(sample_rate))
        band = env_power[(env_freqs >= 2.0) & (env_freqs <= 8.0)]
        syllabic = float(np.sum(band) / (np.sum(env_power[1:]) + _EPS))

    harmonicity = _autocorrelation_peak(x, sample_rate)

    # A digital-silent frame has only the EPS floor in every bin, so the band
    # ratios above describe numerical noise; zero them instead of lying.
    if rms_db < -80.0:
        voice_ratio = low_ratio = high_ratio = 0.0

    return {
        "duration_s": duration,
        "rms": rms,
        "rms_db": rms_db,
        "peak": peak,
        "crest": crest,
        "voice_ratio": voice_ratio,
        "low_ratio": low_ratio,
        "high_ratio": high_ratio,
        "flatness": flatness,
        "entropy": entropy,
        "zcr": zcr,
        "env_cv": env_cv,
        "dyn_range_db": dyn_range,
        "active_ratio": active_ratio,
        "flux": flux,
        "stability": stability,
        "tonal": tonal,
        "peak_stability": peak_stability,
        "harmonicity": harmonicity,
        "syllabic": syllabic,
        "frames": float(frames.shape[0]),
        "sample_rate": float(sample_rate),
        # Qual passa-altas de análise produziu estes números (0 = sinal cru).
        "hp_cutoff_hz": 0.0,
    }


def _scores(f: dict[str, float]) -> dict[str, float]:
    """Heuristic evidence per class, each 0..1 (the winner is picked in
    ``classify``)."""
    flat = f["flatness"]
    zcr = f["zcr"]
    crest = f["crest"]
    active = f["active_ratio"]

    # Keyboard clicks/impacts are short, peaky, spectrally flat and wideband.
    impulsive = _ramp(crest, 6.0, 14.0) * _ramp(0.55 - active, 0.0, 0.4)
    noise = max(_ramp(flat, 0.40, 0.70), impulsive)
    if f["tonal"] > 0.45 or f["harmonicity"] > 0.6:
        noise *= 0.35
    if zcr > 0.18:
        noise = max(noise, _ramp(zcr, 0.18, 0.50) * _ramp(flat, 0.40, 0.60))
    if f["voice_ratio"] > 0.65 and flat < 0.35:
        noise *= 0.25                 # syllables, not clicks: energy stays in-band

    # Stable harmonics, strong peaks and little frame-to-frame change = music.
    tonal = max(_ramp(f["tonal"], 0.22, 0.55),
                _ramp(f["harmonicity"], 0.45, 0.75))
    steady = 0.35 + 0.65 * _ramp(f["stability"], 0.80, 0.96)
    dry = 0.35 + 0.65 * _ramp(0.40 - f["env_cv"], 0.0, 0.30)
    clean = _ramp(0.60 - flat, 0.0, 0.35)
    music = tonal * steady * dry * clean

    # Speech lives in 300–3400 Hz and its envelope rises and falls in syllables.
    voiced = _ramp(f["voice_ratio"], 0.42, 0.78)
    syllabic = _ramp(f["env_cv"], 0.22, 0.70)
    changing = 0.35 + 0.65 * _ramp(0.97 - f["stability"], 0.0, 0.25)
    person = voiced * syllabic * changing * (0.35 + 0.65 * _ramp(flat, 0.01, 0.12))
    if f["harmonicity"] > 0.85 and f["env_cv"] < 0.30:
        person *= 0.45                      # steady sung tone is music, not talk

    # TV = voice band plus broadband bed, compressed: little dynamic range.
    broad = max(_ramp(f["high_ratio"], 0.06, 0.40),
                _ramp(f["low_ratio"], 0.10, 0.40))
    present = max(_ramp(f["voice_ratio"], 0.15, 0.45),
                  0.7 * _ramp(f["high_ratio"], 0.30, 0.70))
    compressed = _ramp(0.80 - f["env_cv"], 0.0, 0.45)
    tv = present * compressed * (0.20 + 0.80 * broad) * (
        0.25 + 0.75 * _ramp(0.94 - f["stability"], 0.0, 0.20))
    if f["tonal"] > 0.45:
        tv *= 0.55
    if noise > 0.55:
        tv *= 0.35
    if zcr > 0.45 or flat > 0.60:
        tv *= 0.40                    # white/hiss is noise, not a program

    silence = 0.0
    if f["rms_db"] <= SILENCE_DBFS:
        silence = 1.0
    elif f["rms_db"] < -45.0:
        silence = 0.5 * _ramp(-45.0 - f["rms_db"], 0.0, 13.0)
    if f["peak"] > 0.02:
        silence *= 0.2
    if silence > 0.8:
        scale = 1.0 - 0.85 * silence
        noise *= scale
        person *= scale
        tv *= scale
        music *= scale

    return {
        SILENCE: _clamp(silence),
        NOISE: _clamp(noise),
        PERSON: _clamp(person),
        TV: _clamp(tv),
        MUSIC: _clamp(music),
    }


@dataclass(slots=True)
class SourceReading:
    """One classification. ``features`` is plain floats, safe to log as JSON."""

    label: str
    confidence: float
    features: dict[str, float] = field(default_factory=dict)

    @property
    def is_silence(self) -> bool:
        return self.label == SILENCE


def _reading(label: str, confidence: float, f: dict[str, float]) -> SourceReading:
    return SourceReading(label=label, confidence=_clamp(confidence, 0.0, 0.99),
                         features=f)


def classify(chunk: object, sample_rate: int = DEFAULT_SAMPLE_RATE, *,
             highpass: HighPass | None = None,
             level_from: object = None) -> SourceReading:
    """Classify one PCM block. ``chunk`` is mono float (-1..1) or int16.

    A block of at least ~0.25 s sees syllables and pauses; shorter blocks still
    get an answer, but ``confidence`` is lower because the envelope is blind.

    ``highpass`` é o passa-altas de ANÁLISE, aplicado aqui a uma CÓPIA, antes
    das features: o áudio que segue para o STT nunca passou por esta função.
    Sem ele (chamadas avulsas, testes, replays de disco) o bloco é lido cru,
    como sempre foi.

    ``level_from`` é para quem já filtrou o bloco antes de entregar (o
    classificador de stream, que guarda a janela limpa): o espectro vem de
    ``chunk`` e o NÍVEL vem daqui. As duas formas são excludentes.
    """
    rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
    if rate < 4000 or rate > 192000:
        raise ValueError(f"sample_rate fora da faixa: {sample_rate}")
    x = _as_mono(chunk)
    level = None
    if highpass is not None and highpass.enabled:
        level = _level(x)                  # o nível é do microfone, não do filtro
        x = highpass.process(x)
    elif level_from is not None:
        level = _level(_as_mono(level_from))
    if x.size == 0:
        f = _features(x, rate, level)
        return _reading(SILENCE, 0.99, f)

    features = _features(x, rate, level)
    if highpass is not None and highpass.enabled:
        features["hp_cutoff_hz"] = float(highpass.cutoff_hz)
    scores = _scores(features)
    label = max(CLASSES, key=lambda name: (scores[name], -CLASSES.index(name)))
    top = scores[label]
    second = max(v for k, v in scores.items() if k != label)
    confidence = 0.50 + 0.49 * top * (0.55 + 0.45 * _ramp(top - second, 0.0, 0.35))
    if features["duration_s"] < 0.25:
        confidence *= 0.75
    if top <= 0.05:
        label, confidence = NOISE, 0.33
    features.update({f"score_{name}": score for name, score in scores.items()})
    return _reading(label, confidence, features)


class SourceClassifier:
    """Rolling classifier for a live stream: feed 10–25 ms blocks, get labels.

    The trailing ``window_ms`` is kept and re-analysed at most every
    ``interval_ms``; between those moments ``push`` returns the cached reading.
    This is what keeps the audio callback cheap.

    One object is one capture session: the analysis high-pass keeps its state
    here, across the blocks, so no block edge can ring a transient back into
    the spectrum. ``highpass_hz=0`` desliga o filtro.
    """

    def __init__(self, *, window_ms: float = 500.0, interval_ms: float = 100.0,
                 sample_rate: int = DEFAULT_SAMPLE_RATE,
                 highpass_hz: float | None = None) -> None:
        self.window_ms = max(100.0, float(window_ms))
        self.interval_ms = max(20.0, float(interval_ms))
        self.sample_rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
        self.highpass_hz = (configured_highpass_hz() if highpass_hz is None
                            else clamp_highpass_hz(highpass_hz))
        self._highpass = make_highpass(self.highpass_hz, self.sample_rate)
        self._chunks: list[np.ndarray] = []    # janela rolante: já filtrada
        self._raws: list[np.ndarray] = []      # a mesma janela, crua: o nível
        self._samples = 0
        self._since_analysis = 0
        self._rate = self.sample_rate
        self._reading = _reading(SILENCE, 0.5, _features(np.zeros(0), self._rate))

    def reset(self) -> None:
        self._chunks.clear()
        self._raws.clear()
        self._samples = 0
        self._since_analysis = 0
        self._rate = self.sample_rate
        if self._highpass is not None:
            self._highpass.reset()
        self._reading = _reading(
            SILENCE, 0.5, _features(np.zeros(0), self._rate))

    @property
    def window_samples(self) -> int:
        return max(16, int(round(self._rate * self.window_ms / 1000.0)))

    def push(self, chunk: object,
             sample_rate: int | None = None) -> SourceReading:
        """Add one block; returns the current (possibly cached) reading."""
        rate = int(sample_rate or self._rate or self.sample_rate)
        if rate != self._rate:
            self._chunks.clear()
            self._raws.clear()
            self._samples = 0
            self._since_analysis = 0
            self._rate = rate
            # Filtro é da taxa de amostragem: mudou a stream, muda o filtro.
            self._highpass = make_highpass(self.highpass_hz, rate)
        x = _as_mono(chunk)
        if x.size == 0:
            return self._reading
        self._raws.append(x)
        if self._highpass is not None:
            # Filtra na ENTRADA: a janela rolante guarda o sinal já limpo, e
            # o estado do filtro segue a stream — reanálise alguma refiltra.
            x = self._highpass.process(x)
        self._chunks.append(x)
        self._samples += x.size
        self._since_analysis += x.size
        limit = self.window_samples
        while self._samples > limit and self._chunks:
            excess = self._samples - limit
            first = self._chunks[0]
            if first.size <= excess:
                self._samples -= first.size
                self._chunks.pop(0)
                self._raws.pop(0)
            else:
                self._chunks[0] = first[excess:]
                self._raws[0] = self._raws[0][excess:]
                self._samples -= excess
                break
        interval = max(16, int(self._rate * self.interval_ms / 1000.0))
        if self._since_analysis < interval:
            return self._reading
        if self._samples < max(16, int(self._rate * 0.08)):
            return self._reading
        self._since_analysis = 0
        raw = (np.concatenate(self._raws) if self._highpass is not None
               else None)
        self._reading = classify(np.concatenate(self._chunks), self._rate,
                                 level_from=raw)
        if self._highpass is not None:
            # O sinal da janela já foi filtrado na entrada; o log diz qual.
            self._reading.features["hp_cutoff_hz"] = float(self.highpass_hz)
        return self._reading
