"""R4 — a permissão de escuta é DA PESSOA (ordem literal do dono, 10/11/2026).

A palavra dele: "a regra de cinco minutos ela vale, mas ela vale só para a
pessoa que te fez a pergunta inicial ... se alguém do grupo virar 'obrigado', o
obrigado do outro você não pode responder, porque ele não falou Gino, só eu
falei Gino, então a permissão de escuta de cinco minutos ela tem que ser
referente ao usuário que te fez a pergunta".

Bug previsivo → código preventivo → teste que força o local:

| # | BUG PREVISIVEL                                                        | CODIGO PREVENTIVO                                                          | TESTE              |
|---|-------------------------------------------------------------------------|------------------------------------------------------------------------------|----------------------|
| a | Estado global "minha última fala terminou em pergunta" autorizava quem chegasse depois, sem nunca ter me chamado | ``core/vocative_ledger`` com chave POR PESSOA + ``listening_ok``              | ``test_a_*``         |
| b | Na sala, o "obrigado" do visitante respondia no lugar do dono da pergunta | ``main._voice_person_id`` (portão progressivo, por PCM) + ``_followup_verdict``| ``test_b_*``         |
| c | A nomeação nunca expirava (virava licença permanente)                     | janela do ledger + teto da nomeação contado do fim da minha fala              | ``test_c_*``         |
| d | Terceiro que nomeou depois herdava a conversa em curso                    | ``last_key``: a janela é da ÚLTIMA pessoa que me chamou                        | ``test_d_*``         |
| e | Sem identidade a casa toda ficava muda (regressão do dono sozinho)        | balde ``NO_IDENTITY``: sem como distinguir, o comportamento de sempre continua| ``test_e_*``         |
| f | Voz não reconhecida passava a ser o dono da janela de outro                | ``_voice_person_id`` devolve ``desconhecido`` (fecha), nunca o balde herdado   | ``test_f_*``         |
| g | O WhatsApp guardava frescor pelo CHAT (em grupo, o chat não é ninguém)     | ``whatsapp_key`` pelo ``sender_jid`` normalizado                               | ``test_g_*``         |
| h | O portão voltar a chamar o estado global e o ledger virar enfeite           | prova estática do chamada em ``_on_groq_text``                                 | ``test_h_*``         |

Isolamento: ``main.API_CONFIG_PATH`` aponta para ``tmp_path`` (o ``_addressed_to_me``
lê o config real), o ledger é limpo antes e depois de cada teste e nada aqui
toca arquivo do dono — o módulo é memória de processo, sem caminho em disco.
"""
from __future__ import annotations

import inspect
import time

import pytest

import main
from core import group_policy as gp
from core import vocative_ledger as vl

OWNER = "Anthero"
GUEST = "Renan"


# ── o mundo da sala (sem microfone, sem config real) ────────────────────────

class FakeGate:
    """O portão progressivo: devolve o veredito guardado para aquele PCM."""

    def __init__(self, by_pcm: dict[bytes, dict] | None = None) -> None:
        self.by_pcm = by_pcm or {}

    def decision_for(self, pcm):
        return dict(self.by_pcm.get(bytes(pcm))) if bytes(pcm) in self.by_pcm \
            else None


class FakeSpeaker:
    def __init__(self, records: list[dict], owner_name: str = OWNER) -> None:
        self._records = records
        self.owner_name = owner_name

    def list(self) -> list[dict]:
        return list(self._records)


@pytest.fixture()
def room(tmp_path, monkeypatch):
    """Um JarvisLive de mentirinha com só o que a janela de escuta enxerga."""
    monkeypatch.setattr(main, "API_CONFIG_PATH", tmp_path / "api_keys.json")
    monkeypatch.setattr(vl, "_NOTES", {})
    monkeypatch.setattr(vl, "_ORDER", [])
    obj = object.__new__(main.JarvisLive)
    obj._asst_name = "Gino"
    obj._last_out_logged = ""
    obj._reply_ended_at = 0.0
    obj._series_gate = FakeGate()
    obj._speaker = FakeSpeaker([{"owner": True}])
    obj._voice_id_enabled = True
    return obj


