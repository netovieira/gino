"""UC-3 — core.ghost_filter: testes unitários.

Cobertura:
  - repeticao: frase identica na janela -> bloqueia
  - classificacao de fonte (SourceClassifier mockado): tv/musica/ruido -> bloqueia
  - confianca baixa -> passa (nao pode calar o dono)
  - evento audio_classified emitido a cada check
  - filtro nunca levanta excecao
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.ghost_filter import GhostFilter
from core.source_class import MUSIC, NOISE, PERSON, SILENCE, TV, SourceReading


# ── fixtures ─────────────────────────────────────────────────────────────────

def _reading(label: str, confidence: float = 0.90) -> SourceReading:
    return SourceReading(label=label, confidence=confidence, features={})


def _filter(**kw) -> GhostFilter:
    events: list[dict] = []
    kw.setdefault("on_classify", events.append)
    kw.setdefault("logger", lambda m: None)
    f = GhostFilter(**kw)
    f._events = events  # type: ignore[attr-defined]
    return f


# ── repeticao ────────────────────────────────────────────────────────────────

def test_frase_identica_na_janela_e_bloqueada():
    f = _filter(repeat_min_count=2)
    r1 = f.check("Inscreva-se e ative o sininho")
    assert r1["action"] == "accept"
    r2 = f.check("Inscreva-se e ative o sininho")
    assert r2["action"] == "reject"
    assert r2["reason"] == "repeat"


def test_frases_diferentes_passam():
    f = _filter(repeat_min_count=2)
    assert f.check("abre o terminal")["action"] == "accept"
    assert f.check("que horas sao")["action"] == "accept"


def test_acentos_e_caixa_sao_normalizados_na_repeticao():
    f = _filter(repeat_min_count=2)
    f.check("Ative o Sininho")
    r = f.check("ative o sininho")
    assert r["action"] == "reject"


def test_fora_da_janela_de_repeticao_passa():
    f = _filter(repeat_min_count=2, repeat_window_s=0.5)
    f.check("Morty Morty Smash")
    time.sleep(0.6)
    r = f.check("Morty Morty Smash")
    assert r["action"] == "accept"


def test_repeticao_com_count_maior_bloqueia_terceira_aparicao():
    f = _filter(repeat_min_count=3)
    assert f.check("frase teste")["action"] == "accept"
    assert f.check("frase teste")["action"] == "accept"
    r = f.check("frase teste")
    assert r["action"] == "reject"


# ── classificacao de fonte ───────────────────────────────────────────────────

def test_tv_com_confianca_alta_e_bloqueada():
    f = _filter()
    pcm = np.zeros(8000, dtype=np.int16)
    with patch("core.ghost_filter._classify_source", return_value=_reading(TV, 0.90)):
        r = f.check("qualquer texto", pcm=pcm)
    assert r["action"] == "reject"
    assert r["reason"] == "tv"


def test_musica_com_confianca_alta_e_bloqueada():
    f = _filter()
    pcm = np.zeros(8000, dtype=np.int16)
    with patch("core.ghost_filter._classify_source", return_value=_reading(MUSIC, 0.80)):
        r = f.check("qualquer texto", pcm=pcm)
    assert r["action"] == "reject"


def test_ruido_com_confianca_alta_e_bloqueado():
    f = _filter()
    pcm = np.zeros(8000, dtype=np.int16)
    with patch("core.ghost_filter._classify_source", return_value=_reading(NOISE, 0.70)):
        r = f.check("texto fantasma", pcm=pcm)
    assert r["action"] == "reject"


def test_pessoa_nunca_e_bloqueada_pela_classificacao():
    f = _filter()
    pcm = np.zeros(8000, dtype=np.int16)
    with patch("core.ghost_filter._classify_source", return_value=_reading(PERSON, 0.95)):
        r = f.check("abre o terminal", pcm=pcm)
    assert r["action"] == "accept"


def test_confianca_abaixo_do_limiar_passa():
    """TV com confianca baixa pode ser o dono em ambiente ruidoso."""
    f = _filter()
    pcm = np.zeros(8000, dtype=np.int16)
    with patch("core.ghost_filter._classify_source",
               return_value=_reading(TV, 0.40)):
        r = f.check("abre o navegador", pcm=pcm)
    assert r["action"] == "accept"


def test_pcm_none_passa_sem_classificacao():
    """Sem PCM (texto digitado ou teste) a classificacao e ignorada."""
    f = _filter()
    r = f.check("texto digitado", pcm=None)
    assert r["action"] == "accept"


def test_source_classifier_levanta_filtro_passa():
    """Se o classificador falhar, a fala passa (seguranca do dono)."""
    f = _filter()
    pcm = np.zeros(8000, dtype=np.int16)
    with patch("core.ghost_filter._classify_source",
               side_effect=RuntimeError("modelo nao encontrado")):
        r = f.check("fala legitima", pcm=pcm)
    assert r["action"] == "accept"


# ── evento audio_classified ──────────────────────────────────────────────────

def test_evento_audio_classified_emitido_em_cada_check():
    f = _filter()
    f.check("oi")
    f.check("abre o terminal")
    assert len(f._events) == 2  # type: ignore[attr-defined]
    assert f._events[0]["action"] == "accept"  # type: ignore[attr-defined]


def test_evento_tv_inclui_label():
    f = _filter()
    pcm = np.zeros(8000, dtype=np.int16)
    with patch("core.ghost_filter._classify_source", return_value=_reading(TV, 0.90)):
        f.check("texto", pcm=pcm)
    assert f._events[-1]["label"] == TV  # type: ignore[attr-defined]


# ── texto vazio ──────────────────────────────────────────────────────────────

def test_texto_vazio_passa_sem_repeticao():
    f = _filter()
    r = f.check("")
    assert r["action"] == "accept"
    # duas vezes vazio nao deve bloquear
    r2 = f.check("")
    assert r2["action"] == "accept"


# ── thread safety ────────────────────────────────────────────────────────────

def test_concurrent_check_nao_derruba():
    import threading
    f = _filter(repeat_min_count=5)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(f.check("frase x")))
        for _ in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)
    assert len(results) == 10


# ── reset ────────────────────────────────────────────────────────────────────

def test_reset_zera_historico_de_repeticao():
    f = _filter(repeat_min_count=2)
    f.check("frase")
    f.reset()
    r = f.check("frase")
    assert r["action"] == "accept"
