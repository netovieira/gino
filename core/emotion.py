"""Emoção na voz — puro, sem rede, sem modelo, sem I/O.

Duas leituras independentes do mesmo turno do dono:

* **léxico pt-BR** — palavras inteiras que carregam humor;
* **prosódia** — RMS, F0 aproximado por autocorrelação, variação de F0 e
  proporção de pausas no PCM de 16 kHz que já está na mão na entrada do turno.

Nada aqui autoriza, bloqueia ou altera um fato: o resultado só vira (a) uma
linha de tom no contexto do modelo e (b) uma tag natural do Fish S2.1 na
saída. Sem sinal confiável o veredito é ``neutro`` — o caminho que sempre
existiu — e o fallback nunca levanta exceção.

Limite conhecido e deliberado: prosódia não separa "irritado" de "animado"
(ambos são ativação alta). O texto decide o rótulo fino; o áudio só REFORÇA
um humor que as palavras já apontaram — ruído de teclado/sala nunca cria
humor sozinho. Um dono calado e irritado não é adivinhado.
"""
from __future__ import annotations

import math
import re
import unicodedata

MOODS = ("irritado", "feliz", "triste", "cansado")
NEUTRAL = "neutro"

SAMPLE_RATE = 16000
_MIN_SECONDS = 0.20          # abaixo disso não há prosódia, só texto
_MAX_SECONDS = 20.0          # uma nota de voz longa não vira análise eterna
_FRAME = 1024                # 64 ms a 16 kHz: ≥ 4 períodos de 70 Hz
_HOP = 512
_MIN_F0 = 70.0
_MAX_F0 = 400.0
_VOICE_FLOOR = 0.006         # ~ -44 dBFS: abaixo é tom de sala
_F0_FRAMES = 80              # teto de frames com F0 calculado
# Fração mínima de frames com F0 plausível para a prosódia valer como
# reforço. Abaixo disso o PCM é ruído não-vocal (teclado, ventoinha) e só a
# confirmação lexical pode sustentar um humor.
_MIN_VOCAL_FRACTION = 0.10

# Palavras já normalizadas (sem acento, minúsculas). O teste é por palavra
# inteira, então "otimo" não casa dentro de "otimista".
_LEXICON = {
    "irritado": (
        "irritado", "irritada", "puto", "puta", "raiva", "odio", "odeio",
        "porra", "merda", "caralho", "droga", "pqp", "absurdo", "ridiculo",
        "ridicula", "pessimo", "horrivel", "inacreditavel", "chega", "basta",
        "nunca mais", "nao aguento", "estou bravo", "estou brava", "que raiva",
        "idiota", "burro", "burra", "inutil", "irritante", "de novo isso",
        "nao funciona", "ta errado", "esta errado", "que saco", "revoltado",
        "revoltada", "indignado", "indignada",
    ),
    "feliz": (
        "feliz", "alegre", "animado", "animada", "otimo", "excelente",
        "maravilhoso", "maravilhosa", "adorei", "amei", "perfeito", "perfeita",
        "que bom", "legal", "bacana", "uhul", "eba", "show", "gostei",
        "parabens", "obrigado", "obrigada", "valeu", "muito bom", "demais",
        "que alegria", "animacao", "empolgado", "empolgada", "incrivel",
    ),
    "triste": (
        "triste", "tristeza", "chorar", "chorando", "chateado", "chateada",
        "magoado", "magoada", "deprimido", "deprimida", "desanimado",
        "desanimada", "sozinho", "sozinha", "saudade", "que pena", "que do",
        "luto", "morreu", "perdi", "perdemos", "desisto", "sem forcas",
    ),
    "cansado": (
        "cansado", "cansada", "exausto", "exausta", "acabado", "acabada",
        "sono", "sonolento", "sonolenta", "dormir", "deitar", "preguica",
        "sem energia", "nao durmi", "noite em claro", "trabalhei o dia todo",
        "estou morto", "estou morta", "que cansaco", "cansaco",
    ),
}

