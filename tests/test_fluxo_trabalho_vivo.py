"""FLUXO TRABALHO ao vivo: plano em arquivo, fio de aprovação, fala de
andamento e conclusão que não se perde (lei do dono 05/11).

Cobre os quatro furos provados pela auditoria de 05/11:

* T2 — o plano do job virava só RAM; agora é ``~/.qoder/plans/<id>.md``
  gravado ANTES do pedido de aprovação, aberto na tela pela ação ``open_in``
  existente (espelho ``obsidian.mirror_plans`` já cuida do cofre).
* T5 — ``approve``/``reject``/``answer`` não sobreviviam à recusa do chat ao
  vivo; agora têm rota própria, e só passam com a janela one-shot de
  core/job_grant.py — aberta exclusivamente pela voz do dono (voiceprint,
  main._try_action_consent) ou pela senha digitada (main._owner_text_gate).
  Auto-aprovação do modelo e "pode" digitado sem senha são recusados.
* T6 — progresso no modo vivo retornava sem falar (corrida de 18:22:38 às
  18:38:59: zero atualizações). Agora sai uma frase com estado real, no
  máximo uma por janela de 40 s, deduplicada, e o guard da boca continua
  valendo: nada de "estou executando", nada de ferramenta/caminho/JSON.
* T7 — 15 conclusões mudas: a causa é o portão do mute em ``_say_blocking``,
  que descartava o aviso sem voz E sem texto ("SYS: microfone mutado — Gino
  não falou: Terminei em segundo plano ..." — activity.log 17539/17553/17575
  de 05/11). Agora, com a boca fechada, a conclusão vai como TEXTO e nunca
  se perde.
"""
from __future__ import annotations

import inspect
import os
import tempfile
import time
import unittest
from unittest import mock

import main
from actions import open_in_app, qoder_job as qoder_job_action
from core import job_grant, qoder_jobs


