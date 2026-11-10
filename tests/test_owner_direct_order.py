"""Owner rule of 02/11/2026 (~19h): the owner's direct order authorises itself.

Said out loud twice: "quando o pedido vem direto do dono, ele já tem
confirmação automática" and "eu quero que você reinicie" — without repeating
"confirmo reiniciar". The privilege is per speaker: guests, unknown voices,
personas and the model keep the two-step flow. These tests pin both sides:

  (a) owner + confirmed voiceprint -> executes without the key phrase;
  (b) same order from a non-owner voice -> nothing runs, request rises to the
      owner (or is refused when the voice is unknown);
  (c) a TV line with "pode" still opens no consent window;
  (d) personas/tools still park their request and wait.

No real restart, power command or audio is touched: the managers are fakes at
the main layer and dry-run at the core layer.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import main
from core import actiongate
from core import restart as restart_mod
from core.power import PowerManager
from core.restart import RestartManager

OWNER = "Anthero"
OWNER_PCM = b"\x01\x00" * 16000


class FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(text)


class FakeSpeaker:
    """A voice index whose answer the test decides (privileged identify)."""

    owner_name = OWNER

    def __init__(self, name=OWNER, known=True, authorized=True, score=0.9,
                 verdict="known"):
        self._name = name
        self._known = known
        self._authorized = authorized
        self._score = score
        self._verdict = verdict

    def embedding(self, _pcm):
        return [1.0, 0.0]

    def identify(self, _emb, device=None):
        return {"name": self._name, "score": self._score,
                "sample_score": self._score,
                "effective_score": self._score,
                "known": self._known, "authorized": self._authorized,
                "verdict": self._verdict}

    def list(self):
        return ([{"name": OWNER, "owner": True, "authorized": True},
                 {"name": self._name, "owner": False, "authorized": True}]
                if self._known else
                [{"name": OWNER, "owner": True, "authorized": True}])


def guest_speaker() -> FakeSpeaker:
    return FakeSpeaker(name="Eloísa", known=True, authorized=True, score=0.93)


def tv_speaker() -> FakeSpeaker:
    return FakeSpeaker(name=None, known=False, authorized=False, score=0.12,
                       verdict="unknown")


class FakeRestart:
    def __init__(self):
        self.pending_value = None
        self.requested = []
        self.confirmed = []
        self.confirmed_owner = []
        self.self_restarts = []

    def pending(self):
        return self.pending_value

    def self_restart(self, reason="", requester="iniciativa propria"):
        """Reinício do próprio processo (dono, 10/11/2026): sem frase.

        Reproduz a guarda de tarefa viva lendo o mesmo sinal que lê o
        RestartManager de verdade, para o teste continuar falando do portão
        e não do mock.
        """
        self.self_restarts.append((requester, reason))
        try:
            busy = bool(main._qoder_jobs.is_busy())
        except Exception:
            busy = False
        if busy:
            return ("Reinício do processo ADIADO: tarefa de fundo em "
                    "execução. Nada foi reiniciado ainda.")
        self.pending_value = None
        return "Restarting the assistant now."

    def confirm_owner(self, kind="assistant", reason=""):
        self.confirmed_owner.append((kind, reason))
        self.pending_value = None
        return ("Restarting the assistant now." if kind == "assistant"
                else "Restarting the computer now.")

    def confirm(self, phrase):
        self.confirmed.append(phrase)
        return "Restarting the assistant now."

    def request_assistant(self, reason=""):
        self.requested.append(("assistant", reason))
        self.pending_value = {"kind": "assistant", "phrase": "confirmo reiniciar"}
        return "pending"

    def request_pc(self, reason=""):
        self.requested.append(("pc", reason))
        self.pending_value = {"kind": "pc", "phrase": "confirmo reiniciar"}
        return "pending"


class FakePower:
    def __init__(self):
        self.executed = []
        self.requested = []
        self.pending_value = None

    def pending(self):
        return self.pending_value

    def execute_owner(self, kind, reason=""):
        self.executed.append((kind, reason))
        self.pending_value = None
        return {"shutdown": "Desligando o computador agora.",
                "suspend": "Suspendendo o computador agora.",
                "unlock": "Tela desbloqueada.",
                "lock": "Tela bloqueada."}[kind]

    def request(self, kind, reason=""):
        self.requested.append((kind, reason))
        self.pending_value = {"kind": kind}
        return "pending"


def _obj(speaker=None, restart=None, power=None):
    obj = object.__new__(main.JarvisLive)
    obj._speaker = speaker if speaker is not None else FakeSpeaker()
    obj._restart = restart if restart is not None else FakeRestart()
    obj._restart_enabled = True
    obj._power = power if power is not None else FakePower()
    obj._current_pcm = OWNER_PCM
    obj._qoder_live = None
    obj._qoder_ignore = False
    obj._is_speaking = False
    obj._reply_active = False
    obj._wake_enabled = False
    obj._awake = True
    obj.ui = FakeUI()
    obj.said = []
    obj._say_now = lambda text, log=True: obj.said.append(text)
    obj._log_heard = lambda text: obj.said.append(f"heard:{text}")
    obj._note_owner_speech = lambda reason="": None
    obj._looks_like_own_voice = lambda text: False
    obj.interrupt = lambda: None
    return obj


class OwnerOrderMatcherTests(unittest.TestCase):
    def test_owner_phrasing_is_a_direct_order(self):
        for text in ("Reinicie você", "eu quero que você reinicie",
                     "Gino, reinicia o assistente",
                     "pode reiniciar o assistente",
                     "Desliga o computador", "Suspende o computador",
                     "Desbloqueia a tela"):
            self.assertTrue(main._owner_order_is_direct(text), text)

    def test_questions_denials_and_third_persons_are_not_orders(self):
        for text in ("quando você vai reiniciar?",
                     "você pode me dizer como reiniciar o assistente",
                     "não desliga o computador",
                     "não quero reiniciar agora",
                     "se você reiniciar, o que acontece",
                     "ele pediu para reiniciar você",
                     "eu não pedi para desligar o computador"):
            self.assertFalse(main._owner_order_is_direct(text), text)

    def test_first_person_plans_are_constatations_not_orders(self):
        # 12/11/2026 12:40:48: "Gino, preciso reiniciar para você cadastrar
        # mesmo" executou o reinício. Constatação não é ordem.
        for text in ("Gino, preciso reiniciar para você cadastrar mesmo",
                     "preciso reiniciar",
                     "vou reiniciar",
                     "tenho que reiniciar",
                     "devo reiniciar o assistente"):
            self.assertFalse(main._owner_order_is_direct(text), text)

    def test_second_person_command_is_still_an_order(self):
        for text in ("preciso que você reinicie",
                     "reinicia o assistente",
                     "reinicie agora"):
            self.assertTrue(main._owner_order_is_direct(text), text)


class OwnerDirectRestartTests(unittest.TestCase):
    def test_owner_order_executes_without_the_key_phrase(self):
        obj = _obj()
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "Reinicie você", OWNER_PCM))
        # Poder próprio, sem frase e sem estacionar nada (dono, 10/11/2026).
        self.assertEqual(obj._restart.self_restarts,
                         [("dono", "ordem falada do dono")])
        self.assertEqual(obj._restart.confirmed_owner, [])
        self.assertEqual(obj._restart.confirmed, [])
        self.assertEqual(obj._restart.requested, [])
        self.assertIn("Reiniciando o meu processo agora.", obj.said)

    def test_owner_own_sentence_from_the_rule_restarts(self):
        obj = _obj()
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "eu quero que você reinicie", OWNER_PCM))
        self.assertEqual(obj._restart.self_restarts[0][0], "dono")

    def test_owner_pc_order_restarts_the_computer(self):
        obj = _obj()
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "Reinicie o computador", OWNER_PCM))
        self.assertEqual(obj._restart.confirmed_owner[0][0], "pc")

    def test_economy_entry_runs_the_owner_order_before_the_model(self):
        obj = _obj()
        obj._qoder_lock = None       # reaching the model would raise TypeError
        main.JarvisLive._qoder_turn(obj, "Reinicie você", OWNER_PCM)
        self.assertEqual(obj._restart.self_restarts[0][0], "dono")

    def test_owner_order_beats_a_stale_pending_power_request(self):
        power = FakePower()
        power.pending_value = {"kind": "shutdown"}
        obj = _obj(power=power)
        obj._qoder_lock = None
        main.JarvisLive._qoder_turn(obj, "Suspende o computador", OWNER_PCM)
        self.assertEqual(power.executed[0][0], "suspend")

    def test_guest_order_executes_the_process_without_a_window(self):
        """Dono, 10/11/2026: "qualquer pessoa pode pedir para você fazer".

        Uma voz cadastrada do lado físico acende o reinício do processo com o
        mesmo caminho do dono; o que muda é só o rótulo na trilha.
        """
        obj = _obj(speaker=guest_speaker())
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "Reinicie você", OWNER_PCM))
        self.assertEqual(obj._restart.self_restarts,
                         [("outra voz", "pedido de voz cadastrada")])
        self.assertEqual(obj._restart.confirmed_owner, [])
        self.assertEqual(obj._restart.requested, [])
        self.assertIn("Reiniciando o meu processo agora.", obj.said)

    def test_guest_pc_order_still_rises_to_the_owner(self):
        """A máquina não mudou: voz cadastrada só estaciona o pedido."""
        obj = _obj(speaker=guest_speaker())
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "Reinicie o computador", OWNER_PCM))
        self.assertEqual(obj._restart.self_restarts, [])
        self.assertEqual(obj._restart.confirmed_owner, [])
        self.assertEqual(obj._restart.requested,
                         [("pc", "pedido de voz não-dona")])
        self.assertTrue(any("Só o dono pode confirmar um reinício." in said
                            for said in obj.said))

    def test_unknown_voice_does_not_execute_nor_park(self):
        obj = _obj(speaker=tv_speaker())
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "Reinicie você", OWNER_PCM))
        self.assertEqual(obj._restart.self_restarts, [])
        self.assertEqual(obj._restart.confirmed_owner, [])
        self.assertEqual(obj._restart.requested, [])
        self.assertTrue(any("voz não reconhecida" in line
                            for line in obj.ui.logs), obj.ui.logs)

    def test_question_about_restart_does_not_restart(self):
        obj = _obj()
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "quando você vai reiniciar?", OWNER_PCM))
        self.assertEqual(obj._restart.confirmed_owner, [])

    def test_constatation_neither_executes_nor_parks_an_order(self):
        obj = _obj()
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "Gino, preciso reiniciar para você cadastrar mesmo",
            OWNER_PCM))
        self.assertEqual(obj._restart.confirmed_owner, [])
        self.assertEqual(obj._restart.requested, [])
        self.assertEqual(obj.said, [])
        self.assertTrue(any("sem ordem explícita" in line
                            for line in obj.ui.logs), obj.ui.logs)

    def test_second_person_order_still_executes(self):
        obj = _obj()
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "preciso que você reinicie", OWNER_PCM))
        self.assertEqual(obj._restart.self_restarts,
                         [("dono", "ordem falada do dono")])

    def test_imperative_order_to_the_assistant_still_executes(self):
        obj = _obj()
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "reinicia o assistente", OWNER_PCM))
        self.assertEqual(obj._restart.self_restarts,
                         [("dono", "ordem falada do dono")])

    def test_denial_does_not_restart(self):
        obj = _obj()
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "não reinicie você", OWNER_PCM))
        self.assertEqual(obj._restart.confirmed_owner, [])

    def test_echo_of_own_voice_does_not_restart(self):
        obj = _obj()
        obj._is_speaking = True
        obj._looks_like_own_voice = lambda text: True
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "Reinicie você", OWNER_PCM))
        self.assertEqual(obj._restart.confirmed_owner, [])

    def test_asleep_does_not_take_direct_orders(self):
        obj = _obj()
        obj._wake_enabled = True
        obj._awake = False
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "Reinicie você", OWNER_PCM))
        self.assertEqual(obj._restart.confirmed_owner, [])

    def test_background_job_postpones_the_process_restart(self):
        """Limite 1: nunca reiniciar com executor vivo — o pedido é adiado.

        Antes a ordem caía no modelo com tarefa de fundo rodando. Agora o
        turno é tratado e o reinício fica adiado pela guarda, com trilha.
        """
        obj = _obj()
        with mock.patch.object(main._qoder_jobs, "is_busy", return_value=True):
            self.assertTrue(main.JarvisLive._owner_direct_action_gate(
                obj, "Reinicie você", OWNER_PCM))
        self.assertEqual(obj._restart.self_restarts,
                         [("dono", "ordem falada do dono")])
        self.assertEqual(obj._restart.confirmed_owner, [])
        self.assertEqual(obj._restart.requested, [])
        self.assertTrue(any("Vou reiniciar o meu processo assim que isso "
                            "terminar" in said for said in obj.said), obj.said)

    def test_background_job_still_holds_the_pc_restart(self):
        obj = _obj()
        with mock.patch.object(main._qoder_jobs, "is_busy", return_value=True):
            self.assertFalse(main.JarvisLive._owner_direct_action_gate(
                obj, "Reinicie o computador", OWNER_PCM))
        self.assertEqual(obj._restart.self_restarts, [])
        self.assertEqual(obj._restart.confirmed_owner, [])

    def test_background_job_holds_shutdown_but_not_lock(self):
        obj = _obj()
        with mock.patch.object(main._qoder_jobs, "is_busy", return_value=True):
            self.assertFalse(main.JarvisLive._owner_direct_action_gate(
                obj, "Desliga o computador", OWNER_PCM))
            self.assertEqual(obj._power.executed, [])
            self.assertTrue(main.JarvisLive._owner_direct_action_gate(
                obj, "Bloqueia a tela", OWNER_PCM))
        self.assertEqual(obj._power.executed[0][0], "lock")

    def test_barge_in_carries_the_audio_to_the_direct_gate(self):
        obj = _obj()
        calls = []
        obj._on_groq_text = lambda *args, **kwargs: calls.append((args, kwargs))
        obj._barge_fragment_needs_proof = lambda text: False
        obj._mark_qoder_voice_preempt = lambda: None
        main.JarvisLive._handle_barge_in(
            obj, "eu quero que você reinicie", OWNER_PCM)
        self.assertEqual(calls[0][0],
                         ("eu quero que você reinicie", OWNER_PCM))


class OwnerDirectPowerTests(unittest.TestCase):
    def test_owner_shutdown_runs_without_the_phrase(self):
        obj = _obj()
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "Desliga o computador", OWNER_PCM))
        self.assertEqual(obj._power.executed[0][0], "shutdown")
        self.assertEqual(obj._power.requested, [])
        self.assertIn("Desligando o computador agora.", obj.said)

    def test_owner_suspend_and_unlock_run_without_the_phrase(self):
        for text, kind in (("Suspende o computador", "suspend"),
                           ("Desbloqueia a tela", "unlock")):
            obj = _obj()
            self.assertTrue(main.JarvisLive._owner_direct_action_gate(
                obj, text, OWNER_PCM))
            self.assertEqual(obj._power.executed[0][0], kind)

    def test_guest_power_order_is_parked_for_the_owner(self):
        obj = _obj(speaker=guest_speaker())
        self.assertTrue(main.JarvisLive._owner_direct_action_gate(
            obj, "Desliga o computador", OWNER_PCM))
        self.assertEqual(obj._power.executed, [])
        self.assertEqual(obj._power.requested[0][0], "shutdown")
        self.assertTrue(any("Só o dono pode confirmar uma ação de energia."
                            in said for said in obj.said))

    def test_unknown_power_order_is_ignored(self):
        obj = _obj(speaker=tv_speaker())
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "Desliga o computador", OWNER_PCM))
        self.assertEqual(obj._power.executed, [])
        self.assertEqual(obj._power.requested, [])

    def test_denied_power_order_is_not_executed(self):
        obj = _obj()
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "não desliga o computador", OWNER_PCM))
        self.assertEqual(obj._power.executed, [])


class ProtectionsStillHoldTests(unittest.TestCase):
    def setUp(self):
        actiongate.close()
        self.addCleanup(actiongate.close)

    def test_tv_saying_pode_still_opens_no_window(self):
        self.assertFalse(actiongate.confirm(
            "Confirm 'whatsapp_control'",
            "Risk: medium. Arguments: action=send contato=Vida"))
        obj = _obj(speaker=tv_speaker())
        self.assertTrue(main.JarvisLive._try_action_consent(
            obj, "Você pode olhar.", OWNER_PCM))
        self.assertFalse(actiongate.authorized())
        self.assertTrue(actiongate.has_pending())

    def test_persona_or_tool_request_still_waits_for_the_owner(self):
        self.assertFalse(actiongate.confirm(
            "Confirm 'personas'", "Risk: high. Arguments: action=create"))
        obj = _obj()
        self.assertFalse(main.JarvisLive._owner_direct_action_gate(
            obj, "Arnaldo, crie uma persona para o Pedro", OWNER_PCM))
        self.assertEqual(obj._restart.confirmed_owner, [])
        self.assertEqual(obj._power.executed, [])
        self.assertTrue(actiongate.has_pending())


class CoreManagersDirectOrderTests(unittest.TestCase):
    def test_restart_manager_runs_the_owner_order(self):
        with TemporaryDirectory() as tmp:
            manager = RestartManager(dry_run=True,
                                     state_path=Path(tmp) / "pending.json")
            out = manager.confirm_owner(kind="assistant",
                                        reason=restart_mod.AUTO_DETECTION_REASON)
            self.assertIn("DRY RUN", out)
            self.assertIsNone(manager.pending())

    def test_power_manager_runs_the_owner_order(self):
        manager = PowerManager(dry_run=True, platform_name="Linux",
                               logger=lambda _m: None)
        out = manager.execute_owner("suspend", "ordem direta do dono")
        self.assertEqual(out, "Suspendendo o computador agora.")
        self.assertIsNone(manager.pending())


if __name__ == "__main__":
    unittest.main()
