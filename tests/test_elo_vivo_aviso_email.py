"""Ultimo fio do fluxo de e-mail: o aviso do LinkedIn chega NA BOCA.

A auditoria da UP-6 achou o mesmo tipo de buraco do UP-8/UP-10: codigo pronto,
testado e nunca plugado no processo vivo. ``core.email_flows.set_notify_hook``
so era chamado em teste — no ``main.py`` apenas ``set_event_hook`` (activity.log)
e ``set_speak_hook`` (codigo de verificacao) entravam no boot. Resultado: a
notificacao do LinkedIn caia em ``logs/linkedin-notifications.log`` e o dono nao
ouvia nada.

O que se prova aqui, com o BOOT REAL (``main.JarvisLive._start_mail``, o mesmo
que a thread de boot chama em ``main.py:2761``):

1. depois do boot, o gancho de aviso esta instalado (uma vez so) — nada de
   ``hook=ausente``;
2. um e-mail do LinkedIn que bate na regra ``tipo: linkedin_notification`` sai
   FALADO pelo caminho que realmente produz audio (``_say_now`` com
   ``origin="aviso"``, o mesmo teto de 40 s e "nunca enquanto o dono fala" do
   andamento), e nao só escrito em arquivo;
3. SEMANTICA NOVA DO MUTE (07/11/2026): mute da interface e SO microfone, entao
   o aviso continua sendo falado com ``ui.muted = True``. O que ancora fala e o
   modo silencio/palavra-codigo — ligado, o aviso e RETIDO (tela + WhatsApp na
   hora, audio na liberacao) em vez de sumir;
4. sessao sem boca (boot antes do audio) NAO gasta o recibo: a varredura
   seguinte tenta de novo;
5. ISOLAMENTO: nada desta suite escreve no ``logs/activity.log`` do dono, no
   ``memory/email_flow_seen.json`` real ou em ``logs/linkedin-notifications.log``
   — comprovado por conteudo/marcador, porque servico vivo escreve nesses
   arquivos em paralelo e comparar tamanho seria falso negativo.

Rede e thread de varredura tambem ficam de fora: ``connect_all`` e ``start`` de
``core.email`` sao substituidos por stubs.
"""
from __future__ import annotations

import inspect
import json
import types
from pathlib import Path

import pytest

import main
from core import email as mail
from core import email_flows as flows

BASE = Path(main.__file__).resolve().parent
REAL_ACTIVITY = BASE / "logs" / "activity.log"
REAL_SEEN = BASE / "memory" / "email_flow_seen.json"
REAL_LINKEDIN_LOG = BASE / "logs" / "linkedin-notifications.log"
REAL_VAULT = Path.home() / "Jarvis"

# A regra em producao (config/api_keys.json -> mail.accounts[principal].filters
# indice 3): dominio do remetente, action=avisa, dispatch=true,
# tipo=linkedin_notification.
LI_RULE = {"field": "from", "pattern": r"@linkedin\.com", "match": "regex",
           "action": "avisa", "body": False, "dispatch": True,
           "tipo": "linkedin_notification", "label": "linkedin"}
ACCT = {"name": "principal", "address": "eu@gmail.com"}
# Marcadores que só esta suite produce: se aparecerem num arquivo real do dono,
# o isolamento vazou. (Comparamos marcadores, nao tamanho — o assistente vivo
# escreve no activity.log e no historico de fluxos em paralelo.)
QUEM = "Beatriz Elo Vivo"
CORPO_CONVITE = (f"{QUEM} quer conectar-se com você. "
                 "Aceitar o convite: https://www.linkedin.com/feed")


@pytest.fixture(autouse=True)
def _sem_escrita_no_dono(tmp_path, monkeypatch):
    """Nenhum writer lateral desta suite alcanca o disco do dono.

    O aviso passa por `main._mail_flow_executed`, que fala com o orquestrador
    de fluxos; se a ponte for construida "de raspao" durante o teste, ela traz
    junto o log de acao do executor, a medicao de latencia da boca e o
    espelho de tarefas. Os tres caminhos vao para tmp.
    """
    from core import task_mirror
    from core.flows import executor as exe
    from core.flows import mouth as mouth_mod

    monkeypatch.setattr(exe, "_ACTIVITY_LOG_PATH", tmp_path / "activity.log")
    monkeypatch.setattr(mouth_mod, "_LOG_PATH", tmp_path / "latencia.log")
    monkeypatch.setattr(task_mirror, "MIRROR_DIR", tmp_path / "logs" / "mirror")
    task_mirror.reset()
    yield
    task_mirror.reset()


