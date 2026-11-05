"""Prefill do cérebro remoto: o bloco fixo que volta TODO turno.

Medido 05/11/2026: o prompt de sistema do Qoder Live (memory/chat/
qoder_live_system.txt) tinha 16.643 bytes (~4,1 mil tokens) reenviados a cada
turno, e o prefill domina o TTFT. A suíte abaixo trava o teto (~8.000 bytes
no pior caso com memória cheia) e as regras duras que continuem inline:
identidade, idioma, persona, segurança (nunca ler segredo, confirmação pela
voz do dono, boca/cérebro nunca afirma resultado sem ferramenta) e execução.

O gerador é testado, não o arquivo: editar o txt sem mexer no gerador é
reescrito por cima na próxima boot (exactamente o bug que o dono vetou).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

import main

# Um "histórico denso" bem pior que o real: se o teto segura com estes
# blocos inflados, segura com qualquer memória de disco.
WARMUP_GROSSO = ("[WARM-UP] " + "índice de níveis e últimos turnos. " * 400)
SONO_GROSSO = ("[SONO] " + "resumo compactado do dia anterior. " * 300)


class _MemoriaCheia:
    def __init__(self):
        self.kwargs: dict = {}

    def warmup_block(self, resumed=False, **kw):
        self.kwargs = dict(resumed=resumed, **kw)
        return WARMUP_GROSSO[:int(kw.get("max_chars", 7000) or 7000)]


def _jarvis(mem, restart_pending=None):
    obj = object.__new__(main.JarvisLive)
    obj._system_prompt_text = (
        "[IDENTITY]\nYour name is Gino. Always refer to yourself as Gino.\n\n"
        + main._load_system_prompt())
    obj._chat_memory = mem
    obj._qoder_resume_session = ""
    obj._restart = SimpleNamespace(pending=lambda: restart_pending)
    return obj


def _prompt(obj, sleep_block=SONO_GROSSO):
    sleep = SimpleNamespace(
        latest_context=lambda max_chars=2500: sleep_block[:max_chars])
    with mock.patch.object(main, "_sleep", sleep):
        return main.JarvisLive._qoder_system_prompt(obj)


def test_prefill_com_memoria_cheia_fica_abaixo_de_8000_bytes():
    obj = _jarvis(_MemoriaCheia())
    text = _prompt(obj)
    n = len(text.encode("utf-8"))
    assert n < 8000, f"prefill com {n} bytes"
    assert text.count("\n") + 1 < 120


def test_prefill_sem_memoria_e_core_tambem_abaixo_do_teto():
    obj = _jarvis(None)
    text = _prompt(obj, sleep_block="")
    assert len(text.encode("utf-8")) < 7200


def test_regras_duras_continuam_inline():
    obj = _jarvis(_MemoriaCheia())
    text = _prompt(obj)
    hard = [
        "never read secrets",                        # segredo nunca em voz
        "owner's own spoken confirmation",           # confirmação pela voz
        "never report a result you did not get",     # boca/cérebro: lei (a)
        "[EXECUTION]", "[PERSONA]", "[LANGUAGE]", "[IDENTITY]",
        "Brazilian Portuguese",
        "You are Gino",                              # identidade da persona
        "[ESTADO NO BOOT]",
    ]
    for token in hard:
        assert token in text, token
    # o que muda devagar virou leitura sob demanda
    assert "core/persona.md" in text
    assert "core/prompt.txt" in text
    # as notas de manutenção do prompt.txt não viajam mais a cada turno
    assert "Anything in {braces} is filled in at startup" not in text


def test_turno_de_voz_resum_warmup_e_sono():
    mem = _MemoriaCheia()
    obj = _jarvis(mem)
    _prompt(obj)
    assert mem.kwargs["max_chars"] <= 800
    assert mem.kwargs["index_chars"] <= 400
    assert mem.kwargs["recent_turns"] <= 6


def test_tier_e_idempotente():
    once = main._tier_qoder_prompt(main._load_system_prompt())
    twice = main._tier_qoder_prompt(once)
    assert once == twice


def test_bloco_de_sono_respeita_o_teto_do_turno():
    obj = _jarvis(None)
    text = _prompt(obj)
    sono = text.split("[SONO", 1)[1].split("[ESTADO NO BOOT", 1)[0]
    assert len(sono) <= 300, len(sono)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
