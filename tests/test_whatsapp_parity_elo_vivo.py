"""Elo vivo da paridade: a ação de áudio existe no PROCESSO, não só no teste.

Padrão pago duas vezes neste projeto (UP-8: 214 linhas ``hook=ausente``; UP-10:
evento sem chamador): o mecanismo estava pronto, testado, e o boot real nunca
o plugava. Aqui a afirmação é feita no ``core/dev_audit.py`` — a auditoria que
roda em produção, no boot, e que hoje tem duas sondas para esta entrega:

  ``whatsapp.paridade-gancho`` (estática)  — o boot vivo chama a auditoria, e o
  catálogo do plugin é fechado com ``_do_send_voice`` no motor único;
  ``whatsapp.paridade-viva``   (viva)      — a linha de evento que ESTE processo
  escreveu desde o início do boot prova a paridade, com as flags ligadas.

O teste fecha o ciclo escrevendo e lendo as mesmas coisas: chama o método real
``JarvisLive._whatsapp_parity_audit`` (o mesmo que ``_start_whatsapp`` chama em
``main.py``), pega a linha que ele gerou, coloca num activity.log temporário e
exige que a sonda viva diga ``ok``. Se alguém mudar a frase do boot sem mudar a
sonda (ou o contrário), o elo quebra aqui — é o único modo honesto de provar que
a auditoria enxerga o que o boot fala.

Nada deste arquivo escreve no ``logs/activity.log`` do dono nem toca o WhatsApp.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import main
from core import dev_audit

REAL_ACTIVITY = Path(main.__file__).resolve().parent / "logs" / "activity.log"


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch):
    """Sondas cegas para o log real: root = repositório, log = temporário.

    O ``logs/activity.log`` do dono é compartilhado com outros testes da mesma
    sessão (a ponte e os fluxos escrevem nele), então a prova de isolamento é
    o caminho configurado — nunca o tamanho do arquivo real, que daria falso
    negativo por culpa de vizinho.
    """
    epoch0 = dev_audit._BOOT_EPOCH_CFG
    root0 = dev_audit._ROOT
    log0 = dev_audit._ACTIVITY_LOG
    dev_audit.configure(activity_log=tmp_path / "activity.log")
    dev_audit.reset_cache()
    assert dev_audit._ACTIVITY_LOG != REAL_ACTIVITY
    yield
    monkeypatch.undo()
    dev_audit.configure(activity_log=dev_audit.DEFAULT_ACTIVITY_LOG,
                        root=root0, boot_epoch=epoch0)
    dev_audit.reset_cache()
    assert dev_audit._ACTIVITY_LOG == log0 == dev_audit.DEFAULT_ACTIVITY_LOG
    assert dev_audit._ROOT == root0


def _stamp(line: str, *, when: float | None = None) -> str:
    """Linha no formato do activity.log (timestamp + corpo de evento)."""
    return f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when))}] {line}"


def _write_log(tmp_lines: list[str]) -> None:
    log = dev_audit._ACTIVITY_LOG
    log.write_text("\n".join(tmp_lines) + "\n", encoding="utf-8")


class _UI:
    """Captura o que o boot escreveria no log (sem UI, sem disco real)."""

    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, text):
        self.lines.append(str(text))


def _boot_line() -> str:
    """A linha que o BOOT VIVO produz hoje, gerada pelo método real de main.py.

    Vem com as flags de meta do Activity Log appended (``[audio={...}]``) — é
    assim que o log do dono grava linha de sistema, e é assim que a sonda lê.
    """
    ui = _UI()
    main.JarvisLive._whatsapp_parity_audit(type("S", (), {"ui": ui})())
    assert ui.lines, "_whatsapp_parity_audit nao escreveu nenhuma linha"
    return ui.lines[-1]


# ── as duas sondas existem e estão registradas ──────────────────────────────

def test_sondas_registradas():
    ids = {a.id for a in dev_audit.REGISTER}
    assert {"whatsapp.paridade-gancho", "whatsapp.paridade-viva"} <= ids
    for a in dev_audit.REGISTER:
        if a.id in ("whatsapp.paridade-gancho", "whatsapp.paridade-viva"):
            assert a.probe_type in ("estatica", "viva")
            assert a.claims, "afirmação sem texto não é afirmação"


# ── estática: o gancho está no boot, e o plugin tem a rota de áudio ─────────

def test_sonda_estatica_verde_no_codigo_atual():
    verdict, reason = dev_audit._sonda_whatsapp_paridade_gancho()
    assert verdict == dev_audit.OK, reason


def _mirror(tmp_path: Path, *, main_src: str | None = None,
            plugin_src: str | None = None) -> Path:
    """Cópia mínima do projeto para a sonda ler: o resto vem do original."""
    root = tmp_path / "projeto"
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text(
        main_src if main_src is not None
        else (dev_audit.BASE_DIR / "main.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    (root / "plugins" / "whatsapp_control.py").write_text(
        plugin_src if plugin_src is not None
        else (dev_audit.BASE_DIR / "plugins" / "whatsapp_control.py")
        .read_text(encoding="utf-8"), encoding="utf-8")
    return root


def test_sonda_estatica_pega_boot_sem_gancho(monkeypatch, tmp_path):
    """Se ``_start_whatsapp`` parar de chamar a auditoria, a sonda falha."""
    src = (dev_audit.BASE_DIR / "main.py").read_text(encoding="utf-8")
    assert "self._whatsapp_parity_audit()" in src
    root = _mirror(tmp_path, main_src=src.replace(
        "self._whatsapp_parity_audit()", "pass"))
    monkeypatch.setattr(dev_audit, "_ROOT", root)
    verdict, reason = dev_audit._sonda_whatsapp_paridade_gancho()
    assert verdict == dev_audit.FALHA, reason
    assert "nao chama a auditoria" in reason


def test_sonda_estatica_pega_catalogo_reaberto(monkeypatch, tmp_path):
    """Com o ``else`` do fallback de volta na rota, a sonda tem que dizer falha."""
    real = (dev_audit.BASE_DIR / "plugins" / "whatsapp_control.py") \
        .read_text(encoding="utf-8")
    root = _mirror(tmp_path, plugin_src=real.replace(
        "if canonical is None:", "if False and canonical is None:"))
    monkeypatch.setattr(dev_audit, "_ROOT", root)
    verdict, reason = dev_audit._sonda_whatsapp_paridade_gancho()
    assert verdict == dev_audit.FALHA, reason
    assert "action desconhecida" in reason


# ── viva: a linha do boot é o que a sonda procura ───────────────────────────

def test_sonda_viva_verde_com_a_linha_do_boot_real():
    line = _boot_line()
    assert "paridade de mecanismos" in line and line.startswith("SYS:")
    assert "send_voice=1" in line, "o boot tem que dizer que a ação existe"
    _write_log([_stamp(line)])
    dev_audit._BOOT_EPOCH_CFG = time.time() - 60
    dev_audit.reset_cache()
    verdict, reason = dev_audit._sonda_whatsapp_paridade_viva()
    assert verdict == dev_audit.OK, reason


def test_sonda_viva_falha_quando_o_boot_nao_registrou_nada():
    _write_log([_stamp("SYS: outra coisa qualquer")])
    dev_audit._BOOT_EPOCH_CFG = time.time() - 60
    dev_audit.reset_cache()
    verdict, reason = dev_audit._sonda_whatsapp_paridade_viva()
    assert verdict == dev_audit.FALHA
    assert "nao registrou" in reason


def test_sonda_viva_falha_quando_a_rota_esta_quebrada():
    _write_log([_stamp("SYS: [WhatsApp] paridade de mecanismos: send_voice=0 "
                       "catalogo_fechado=1 acoes=11 motor_unico=1")])
    dev_audit._BOOT_EPOCH_CFG = time.time() - 60
    dev_audit.reset_cache()
    verdict, _ = dev_audit._sonda_whatsapp_paridade_viva()
    assert verdict == dev_audit.FALHA


def test_sonda_viva_sem_log_e_nao_verificado():
    dev_audit.reset_cache()
    verdict, reason = dev_audit._sonda_whatsapp_paridade_viva()
    assert verdict == dev_audit.NV
    assert "log" in reason


def test_linha_antiga_do_boot_nao_vale_prova():
    """Escrevida antes deste processo → fora da janela → nao_verificado.

    ``dev_audit`` trata ausência de dado como prova de nada (nunca verde), e é
    exatamente por isso que o teste do elo vivo precisa de uma linha desta
    geração: a paridade de um boot anterior não diz nada sobre este.
    """
    line = _boot_line()
    _write_log([_stamp(line, when=time.time() - 7200)])
    dev_audit._BOOT_EPOCH_CFG = time.time()
    dev_audit.reset_cache()
    verdict, _ = dev_audit._sonda_whatsapp_paridade_viva()
    assert verdict == dev_audit.NV


# ── o método do boot nunca derruba o boot ──────────────────────────────────

def test_auditoria_de_boot_nao_estoura(monkeypatch):
    """Catálogo ilegível: a linha diz nao_verificado e o boot segue."""
    from plugins import whatsapp_control as wa
    monkeypatch.setattr(wa, "ACTION_ALIASES", object())
    ui = _UI()
    line = main.JarvisLive._whatsapp_parity_audit(type("S", (), {"ui": ui})())
    assert "nao_verificado" in line, line
    assert ui.lines and line in ui.lines[0], \
        "a recusa também tem que ir para o log (com as flags de meta)"
    assert "chan=sistema" in ui.lines[0] or '"chan":"sistema"' in ui.lines[0]


def test_o_erro_de_write_log_nao_derruba_o_boot(monkeypatch):
    class _BoomUI:
        def write_log(self, _text):
            raise RuntimeError("UI sem janela")
    seen: list[str] = []
    monkeypatch.setattr(dev_audit, "_LOG", seen.append)
    line = main.JarvisLive._whatsapp_parity_audit(type("S", (), {"ui": _BoomUI})())
    assert "paridade de mecanismos" in line


# ── o boot vivo chama mesmo a auditoria (rota _start_whatsapp, não o AST) ───

def test_start_whatsapp_grava_a_paridade_no_log_do_boot(monkeypatch):
    """Executa ``JarvisLive._start_whatsapp`` de verdade com o WhatsApp stubado.

    Ler o AST prova que a linha está no arquivo; rodar o método prova que ela
    sai no processo — é a diferença entre o UP-8 verde em teste e mudo no vivo.
    A linha gerada é a mesma que a sonda viva procura, e a sonda aprovada é a
    conclusão do ciclo.
    """
    from core import whatsapp as wa_core

    class _Live(main.JarvisLive):
        def __init__(self):                     # sem __init__ pesado do app
            self.ui = _UI()

        def _whatsapp_startup_notice(self, states):
            return []

    monkeypatch.setattr(wa_core, "connect_all", lambda on_message=None: [])
    monkeypatch.setattr(wa_core, "status",
                        lambda account=None: {"qr_path": "", "connected": True,
                                              "number": ""})
    monkeypatch.setattr(wa_core, "ffmpeg_ready", lambda: True)
    live = _Live()
    live._start_whatsapp()

    parity = [ln for ln in live.ui.lines if "paridade de mecanismos" in ln]
    assert len(parity) == 1, f"o boot escreveu {parity}"
    assert "send_voice=1" in parity[0] and "catalogo_fechado=1" in parity[0]
    _write_log([_stamp(parity[0])])
    dev_audit._BOOT_EPOCH_CFG = time.time() - 60
    dev_audit.reset_cache()
    assert dev_audit._sonda_whatsapp_paridade_viva()[0] == dev_audit.OK