class PlanFileTests(unittest.TestCase):
    """(a) O plano vira arquivo antes da aprovação; open_in tem o caminho."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _job(self):
        return qoder_jobs.Job("mudar o elo de relançamento", "write", "m",
                              None, None, None, requires_approval=True)

    def test_plan_markdown_has_the_contract_sections(self):
        job = self._job()
        text = qoder_jobs.plan_markdown(job, "1) ler main.py 2) editar")
        for section in ("# Plano", "## Passos", "## O que vai mudar",
                        "## Risco", "## O que NÃO será feito"):
            self.assertIn(section, text)
        self.assertIn("mudar o elo de relançamento", text)

    def test_save_plan_file_writes_under_plans_dir(self):
        job = self._job()
        path = qoder_jobs.save_plan_file(job, "passos aqui", plans_dir=self.dir)
        self.assertTrue(path)
        self.assertEqual(os.path.basename(path), f"{job.id}.md")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("passos aqui", fh.read())

    def test_open_in_routes_plan_md_to_obsidian(self):
        # A rota de abertura é a existente: .md vira Obsidian, sem código novo.
        job = self._job()
        path = qoder_jobs.save_plan_file(job, "passos", plans_dir=self.dir)
        self.assertEqual(open_in_app._kind_for(open_in_app.Path(path)),
                         "obsidian")

    def test_run_once_writes_file_before_asking_approval(self):
        job = self._job()
        seen: list[bool] = []

        def sink(ev):
            if ev.get("kind") == "approval":
                p = str(ev.get("plan_path") or "")
                seen.append(bool(p) and os.path.exists(p))

        with mock.patch.object(qoder_jobs, "_default_plans_dir",
                               lambda: __import__("pathlib").Path(self.dir)), \
             mock.patch("actions.qoder_agent.prepare_job",
                        lambda j, **kw: {"st": None, "prompt": "", "model": "m",
                                         "system": "", "proxy_system": "",
                                         "root": __import__("pathlib").Path(".")}), \
             mock.patch("actions.qoder_agent.parse_question", lambda r: None), \
             mock.patch.object(qoder_jobs, "_ask_qoder",
                               lambda j, prepared, on_progress:
                               ("Passo 1: editar o helper.", False)), \
             mock.patch.object(qoder_jobs, "_SINK", sink):
            qoder_jobs._run_once(job)
        self.assertEqual(seen, [True],
                         "o evento approval só sai com o plano já em arquivo")
        self.assertEqual(job.state, "waiting_approval")
        self.assertTrue(job.plan_path)


class JobGrantTests(unittest.TestCase):
    """(b) A janela one-shot que só os dois fatores do dono abrem."""

    def setUp(self):
        job_grant.reset()
        self.addCleanup(job_grant.reset)

    def test_arm_refuses_unknown_factor(self):
        self.assertEqual(job_grant.arm("J1", "texto digitado"), 0.0)
        self.assertEqual(job_grant.arm("J1", "modelo"), 0.0)
        self.assertEqual(job_grant.arm("", "voz do dono"), 0.0)
        self.assertFalse(job_grant.has_valid("J1"))

    def test_arm_and_consume_is_one_shot(self):
        self.assertGreater(job_grant.arm("J1", "voz do dono"), 0.0)
        self.assertTrue(job_grant.has_valid("J1"))
        info = job_grant.consume("J1", "approve")
        self.assertEqual(info["factor"], "voz do dono")
        self.assertIsNone(job_grant.consume("J1", "approve"))

    def test_grant_expires(self):
        job_grant.arm("J1", "senha digitada", ttl=0.01)
        time.sleep(0.03)
        self.assertIsNone(job_grant.consume("J1", "approve"))

    def test_grant_does_not_cross_jobs(self):
        job_grant.arm("J1", "voz do dono")
        self.assertIsNone(job_grant.consume("J2", "approve"))

    def test_audit_hook_fires_on_consume_without_channel_or_number(self):
        lines: list[str] = []
        job_grant.set_audit(lines.append)
        self.addCleanup(job_grant.set_audit, None)
        job_grant.arm("J1", "senha digitada")
        job_grant.consume("J1", "approve")
        self.assertEqual(len(lines), 1)
        self.assertIn("dono", lines[0])
        self.assertIn("senha digitada", lines[0])
        self.assertNotIn("whatsapp", lines[0].lower())
        self.assertFalse(any(ch.isdigit() for ch in lines[0]))


class HandlerApprovalTests(unittest.TestCase):
    """(b) O handler: sem janela, o modelo não aprova nem recusa por conta."""

    def setUp(self):
        job_grant.reset()
        self.addCleanup(job_grant.reset)

    def _call(self, action, answer=""):
        return qoder_job_action.qoder_job(
            {"action": action, "answer": answer})

    def test_approve_without_grant_is_refused_and_never_reaches_job(self):
        with mock.patch.object(qoder_jobs, "pending_reply_id",
                               return_value=""), \
             mock.patch.object(qoder_jobs, "approve") as approve:
            out = self._call("approve")
        self.assertIn("Nada foi alterado", out)
        approve.assert_not_called()

    def test_approve_with_owner_voice_grant_reaches_job(self):
        with mock.patch.object(qoder_jobs, "pending_reply_id",
                               return_value="J9"), \
             mock.patch.object(qoder_jobs, "approve",
                               return_value="Plano aprovado.") as approve:
            job_grant.arm("J9", "voz do dono")
            out = self._call("approve")
        self.assertEqual(out, "Plano aprovado.")
        approve.assert_called_once()

    def test_reject_and_answer_also_need_the_grant(self):
        with mock.patch.object(qoder_jobs, "pending_reply_id",
                               return_value=""), \
             mock.patch.object(qoder_jobs, "reject") as rej, \
             mock.patch.object(qoder_jobs, "answer") as ans:
            self.assertIn("Nada foi alterado", self._call("reject"))
            self.assertIn("Nada foi alterado", self._call("answer", "sim"))
        rej.assert_not_called()
        ans.assert_not_called()

    def test_status_and_cancel_keep_working(self):
        with mock.patch.object(qoder_jobs, "status", return_value="livre"), \
             mock.patch.object(qoder_jobs, "pending_reply_id",
                               return_value=""):
            self.assertEqual(self._call("status"), "livre")


class OwnerIdentityWiringTests(unittest.TestCase):
    """(b) Quem arma a janela — e o que NÃO arma."""

    def setUp(self):
        job_grant.reset()
        self.addCleanup(job_grant.reset)

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj.ui = type("UI", (), {"write_log": staticmethod(lambda t: None)})()
        return obj

    def test_arm_requires_a_waiting_job(self):
        with mock.patch.object(main._qoder_jobs, "pending_reply_id",
                               return_value=""):
            self.assertEqual(
                main.JarvisLive._arm_job_grant(self._obj(), "voz do dono"),
                0.0)
        self.assertFalse(job_grant.has_valid())

    def test_arm_accepts_only_the_two_owner_factors(self):
        with mock.patch.object(main._qoder_jobs, "pending_reply_id",
                               return_value="J5"):
            self.assertEqual(
                main.JarvisLive._arm_job_grant(self._obj(), "modelo"), 0.0)
            self.assertGreater(
                main.JarvisLive._arm_job_grant(self._obj(), "senha digitada"),
                0.0)
        self.assertTrue(job_grant.has_valid("J5"))

    def test_typed_pode_never_arms_the_grant(self):
        # Texto digitado comum: _owner_text_gate devolve False antes de
        # qualquer armar — só o comando de senha alcança o fator "senha".
        obj = object.__new__(main.JarvisLive)
        self.assertFalse(
            main.JarvisLive._owner_text_gate(obj, "pode", "ui"))
        self.assertFalse(job_grant.has_valid())

    def test_voiceprint_consent_path_calls_the_arm(self):
        src = inspect.getsource(main.JarvisLive._try_action_consent)
        self.assertIn('_arm_job_grant("voz do dono")', src)

    def test_owner_password_path_calls_the_arm(self):
        # O fator senha abre agora TODAS as janelas num único ponto
        # (_owner_password_grant, chamado pelo portão de senha).
        src = inspect.getsource(main.JarvisLive._owner_password_grant)
        self.assertIn("_arm_job_grant(_owner_auth.FACTOR_TEXT)", src)
        gate = inspect.getsource(main.JarvisLive._owner_text_gate)
        self.assertIn("_owner_password_grant()", gate)

    def test_restart_power_send_gates_untouched(self):
        # Segurança inegociável: o relay do job não afrouxa os portões.
        src = inspect.getsource(main.JarvisLive._bridge_confirm)
        self.assertIn("_actiongate.confirm(title, message)", src)


class BridgeRouteTests(unittest.TestCase):
    """(b) A rota própria sobrevive à dispatch do chat ao vivo."""

    def setUp(self):
        job_grant.reset()
        self.addCleanup(job_grant.reset)

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        return obj

    def test_reply_actions_pass_the_dispatch_refusal(self):
        for action in ("approve", "reject", "answer", "aprovar", "responder"):
            self.assertEqual(
                main.JarvisLive._job_reply_action(self._obj(),
                                                  {"action": action}),
                action)

    def test_start_stays_out_of_the_live_chat(self):
        for action in ("start", "status", "cancel", ""):
            self.assertEqual(
                main.JarvisLive._job_reply_action(self._obj(),
                                                  {"action": action}),
                "")
        out = main.JarvisLive._bridge_dispatch(
            self._obj(), "qoder_job", {"action": "start"})
        self.assertIn("não existe no chat ao vivo", out)

    def test_preauthorize_only_with_a_valid_grant(self):
        obj = self._obj()
        with mock.patch.object(main._qoder_jobs, "pending_reply_id",
                               return_value="J7"):
            self.assertFalse(main.JarvisLive._bridge_preauthorize(
                obj, "qoder_job", {"action": "approve"}))
            job_grant.arm("J7", "voz do dono")
            self.assertTrue(main.JarvisLive._bridge_preauthorize(
                obj, "qoder_job", {"action": "approve"}))
            # Nenhuma outra ação é preautorizada por isto.
            self.assertFalse(main.JarvisLive._bridge_preauthorize(
                obj, "restart_pc", {}))


class ProgressSpeechTests(unittest.TestCase):
    """(c) Andamento no modo vivo: uma frase por 40 s, nada proibido."""

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj.said: list[str] = []
        obj._say_now = lambda text, log=True, origin="": obj.said.append(text)
        obj._last_user_speech = 0.0
        obj._last_qoder_update = 0.0
        return obj

    def test_empty_and_tool_narration_never_speak(self):
        obj = self._obj()
        for bad in ("", "Estou executando as mudanças agora.",
                    "Abrindo o Terminal.", "Vou rodar os testes",
                    '{"kind": "tool", "name": "Bash"}'):
            self.assertEqual(
                main.JarvisLive._live_progress_speech(obj, bad), None)
        self.assertEqual(obj.said, [])

    def test_real_state_phrases_at_most_one_per_40s_window(self):
        obj = self._obj()
        main.JarvisLive._live_progress_speech(
            obj, "Metade dos testes já passou, faltam dois arquivos.")
        main.JarvisLive._live_progress_speech(
            obj, "Correção do helper aplicada, conferindo o boot.")
        self.assertEqual(len(obj.said), 1,
                         "duas frases na mesma janela de 40 s é spam")
        # Janela vencida: a próxima frase real sai.
        obj._last_qoder_update = time.monotonic() - 41.0
        main.JarvisLive._live_progress_speech(
            obj, "Espelho do terminal aberto com o log da corrida.")
        self.assertEqual(len(obj.said), 2)

    def test_repeated_line_is_deduplicated_even_after_the_gap(self):
        obj = self._obj()
        main.JarvisLive._live_progress_speech(obj, "Fase de testes em curso.")
        obj._last_qoder_update = time.monotonic() - 41.0
        main.JarvisLive._live_progress_speech(obj, "Fase de testes em curso.")
        self.assertEqual(len(obj.said), 1)

    def test_directive_progress_events_route_through_the_speech(self):
        src = inspect.getsource(main.JarvisLive._deliver_qoder_event_direct)
        self.assertIn("_live_progress_speech", src)
        src2 = inspect.getsource(main.JarvisLive._qoder_on_task)
        self.assertIn("_live_progress_speech", src2)


class MutedCompletionTextTests(unittest.TestCase):
    """(d) Conclusão com a boca fechada: vira TEXTO, nunca se perde."""

    def _obj(self, muted: bool):
        ui_lines: list[str] = []
        contents: list[tuple[str, str]] = []

        class UI:
            def __init__(self):
                self.muted = muted

            def write_log(self, text):
                ui_lines.append(text)

            def show_content(self, title, text):
                contents.append((title, text))

        obj = object.__new__(main.JarvisLive)
        obj.ui = UI()
        obj._asst_name = "Gino"
        obj._session_log: list[str] = []
        obj._turn_ref = 7
        obj._resolve_say_style = lambda origin, ref: ""
        obj._note_speech = lambda *a, **k: 0
        return obj, ui_lines, contents

    def test_muted_announcement_goes_as_text(self):
        obj, lines, contents = self._obj(muted=True)
        main.JarvisLive._say_blocking(
            obj, "Terminei em segundo plano: Consertar elo de relançamento.",
            log=True, origin="aviso")
        spoken_text = [l for l in lines
                       if "Terminei em segundo plano" in l and "Gino:" in l]
        self.assertEqual(len(spoken_text), 1,
                         "a conclusão mudada tem de sair exatamente uma vez "
                         "como texto")
        self.assertIn('"audio_generated":false', spoken_text[0])
        self.assertIn("aviso-texto", spoken_text[0])
        self.assertTrue(any("Consertar elo" in text
                            for _t, text in contents))
        self.assertEqual(len(obj._session_log), 1)

    def test_ordinary_lines_while_muted_are_still_dropped(self):
        # A lei do mute não mudou: só o aviso de segundo plano tem texto.
        obj, lines, contents = self._obj(muted=True)
        main.JarvisLive._say_blocking(
            obj, "Pode, autorizado.", log=True, origin="gate")
        self.assertTrue(any("não falou" in l for l in lines))
        self.assertEqual(obj._session_log, [])
        self.assertEqual(contents, [])

    def test_text_fallback_helper_is_self_contained(self):
        obj, lines, contents = self._obj(muted=False)
        main.JarvisLive._announce_text_fallback(
            obj, "A tarefa em segundo plano falhou.", turn=None)
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(contents), 1)


if __name__ == "__main__":
    unittest.main()
