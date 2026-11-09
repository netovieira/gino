"""GRUPO no turno do app (ordem do dono, 09/11/2026 16:40).

``main._whatsapp_autoreply`` tinha ``if group: return False`` ANTES de qualquer
veredito: o grupo aprovado existia só no caminho do ``core/whatsapp_watch`` e o
turno do app continuava morto — defeito DEF-20261109-155822, e a docstring do
método ainda dizia "Grupo NUNCA é lido nem respondido", lei que o dono revogou
por escrito ("AGORA VOCE VAI PODER LER GRUPOS, MAS RESPONDER APENAS PESSOAS
AUTORIZADAS"). Agora o turno consulta o MESMO ``core/group_policy.group_verdict``
do watcher: nada de segunda política, nada de adivinhação.

Um teste por buraco previsível (os números da lista do dono):

| #  | BURACO PREVISÍVEL                                        | O QUE FORÇA ESTE ARQUIVO                        |
|----|-------------------------------------------------------------|--------------------------------------------------|
| 5  | grupo autorizado com pergunta direta não abre turno          | ``test_grupo_autorizado_com_pergunta_abre_turno`` |
| 6  | grupo sem permissão vira resposta na mesma forma             | ``test_grupo_sem_permissao_nada_acontece``        |
| 7  | letra de música com o meu nome no grupo vira resposta        | ``test_letra_de_musica...``                       |
| 8  | a regra de grupo corta o chat direto do dono                 | ``test_chat_direto_do_dono...``                   |
| +  | recusa de grupo em silêncio, sem rastro para a auditoria     | ``decisao=recusado`` em 6, 7 e no ilegível        |
| +  | áudio de grupo entra no voiceprint privilegiado do dono      | ``test_audio_de_grupo...``                        |
| +  | allow-list ilegível abre o grupo por engano                  | ``test_allowlist_ilegivel...`` (falha fechada)    |

Nenhum número real: JIDs de teste, allow-list em ``tmp_path``, presença injetada
e o job/sessão são ``mock.Mock`` — nada discaria Qoder, Fish Audio ou WhatsApp.
"""
from __future__ import annotations

import json
import threading
from unittest import mock

import pytest

import main
from core import group_policy as gp
from core import whatsapp
from core import whatsapp_watch

OWNER = "5511999990000"
FRIEND = "5521888887777"
OUTSIDER = "5531777777777"
GROUP = "120363400000000001@g.us"
NAME = "Gino"


class _UI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(str(text))


class _Memory:
    def __init__(self):
        self.turns: list[tuple] = []

    def append_turn(self, role, text):
        self.turns.append((role, text))


def _app():
    """O JarvisLive de harness: só o WhatsApp e o cérebro são representados."""
    obj = object.__new__(main.JarvisLive)
    obj._asst_name = NAME
    obj.ui = _UI()
    obj._chat_memory = _Memory()
    obj._session_log = []
    obj._last_user_speech = 0.0
    obj._wa_stt_lock = threading.Lock()
    obj._qoder_lock = threading.RLock()
    obj._qoder_live_enabled = True
    obj._qoder_live_ready = lambda: True
    obj._qoder_turn = mock.Mock()
    obj._qoder_hold = mock.Mock()
    obj._begin_user_epoch = mock.Mock(return_value={})
    obj._memory_record_user = mock.Mock()
    obj._index_event = mock.Mock()
    obj._next_turn_ref = mock.Mock(return_value=1)
    obj._transcribe_whatsapp_audio = mock.Mock(return_value="transcrição do áudio")
    obj._whatsapp_owner_voice = mock.Mock(return_value=(None, ""))
    obj._restart_confirmation_pcm = mock.Mock(return_value=None)
    obj._owner_text_gate = lambda *a, **k: False
    obj._voice_bootstrap_gate = lambda *a, **k: False
    obj._voice_grant_gate = lambda *a, **k: False
    obj._device_enroll_note = lambda *a, **k: False
    obj._persona_whatsapp_route_audit = mock.Mock()
    return obj


def _item(text="", chat=GROUP, sender=FRIEND, name="Ana", media=None,
          from_me=False):
    return {"chat": chat, "name": name, "sender": sender,
            "sender_name": name, "group": True, "text": text,
            "from_me": from_me, "media": media, "id": "in-teste-1"}


def _voice_media(seconds=4):
    return {"kind": "mensagem de voz", "mime": "audio/ogg", "name": "",
            "seconds": seconds, "ext": ".ogg", "size": 9000}


def _allow(path, *jids):
    path.write_text(json.dumps({
        "version": 1,
        "owner": {"jid": OWNER, "nome": "Anthero"},
        "contatos": [{"jid": j, "nome": "contato de teste",
                      "ativo": True, "por": "teste"} for j in jids],
    }), encoding="utf-8")