_LEX_WEIGHT = 1.0
_LEX_CAP = 2.0               # três "adorei" não valem mais que dois
_NEGATION = re.compile(r"\b(?:nao|nunca|nem)\s+(?:\w+\s+){0,2}$")

# A tag é do ASSISTENTE, não do dono: um dono irritado recebe calma, não
# espelho de raiva. Fish S2.1 aceita essas marcas naturais no texto.
_TAGS = {
    "irritado": "[calm]",
    "feliz": "[excited]",
    "triste": "[soft tone]",
    "cansado": "[soft tone]",
}

_PROMPT = {
    "irritado": "O dono está irritado agora (pela fala). Responda calmo, "
                "direto e curto — sem sermão e sem repetir o pedido.",
    "feliz": "O dono está bem-humorado agora (pela fala). Responda em tom "
             "caloroso e leve, sem exagero.",
    "triste": "O dono está para baixo agora (pela fala). Responda sério e "
              "gentil, sem piada e sem drama.",
    "cansado": "O dono está cansado agora (pela fala). Responda curto e "
               "calmo, sem rodeio.",
}
_PROMPT_INSTRUCTION = (
    "Ajuste apenas o TOM da fala. Não cite este rótulo, não comente o estado "
    "do dono e não mude nenhum fato, número ou decisão."
)


# ── texto ────────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Minúsculas e sem acento, para o léxico ter uma grafia só."""
    folded = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def _has_letters(text: str) -> bool:
    """Há pelo menos uma letra? ``""``, ``"..."`` e ``"123"`` não são fala."""
    return any(ch.isalpha() for ch in str(text or ""))


def _text_scores(text: str) -> tuple[dict[str, list[str]], dict[str, float]]:
    """Hits por humor (palavra que casou) e nota limitada de cada um."""
    plain = normalize(text)
    hits: dict[str, list[str]] = {}
    scores: dict[str, float] = {}
    if not plain:
        return hits, scores
    for mood, words in _LEXICON.items():
        found = []
        for word in words:
            pattern = r"\b" + re.escape(word) + r"\b"
            for match in re.finditer(pattern, plain):
                before = plain[max(0, match.start() - 30):match.start()]
                if _NEGATION.search(before):
                    continue      # "não estou irritado" não é irritação
                found.append(word)
                break
        if found:
            hits[mood] = found
            scores[mood] = min(_LEX_CAP, len(found) * _LEX_WEIGHT)
    return hits, scores


# ── prosódia ─────────────────────────────────────────────────────────────────

def _to_float(pcm) -> "object":
    """bytes/int16/float PCM → float32 em [-1, 1]; vazio em qualquer erro."""
    import numpy as np
    if pcm is None:
        return np.zeros(0, dtype=np.float32)
    if isinstance(pcm, (bytes, bytearray, memoryview)):
        raw = bytes(pcm)
        if len(raw) % 2:
            raw = raw[:-1]
        if not raw:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    arr = np.asarray(pcm)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    if arr.dtype.kind in "iu":
        return arr.astype(np.float32) / 32768.0
    out = arr.astype(np.float32)
    if out.size and float(np.max(np.abs(out))) > 1.5:
        out = out / 32768.0
    return out


