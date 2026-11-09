"""Voz que sumiu do Fish Audio: o Gino fala pela persona (por procuração).

Ordem do dono (PLANO-2026-11-07, respondida em 09/11/2026): "quando a voz de
uma persona falha no meio da fala, o Gino fala por ela com uma frase do tipo
'o fulano está sem voz, me pediu para lhe dizer'". Nada de exceção nova na
cara do dono: o 404 do Fish Audio vira fala, não traceback.

Nenhum teste deste arquivo sai para a rede — o cliente do Fish Audio é o
``_FakeFish`` abaixo, e o ``miniaudio`` é substituído por um decodificador de
mentira. É o que a regra pedia: sem cliente de voz falso a suíte inteira
discaria a API com a chave do dono.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import brain, personas

#: A voz configurada do orquestrador (nunca a da persona).
GINO = "a" * 32
BERNARDO = "b" * 32
ARIEL = "c" * 32

AUDIO = b"\x01\x00\x02\x00"


class _Resp:
    def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeFish:
    """O cliente de voz dos testes: decide pela ``reference_id`` pedida.

    ``missing``: vozes que o serviço não conhece (404 em todo pedido).
    ``missing_after``: vozes que somem no MEIO da fala — o primeiro pedido da
    vez sai, o segundo já é 404 (é assim que a voz morre no meio da frase).
    ``no_credits``: 402, o caminho do crédito esgotado.
    """

    def __init__(self, missing=(), missing_after=(), no_credits=()):
        self.missing = set(missing)
        self.missing_after = set(missing_after)
        self.no_credits = set(no_credits)
        self.seen: list[tuple[str, str]] = []
        self._count: dict[str, int] = {}

    def post(self, url, headers=None, json=None, data=None, timeout=None):
        body = json if isinstance(json, dict) else {}
        ref = str(body.get("reference_id") or "")
        text = str(body.get("text") or "")
        self.seen.append((ref, text))
        index = self._count.get(ref, 0)
        self._count[ref] = index + 1
        if ref in self.missing or (ref in self.missing_after and index >= 1):
            return _Resp(404, text="model not found")
        if ref in self.no_credits:
            return _Resp(402, text="insufficient credits")
        return _Resp(200, content=AUDIO)


class _Dec:
    samples = AUDIO


@pytest.fixture
def fish(monkeypatch, tmp_path):
    """Instala o cliente falso, a config do Gino e um store de personas vazio."""
    def install(client: _FakeFish, **records):
        monkeypatch.setattr(brain.requests, "post", client.post)
        # O decode real precisa de WAV de verdade; aqui o PCM já vem pronto.
        import miniaudio
        monkeypatch.setattr(miniaudio, "decode", lambda *a, **k: _Dec())
        monkeypatch.setattr("memory.config_manager.get_plugin_config",
                            lambda *a, **k: {"fish_voice_id": GINO,
                                             "fish_model": "s2.1-pro-free"})
        store = tmp_path / "personas.json"
        store.write_text(json.dumps({"version": 1, "personas": records,
                                     "retired": {}}), encoding="utf-8")
        monkeypatch.setattr(personas, "PERSONAS_PATH", store)
        # qwen3 tenta carregar torch: fora da cadeia dos testes.
        monkeypatch.setattr(brain, "tts_qwen3_pcm24k",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("sem modelo local no teste")))
        return client

    return install


@pytest.fixture
def st():
    return {"tts_engine": "fish", "fish_api_key": "chave-de-teste",
            "fish_voice_id": GINO, "fish_model": "s2.1-pro-free"}


def bernardo(**over):
    record = {"id": "bernardo", "name": "Bernardo", "gender": "m",
              "voice_id": BERNARDO}
    record.update(over)
    return record


# ── o buraco 1: 404 no meio da frase ────────────────────────────────────────

def test_404_no_meio_da_frase_o_gino_fala_por_procuracao(fish, st, monkeypatch):
    """A segunda peça morre no 404: a linha inteira sai pela voz do Gino."""
    client = fish(_FakeFish(missing_after={BERNARDO}),
                  bernardo=bernardo())
    texto = "O valor ficou em dez mil, oitocentos e noventa reais."

    def _pieces(t):
        # A frase longa vai ao Fish aos pedaços; a procuração sai inteira.
        if brain.VOICE_LOST_LINE in t:
            return [t]
        return ["O valor ficou em dez mil,", "oitocentos e noventa reais."]

    monkeypatch.setattr(brain, "_fish_pieces", _pieces)

    pcm = brain.synth_pcm24k(texto, st, voice_id=BERNARDO)

    assert pcm, "sem voz e sem procuração é linha calada"
    # A frase completa é re-dita do zero: nada sai pela metade na voz dela.
    procurada = [text for ref, text in client.seen if ref == GINO]
    assert len(procurada) == 1
    assert procurada[0].startswith(f"O Bernardo {brain.VOICE_LOST_LINE}: ")
    assert procurada[0].endswith(texto)
    # A boca da persona nunca recebe a frase de procuração.
    assert all(brain.VOICE_LOST_LINE not in text for ref, text in client.seen
               if ref == BERNARDO)


def test_quarenta_e_quatro_no_cliente_vira_excecao_nomeada(fish, st):
    """O 404 do Fish é ``FishVoiceNotFound`` com a voz e o texto a bordo."""
    fish(_FakeFish(missing={BERNARDO}))
    with pytest.raises(brain.FishVoiceNotFound) as got:
        brain.tts_fish_pcm24k("boa tarde", {"fish_api_key": "k",
                                            "fish_voice_id": BERNARDO,
                                            "fish_model": "s2.1-pro-free"})
    assert got.value.voice_id == BERNARDO
    assert got.value.text == "boa tarde"
    # Continua sendo RuntimeError: quem só sabia do RuntimeError não quebrou.
    assert isinstance(got.value, RuntimeError)


# ── a frase de procuração ───────────────────────────────────────────────────

@pytest.mark.parametrize("gender,expected", [
    ("m", "O Bernardo está sem voz, me pediu pra lhe dizer: o build passou"),
    ("f", "A Ariel está sem voz, me pediu pra lhe dizer: o build passou"),
    ("n", "Dado está sem voz, me pediu pra lhe dizer: o build passou"),
])
def test_frase_por_procuracao_respeita_o_genero_e_e_verbatim(gender, expected):
    record = {"name": {"m": "Bernardo", "f": "Ariel", "n": "Dado"}[gender],
              "gender": gender, "voice_id": "v"}
    said = brain.voice_stand_in_text("  o build passou  ", persona=record)
    assert said == expected


def test_procuracao_nunca_atribui_a_persona_o_que_ela_nao_disse():
    """O nome aparece só na cláusula do aviso, nunca como fala da persona."""
    record = bernardo(name="Bernardo")
    said = brain.voice_stand_in_text("entrego amanhã", persona=record)
    aviso, corpo = said.split(": ", 1)
    assert corpo == "entrego amanhã"          # texto dele, palavra por palavra
    assert "entrego" not in aviso             # nada posto na boca dela
    assert said.startswith("O Bernardo ")     # terceira pessoa: quem fala é o Gino
    assert brain.VOICE_LOST_LINE in said


def test_voz_sem_dona_no_cadastro_nao_inventa_pessoa(fish, st):
    """404 de uma voz que não é de ninguém: o Gino diz o texto, liso."""
    client = fish(_FakeFish(missing={ARIEL}))
    pcm = brain.synth_pcm24k("a reunião é às duas", st, voice_id=ARIEL)
    assert pcm
    assert [text for ref, text in client.seen if ref == GINO] == [
        "a reunião é às duas"]


def test_url_cadastrada_chega_na_mesma_persona(monkeypatch, tmp_path):
    """Cadastro guardou a página da voz; o Fish devolve o id puro."""
    store = tmp_path / "personas.json"
    store.write_text(json.dumps({"version": 1, "retired": {}, "personas": {
        "bernardo": bernardo(voice_id=f"https://fish.audio/m/{BERNARDO}/")}}),
        encoding="utf-8")
    monkeypatch.setattr(personas, "PERSONAS_PATH", store)

    assert personas.address(brain.persona_of_voice(BERNARDO)) == "o Bernardo"
    assert brain.voice_stand_in_text("o build passou", voice_id=BERNARDO) \
        == f"O Bernardo {brain.VOICE_LOST_LINE}: o build passou"
    # O caminho inverso também: pedem a URL, acham o id puro.
    assert brain.persona_of_voice(f"https://fish.audio/m/{BERNARDO}/")["id"] \
        == "bernardo"


# ── persona sem voice_id / sem procuração possível ─────────────────────────

def test_persona_sem_voice_id_fala_normal_e_nada_de_procuracao(fish, st,
                                                               monkeypatch):
    """Sem ``voice_id`` não há por quem falar: a cadeia segue, sem exceção."""
    client = fish(_FakeFish(missing={GINO}))
    monkeypatch.setattr(brain, "tts_pcm24k",
                        lambda text, voice: b"EDGE")
    pcm = brain.synth_pcm24k("bom dia", st)   # sem override: a voz é do Gino
    assert pcm == b"EDGE"
    assert all(brain.VOICE_LOST_LINE not in text for _ref, text in client.seen)


def test_procuracao_nao_recae_em_procuracao_sem_fim(fish, st, monkeypatch):
    """A voz do Gino também está morta: uma tentativa e a cadeia segue."""
    client = fish(_FakeFish(missing={BERNARDO, GINO}),
                  bernardo=bernardo())
    monkeypatch.setattr(brain, "tts_pcm24k", lambda text, voice: b"EDGE")
    pcm = brain.synth_pcm24k("bom dia", st, voice_id=BERNARDO)
    assert pcm == b"EDGE"                     # nada de exceção na cara do dono
    avisos = [text for _ref, text in client.seen
              if brain.VOICE_LOST_LINE in text]
    assert len(avisos) == 1


def test_credito_esgotado_na_procuracao_chega_ao_chamador(fish, st):
    """Sem crédito a procuração também para: é o chamador que troca a sessão."""
    fish(_FakeFish(missing={BERNARDO}, no_credits={GINO}),
         bernardo=bernardo())
    with pytest.raises(brain.FishCreditsExhausted):
        brain.synth_pcm24k("bom dia", st, voice_id=BERNARDO)