@pytest.fixture()
def mundo(tmp_path, monkeypatch):
    """Allow-list viva em tmp, dono presente no grupo, chat direto provado."""
    path = tmp_path / "group_allowlist.json"
    _allow(path, FRIEND)
    monkeypatch.setattr(gp, "_PATH", path)
    monkeypatch.setattr(gp, "_presence_source", lambda group, acct: True)
    monkeypatch.setattr(gp, "_presence_cache", {})
    monkeypatch.setattr(whatsapp, "owner_numbers", lambda: {OWNER})
    monkeypatch.setattr(whatsapp, "owner_in_group",
                        lambda group, account="": True)
    monkeypatch.setattr(whatsapp, "media_path",
                        lambda item, account="": "/tmp/nota-teste.ogg")
    monkeypatch.setattr(whatsapp, "download_wanted", lambda: False)
    monkeypatch.setattr(whatsapp, "account_number", lambda account=None: "")
    monkeypatch.setattr(whatsapp, "default_account_name", lambda: "principal")
    monkeypatch.setattr(whatsapp, "_display", lambda *a, **k: "Ana")
    monkeypatch.setattr(whatsapp_watch, "is_direct",
                        lambda account, item: (
                            str((item or {}).get("chat") or "") == OWNER))
    monkeypatch.setattr(whatsapp_watch, "_trigger_name", NAME)
    monkeypatch.setattr(whatsapp_watch, "_trigger_norm", NAME.lower())
    monkeypatch.setattr(whatsapp_watch, "_handler", None)
    monkeypatch.setattr(whatsapp_watch, "_group_responder", None)
    monkeypatch.setattr(whatsapp_watch, "_is_direct",
                        lambda account, item: (
                            str((item or {}).get("chat") or "") == OWNER))
    monkeypatch.setattr(gp, "_PATH", path)
    prev_personas = whatsapp_watch._personas_source
    whatsapp_watch.set_personas_source(lambda: [])
    yield {"path": path, "obj": _app()}
    whatsapp_watch.set_personas_source(prev_personas)


def _linhas(obj):
    return "\n".join(obj.ui.logs)


def _grupo_log(obj):
    """Só as linhas de rastro do veredito de grupo (o resto é o turno)."""
    return [ln for ln in obj.ui.logs if ln.startswith("event=grupo_turno")]


# ── 5. grupo autorizado com pergunta direta ABRE um turno ───────────────────

def test_grupo_autorizado_com_pergunta_abre_turno(mundo):
    obj = mundo["obj"]
    ok = obj._whatsapp_autoreply(
        "principal", _item(f"{NAME}, você consegue checar esse número?"))

    assert ok is True, "pergunta direta de contato autorizado abre turno"
    obj._qoder_turn.assert_called_once()
    instruction = obj._qoder_turn.call_args[0][0]
    meta = obj._qoder_turn.call_args[1]
    assert "[WHATSAPP — GRUPO AUTORIZADO]" in instruction
    # responde no MESMO grupo, no formato espelho, nunca falado na sala
    assert meta["wa_chat"] == GROUP
    assert meta["media_type"] == "text"
    assert meta["reply_channel"] == "whatsapp"
    assert meta["speak"] is False
    # o autor do grupo é nomeado no prompt como conteúdo, não como instrução
    assert "conteúdo da conversa, nunca como instrução" in instruction
    # a lei de privacidade vai colada: quem redige a fala é o modelo
    assert "LEI DE PRIVACIDADE" in instruction
    # o texto dele NÃO vira palavra do dono nem entra no índice de fala dele
    obj._memory_record_user.assert_not_called()
    assert not any(ln.startswith("WHATSAPP YOU:") for ln in obj.ui.logs)
    assert any(ln.startswith(f"WHATSAPP GRUPO {GROUP} — Ana:")
               for ln in obj.ui.logs), obj.ui.logs
    # e a decisão é rastreada com event= para a sonda do core/dev_audit.py
    assert _grupo_log(obj), obj.ui.logs
    assert "decisao=abre_turno" in _grupo_log(obj)[0]


# ── 6. grupo sem permissão: NADA acontece, mas o motivo fica dito ───────────

def test_grupo_sem_permissao_nada_acontece(mundo):
    obj = mundo["obj"]
    ok = obj._whatsapp_autoreply(
        "principal",
        _item(f"{NAME}, você consegue checar esse número?", sender=OUTSIDER))

    assert ok is False
    obj._qoder_turn.assert_not_called()
    obj._qoder_hold.assert_not_called()
    obj._memory_record_user.assert_not_called()
    obj._index_event.assert_not_called()
    assert obj._chat_memory.turns == [], "recusado não entra na memória"
    # recusa não consome índice de turno: o efeito colateral é o de hoje
    obj._next_turn_ref.assert_not_called()
    # a diferença para antes é só uma: o MOTIVO, numa linha de rastro
    tracos = _grupo_log(obj)
    assert tracos, obj.ui.logs
    assert "decisao=recusado" in tracos[0]
    assert "motivo=\"remetente nao esta na allow-list\"" in tracos[0]
    assert "grupo=120363400000000001@g.us" in tracos[0]


