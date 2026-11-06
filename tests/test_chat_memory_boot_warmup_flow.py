"""Fluxo do boot: o warm-up que uma sessão nova do Qoder lê no start.

Reproduz o defeito provado pelo dono em 05/11/2026 (boot 22:02): o bloco
gravado em memory/chat/qoder_live_system.txt chegava com o nível 1 cortado
no meio da frase ("O material do dia girou em…") e "Últimos turnos" com uma
única linha fatiada — memória que "não carrega" porque chega podada. Os
parâmetros abaixo são os do caminho real do boot (main.py:
_QODER_WARMUP_MAX_CHARS=800, _QODER_WARMUP_TURNS=6,
_QODER_WARMUP_INDEX_CHARS=400).

Run from the repo root:
    .venv/bin/python -m unittest tests.test_chat_memory_boot_warmup_flow -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chat_memory import ChatMemory  # noqa: E402

# Parâmetros exatos do boot (main.py _QODER_WARMUP_*).
BOOT_KWARGS = dict(max_chars=800, recent_turns=6, index_chars=400,
                   resumed=True)

TURNOS = [
    ("user", "Mas agora voce me garante que tudo está funcionando!? TESTE "
             "UNITARIO NAO GARANTE FLUXO! VOCE TESTOU OS FLUXOS E VIU SE "
             "COBRE TUDO MESMO O QUE FEZ?"),
    ("assistant", "Segundo plano rodando — provar memória registrando de "
                  "ponta a ponta, com probe no arquivo vivo."),
    ("user", "No boot da memoria deu erro de novo, resolve isso agora."),
    ("assistant", "Integrado, hash cinco seis sete um sete dois. Suíte na "
                  "área local, sem push."),
    ("user", "De 10 solicitações minhas 15 você erra, olha no log do git a "
             "quantidade de commit de retrabalho gerado."),
    ("assistant", "Contei no meu próprio registro: trinta e três commits de "
                  "correção contra sessenta e seis de trabalho novo."),
    ("user", "A tela do painel mostrou o erro no boot e você não corrigiu "
             "nada, quero isso resolvido hoje."),
    ("assistant", "Correção em campo: o corte por caractere virou corte em "
                  "linha inteira com quota por seção."),
]


class BootWarmupFlowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="boot-warmup-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "chat"
        self.now = datetime(2026, 11, 5, 22, 2)
        patcher = mock.patch("core.chat_memory._now",
                             side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mem = ChatMemory(self.base)
        # Store realista: nível 0 (turnos), hot, state com tópico/threads/
        # notas, e uma hora com bastante conteúdo.
        self.mem.note("memoria no boot", kind="topico")
        self.mem.note("corrigir truncamento do warm-up no boot", kind="thread")
        self.mem.note("corte por caractere virou corte em linha inteira",
                      kind="decisao")
        for role, text in TURNOS:
            self.mem.append_turn(role, text)

    # ── helpers ────────────────────────────────────────────────────────────
    def _section_lines(self, block, header):
        out, inside = [], False
        for line in block.splitlines():
            if line.strip() == header:
                inside = True
                continue
            if inside:
                if line.startswith("#"):
                    break
                if line.strip():
                    out.append(line)
        return out

    # ── o fluxo do boot ─────────────────────────────────────────────────────
    def test_boot_warmup_chega_completo_e_sem_frase_cortada(self):
        block = self.mem.warmup_block(**BOOT_KWARGS)
        self.assertTrue(block)
        self.assertLessEqual(len(block), 800)
        # Nada vazio quando há material: nível 1 e últimos turnos presentes.
        turns = [l for l in self._section_lines(
            block, "## Últimos turnos (palavras exatas, nível 0/1)")
            if "Você:" in l or "Assistente:" in l]
        hot = [l for l in self._section_lines(
            block, "## Nível 1 — últimas horas (hot.md)")
            if l.startswith("- ") and ("Você:" in l or "Assistente:" in l)]
        self.assertGreaterEqual(len(turns), 2,
                                "boot com 'Últimos turnos' podado: " + block)
        self.assertTrue(hot, "nível 1 vazio com material no hot: " + block)
        # Nenhuma linha termina no meio da frase: todo turno escrito termina
        # em "." ou "?" e assim deve chegar ao bloco.
        for line in turns + hot:
            self.assertTrue(line.rstrip()[-1:] in ".!?",
                            f"linha cortada no meio da frase: {line!r}")
        # O rodapé de honestidade chega inteiro (era a vítima do fatiamento).
        self.assertTrue(block.rstrip().endswith("não lembra."))

    def test_boot_index_md_tambem_nao_sai_fatiado(self):
        index = self.mem.index_block(max_chars=400)
        self.assertTrue(index)
        self.assertLessEqual(len(index), 400)
        for line in index.splitlines():
            if line.startswith("- ") and ("Você:" in line
                                          or "Assistente:" in line):
                self.assertTrue(line.rstrip()[-1:] in ".!?",
                                f"hot fatiado no index.md: {line!r}")

    def test_orcamento_grande_mostra_todas_as_secoes_com_material(self):
        block = self.mem.warmup_block()  # default 7000, 14 turnos
        for header in ("## Nível 0 — sessão viva",
                       "## Nível 1 — últimas horas (hot.md)",
                       "## Nível 2 — fio atual (state.md)",
                       "## Nível 3 — horas, dias e tópicos (memory_search/read)",
                       "## Nível 4 — longo prazo",
                       "## Nível 5 — não existe",
                       "## Últimos turnos (palavras exatas, nível 0/1)"):
            self.assertIn(header, block)
        self.assertIn("Assunto atual: [[topics/memoria-no-boot]]", block)
        self.assertIn("corrigir truncamento do warm-up no boot", block)
        turn_lines = [l for l in self._section_lines(
            block, "## Últimos turnos (palavras exatas, nível 0/1)")
            if "Você:" in l or "Assistente:" in l]
        self.assertEqual(len(turn_lines), len(TURNOS))
        for line in turn_lines:
            self.assertTrue(line.rstrip()[-1:] in ".!?",
                            f"frase cortada: {line!r}")

    def test_turno_gigante_termina_em_palavra_inteira(self):
        longo = ("Registrei a auditoria completa dos fluxos, com hora, pid e "
                 "o caminho inteiro do log, " * 20) + "fim."
        self.mem.append_turn("assistant", longo, at=self.now)
        block = self.mem.warmup_block(**BOOT_KWARGS)
        turn_lines = self._section_lines(
            block, "## Últimos turnos (palavras exatas, nível 0/1)")
        big = [l for l in turn_lines if "auditoria completa" in l]
        self.assertTrue(big, "turno longo sumiu do bloco: " + block)
        for line in big:
            # nunca termina em palavra partida: o último token é uma palavra
            # real do texto-fonte.
            last = line.rstrip().split(" ")[-1].strip(".!?:")
            self.assertIn(last, longo.split()), \
                f"linha termina em palavra partida: {line!r}"


if __name__ == "__main__":
    unittest.main()