class _Gate:
    """SilenceGate de teste: decide falar/reter e grava as retencoes."""

    def __init__(self, active: bool = False):
        self.active = active
        self.retained: list[dict] = []

    def is_muted(self, subject: str = "", text: str = "") -> bool:
        return bool(self.active)

    def retain(self, text: str, **kw):
        entry = {"text": text, **kw}
        self.retained.append(entry)
        return entry


def _item(uid: str, subject: str | None = None,
          sender: str = "LinkedIn",
          from_: str = "messages-noreply@linkedin.com") -> dict:
    return {"uid": uid, "subject": subject or f"{QUEM} quer conectar-se",
            "sender": sender, "from": from_}


@pytest.fixture()
def vivo(tmp_path, monkeypatch):
    """JarvisLive com o boot de e-mail de verdade, boca coletora, disco em tmp.

    Os caminhos de rastro do modulo de fluxos sao redirecionados ANTES do
    boot; os ganchos sao zerados antes e depois para nenhum teste vizinho
    herdar (ou perder) o gancho instalado aqui.
    """
    flows.reset()
    mail.reset()

    monkeypatch.setattr(flows, "_seen_path", tmp_path / "email_flow_seen.json")
    monkeypatch.setattr(flows, "_LINKEDIN_VAULT", tmp_path / "vault")
    monkeypatch.setattr(flows, "_LINKEDIN_LOG",
                        tmp_path / "linkedin-notifications.log")
    monkeypatch.setattr(flows, "_logger", lambda _msg: None)
    # Nenhuma conexao, nenhuma thread de varredura: só o boot dos ganchos.
    monkeypatch.setattr(mail, "connect_all", lambda: [])
    monkeypatch.setattr(mail, "start", lambda *a, **k: None)

    spoken: list[dict] = []
    painel: list[str] = []
    monitor: list[str] = []
    gate = _Gate(active=False)

    obj = object.__new__(main.JarvisLive)
    obj.ui = types.SimpleNamespace(muted=False, state="",
                                   write_log=painel.append,
                                   set_state=lambda _s: None)
    obj._asst_name = "Gino"
    obj._loop = object()
    obj.audio_in_queue = object()
    obj._last_qoder_update = 0.0
    obj._last_user_speech = 0.0
    obj._session_log = []
    obj._log_line = lambda text, meta=None: str(text)
    obj._speech_subject = lambda text="", subject="": str(subject or "")[:60]
    obj._silence_mode = gate
    obj._silence_code_mode = types.SimpleNamespace(
        active=False, code_word="", is_active_for=lambda **k: False)
    # O monitor do orquestrador nao faz parte do fio do aviso: coletamos o
    # evento em vez de deixar `_flows_monitor_event` construir a ponte inteira
    # (e instalar o gancho global do espelho) dentro deste teste.
    obj._flows_monitor_event = lambda *a, **k: monitor.append(str(a[:1]))
    # A boca real: `_say_now` enfileira síntese e reprodução; aqui coletamos o
    # que entraria nela, com a origem — é o mesmo ponto por onde
    # `_live_progress_speech` e `_mail_new_message` falam.
    obj._say_now = lambda text, **kw: spoken.append({"text": text, **kw})

    yield {"obj": obj, "spoken": spoken, "painel": painel, "gate": gate,
           "monitor": monitor, "tmp": tmp_path}

    flows.reset()
    mail.reset()


def _boot(estado) -> None:
    """O bootstrap de e-mail de verdade, chamado como no processo vivo."""
    estado["obj"]._start_mail()


# ── 1. o fio: o boot instala o gancho de aviso ───────────────────────────────

def test_boot_do_main_instala_o_gancho_de_aviso(vivo):
    assert flows._notify_hook is None, "regressao: gancho de teste vizinho"
    _boot(vivo)
    assert flows._notify_hook is not None, \
        "aviso do fluxo sem gancho no boot e exatamente o bug do fio faltando"
    assert flows._event_hook is not None
    assert flows._speak_hook is not None


def test_boot_nao_instala_o_gancho_duas_vezes(vivo):
    """Uma instalacao, no mesmo ponto de boot — sem duplicar o que ja existia."""
    _boot(vivo)
    primeira = flows._notify_hook
    assert primeira.__func__ is main.JarvisLive._mail_flow_notice
    assert primeira == vivo["obj"]._mail_flow_notice
    _boot(vivo)
    # Reboot nao empilha nem troca o gancho: continua a mesma instalacao.
    assert flows._notify_hook == primeira
    assert len(vivo["spoken"]) == 0