def _frame_f0(frame, sample_rate: int) -> float:
    """F0 de um frame por autocorrelação (FFT); 0 quando não é vozeado."""
    import numpy as np
    n = int(frame.size)
    if n < 64:
        return 0.0
    windowed = frame * np.hanning(n)
    spec = np.fft.rfft(windowed, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    if ac.size < 2 or ac[0] <= 0:
        return 0.0
    ac = ac / ac[0]
    lo = max(1, int(sample_rate / _MAX_F0))
    hi = min(n - 1, int(sample_rate / _MIN_F0))
    if hi <= lo:
        return 0.0
    seg = ac[lo:hi + 1]
    k = int(np.argmax(seg))
    if float(seg[k]) < 0.30:
        return 0.0
    lag = float(lo + k)
    if 0 < k < len(seg) - 1:
        a, b, c = float(seg[k - 1]), float(seg[k]), float(seg[k + 1])
        denom = a - 2.0 * b + c
        if abs(denom) > 1e-9:
            lag += 0.5 * (a - c) / denom
    if lag <= 0:
        return 0.0
    f0 = sample_rate / lag
    return f0 if _MIN_F0 <= f0 <= _MAX_F0 else 0.0


def _prosody(pcm, sample_rate: int = SAMPLE_RATE) -> dict:
    """RMS, F0 mediano, variação de F0 e pausas. ``{}`` quando não há sinal."""
    try:
        import numpy as np
        x = _to_float(pcm)
        rate = int(sample_rate) or SAMPLE_RATE
        if x.size < int(rate * _MIN_SECONDS):
            return {}
        x = x[: int(rate * _MAX_SECONDS)]
        if x.size < _FRAME:
            return {}
        frames = np.lib.stride_tricks.sliding_window_view(x, _FRAME)[::_HOP]
        if frames.shape[0] < 2:
            return {}
        rms = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1))
        loudest = float(rms.max())
        if loudest <= 0.0:
            return {}
        threshold = max(_VOICE_FLOOR, 0.12 * loudest)
        voiced = rms >= threshold
        if not bool(voiced.any()):
            return {}
        pause_ratio = float(1.0 - voiced.mean())
        all_voiced = frames[voiced]
        if all_voiced.shape[0] > _F0_FRAMES:
            # Amostra uniforme: os primeiros 80 frames de 20 s não representam
            # a nota inteira, e a fração vocal precisa da gravação toda.
            pick = np.linspace(0, all_voiced.shape[0] - 1,
                               _F0_FRAMES).astype(int)
            voiced_frames = all_voiced[pick]
        else:
            voiced_frames = all_voiced
        f0s = [f for f in (_frame_f0(fr, rate) for fr in voiced_frames) if f > 0]
        f0_med = float(np.median(f0s)) if f0s else 0.0
        f0_var = 0.0
        if len(f0s) >= 3 and f0_med > 0:
            f0_var = float(np.std(f0s)) / f0_med
        vocal_fraction = 0.0
        if voiced_frames.shape[0]:
            vocal_fraction = (len(f0s) / float(voiced_frames.shape[0])) \
                * float(voiced.mean())
        rms_db = 20.0 * math.log10(max(float(np.mean(rms[voiced])), 1e-6))
        return {
            "rms_db": round(rms_db, 1),
            "f0_hz": round(f0_med, 1),
            "f0_variation": round(f0_var, 3),
            "pause_ratio": round(pause_ratio, 3),
            "voiced_frames": int(voiced.sum()),
            "vocal_fraction": round(vocal_fraction, 3),
        }
    except Exception:
        return {}


def _prosody_scores(p: dict) -> dict[str, float]:
    """Ativação alta → irritado; energia baixa → triste/cansado.

    Deliberadamente grosseiro: o áudio separa "para cima" de "para baixo",
    o texto separa o resto. Pausa longa pesa como cansaço; pausa curta com
    voz baixa e plana pesa como tristeza.
    """
    scores = {mood: 0.0 for mood in MOODS}
    if not p:
        return scores
    loud = p["rms_db"] >= -24.0
    quiet = p["rms_db"] <= -34.0
    animated = p["f0_variation"] >= 0.10
    flat = p["f0_variation"] <= 0.06
    high = p["f0_hz"] >= 170.0
    paused = p["pause_ratio"] >= 0.35
    if loud and animated:
        scores["irritado"] += 1.5
    if loud and high:
        scores["irritado"] += 0.5
    if animated and not quiet:
        scores["feliz"] += 1.0
    if quiet and flat:
        scores["triste"] += 0.5
    if quiet and paused:
        scores["cansado"] += 1.5
    if quiet and not paused:
        scores["triste"] += 0.7
    if flat and paused:
        scores["triste"] += 0.5
    return scores


