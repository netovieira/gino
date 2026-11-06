"""Rastro de runtime dos passos que a auditoria de 05/11 achou EM DISCO SEM
RASTRO e sem teste de fluxo (contrato: skills/fluxos/FLUXOS.md).

Cobre três ligações do contrato com a função REAL do core/main e afirma o
MARCADOR de log que cada uma emite — não só o valor de retorno:

* FALA 3 ("se tem memoria pra carregar e carrega") — o roteador popula
  ``lookups=[Lookup("memory", …)]`` (core/flows/router.py:285) e o
  orquestrador imprime o evento ``[Fluxos] {'type': 'turn', …,
  'lookups': ['memory'], …}`` (core/flows/orchestrator.py:182-190/444-451).
  Antes só existia o veredito unitário do router, sem o marcador.

* FALA 4 / RESPOSTA 2 (o cérebro manda pra boca, primeira unidade medida) —
  ``main.JarvisLive._qoder_say`` (main.py:6857) imprime
  ``[Rota] caminho=cerebro primeira_unidade_ms=NNN`` uma única vez por
  turno. O marcador ``caminho=boca`` já tinha teste
  (test_flows_fala_paralela); o ``caminho=cerebro`` não tinha nenhum.

* TRABALHO 5/6 (plano aprovado → notificação) — o ``approve()`` real de
  core/qoder_jobs.py emite no sink o evento progress com o texto literal
  "plano aprovado — aplicando as mudanças" (:210) e o retorno "Plano
  aprovado." (:211). Os testes do handler aprovavam com ``approve``
  mockado; aqui o approve real roda (o worker é intercetado para não
  abrir processo Qoder — dependência externa, não comportamento).

Nada aqui depende de microfone, GPU, WhatsApp emparelhado ou rede: só os
caminhos puros do core com o mínimo de stubs de fiação.
"""
from __future__ import annotations

import contextlib
import io
import re
import threading
import time
import unittest
from unittest import mock

import main
from core.flows.config import FlowsConfig
from core.flows.executor import Executor
from core.flows.orchestrator import Orchestrator
from core import qoder_jobs


# ── FALA 3 — consulta de memória chega ao evento com rastro impresso ───────

def _orch_log():
    lines: list[str] = []
    cfg = FlowsConfig(enabled=True, mouth_enabled=False,
                      partial_prewarm=False, partial_speech=False,
                      executor_enabled=False, log_events=True)
    orch = Orchestrator(cfg, log=lines.append,
                        executor=Executor(enabled=False),
                        clock=lambda: 1000.0)
    return orch, lines


def test_fala3_memoria_emite_evento_turno_com_lookups():
    # "o que eu pedi ontem" é exatamente a regra CONTEXT_RE →
    # Lookup("memory", …) do roteador (core/flows/router.py:284-285).
    orch, lines = _orch_log()
    plan = orch.begin("t1", "lembra o que eu pedi ontem")
    assert plan is not None
    assert [lk.kind for lk in plan.decision.lookups] == ["memory"]
    # Rastro: a linha literal do evento, no formato que cai no activity.log.
    marcador = [ln for ln in lines if "'type': 'turn'" in ln]
    assert marcador, f"nenhum evento turn impresso em {lines!r}"
    assert "'lookups': ['memory']" in marcador[0], marcador[0]
    assert "[Fluxos]" in marcador[0]


def test_fala3_rota_livre_nao_inventa_lookups():
    orch, lines = _orch_log()
    plan = orch.begin("t2", "e aí, tudo bem?")
    assert plan.decision.lookups == []
    turno = [ln for ln in lines if "'type': 'turn'" in ln]
    assert turno and "'lookups': []" in turno[0], turno


# ── FALA 4 / RESPOSTA 2 — [Rota] caminho=cerebro com o método real ─────────