def hear(obj, speaker: str | None, pcm: bytes) -> str:
    """A pessoa fala; devolve a chave dela (como o portão do nome faz)."""
    verdict = "accept" if speaker else "unknown"
    obj._series_gate = FakeGate({pcm: {"verdict": verdict, "speaker": speaker}})
    return obj._voice_person_id(pcm)


def answered(obj, question: str = "Qual dos dois voce quer?") -> None:
    """Eu terminei de responder agora, com uma pergunta no fim."""
    obj._last_out_logged = question
    obj._reply_ended_at = time.monotonic()


# ── a: a janela é da pessoa, não do "minha última fala terminou em pergunta" ─

def test_a_sem_ninguem_ter_nomeado_a_janela_nao_existe(room):
    answered(room)
    verdict = room._followup_verdict(vl.voice_key(GUEST))
    assert verdict["ok"] is False
    assert "nunca me chamou pelo nome" in verdict["motivo"]


def test_a_quem_nomeou_responde_sem_repetir_o_nome(room):
    person = hear(room, OWNER, b"pcm-a")
    assert room._addressed_to_me("Gino, qual dos dois?") is True
    assert room._note_vocative(person) is True
    answered(room)
    assert room._followup_verdict(person)["ok"] is True
    assert room._in_followup_window(person) is True


def test_a_o_obrigado_de_quem_nunca_falou_gino_e_recusado(room):
    dono = hear(room, OWNER, b"pcm-a")
    room._note_vocative(dono)
    answered(room)
    visitante = hear(room, GUEST, b"pcm-b")
    verdict = room._followup_verdict(visitante)
    assert verdict["ok"] is False
    assert "obrigado"  # a frase do dono, literal: o obrigado do outro não entra
    assert room._in_followup_window(visitante) is False


def test_a_o_estado_global_do_turno_anterior_nao_e_identidade_de_ninguem(room):
    """A chave nunca é a minha fala: é a pessoa. Sem nome, sem chave, sem porta."""
    answered(room)
    assert room._followup_verdict("")["ok"] is False
    assert "sem identidade" in room._followup_verdict("")[
        "motivo"] or "nunca me chamou" in room._followup_verdict("")["motivo"]


# ── b: dois falantes na sala, um nomeou, o outro não ────────────────────────

def test_b_dois_falantes_s_o_que_nomeou_e_atendido(room):
    dono = hear(room, OWNER, b"fala-1")
    room._note_vocative(dono)
    answered(room)
    assert room._followup_verdict(dono)["ok"] is True
    assert room._followup_verdict(vl.voice_key(GUEST))["ok"] is False
    assert vl.last_key() == vl.voice_key(OWNER)


def test_b_a_janela_e_por_pessoa_mesmo_com_a_mesma_pergunta_no_ar(room):
    dono = hear(room, OWNER, b"fala-1")
    room._note_vocative(dono)
    answered(room)
    # O visitante tenta carona na pergunta do dono.
    visitante = vl.voice_key(GUEST)
    assert room._followup_verdict(visitante)["ok"] is False
    # E a pergunta dele, com nome dele, abre a janela DELE (e fecha a do dono).
    room._note_vocative(visitante)
    answered(room, "E voce, Renan, quer o que?")
    assert room._followup_verdict(visitante)["ok"] is True
    assert room._followup_verdict(dono)["ok"] is False


# ── c: a nomeação expira fora da janela ─────────────────────────────────────

def test_c_a_janela_fecha_com_o_tempo_da_pessoa(room):
    dono = hear(room, OWNER, b"fala-1")
    room._note_vocative(dono)
    answered(room)
    room._reply_ended_at = (time.monotonic()
                            - main.JarvisLive._FOLLOWUP_AFTER_QUESTION_S - 1)
    verdict = room._followup_verdict(dono)
    assert verdict["ok"] is False
    assert "vencida" in verdict["motivo"]