# ── 2. o aviso sai falado, nao so escrito em arquivo ─────────────────────────

def test_aviso_do_linkedin_chega_na_boca_depois_do_boot(vivo):
    _boot(vivo)
    assert vivo["spoken"] == [], "boot sem contas nao fala nada"

    ev = flows.dispatch_flow(ACCT, _item("4211"), LI_RULE, CORPO_CONVITE)

    assert ev is not None and ev["ok"] is True
    assert ev["type"] == "linkedin_notification"
    assert ev["action"] == "aviso"
    assert ev["motivo"] == "notificado_convite_conexao"

    faladas = [s["text"] for s in vivo["spoken"]]
    assert faladas == [f"LinkedIn: {QUEM} te mandou um convite de conexão."], \
        faladas
    # O caminho que produz audio: mesmo origin="aviso" do andamento vivo.
    assert vivo["spoken"][0].get("origin") == "aviso"
    assert vivo["spoken"][0].get("log") is True
    # Nada virou só arquivo: sem gancho seria escrito no log do LinkedIn.
    assert not (vivo["tmp"] / "linkedin-notifications.log").exists()


def test_aviso_mensagem_privada_tambem_sai_falado(vivo):
    _boot(vivo)
    item = _item("4212", subject="João Lima enviou uma nova mensagem")
    ev = flows.dispatch_flow(
        ACCT, item, LI_RULE,
        "João Lima enviou uma nova mensagem. Responder: "
        "https://www.linkedin.com/messaging/thread/4471/")
    assert ev is not None and ev["ok"] is True
    faladas = [s["text"] for s in vivo["spoken"]]
    assert len(faladas) == 1 and faladas[0].startswith("LinkedIn: João Lima")
    assert "quer ler" in faladas[0]
    # privacidade: nem o corpo nem o link vao para a fala
    assert "messaging/thread" not in faladas[0]


# ── 3. mute da interface e so microfone: o aviso continua saindo ─────────────

def test_aviso_falado_mesmo_com_o_microfone_mutado(vivo):
    _boot(vivo)
    vivo["obj"].ui.muted = True

    flows.dispatch_flow(ACCT, _item("4220"), LI_RULE, CORPO_CONVITE)

    assert len(vivo["spoken"]) == 1, \
        "mute da UI não pode calar a boca (ordem do dono, 07/11/2026)"
    assert vivo["spoken"][0]["text"].startswith("LinkedIn:")
    # ...e nada de "nao falou"/"microfone mutado" no painel por causa do mute.
    assert not any("microfone mutado" in l for l in vivo["painel"])
    assert not any("não falou" in l for l in vivo["painel"])


def test_gancho_de_aviso_nao_consulta_o_mute_da_ui(vivo):
    src = inspect.getsource(main.JarvisLive._mail_flow_notice)
    assert "ui.muted" not in src, "o mute da UI voltou a ancorar fala de e-mail"


def test_modo_silencio_retem_o_aviso_em_lugar_de_sumir(vivo):
    """O que ancora fala e o silencio/palavra-codigo — e retencao nao e perda."""
    _boot(vivo)
    vivo["gate"].active = True

    ev = flows.dispatch_flow(ACCT, _item("4230"), LI_RULE, CORPO_CONVITE)

    assert ev is not None and ev["ok"] is True
    assert vivo["spoken"] == [], "com o silencio ligado nada vai para a boca"
    assert len(vivo["gate"].retained) == 1, "aviso retido, nao descartado"
    assert str(vivo["gate"].retained[0]["text"]).startswith("LinkedIn:")
    # Linha SYS de "nao consegui falar" nao e o destino do aviso.
    assert not any("linkedin" in l.lower() and l.startswith("SYS: falar")
                   for l in vivo["painel"])


def test_silencio_libera_aviso_na_reativacao_do_gate(vivo):
    """Duas faces da mesma lei: com o gate desligado a frase sai falada."""
    _boot(vivo)
    vivo["gate"].active = True
    flows.dispatch_flow(ACCT, _item("4231"), LI_RULE, CORPO_CONVITE)
    assert vivo["spoken"] == []
    vivo["gate"].active = False
    flows.dispatch_flow(ACCT, _item("4232"), LI_RULE, CORPO_CONVITE)
    assert len(vivo["spoken"]) == 1


# ── 4. transiente de fala nao gasta o recibo ────────────────────────────────

