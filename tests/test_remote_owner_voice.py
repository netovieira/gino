"""The owner's VOICE from WhatsApp is his voice; typed text from anywhere is not.

Live defect of 02/11/2026 (19:17:37): he answered "Pode" with a WhatsApp voice
note. The transcript was correct, but every gate keyed off live-microphone PCM
(``pcm is not None``), so the note was treated like typed text and the
privileged calls kept coming back 403 ("só escuta voz pelo microfone da
máquina"). The same hole swallowed his ``from_me`` voice notes recorded inside
a third party's chat: they were never transcribed at all.

These tests pin the fix at the three seams that matter:

  (a) the saved Opus is decoded to the same 16 kHz PCM the microphone gives and
      the SAME privileged voiceprint decides (positive => owner; negative =>
      nothing is authorised and an anomaly is logged);
  (b) typed "pode" — from any channel — still opens nothing;
  (c) an owner voice note inside a third-party chat is transcribed ONLY to
      hear the name trigger (SKILL do WhatsApp, regras 1/3/5, 04/11/2026):
      without the name it dies in total silence; WITH the name it is an
      ORDER answered in that same chat — but nothing there ever runs the
      privileged voiceprint, decodes PCM for the gates or records an owner
      turn. Text typed anywhere never is privileged either.

No network, no real audio, no real speaker model: decoding and speaker-id are
fakes; the gates themselves are the real code.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import main
from core import actiongate, group_policy, whatsapp_watch

OWNER = "Anthero"
OWNER_PCM = b"\x01\x00" * 8000
ACCOUNT = "trabalho"
OWNER_CHAT = "5517999990000"
THIRD_CHAT = "5511888888888"
THIRD_NAME = "Renan"


class FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)


class FakeMemory:
    def __init__(self):
        self.turns = []

    def append_turn(self, role, text):
        self.turns.append((role, text))


class FakeSpeaker:
    """Privileged index: ``identify`` mirrors the real signature (including the
    ``device`` filter) and records the PCM it was asked to embed, proving the
    decoded file is what reached the voiceprint."""

    owner_name = OWNER

    def __init__(self, name=OWNER, known=True, authorized=True, score=0.9):
        self._name = name
        self._known = known
        self._authorized = authorized
        self._score = score
        self.embedded: list[bytes] = []
        self.identify_devices: list[str | None] = []

    def embedding(self, pcm):
        self.embedded.append(pcm)
        return [1.0, 0.0]

    def identify(self, _emb, device=None):
        self.identify_devices.append(device)
        return {"name": self._name, "score": self._score,
                "sample_score": self._score,
                "effective_score": self._score,
                "known": self._known, "authorized": self._authorized,
                "verdict": "known" if self._known else "unknown"}

    def list(self):
        return [{"name": OWNER, "owner": True, "authorized": True}]


def guest_speaker() -> FakeSpeaker:
    return FakeSpeaker(name="Ana", known=True, authorized=True, score=0.93)


def _obj(speaker=None):
    obj = object.__new__(main.JarvisLive)
    obj._qoder_lock = threading.RLock()
    obj._qoder_live_enabled = True
    obj._qoder_live_ready = lambda: True
    obj._asst_name = "Gino"
    obj.ui = FakeUI()
    obj._chat_memory = FakeMemory()
    obj._session_log = []
    obj._next_turn_ref = mock.Mock()
    obj._begin_user_epoch = mock.Mock(return_value={})
    obj._memory_record_user = mock.Mock()
    obj._index_event = mock.Mock()
    obj._last_user_speech = 0.0
    obj._qoder_turn = mock.Mock()
    obj._qoder_hold = mock.Mock()
    obj._speaker = speaker if speaker is not None else FakeSpeaker()
    obj._restart = None
    obj._power = None
    obj._is_speaking = False
    obj._reply_active = False
    obj._cinema_mode_enabled = False
    obj._series_gate = None
    obj._series_decision_for = lambda pcm: None
    obj._looks_like_own_voice = lambda text: False
    obj._wa_stt_lock = threading.Lock()
    obj._transcribe_whatsapp_audio = mock.Mock(return_value="Pode")
    obj._decode_whatsapp_pcm = mock.Mock(return_value=OWNER_PCM)
    obj.said: list[str] = []
    obj._say_now = lambda text, **kw: obj.said.append(text)
    obj._log_heard = lambda text: obj.said.append(f"heard:{text}")
    obj._note_owner_speech = lambda *a, **k: None
    return obj


def _voice_item(chat=OWNER_CHAT, from_me=False, name="Anthero"):
    return {"ts": 0, "chat": chat, "name": name, "from_me": from_me,
            "group": False, "text": "",
            "media": {"kind": "mensagem de voz", "ext": ".ogg"},
            "id": f"voice-{chat}"}


def _autoreply(obj, item, direct):
    with mock.patch.object(whatsapp_watch, "is_direct", return_value=direct), \
            mock.patch("core.whatsapp.media_path",
                       return_value="/tmp/nota.ogg"):
        return obj._whatsapp_autoreply(ACCOUNT, item)


class RemoteOwnerVoiceArmsTests(unittest.TestCase):
    def setUp(self):
        actiongate.close()
        self.addCleanup(actiongate.close)

    def test_owner_voice_note_is_decoded_and_reaches_the_privileged_print(self):
        speaker = FakeSpeaker()
        obj = _obj(speaker=speaker)
        self.assertTrue(_autoreply(obj, _voice_item(), direct=True))

        # The decoded PCM (not the file, not the transcript) is what the
        # privileged embedding saw: remote voiceprint really runs.
        self.assertEqual(speaker.embedded, [OWNER_PCM])
        self.assertIn("celular", speaker.identify_devices,
                      "a nota remota deve ser comparada com o perfil do "
                      "celular, não com o do microfone do PC")
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["pcm"], OWNER_PCM)
        self.assertTrue(kwargs["owner_voice"])
        self.assertEqual(kwargs["source"], "owner")
        # O canal de entrega não é o rótulo da voz: a resposta volta pelo
        # WhatsApp mesmo com source="owner".
        self.assertEqual(kwargs["reply_channel"], "whatsapp")
        self.assertFalse(kwargs["speak"])
        self.assertEqual(kwargs["gate_text"], "Pode")

    def test_owner_voice_pode_opens_the_action_window(self):
        obj = _obj()
        main.JarvisLive._qoder_turn(
            obj, "[WHATSAPP — CHAT DIRETO DO DONO] ...",
            pcm=OWNER_PCM, owner_voice=True, gate_text="Pode",
            source="owner", reply_channel="whatsapp", speak=False,
            wa_account=ACCOUNT, wa_chat=OWNER_CHAT)
        self.assertTrue(actiongate.authorized())
        self.assertIn("Pode, autorizado.", obj.said)

    def test_channel_rule_keeps_the_owner_label_authorising(self):
        # (d) A entrega passou a olhar o CANAL; `source="owner"` continua
        # sendo a autorização dos portões privilegiados. O mesmo turno é
        # roteado ao WhatsApp e abre a janela de ação.
        obj = _obj()
        main.JarvisLive._qoder_turn(
            obj, "[WHATSAPP — CHAT DIRETO DO DONO] ...",
            pcm=OWNER_PCM, owner_voice=True, gate_text="Pode",
            source="owner", reply_channel="whatsapp", speak=False,
            wa_account=ACCOUNT, wa_chat=OWNER_CHAT)
        self.assertTrue(main.JarvisLive._answers_on_whatsapp(
            {"source": "owner", "reply_channel": "whatsapp"}))
        self.assertFalse(main.JarvisLive._answers_on_whatsapp(
            {"source": "owner", "reply_channel": "voice"}))
        self.assertTrue(actiongate.authorized())

    def test_owner_voice_direct_restart_order_needs_no_key_phrase(self):
        """Dono, 10/11/2026: o processo é poder próprio — nem ele espera frase.

        A nota dele no chat direto abre `self_restart` (sem janela) e a
        máquina continua em `confirm_owner`, que é o portão antigo.
        """
        obj = _obj()
        obj._restart_enabled = True
        obj._restart = mock.Mock()
        obj._restart.pending.return_value = None
        obj._restart.self_restart.return_value = "Restarting the assistant"
        obj._background_job_running = lambda: False
        obj._qoder_live = None
        obj.interrupt = mock.Mock()
        main.JarvisLive._owner_direct_action_gate(
            obj, "eu quero que você reinicie", OWNER_PCM)
        obj._restart.self_restart.assert_called_once()
        obj._restart.confirm_owner.assert_not_called()
        self.assertEqual(obj._restart.self_restart.call_args.kwargs["requester"],
                         "dono")

    def test_owner_voice_pc_restart_order_still_uses_the_machine_gate(self):
        obj = _obj()
        obj._restart_enabled = True
        obj._restart = mock.Mock()
        obj._restart.pending.return_value = None
        obj._restart.confirm_owner.return_value = "Restarting the computer"
        obj._background_job_running = lambda: False
        obj._qoder_live = None
        obj.interrupt = mock.Mock()
        main.JarvisLive._owner_direct_action_gate(
            obj, "reinicia o computador agora", OWNER_PCM)
        obj._restart.self_restart.assert_not_called()
        obj._restart.confirm_owner.assert_called_once()
        self.assertEqual(obj._restart.confirm_owner.call_args.kwargs["kind"],
                         "pc")

    def test_unknown_voice_note_authorises_nothing_and_is_logged(self):
        obj = _obj(speaker=guest_speaker())
        self.assertTrue(_autoreply(obj, _voice_item(), direct=True))

        _, kwargs = obj._qoder_turn.call_args
        self.assertIsNone(kwargs["pcm"])
        self.assertFalse(kwargs["owner_voice"])
        self.assertIsNone(kwargs["gate_text"])
        self.assertEqual(kwargs["source"], "whatsapp")
        self.assertFalse(actiongate.authorized())
        self.assertTrue(any("não autorizado" in line for line in obj.ui.logs))
        self.assertTrue(any("não confirmado como dono" in line
                            for line in obj.ui.logs))

    def test_no_voice_index_means_no_decode_and_the_gate_stays_closed(self):
        obj = _obj()
        obj._speaker = None
        self.assertTrue(_autoreply(obj, _voice_item(), direct=True))
        obj._decode_whatsapp_pcm.assert_not_called()
        _, kwargs = obj._qoder_turn.call_args
        self.assertIsNone(kwargs["pcm"])
        self.assertFalse(kwargs["owner_voice"])

    def test_typed_pode_on_whatsapp_still_arms_nothing(self):
        obj = _obj()
        item = {"ts": 0, "chat": OWNER_CHAT, "name": "Anthero",
                "from_me": True, "group": False, "text": "Pode",
                "media": None, "id": "typed-1"}
        self.assertTrue(_autoreply(obj, item, direct=True))
        _, kwargs = obj._qoder_turn.call_args
        self.assertIsNone(kwargs["pcm"])
        self.assertIsNone(kwargs["gate_text"])
        self.assertFalse(actiongate.authorized())
        # And the gate itself refuses the same words with no audio.
        self.assertFalse(main.JarvisLive._try_action_consent(
            obj, "Pode", None, channel="whatsapp"))
        self.assertFalse(actiongate.authorized())


class ThirdPartyEchoNameGateTests(unittest.TestCase):
    """Regra 5 do SKILL: eco no chat de terceiro é silêncio SEM o nome e
    ORDEM respondida no mesmo chat COM o nome — e nunca abre portão
    privilegiado (voiceprint/consentimento só no chat direto do dono)."""

    def setUp(self):
        actiongate.close()
        self.addCleanup(actiongate.close)

    def test_owner_voice_in_third_party_chat_is_ignored_in_silence(self):
        obj = _obj()
        item = _voice_item(chat=THIRD_CHAT, from_me=True, name=THIRD_NAME)
        # The watcher arms a reply grant for the third-party chat before the
        # handler runs; the ignored note must leave it revoked.
        actiongate.allow_whatsapp_reply(ACCOUNT, THIRD_CHAT, THIRD_NAME)
        self.assertFalse(_autoreply(obj, item, direct=False))

        obj._memory_record_user.assert_not_called()
        self.assertEqual(obj._chat_memory.turns, [],
                         "nota do dono em chat de terceiro não vira turno")
        obj._qoder_turn.assert_not_called()
        obj._qoder_hold.assert_not_called()
        self.assertEqual(obj.ui.logs, [],
                         "silêncio total: nada é narrado nem avisado")
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control",
            {"action": "send", "to": THIRD_CHAT, "account": ACCOUNT}),
            "responder ao terceiro nunca é autorizado por esta nota")

    def test_owner_typed_text_without_the_name_in_third_party_chat_is_silence(self):
        """Regra 5: eco SEM nome continua eco — silêncio, nada de turno."""
        obj = _obj()
        item = {"ts": 0, "chat": THIRD_CHAT, "name": THIRD_NAME,
                "from_me": True, "group": False,
                "text": "fala pra Carol como você se sente",
                "media": None, "id": "typed-third"}
        prev = whatsapp_watch.trigger_name()
        whatsapp_watch.set_assistant_name("Gino")
        self.addCleanup(whatsapp_watch.set_assistant_name, prev)
        actiongate.allow_whatsapp_reply(ACCOUNT, THIRD_CHAT, THIRD_NAME)
        self.assertFalse(_autoreply(obj, item, direct=False))

        obj._memory_record_user.assert_not_called()
        self.assertEqual(obj._chat_memory.turns, [],
                         "eco sem nome não vira turno")
        obj._qoder_turn.assert_not_called()
        obj._qoder_hold.assert_not_called()
        self.assertEqual(obj.ui.logs, [])
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control",
            {"action": "send", "to": THIRD_CHAT, "account": ACCOUNT}))

    def test_owner_typed_text_naming_the_assistant_is_an_order_in_that_chat(self):
        """Regra 5 (incidente 05/11/2026 22:59): eco do dono COM o nome no
        chat de um terceiro é ordem e é respondida no MESMO chat — texto
        digitado, porém, nunca abre portão privilegiado algum."""
        obj = _obj()
        item = {"ts": 0, "chat": THIRD_CHAT, "name": THIRD_NAME,
                "from_me": True, "group": False,
                "text": "Gino, fala pra Carol como você se sente",
                "media": None, "id": "typed-third-named"}
        prev = whatsapp_watch.trigger_name()
        whatsapp_watch.set_assistant_name("Gino")
        self.addCleanup(whatsapp_watch.set_assistant_name, prev)
        actiongate.allow_whatsapp_reply(ACCOUNT, THIRD_CHAT, THIRD_NAME)
        self.assertTrue(_autoreply(obj, item, direct=False))

        obj._qoder_turn.assert_called_once()
        instruction = obj._qoder_turn.call_args[0][0]
        self.assertIn("ORDEM DO DONO NO CHAT DE UM TERCEIRO", instruction)
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["wa_chat"], THIRD_CHAT)  # o MESMO chat
        self.assertFalse(kwargs["owner_voice"],
                         "texto digitado nunca é voz privilegiada")
        self.assertIsNone(kwargs["gate_text"],
                          "nada de transcript/ordem nos portões privilegiados")
        obj._memory_record_user.assert_not_called()
        obj._decode_whatsapp_pcm.assert_not_called()

    def test_unnamed_voice_echo_is_transcribed_only_for_the_name_then_silent(self):
        """A nota é lida SÓ para ouvir o gatilho; sem o nome, silêncio e
        nenhum PCM, nenhum turno, nenhuma autorização viva."""
        obj = _obj(speaker=guest_speaker())
        item = _voice_item(chat=THIRD_CHAT, from_me=True, name=THIRD_NAME)
        prev = whatsapp_watch.trigger_name()
        whatsapp_watch.set_assistant_name("Gino")
        self.addCleanup(whatsapp_watch.set_assistant_name, prev)
        actiongate.allow_whatsapp_reply(ACCOUNT, THIRD_CHAT, THIRD_NAME)
        self.assertFalse(_autoreply(obj, item, direct=False))
        obj._transcribe_whatsapp_audio.assert_called_once()
        obj._decode_whatsapp_pcm.assert_not_called()
        obj._qoder_turn.assert_not_called()
        obj._qoder_hold.assert_not_called()
        self.assertEqual(obj._chat_memory.turns, [])
        self.assertEqual(obj.ui.logs, [])
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control",
            {"action": "send", "to": THIRD_CHAT, "account": ACCOUNT}),
            "nota sem nome não deixa grant de resposta vivo")

    def test_named_voice_echo_answers_in_the_chat_without_any_privilege(self):
        """Com o nome no transcript, a nota do dono vira ordem no MESMO
        chat — mas em chat de terceiro nada decodifica PCM para os portões
        nem vira turno 'owner'."""
        obj = _obj(speaker=FakeSpeaker())
        obj._transcribe_whatsapp_audio = mock.Mock(
            return_value="Gino, responde aquela dúvida dela")
        item = _voice_item(chat=THIRD_CHAT, from_me=True, name=THIRD_NAME)
        prev = whatsapp_watch.trigger_name()
        whatsapp_watch.set_assistant_name("Gino")
        self.addCleanup(whatsapp_watch.set_assistant_name, prev)
        actiongate.allow_whatsapp_reply(ACCOUNT, THIRD_CHAT, THIRD_NAME)
        self.assertTrue(_autoreply(obj, item, direct=False))

        obj._decode_whatsapp_pcm.assert_not_called()
        obj._qoder_turn.assert_called_once()
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["wa_chat"], THIRD_CHAT)
        self.assertEqual(kwargs["media_type"], "audio",
                         "espelho: nota de voz responde em áudio")
        self.assertFalse(kwargs["owner_voice"])
        self.assertIsNone(kwargs["gate_text"])
        self.assertEqual(kwargs["source"], "whatsapp")

    def test_no_direct_chat_resolved_still_sends_nothing_anywhere(self):
        obj = _obj()
        item = _voice_item(chat=THIRD_CHAT, from_me=True, name=THIRD_NAME)
        with mock.patch.object(whatsapp_watch, "owner_direct_chat",
                               return_value=""):
            self.assertFalse(_autoreply(obj, item, direct=False))
        obj._qoder_turn.assert_not_called()
        obj._qoder_hold.assert_not_called()


class WatcherCandidateTests(unittest.TestCase):
    def setUp(self):
        whatsapp_watch.reset()
        whatsapp_watch.set_assistant_name("Gino")
        self.addCleanup(whatsapp_watch.reset)
        self.addCleanup(whatsapp_watch.set_assistant_name, "Gino")

    def _item(self, **kw):
        item = {"ts": time.time(), "chat": THIRD_CHAT, "name": THIRD_NAME,
                "from_me": True, "group": False, "text": "[mensagem de voz]",
                "media": {"kind": "mensagem de voz", "ext": ".ogg"},
                "id": "w-1"}
        item.update(kw)
        return item

    def test_from_me_audio_in_a_third_party_chat_is_a_candidate(self):
        with mock.patch.object(whatsapp_watch.whatsapp, "sent_by_me",
                               return_value=False), \
                mock.patch.object(whatsapp_watch.whatsapp, "list_accounts",
                                  return_value=[]), \
                mock.patch.object(whatsapp_watch.whatsapp, "account_number",
                                  return_value=OWNER_CHAT):
            self.assertTrue(whatsapp_watch.is_candidate(
                self._item(), ACCOUNT))

    def test_from_me_text_without_the_name_is_still_not_a_candidate(self):
        with mock.patch.object(whatsapp_watch.whatsapp, "sent_by_me",
                               return_value=False), \
                mock.patch.object(whatsapp_watch.whatsapp, "list_accounts",
                                  return_value=[]), \
                mock.patch.object(whatsapp_watch.whatsapp, "account_number",
                                  return_value=OWNER_CHAT):
            self.assertFalse(whatsapp_watch.is_candidate(
                self._item(media=None, text="oi, tudo bem?"), ACCOUNT))

    # ── ORDEM QUE MUDOU ESTE CASO (escrita aqui porque o teste afirmava o
    # contrário) ──────────────────────────────────────────────────────────────
    # Este caso se chamava `test_group_audio_is_never_a_candidate` e era a
    # regra 4 de 01/11 e 02/11/2026 ("Grupo NUNCA é lido nem respondido",
    # ``skills/whatsapp/SKILL.md:14``) aplicada ao pré-filtro do watcher.
    # O dono REVOGOU essa regra com data, na rodada de decisões do plano de
    # 07/11, no vault
    # ``Documentos/Obsidian Vault/PLANO-2026-11-07-fluxo-plano-aprovacao-tarefa-e-vault-de-obras.md``:
    #   linha 69-70: pergunta "Revogar a SUA regra vigente sobre grupos" ->
    #                "AGORA VOCE VAI PODER LER GRUPOS, MAS RESPONDER APENAS
    #                 PESSOAS AUTORIZADAS";
    #   linha 84-85: pergunta "Voz desconhecida dentro de grupo autorizado" ->
    #                "QUANDO ALGUEM QUE VOCE NAO CONHECE PERGUNTAR ALGO POR
    #                 VOZ, REGISTRE A VOZ COMO MEMORIA PARA O MESMO, E O DE
    #                 PERMISSOES DE VIZITANTE".
    # Para registrar a voz de quem perguntou POR VOZ é preciso OUVIR a nota, e
    # para ouvir é preciso que ela entre no fluxo: a nota de grupo voltou a ser
    # candidata (pré-filtro barato, sem disco nem modelo -- o veredito em
    # ``core/group_policy.group_verdict`` é quem decide, e a gravação de
    # visitante mora em ``core/group_reply._register_unknown_voice``, sempre no
    # nível VISITANTE: jamais dono). O que a linha 70 NÃO revogou é o
    # privilégio, e o teste abaixo passa a travar exatamente isso, no lugar
    # certo: ser candidato nunca é virar voz privilegiada do dono, abrir portão
    # de ações nem chamar o handler do 1:1.
    def test_group_audio_is_a_candidate_for_the_verdict(self):
        with mock.patch.object(whatsapp_watch.whatsapp, "sent_by_me",
                               return_value=False), \
                mock.patch.object(whatsapp_watch.whatsapp, "list_accounts",
                                  return_value=[]), \
                mock.patch.object(whatsapp_watch.whatsapp, "account_number",
                                  return_value=OWNER_CHAT):
            self.assertTrue(whatsapp_watch.is_candidate(
                self._item(group=True, chat="120363000000000000@g.us"),
                ACCOUNT))

    def test_group_audio_never_reaches_the_privileged_handler(self):
        """Candidato ≠ privilégio: sem o dono no grupo, silêncio absoluto.

        A nota de grupo entra na fila, mas o veredito (presença = ordem 6 do
        dono, vault ...:82 "SE EU NAO ESTIVER NO CHAT, VOCE NEM VAI VER O
        CHAT") fecha antes de qualquer I/O: nem o handler do 1:1 -- o único
        caminho que roda voiceprint privilegiado, ``_decode_whatsapp_pcm`` e o
        "pode" dos portões -- nem o respondedor de grupo são chamados, e nada
        fica autorizado.
        """
        group = "120363000000000000@g.us"
        calls: list = []
        actiongate.close()
        prev_responder = whatsapp_watch._group_responder
        try:
            with tempfile.TemporaryDirectory() as raw, \
                    mock.patch.object(group_policy, "_PATH",
                                      Path(raw) / "group_allowlist.json"), \
                    mock.patch.object(group_policy, "_presence_source",
                                      lambda _g, _a: False), \
                    mock.patch.object(group_policy, "_presence_cache", {}), \
                    mock.patch.object(whatsapp_watch.whatsapp, "sent_by_me",
                                      return_value=False), \
                    mock.patch.object(whatsapp_watch.whatsapp, "list_accounts",
                                      return_value=[]), \
                    mock.patch.object(whatsapp_watch.whatsapp, "account_number",
                                      return_value=OWNER_CHAT):
                whatsapp_watch.set_group_responder(
                    lambda account, item, verdict: (
                        calls.append(("respondedor", item)) or False))
                whatsapp_watch.start(
                    lambda account, item: calls.append(("handler", item)) or True,
                    logger=lambda _m: None, assistant_name="Gino",
                    direct_account=ACCOUNT, direct_chat=OWNER_CHAT)
                item = self._item(group=True, chat=group)
                # Entra no fluxo como candidato barato...
                self.assertTrue(whatsapp_watch.is_candidate(item, ACCOUNT))
                # ...e morre no veredito, sem nada autorizado.
                self.assertFalse(whatsapp_watch.handle(ACCOUNT, item))
        finally:
            whatsapp_watch.stop()
            whatsapp_watch.set_group_responder(prev_responder)
            actiongate.close()
        self.assertEqual(calls, [],
                         "grupo jamais chama o handler do 1:1 (voz privilegiada)"
                         " nem o respondedor sem o veredito autorizar")
        self.assertFalse(actiongate.authorized(),
                         "nota de grupo não abre portão de ações")
        self.assertFalse(actiongate.whatsapp_reply_allowed(
            "whatsapp_control",
            {"action": "send", "to": group, "account": ACCOUNT}),
            "grupo recusado não deixa grant de resposta vivo")


if __name__ == "__main__":
    unittest.main()