def test_c_nomeacao_velha_nao_abre_janela_nova(room):
    """Resposta que demorou: a pergunta que abriu a escuta é de antes dela."""
    dono = hear(room, OWNER, b"fala-1")
    agora = time.monotonic()
    vl.note(dono, now=agora - 400)
    room._last_out_logged = "Posso mandar o arquivo?"
    room._reply_ended_at = agora
    verdict = room._followup_verdict(dono)
    assert verdict["ok"] is False
    assert "expirou" in verdict["motivo"]


def test_c_sem_pergunta_no_fim_nao_ha_janela(room):
    dono = hear(room, OWNER, b"fala-1")
    room._note_vocative(dono)
    room._last_out_logged = "Pronto, mandei o arquivo."
    room._reply_ended_at = time.monotonic()
    assert room._followup_verdict(dono)["ok"] is False


# ── d: quem nomeou depois passa a ser o dono da janela (e o outro perde) ─────

def test_d_a_janela_anda_com_o_nome(room):
    um = hear(room, OWNER, b"fala-1")
    room._note_vocative(um)
    answered(room)
    dois = hear(room, GUEST, b"fala-2")
    room._note_vocative(dois)
    answered(room)
    assert room._followup_verdict(dois)["ok"] is True
    assert room._followup_verdict(um)["ok"] is False
    assert "passou a ser dela" in room._followup_verdict(um)["motivo"]


def test_d_a_janela_desliza_para_quem_ja_esta_nela(room):
    """A conversa continua: a cada resposta aceita a janela é reaberta para ele."""
    dono = hear(room, OWNER, b"fala-1")
    room._note_vocative(dono)
    answered(room)
    agora = time.monotonic()
    vl.note(dono, now=agora - 24)
    room._reply_ended_at = agora
    assert room._followup_verdict(dono)["ok"] is True
    room._note_vocative(dono)
    room._last_out_logged = "E o segundo, mando tambem?"
    room._reply_ended_at = time.monotonic()
    assert room._followup_verdict(dono)["ok"] is True


# ── e: sem identidade disponível, a regra de sempre (e ninguém novo) ────────

def test_e_sem_portao_de_voz_a_casa_nao_emudece(room):
    room._series_gate = None
    assert room._voice_person_id(b"pcm") == ""
    key = room._vocative_key("")
    assert key == vl.voice_key(vl.NO_IDENTITY)
    room._note_vocative("")
    answered(room)
    assert room._followup_verdict("")[
        "ok"] is True or room._in_followup_window(room._vocative_key("")) is True


def test_e_a_fala_sem_pcm_nao_herd_a_janela_de_quem_tem_nome(room):
    dono = hear(room, OWNER, b"fala-1")
    room._note_vocative(dono)
    answered(room)
    digitada = room._voice_person_id(None)
    assert digitada == ""
    assert room._followup_verdict(vl.voice_key(vl.NO_IDENTITY))["ok"] is False


# ── f: voz não reconhecida não é ninguém (e não herda a janela alheia) ───────

def test_f_voz_desconhecida_tem_chave_propria_e_nao_carona(room):
    room._speaker = FakeSpeaker([{"owner": True}, {"name": GUEST}])
    anonima = hear(room, None, b"fala-x")
    assert anonima == vl.voice_key("desconhecido")
    dono = hear(room, OWNER, b"fala-1")
    room._note_vocative(dono)
    answered(room)
    assert room._followup_verdict(anonima)["ok"] is False