class _Live:
    """JarvisLive de fiação mínima: só os atributos que _qoder_say toca."""

    def __init__(self, turn_t0: float):
        self._qoder_ignore = False
        self._qoder_lock = threading.Lock()
        self._qoder_turns = {"t1": object()}
        self._qoder_spoken: dict[str, list[str]] = {}
        self._turn_t0 = turn_t0
        self._turn_input_chan = ""
        self.spoken_calls: list[dict] = []

        def _say_now(sentence, log=False, origin="", chan=""):
            self.spoken_calls.append({"sentence": sentence,
                                      "origin": origin, "chan": chan})
            return None

        self._say_now = _say_now
        self._set_say_style = lambda *a, **k: None
        self._qoder_emotion_style = lambda _tid: ""
        # Um único objeto de turno vivo: a flag de "1ª frase" tem de
        # persistir entre as frases, como no JarvisLive real.
        self.obj = object.__new__(main.JarvisLive)
        for attr in ("_qoder_ignore", "_qoder_lock", "_qoder_turns",
                     "_qoder_spoken", "_turn_t0", "_turn_input_chan",
                     "_say_now", "_set_say_style", "_qoder_emotion_style"):
            setattr(self.obj, attr, getattr(self, attr))

    def _call_say(self, sentence: str):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            obj_ret = main.JarvisLive._qoder_say(self.obj, "t1", sentence)
        return buf.getvalue(), obj_ret


def _novo_live(atraso_ms: float) -> _Live:
    return _Live(turn_t0=time.monotonic() - atraso_ms / 1000.0)


def test_resposta2_marca_rota_cerebro_na_primeira_fala():
    live = _novo_live(900.0)
    saida, _ = live._call_say("Deixa eu te contar, criador.")
    m = re.search(r"\[Rota\] caminho=cerebro primeira_unidade_ms=(\d+)",
                  saida)
    assert m, f"marcador ausente na saída: {saida!r}"
    assert 0 < int(m.group(1)) < 100000
    # A frase segue pra boca pelo caminho real (origin=qoder).
    assert live.spoken_calls and \
        live.spoken_calls[0]["origin"] == "qoder"


def test_resposta2_marca_rota_cerebro_uma_unica_vez_por_turno():
    live = _novo_live(900.0)
    primeira, _ = live._call_say("Primeira frase.")
    segunda, _ = live._call_say("Segunda frase.")
    assert "caminho=cerebro" in primeira
    assert "caminho=cerebro" not in segunda, \
        "medição de rota repetida: o contrato é uma unidade por turno"


def test_resposta2_fala_vazia_nao_emite_marca():
    live = _novo_live(900.0)
    saida, ret = live._call_say("   ")
    assert saida == "" and ret is None
    assert live.spoken_calls == []


# ── TRABALHO 5/6 — approve real emite o rastro no sink ─────────────────────

class ApprovalTraceTests(unittest.TestCase):
    def setUp(self):
        self.eventos: list[dict] = []
        self._sink_antigo = qoder_jobs._SINK
        self._ativo_antigo = qoder_jobs._ACTIVE
        qoder_jobs.set_sink(self.eventos.append)

    def tearDown(self):
        qoder_jobs.set_sink(self._sink_antigo)
        qoder_jobs._ACTIVE = self._ativo_antigo

    def _job_esperando(self):
        job = qoder_jobs.Job("reorganizar o elo de relançamento", "write",
                             "m", None, None, None, requires_approval=True)
        job.state = "waiting_approval"
        job.plan = "1) ler 2) editar"
        qoder_jobs._ACTIVE = job
        return job

    def test_aprove_real_emite_rastro_plano_aprovado(self):
        job = self._job_esperando()
        with mock.patch.object(qoder_jobs, "_worker",
                               side_effect=lambda *a, **k: None) as wk:
            resposta = qoder_jobs.approve()
        self.assertIn("Plano aprovado", resposta)
        progressos = [e for e in self.eventos
                      if e.get("kind") == "progress"]
        self.assertTrue(progressos, f"nenhum evento no sink: {self.eventos}")
        self.assertEqual(progressos[-1]["text"],
                         "plano aprovado — aplicando as mudanças")
        self.assertEqual(progressos[-1]["id"], job.id)
        self.assertEqual(job.state, "running")
        wk.assert_called_once()                      # o worker real foi acionado
        self.assertNotIn("senha", str(self.eventos).lower())

    def test_aprove_sem_plano_nao_emite_rastro(self):
        qoder_jobs._ACTIVE = None
        resposta = qoder_jobs.approve()
        self.assertIn("Não há plano", resposta)
        self.assertEqual(self.eventos, [])


if __name__ == "__main__":
    unittest.main()