def _confirmed_dsp_scores(dsp_scores: dict[str, float],
                          text_scores: dict[str, float],
                          p: dict) -> dict[str, float]:
    """Prosódia só REFORÇA humor que as palavras já sugeriram.

    Duas travas deliberadas:

    * humor sem nenhum hit lexical recebe zero — áudio sozinho (teclado,
      ventoinha, TV) nunca inventa "irritado";
    * fração vocal baixa (poucos frames com F0 plausível) zera o reforço:
      o PCM não é fala confiável nem para confirmar o que o texto diz.
    """
    scores = {mood: 0.0 for mood in MOODS}
    if not p:
        return scores
    reliable = float(p.get("vocal_fraction") or 0.0) >= _MIN_VOCAL_FRACTION
    if not reliable:
        return scores
    for mood in MOODS:
        if text_scores.get(mood, 0.0) > 0.0:
            scores[mood] = float(dsp_scores.get(mood, 0.0))
    return scores


# ── API ──────────────────────────────────────────────────────────────────────

def detect(text: str, pcm=None, *, sample_rate: int = SAMPLE_RATE) -> dict:
    """Humor provável do dono nesta fala. Nunca levanta; cai em ``neutro``.

    Retorna ``{"mood", "confidence", "scores", "source", "text_hits",
    "prosody"}``. ``confidence`` é a nota vencedora normalizada pela soma —
    um valor baixo significa "não sei", não "neutro de verdade".
    """
    result = {
        "mood": NEUTRAL,
        "confidence": 0.0,
        "scores": {},
        "source": "neutro",
        "text_hits": {},
        "prosody": {},
    }
    try:
        # Sem uma única letra não houve fala transcrita — ``"..."``, ``"123"``
        # ou o vazio que o STT devolve sobre teclado/ruído. Sem texto o
        # resultado é neutro e confiança zero; o PCM nem é medido.
        if not _has_letters(text):
            return result
        hits, text_scores = _text_scores(text)
        p = _prosody(pcm, sample_rate)
        dsp_scores = _confirmed_dsp_scores(
            _prosody_scores(p), text_scores, p)
        scores = {mood: text_scores.get(mood, 0.0) + dsp_scores.get(mood, 0.0)
                  for mood in MOODS}
        best = max(scores.values())
        result.update({
            "scores": {m: round(v, 2) for m, v in scores.items()},
            "text_hits": {m: list(v) for m, v in hits.items()},
            "prosody": p,
        })
        if best < 1.0:
            return result
        mood = next(m for m in MOODS if scores[m] == best)
        total = sum(scores.values())
        sources = []
        if hits:
            sources.append("texto")
        if any(dsp_scores.values()):
            sources.append("dsp")
        result.update({
            "mood": mood,
            "confidence": round(best / total, 2) if total > 0 else 0.0,
            "source": "+".join(sources) or "texto",
        })
        return result
    except Exception:
        return result


def mood_of(value) -> str:
    """Aceita o dict de :func:`detect` ou o rótulo cru; devolve um humor válido."""
    if isinstance(value, dict):
        value = value.get("mood")
    mood = str(value or "").strip().lower()
    return mood if mood in MOODS else NEUTRAL


def prompt_line(value) -> str:
    """Linha de tom para o contexto do modelo; ``""`` quando é neutro."""
    mood = mood_of(value)
    if mood not in _PROMPT:
        return ""
    return ("[TOM DO DONO AGORA] " + _PROMPT[mood] + " "
            + _PROMPT_INSTRUCTION)


def tts_tag(value) -> str:
    """Tag natural do Fish para o TOM da resposta; ``""`` quando é neutro."""
    return _TAGS.get(mood_of(value), "")


def is_fresh(stamp: float, now: float, ttl: float) -> bool:
    """Uma detecção só vale para o turno que a produziu."""
    try:
        return bool(stamp) and 0.0 <= float(now) - float(stamp) <= float(ttl)
    except Exception:
        return False