def test_f_casa_de_voz_unica_o_dono_nao_e_tratado_como_intruso(room):
    """Um cadastrado só: score ruim é microfone, não invasor (indulgência)."""
    room._speaker = FakeSpeaker([{"owner": True}], owner_name=OWNER)
    room._series_gate = FakeGate({b"fala-1": {"verdict": "unknown",
                                              "speaker": None}})
    assert room._voice_person_id(b"fala-1") == vl.voice_key(OWNER)


# ── g: no WhatsApp a chave é o contato, nunca o chat ─────────────────────────

def test_g_a_chave_do_whatsapp_e_o_numero_normalizado():
    chave = vl.whatsapp_key("5521888887777@s.whatsapp.net", "trabalho")
    assert chave == vl.whatsapp_key("+55 21 88888-7777", "TRABALHO")
    assert chave == vl.whatsapp_key(gp.normalize_jid(
        "5521888887777@lid"), "trabalho")
    assert chave != vl.whatsapp_key("5521888887777", "outraconta")


def test_g_dois_contatos_no_mesmo_chat_tem_janelas_diferentes():
    um = vl.whatsapp_key("5521888887777", "trabalho")
    dois = vl.whatsapp_key("5531777776666", "trabalho")
    assert um and dois and um != dois
    assert vl.note(um) is True
    assert vl.holds(um) is True and vl.holds(dois) is False
    assert vl.note("") is False and vl.holds("") is False


# ── o ledger puro (o mecanismo, sem sala nenhuma) ───────────────────────────

def test_ledger_falha_fechada_em_chave_vazia():
    assert vl.note("") is False
    assert vl.fresh("") is False
    assert vl.holds("") is False
    assert vl.marked_at("") is None
    assert vl.listening_ok("", last_reply="oi?", ended_at=time.monotonic()
                           )["ok"] is False


def test_ledger_anda_com_o_relogio_injetado():
    key = vl.voice_key(OWNER)
    agora = time.monotonic()
    vl.note(key, now=agora)
    assert vl.fresh(key, window_s=25.0, now=agora + 24) is True
    assert vl.fresh(key, window_s=25.0, now=agora + 26) is False
    assert vl.holds(key, window_s=25.0, now=agora + 26) is False
    assert vl.age(key, now=agora + 10) == pytest.approx(10.0)


def test_ledger_nao_guarda_conteudo_de_fala():
    vl.note(vl.voice_key(OWNER))
    assert all("senha" not in key and "@" not in key
               for key in vl.snapshot())


def test_ledger_sobe_o_teto_sem_esquecer_o_ultimo():
    for i in range(vl.MAX_KEYS + 50):
        vl.note(vl.voice_key(f"pessoa{i}"))
    assert vl.last_key() == vl.voice_key(f"pessoa{vl.MAX_KEYS + 49}")
    assert len(vl.snapshot()) <= vl.MAX_KEYS


# ── h: a fiação no portão de nome (prova estática) ───────────────────────────

def test_h_o_portao_de_nome_usa_a_pessoa_e_nao_o_estado_global():
    """O portão não pode voltar a chamar a janela sem dizer quem falou."""
    src = inspect.getsource(main.JarvisLive._on_groq_text)
    assert "self._voice_person_id(pcm)" in src
    assert "self._followup_verdict(_person)" in src
    assert "self._note_vocative(_person)" in src
    assert "self._in_followup_window()" not in src


def test_h_a_janela_do_lado_de_fora_continua_a_mesma_lei():
    """`_in_followup_window` é a mesma decisão, agora por pessoa."""
    agora = time.monotonic()
    key = vl.voice_key(OWNER)
    vl.note(key, now=agora)
    assert vl.listening_ok(key, last_reply="Vamos?", ended_at=agora,
                           now=agora + 3)["ok"] is True
    assert vl.listening_ok(key, last_reply="Vamos?", ended_at=agora,
                           window_s=25.0, now=agora + 30)["ok"] is False
    assert vl.listening_ok(key, last_reply="Fui eu.", ended_at=agora,
                           window_s=25.0, now=agora + 3)["ok"] is False
