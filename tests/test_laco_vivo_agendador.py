"""O LAÇO VIVO do agendador — ``main.py:_task_watch_step`` chamando ``tick``.

Plano ``PLANO-2026-11-09-fluxo-agendamento-e-avisos-por-evento``, tarefa
``laco-vivo-e-portao-de-fluxo``. O contrato (itens 4 e 5 do adendo de 09/11):

* o agendador **não abre laço próprio**: quem chama ``tick`` é o laço de hora
  que já existe. O ritmo real, medido no código (``JarvisLive._task_watch``,
  thread ``task-watch``): ``time.sleep(15)`` por volta — o disparo tem
  resolução de **15 s** e nenhum quarto laço foi criado (risco 1 do plano).
* à prova de exceção nos dois sentidos: um gatilho quebrado não derruba a
  passada (os outros registros da mesma volta disparam) e um ``tick`` quebrado
  não derruba o passo que atende o dono (a fila continua).
* a barreira da persona (:func:`core.scheduler._barreira_da_persona`) vale do
  mesmo jeito pelo caminho novo: persona não injeta gatilho no laço.

Hermetismo: cofre em ``tmp_path`` (o ``tests/conftest.py`` já isola
``obsidian.VAULT``), boca e WhatsApp do dono trocados por listas, rede nunca
tocada. Provas de paridade com marca de tempo **dentro da graça**
(``LATE_GRACE_SECONDS`` = 180 s): vencido longe demais vai para o resumo da
regra 6, não para o disparo.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import main
from actions import schedule as ferramenta
from core import obsidian, scheduler

#: O cofre do dono. Nenhum teste pode criar nota aqui.
REAL_MEMORIA = Path.home() / "Jarvis" / "Memoria"

GATILHO_VOZ = {"tipo": "funcao", "modulo": "core.scheduler",
               "funcao": "agora", "destino": ["voz"]}


@pytest.fixture
def cofre(tmp_path, monkeypatch):
    """Cofre isolado, portas zeradas e nenhum estado do módulo herdado."""
    root = tmp_path / "Jarvis"
    monkeypatch.setattr(obsidian, "VAULT", root)
    monkeypatch.setattr(scheduler, "REGISTRO_SCRIPTS",
                        tmp_path / "config" / "schedule_scripts.json")
    for nome, valor in (("_cache", {"assinatura": None, "linhas": {}}),
                        ("_scripts_cache", {"assinatura": None, "lista": []}),
                        ("_PORTAS", {}), ("_pendente_aviso", set()),
                        ("_pedidos_aprovacao", []), ("_ja_acordei", False)):
        monkeypatch.setattr(scheduler, nome, valor)
    return root


def amanha(hora: int = 9) -> str:
    return (datetime.now() + timedelta(days=1)).replace(
        hour=hora, minute=0, second=0, microsecond=0).isoformat()


def vencido(titulo: str, *, minutos: int = 1, gatilho: dict | None = None,
             origem: str = "fala_do_dono", persona: str = "") -> dict:
    """Cria pelo caminho real (hora futura) e chega atrasado dentro da graça."""
    saida = scheduler.registrar(
        amanha(), titulo, gatilho or GATILHO_VOZ, origem=origem,
        persona=persona)
    assert saida["ok"] is True, saida
    linha = saida["linha"]
    linha["quando"] = (datetime.now().astimezone()
                       - timedelta(minutes=minutos)).isoformat()
    scheduler._gravar(linha)
    scheduler.carregar(forcar=True)
    return linha


class _Dono:
    """O mínimo de ``JarvisLive`` que o laço do agendador encosta."""

    def __init__(self):
        self.falado: list[str] = []
        self.enviado: list[str] = []
        self.explode = False

    def _say_now(self, texto, **_kw):
        if self.explode and "eletricista" in str(texto):
            raise RuntimeError("sintetizador no ar")
        self.falado.append(str(texto))

    def _whatsapp_notify_owner(self, texto):
        self.enviado.append(str(texto))
        return True


def _dono() -> _Dono:
    obj = object.__new__(main.JarvisLive)
    dono = _Dono()

    obj._say_now = dono._say_now
    obj._whatsapp_notify_owner = dono._whatsapp_notify_owner
    obj.dono = dono
    obj._qoder_lock = threading.RLock()
    obj._qoder_current_id = None
    obj._is_speaking = False
    obj._qoder_live_ready = lambda: True
    return obj


# ── (a) o laço chama o tick, e o vencido dispara na passada ────────────────

def test_passada_do_laco_chama_tick(cofre, monkeypatch):
    chamadas: list[dict] = []
    monkeypatch.setattr(scheduler, "tick",
                        lambda **kw: (chamadas.append(kw), [])[1])
    assert _dono()._scheduler_watch_step() == []
    assert len(chamadas) == 1, "o laço vivo não chamou scheduler.tick"


def test_gatilho_vencido_dispara_na_passada(cofre):
    linha = vencido("tomar o remédio")
    obj = _dono()
    eventos = obj._scheduler_watch_step()
    disparos = [e for e in eventos if e.get("tipo") == "disparo"]
    assert [d["id"] for d in disparos] == [linha["id"]], eventos
    assert disparos[0]["ok"] is True and "voz" in disparos[0]["feito"]
    assert any("remédio" in f for f in obj.dono.falado), obj.dono.falado
    # o segundo tick do mesmo id não repete o recado (marca vem antes da fala)
    antes = list(obj.dono.falado)
    obj._scheduler_watch_step()
    assert obj.dono.falado == antes


def test_ponte_liga_a_boca_e_o_whatsapp_do_dono(cofre):
    vencido("buscar a encomenda",
            gatilho={"tipo": "funcao", "modulo": "core.scheduler",
                     "funcao": "agora", "destino": ["voz", "whatsapp"]})
    obj = _dono()
    obj._scheduler_watch_step()
    assert obj.dono.falado and obj.dono.enviado, "destino duplo virou metade"
    assert scheduler.ponte() == {"voz": True, "whatsapp": True}


def test_ritmo_do_laco_e_de_quinze_segundos(cofre):
    """O ritmo não foi chutado: é o ``time.sleep`` do laço que já existe."""
    import inspect

    fonte = inspect.getsource(main.JarvisLive._task_watch)
    assert "time.sleep(15)" in fonte
    assert main.JarvisLive._SCHEDULER_STEP_SECONDS == 15.0
    assert scheduler.LATE_GRACE_SECONDS >= main.JarvisLive._SCHEDULER_STEP_SECONDS


# ── (b) exceção não derruba o laço nem pula os demais ──────────────────────

def test_gatilho_quebrado_nao_pula_os_demais(cofre):
    podre = vencido("ligar para o eletricista", minutos=2)
    sao = vencido("avisar a faxineira", minutos=1)
    obj = _dono()
    obj.dono.explode = True
    eventos = obj._scheduler_watch_step()
    erros = [e for e in eventos if e.get("tipo") == "erro"]
    assert [e["id"] for e in erros] == [podre["id"]], eventos
    assert "RuntimeError" in erros[0]["motivo"]
    disparos = [e for e in eventos if e.get("tipo") == "disparo"]
    assert [d["id"] for d in disparos] == [sao["id"]], \
        "o registro seguinte foi pulado pelo que quebrou"
    assert any("faxineira" in f for f in obj.dono.falado)


def test_tick_quebrado_nao_derruba_a_passada(cofre, monkeypatch):
    def _explode(**_kw):
        raise OSError("cofre indisponível")

    monkeypatch.setattr(scheduler, "tick", _explode)
    obj = _dono()
    assert obj._scheduler_watch_step() == [], "tick podre vazou exceção"


def test_passada_podre_nao_impede_a_fila_do_dono(cofre, monkeypatch):
    """``_task_watch_step`` continua o caminho da fila mesmo com erro lá."""
    obj = _dono()
    obj._flush_stale_carryover_hot = lambda: None
    monkeypatch.setattr(scheduler, "tick",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("podre")))
    assert obj._task_watch_step() is False


def test_ponte_quebrada_para_a_passada_do_agendador(cofre, monkeypatch):
    """Sem boca ligada não há disparo — e nada é fingido como sucesso."""
    monkeypatch.setattr(main.JarvisLive, "_scheduler_ports",
                        lambda self: (_ for _ in ()).throw(RuntimeError("sem porta")))
    vencido("combinar o conserto")
    obj = _dono()
    assert obj._scheduler_watch_step() == []
    assert scheduler.listar() != [], "o registro sumiu"


# ── (c) hermetismo: o laço em teste nunca encosta no cofre do dono ─────────

def test_teste_nao_toca_o_cofre_real(cofre):
    assert str(REAL_MEMORIA) not in str(scheduler.pasta_das_notas())
    antes = {p.name for p in REAL_MEMORIA.glob("TAREFA-*.md")} \
        if REAL_MEMORIA.is_dir() else set()
    vencido("aviso de teste")
    _dono()._scheduler_watch_step()
    depois = {p.name for p in REAL_MEMORIA.glob("TAREFA-*.md")} \
        if REAL_MEMORIA.is_dir() else set()
    assert antes == depois, "o laço escreveu nota no cofre real do dono"


# ── (d) paridade: barreira da persona e os dois portos pelo caminho novo ───

def test_persona_nao_injeta_gatilho_no_laco(cofre):
    saida = scheduler.registrar(amanha(), "aviso da persona", GATILHO_VOZ,
                                origem="automatica", persona="beatriz")
    assert saida["ok"] is False, saida
    assert saida.get("pedido"), "persona não virou pedido de aprovação"
    assert scheduler.listar() == [], "persona gravou nota no cofre"
    obj = _dono()
    eventos = obj._scheduler_watch_step()
    assert not [e for e in eventos
                if e.get("tipo") in ("disparo", "erro")], eventos
    assert obj.dono.falado == []


def test_o_que_o_portao_pode_o_fluxo_pode(cofre):
    """Registro criado pela action (texto) dispara no laço (tempo) igual."""
    saida = json.loads(ferramenta.schedule(
        {"acao": "add", "titulo": "porta do portão", "quando": amanha(),
         "gatilho": GATILHO_VOZ, "origem": "fala_do_dono"}))
    assert saida["status"] == "ok", saida
    linha = scheduler.linha_de(saida["id"])
    linha["quando"] = (datetime.now().astimezone()
                       - timedelta(minutes=1)).isoformat()
    scheduler._gravar(linha)
    scheduler.carregar(forcar=True)
    eventos = _dono()._scheduler_watch_step()
    disparos = [e for e in eventos if e.get("tipo") == "disparo"]
    assert [d["titulo"] for d in disparos] == ["porta do portão"], eventos
