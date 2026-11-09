"""core/dev_audit.py — a auditoria que confere o "fechado antes" contra o vivo.

A ordem do dono (07/11/2026) pedia o mecanismo; o que já doeu na sessão foi o
contrário: tarefa marcada como feita sem elo no processo real (UP-8 com o
gancho instalado só em teste — 214 linhas ``hook=ausente``; UP-10 sem
chamador em produção; LinkedIn "concluído" com commit de comentário). Os
testes abaixo fixam as quatro exigências que dão crédito ao mecanismo:

1. a sonda ESTÁTICA pega o caso real do ``hook=ausente`` — instalação que só
   existe em ``tests/`` é FALHA, não verde por associação;
2. sonda VIVA sem dados suficientes dá ``nao_verificado``, nunca ``ok``;
3. exceção dentro de uma sonda não derruba a auditoria (e nem o boot — a
   thread de ``main._start_dev_audit`` tem o mesmo envoltório);
4. uma passada por boot (cache), com reexecução manual por ``force=True``.

E a RESTRIÇÃO PERMANENTE, provada com marcador plantado: nenhuma sonda lê a
fala do dono. O log falso recebe utterances com um marcador único; nada do
marrador pode aparecer no resultado, na linha de evento ou nos motivos — e a
sonda viva, que NÃO pode confiar em linha de fala, continua ``nao_verificado``
mesmo com "hook=ausente" escrito dentro de uma frase.

Isolamento: raiz, activity.log, canal de log e abertura de defeito vão todos
para ``tmp_path``/coletores via monkeypatch — nada deste arquivo escreve no
``logs/activity.log`` do dono, em ``memory/defeitos/abertos.jsonl`` nem toca
a rede. O último teste roda as sondas estáticas contra a árvore REAL
(somente leitura de código-fonte) — é ele que quebra se alguém desligar o
elo vivo de novo.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import dev_audit


# ── palco isolado ───────────────────────────────────────────────────────────

@pytest.fixture
def palco(tmp_path, monkeypatch):
    """Estado global do módulo trocado por tmp/coletores; volta sozinho.

    ``boot_epoch`` fica 2 s no passado: a linha escrita "agora" cai dentro da
    janela de boot mesmo com a truncagem de segundos do formato do log.
    """
    root = tmp_path / "repo"
    (root / "core").mkdir(parents=True)
    (root / "tests").mkdir()
    lines: list[str] = []
    defects: list[str] = []
    monkeypatch.setattr(dev_audit, "_ROOT", root)
    monkeypatch.setattr(dev_audit, "_ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(dev_audit, "_LOG", lines.append)
    monkeypatch.setattr(dev_audit, "_DEFECT_OPENER", defects.append)
    monkeypatch.setattr(dev_audit, "_BOOT_EPOCH_CFG", time.time() - 2)
    monkeypatch.setattr(dev_audit, "_last", None)
    monkeypatch.setattr(dev_audit, "_reported", set())
    monkeypatch.setattr(dev_audit, "_event_cache", None)
    monkeypatch.setattr(dev_audit, "_CFG", lambda ns: {})
    return SimpleNamespace(root=root, lines=lines, defects=defects,
                           log=tmp_path / "activity.log", tmp=tmp_path)


def _ts(seconds_from_now: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(
        time.time() + seconds_from_now))


def _write_log(path: Path, bodies: list[tuple[float, str]]) -> None:
    path.write_text(
        "".join(f"[{_ts(off)}] {body}\n" for off, body in bodies),
        encoding="utf-8")


# ── 1. sonda estática pega o caso real do hook=ausente ─────────────────────

_MAIN_SEM_ELO = '''
class JarvisLive:
    def _flows_get(self):
        rt = _build_runtime()
        return rt

    def _flows_progress_hook(self, rt):
        _task_mirror.set_progress_hook(object())
        return True
'''
_MAIN_COM_ELO = '''
class JarvisLive:
    def _flows_get(self):
        rt = _build_runtime()
        self._flows_progress_hook(rt)
        return rt

    def _flows_progress_hook(self, rt):
        _task_mirror.set_progress_hook(object())
        return True
'''
# O elo "instalado" só dentro de teste — o formato exato do bug de 07/11.
_TESTE_INSTALANDO_MAOS = '''
def test_hook():
    JarvisLive()._flows_progress_hook(rt)
'''


def test_sonda_estatica_pega_o_caso_real_hook_ausente(palco):
    (palco.root / "main.py").write_text(_MAIN_SEM_ELO, encoding="utf-8")
    (palco.root / "tests" / "test_hook.py").write_text(_TESTE_INSTALANDO_MAOS,
                                                       encoding="utf-8")
    verdict, motivo = dev_audit._sonda_up8_gancho()
    assert verdict == dev_audit.FALHA
    assert "_flows_get" in motivo


def test_sonda_estatica_da_ok_quando_o_boot_chama_o_gancho(palco):
    (palco.root / "main.py").write_text(_MAIN_COM_ELO, encoding="utf-8")
    assert dev_audit._sonda_up8_gancho() == (dev_audit.OK, "")


def test_sonda_de_mute_nao_se_confunde_com_teste_que_cita_mute(palco):
    """A sonda de mute olha o main.py; um teste que cite mute não a engana."""
    (palco.root / "main.py").write_text(
        _MAIN_COM_ELO + '''
    def _say_blocking(self, text):
        if self.ui.muted:
            return
''', encoding="utf-8")
    verdict, motivo = dev_audit._sonda_mute_somente_microfone()
    assert verdict == dev_audit.FALHA
    assert "_say_blocking" in motivo


# ── 2. sonda viva sem dados é nao_verificado, nunca ok ─────────────────────

def test_sonda_viva_sem_log_da_nao_verificado(palco):
    v, m = dev_audit._sonda_up8_viva()
    assert v == dev_audit.NV
    assert "indisponivel" in m


def test_sonda_viva_com_janela_so_com_linhas_antigas_da_nao_verificado(palco):
    _write_log(palco.log, [(-3600, "event=task_progress id=x hook=installado"),
                           (-3000, "SYS: Gino online")])
    assert dev_audit._sonda_up8_viva()[0] == dev_audit.NV


def test_sonda_viva_pega_hook_ausente_novo_e_instalada_no_boot(palco):
    _write_log(palco.log, [(0, "[fluxos] progress_hook instalada=1 boca=1")])
    assert dev_audit._sonda_up8_viva()[0] == dev_audit.OK
    _write_log(palco.log, [(0, "[fluxos] progress_hook instalada=1 boca=1"),
                           (1, "event=task_progress id=live hook=ausente")])
    v, m = dev_audit._sonda_up8_viva()
    assert v == dev_audit.FALHA and "hook=ausente" in m


# ── 3. exceção em sonda não derruba a auditoria ────────────────────────────

def test_excecao_na_sonda_vira_nao_verificado_e_a_auditoria_termina(
        palco, monkeypatch):
    explosiva = dev_audit.Assertion(
        "BOOM", "sonda que explode", "estatica",
        lambda: (_ for _ in ()).throw(RuntimeError("kabum")))
    monkeypatch.setattr(dev_audit, "REGISTER",
                        list(dev_audit.REGISTER) + [explosiva])
    r = dev_audit.run_audit()
    assert r["verdicts"]["BOOM"][0] == dev_audit.NV
    assert "explodiu" in r["verdicts"]["BOOM"][1]
    assert r["total"] == len(dev_audit.REGISTER)  # todas as outras rodaram


# ── 4. uma passada por boot; reexecução manual ─────────────────────────────

def test_cache_roda_uma_vez_por_boot_e_forcar_reexecuta(palco, monkeypatch):
    calls: list[int] = []
    contadora = dev_audit.Assertion(
        "CONTADOR", "sonda contável", "estatica",
        lambda: (calls.append(1), (dev_audit.OK, ""))[1])
    monkeypatch.setattr(dev_audit, "REGISTER", [contadora])
    r1 = dev_audit.run_audit()
    dev_audit.run_audit()
    dev_audit.run_audit()
    assert len(calls) == 1 and r1["ok"] == 1
    dev_audit.run_audit(force=True)
    assert len(calls) == 2


# ── 5. restrição permanente: nada lê a fala do dono ────────────────────────

MARCADOR = "MARKER-FALA-DO-DONO-7C41"


def test_nenhuma_sonda_le_a_fala_do_dono(palco):
    """Utterances plantadas não viram prova, nem aparecem no resultado.

    A linha de fala até repete o padrão procurado pela sonda viva
    (``hook=ausente``): se o portão de evento falhasse, o veredito seria
    FALHA com conteúdo de fala ao redor. O certo — e cobrado aqui — é a sonda
    não enxergar a linha e o marcador não sobreviver a nenhuma saída.
    """
    _write_log(palco.log, [
        (0, f"Dono: {MARCADOR} verifica se eu disse hook=ausente mesmo"),
        (0, f"Gino: entendi {MARCADOR} [audio={{\"turn\":7,\"chan\":\"voz\"}}]"),
        (0, f"SYS: transcrição descartada: \"{MARCADOR} palavração\""),
    ])
    assert dev_audit._sonda_up8_viva()[0] == dev_audit.NV
    r = dev_audit.run_audit()
    blob = (dev_audit.event_line(r) + dev_audit.dump_result()
            + "".join(palco.lines) + str(r["verdicts"]))
    assert MARCADOR not in blob


def test_sonda_de_fala_e_a_unica_porta_do_log(palco):
    """Uma linha de evento legível passa; o resto fica de fora da varrida."""
    _write_log(palco.log, [
        (0, f"event=task_progress id=x {MARCADOR}"),  # evento: passa no portão
        (0, f"Dono: {MARCADOR}"),                     # fala: barrada no portão
    ])
    linhas = dev_audit.event_lines_since_boot()
    assert len(linhas) == 1 and "task_progress" in linhas[0]


# ── 6. falha vira defeito no mecanismo existente (uma vez por id por boot) ──

def test_falha_abre_defeito_uma_unica_vez_por_id(palco, monkeypatch):
    com_falha = dev_audit.Assertion(
        "X.quebra", "afirma algo falso", "estatica",
        lambda: (dev_audit.FALHA, "elo sumido"))
    monkeypatch.setattr(dev_audit, "REGISTER",
                        list(dev_audit.REGISTER) + [com_falha])
    dev_audit.run_audit()
    dev_audit.run_audit(force=True)
    abertos = [d for d in palco.defects if "X.quebra" in d]
    assert len(abertos) == 1
    assert "elo sumido" in abertos[0]
    assert palco.lines and palco.lines[0].startswith("event=dev_audit")


# ── 7. portão de fechamento: aviso registrado, nunca bloqueio ──────────────

def test_assert_ready_to_close_desconhece_id_sem_assercao(palco):
    got = dev_audit.assert_ready_to_close("NADA-99")
    assert got["known"] is False and got["verdicts"] == {}


def test_assert_ready_to_close_re_sonda_o_id_da_registro(palco):
    (palco.root / "main.py").write_text(_MAIN_SEM_ELO, encoding="utf-8")
    got = dev_audit.assert_ready_to_close("UP-8")
    assert got["known"] is True
    assert got["verdicts"]["UP-8.gancho-boot"][0] == dev_audit.FALHA
    assert got["verdicts"]["UP-8.vivo"][0] == dev_audit.NV


def test_advise_close_grava_aviso_e_nao_levanta_mesmo_com_log_explosivo(
        palco, monkeypatch):
    (palco.root / "main.py").write_text(_MAIN_SEM_ELO, encoding="utf-8")
    dev_audit.advise_close("UP-8", "done")
    avisos = [ln for ln in palco.lines if ln.startswith("event=close_advisory")]
    assert avisos and "UP-8.gancho-boot" in avisos[0]
    # status que não é conclusão não avisa nada
    palco.lines.clear()
    dev_audit.advise_close("UP-8", "error")
    assert not [ln for ln in palco.lines if "close_advisory" in ln]

    def _log_que_explode(_m: str) -> None:
        raise OSError("log morto")
    monkeypatch.setattr(dev_audit, "_LOG", _log_que_explode)
    dev_audit.advise_close("UP-8", "done")       # não pode levantar


# ── 8. árvore real: os elos desta sessão continuam vivos (só leitura) ───────

def test_sondas_estaticas_passam_na_arvore_real():
    """Deposite do elo vivo: se alguém desligar um gancho do boot, isto quebra.

    Roda contra o repositório de verdade (paths default, sem palco) e só
    toca as sondas estáticas/config — nenhuma escreve em disco, nenhum log é
    lido, nenhuma fala circula.
    """
    for a in dev_audit.REGISTER:
        if a.probe_type in ("estatica", "config"):
            v, m = a.probe()
            if a.id in dev_audit.PENDENCIAS_DECLARADAS:
                assert v == dev_audit.NV, (
                    f"{a.id}: pendencia declarada deveria estar nao_verificada,"
                    f" mas a sonda respondeu {v} ({m}) — se o mecanismo entrou "
                    "de verdade, tire o id de PENDENCIAS_DECLARADAS")
                continue
            assert v == dev_audit.OK, f"{a.id}: {v} ({m})"