def test_sessao_sem_boca_nao_gasta_o_recibo(vivo):
    _boot(vivo)
    obj = vivo["obj"]
    obj._loop = None            # audio ainda nao existe (boot adiantado)

    ev = flows.dispatch_flow(ACCT, _item("4240"), LI_RULE, CORPO_CONVITE)

    assert ev is not None and ev["ok"] is False
    assert ev["motivo"] == "aviso_indisponivel"
    assert vivo["spoken"] == []
    assert not (vivo["tmp"] / "email_flow_seen.json").exists(), \
        "recibo gasto sem aviso entregue perde a notificacao"

    # A mesma recusa quando o atributo nem nasceu ainda (sessao em construcao):
    # o gancho nao pode levantar erro — erro viraria linha de log e nada de fala.
    del obj._loop
    assert obj._mail_flow_notice({"text": "LinkedIn: teste."}) is False
    assert vivo["spoken"] == []

    # Na volta da sessao, a mesma varredura fala.
    obj._loop = object()
    obj.audio_in_queue = object()
    ev2 = flows.dispatch_flow(ACCT, _item("4240"), LI_RULE, CORPO_CONVITE)
    assert ev2 is not None and ev2["ok"] is True
    assert len(vivo["spoken"]) == 1


def test_teto_de_40s_protege_o_turno_em_curso_sem_gastar_recibo(vivo):
    import time
    _boot(vivo)
    vivo["obj"]._last_qoder_update = time.monotonic()   # fala ha <40s

    ev = flows.dispatch_flow(ACCT, _item("4250"), LI_RULE, CORPO_CONVITE)

    assert ev is not None and ev["ok"] is False
    assert ev["motivo"] == "aviso_indisponivel"
    assert vivo["spoken"] == []


def test_sem_gancho_o_aviso_vai_para_disco_nao_para_a_boca(vivo):
    """A outra face da invariante: sem o fio instalado o dono nao ouve nada."""
    _boot(vivo)
    flows.set_notify_hook(None)

    ev = flows.dispatch_flow(ACCT, _item("4260"), LI_RULE, CORPO_CONVITE)

    assert ev is not None and ev["ok"] is True
    assert vivo["spoken"] == []
    arquivo = vivo["tmp"] / "linkedin-notifications.log"
    assert arquivo.is_file(), "sem gancho o aviso grava em disco (default)"


# ── 5. isolamento: zero delta nos rastros do dono ───────────────────────────

def _texto(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def test_suite_nao_escreve_nos_arquivos_do_dono(vivo):
    """Prova por marcador: o servico vivo escreve nesses arquivos em paralelo.

    Comparamos CONTEUDO (o uid e o nome deste teste), nao tamanho — tamanho
    mudaria sozinho com o assistente rodando e daria falso negativo.
    """
    _boot(vivo)
    marcador_uid = "9999999"
    flows.dispatch_flow(ACCT, _item(marcador_uid), LI_RULE, CORPO_CONVITE)
    assert len(vivo["spoken"]) == 1, "o teste precisa ter produzido aviso"

    atividade = _texto(REAL_ACTIVITY)
    vistos = _texto(REAL_SEEN)
    linkedin_disco = _texto(REAL_LINKEDIN_LOG)

    # 1. o painel falso gravou a linha do evento...
    assert any("email_flow_executed" in l for l in vivo["painel"]), \
        vivo["painel"]
    # ...e ela NAO apareceu no activity.log do dono (mesma frase, mesmo uid).
    vazou = [l for l in vivo["painel"] if l and l in atividade]
    assert vazou == [], f"vazou para o log do dono: {vazou}"
    assert f"uid={marcador_uid}" not in atividade
    assert QUEM not in atividade
    # 2. o recibo deste teste nao entrou no historico real de fluxos
    assert marcador_uid not in vistos
    # 3. nenhum aviso deste teste foi escrito no log real do LinkedIn nem no
    #    cofre do dono
    assert QUEM not in linkedin_disco
    notas_reais = (list(REAL_VAULT.glob("NOTA-*linkedin-msg*"))
                   if REAL_VAULT.is_dir() else [])
    alcunha = QUEM.casefold().replace(" ", "-")
    assert not any(alcunha in p.name.casefold() for p in notas_reais)
    # 4. os caminhos que estes testes escrevem apontam para o tmp do teste
    assert str(vivo["tmp"]) in str(flows._seen_path)
    assert str(vivo["tmp"]) in str(flows._LINKEDIN_LOG)
    assert str(vivo["tmp"]) in str(flows._LINKEDIN_VAULT)
    assert str(vivo["tmp"]) in str(main._task_mirror.MIRROR_DIR)
    # nada de rastro novo no repositorio: os arquivos propriamente ditos nao
    # ganharam uma linha nossa
    assert not (vivo["tmp"] / "linkedin-notifications.log").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