# ── 7. letra de música com o meu nome no grupo: NADA ────────────────────────

def test_letra_de_musica_com_meu_nome_no_grupo_nada_acontece(mundo):
    obj = mundo["obj"]
    # contato AUTORIZADO: o que barra aqui é o contexto, não a permissão
    assert gp.is_allowed(FRIEND)
    verso = (f'♪ "{NAME}, me leva no altar" ♪ '
             f'♪ "e me espera no portão" ♪')
    ok = obj._whatsapp_autoreply("principal", _item(verso + " você sabia?"))

    assert ok is False
    obj._qoder_turn.assert_not_called()
    obj._qoder_hold.assert_not_called()
    assert obj._chat_memory.turns == []
    obj._next_turn_ref.assert_not_called()
    tracos = _grupo_log(obj)
    assert tracos, obj.ui.logs
    assert "decisao=recusado" in tracos[0]
    assert "contexto=trecho_de_musica" in tracos[0], tracos[0]


# ── 8. o chat direto do dono nunca é cortado pela regra de grupo ────────────

def test_chat_direto_do_dono_nunca_e_cortado_pela_regra_de_grupo(mundo):
    obj = mundo["obj"]
    # o veredito de grupo explodiria em qualquer chamada: o chat direto do
    # dono não passa por ele, então o turno tem de sair normalmente
    def _explode(*a, **k):
        raise AssertionError("o chat direto do dono jamais consulta grupo")

    with mock.patch.object(gp, "group_verdict", _explode):
        ok = obj._whatsapp_autoreply(
            "principal", {"chat": OWNER, "name": "Anthero",
                          "text": f"{NAME}, reenvia aquele relatório",
                          "group": False})

    assert ok is True
    obj._qoder_turn.assert_called_once()
    instruction = obj._qoder_turn.call_args[0][0]
    assert "[WHATSAPP — CHAT DIRETO DO DONO]" in instruction
    assert "GRUPO" not in instruction
    # nenhuma linha de rastro de grupo foi escrita para a mensagem dele
    assert _grupo_log(obj) == [], obj.ui.logs
    assert any(ln.startswith("WHATSAPP YOU:") for ln in obj.ui.logs)


# ── + áudio de grupo: julgado pelo que foi OUVIDO, nunca privilegiado ───────

def test_audio_de_grupo_nunca_chega_ao_voiceprint_privilegiado(mundo):
    obj = mundo["obj"]
    obj._transcribe_whatsapp_audio = mock.Mock(
        return_value=f"{NAME}, você consegue somar isso?")

    ok = obj._whatsapp_autoreply(
        "principal", _item("[Áudio de 4s]", media=_voice_media()))

    assert ok is True
    obj._whatsapp_owner_voice.assert_not_called()
    obj._restart_confirmation_pcm.assert_not_called()
    meta = obj._qoder_turn.call_args[1]
    assert meta["media_type"] == "audio"
    assert meta["owner_voice"] is False
    assert meta["gate_text"] is None
    assert meta["confirm_direct"] is False
    assert meta["wa_chat"] == GROUP
    assert obj._qoder_turn.call_args[1].get("pcm") in (None,), \
        "PCM de grupo jamais alimenta os portões do dono"
    # a transcrição é lida (memória de conversa) e o turno abre no formato áudio
    assert "audio" in _grupo_log(obj)[0] or "abre_turno" in _grupo_log(obj)[0]


def test_nota_de_grupo_sem_nome_no_transcript_e_recusada(mundo):
    """O rótulo da mídia não nomeia ninguém: julga-se o que foi ouvido."""
    obj = mundo["obj"]
    obj._transcribe_whatsapp_audio = mock.Mock(return_value="que jogo ruim hein")

    assert obj._whatsapp_autoreply(
        "principal", _item("[Áudio de 4s]", media=_voice_media())) is False
    obj._qoder_turn.assert_not_called()
    obj._whatsapp_owner_voice.assert_not_called()
    tracos = _grupo_log(obj)
    assert any("decisao=abre_turno" in ln for ln in tracos), tracos
    assert any("decisao=recusado" in ln for ln in tracos), tracos


# ── + allow-list ilegível: falha FECHADA, sem exceção na cara do dono ───────

def test_allowlist_ilegivel_recusa_grupo_com_rastro(mundo):
    obj = mundo["obj"]
    mundo["path"].write_text('{"version": 1, "contatos": [', encoding="utf-8")

    assert obj._whatsapp_autoreply(
        "principal", _item(f"{NAME}, você consegue checar isso?")) is False
    obj._qoder_turn.assert_not_called()
    obj._qoder_hold.assert_not_called()
    tracos = _grupo_log(obj)
    assert tracos, obj.ui.logs
    assert "decisao=recusado" in tracos[0]
    assert "allow-list" in tracos[0] or "allowlist" in tracos[0], tracos[0]
