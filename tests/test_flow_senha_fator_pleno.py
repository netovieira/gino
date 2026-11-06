"""FLUXO REAL: senha digitada é fator PLENO em toda classe privilegiada.

Decisão do dono (voz, 06/11/2026): "eu posso liberar QUALQUER coisa com a
senha ou com a minha voz". A restrição de que reinício, energia e sudo só
aceitam voz foi invenção do agente e está sendo removida. Este teste roda o
fluxo completo por classe, sem monkeypatch no portão:

  1. senha digitada correta com reinício pendente  → reinício CONFIRMADO;
  2. senha digitada correta com energia pendente   → energia CONFIRMADA;
  3. senha digitada correta                        → janela de sudo ABERTA;
  4. voz do dono (voiceprint positivo)             → idem nos três;
  5. senha ERRADA                                  → negado nos três;
  6. texto digitado SEM senha                      → negado nos três;
  7. segunda chamada dentro do TTL                 → aprovada;
  8. TTL expirado                                  → negado;
  9. a senha nunca aparece em log, fala ou memória.

Os gerentes de reinício/energia são dry-run (nada real é reiniciado ou
desligado) e o e-mail é mockado (nenhum SMTP é tocado). A senha deste teste é
segredo de teste: só o placeholder circula nas asserções.
"""
from __future__ import annotations

import pytest

import main
from core import actiongate, email as _mailmod, owner_auth, rootgate
from core.power import PowerManager
from core.restart import RestartManager

# Segredo de teste — placeholder nos relatórios; jamais logar o valor.
_SENHA_TESTE = "senha-de-teste-NAO-LOGAR-4f7b"


class _Clock:
    def __init__(self) -> None:
        self.t = 5000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class _UI:
    muted = False

    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(str(text))


def _live_obj():
    obj = object.__new__(main.JarvisLive)
    obj._owner_text_until = 0.0
    obj._owner_text_channel = ""
    obj._owner_text_fails = 0
    obj._owner_text_locked_until = 0.0
    obj._voice_bootstrap_until = 0.0
    obj._voice_bootstrap_by = ""
    obj.ui = _UI()
    obj.said: list[str] = []
    obj._say_now = lambda text, **kw: obj.said.append(str(text))
    obj.sent: list[tuple] = []
    obj._send_whatsapp_answer = lambda account, chat, text, media="text": (
        obj.sent.append((account, chat, str(text))) or True)
    obj._arm_job_grant = lambda factor: 0.0
    obj._qoder_live = None
    obj.interrupt = lambda: None
    obj._owner_speaker_confirmed = lambda text, pcm, device=None: (
        True, 0.9, "dono (teste)")
    # Gerentes reais nos caminhos de código, mas dry-run: nada de reinício,
    # desligamento ou SMTP de verdade.
    obj._restart_enabled = True
    obj._restart = RestartManager(dry_run=True, logger=lambda _m: None)
    obj._power = PowerManager(dry_run=True, logger=lambda _m: None)
    obj._root_ran = 0
    obj._run_pending_root = lambda: setattr(obj, "_root_ran",
                                           obj._root_ran + 1)
    return obj


@pytest.fixture()
def fluxo(tmp_path, monkeypatch):
    monkeypatch.setattr(owner_auth, "is_configured", lambda: True)
    monkeypatch.setattr(owner_auth, "verify", lambda p: p == _SENHA_TESTE)
    clock = _Clock()
    monkeypatch.setattr(actiongate, "time", clock)
    monkeypatch.setattr(rootgate, "time", clock)
    actiongate.close()
    rootgate.close()
    # E-mail mockado: o portão de senha confirma envio pendente, mas nenhum
    # SMTP é tocado.
    mails = {"pending": None, "confirmed": []}

    def _confirm_send(voice_ok=False):
        if mails["pending"]:
            mails["confirmed"].append(voice_ok)
            return True, "enviado (mock)"
        return False, "nada pendente"

    monkeypatch.setattr(_mailmod, "pending_send", lambda: mails["pending"])
    monkeypatch.setattr(_mailmod, "confirm_send", _confirm_send)
    obj = _live_obj()
    yield obj, clock, mails
    actiongate.close()
    rootgate.close()


def _senha(obj, valor: str | None = None) -> bool:
    return main.JarvisLive._owner_text_gate(
        obj, f"senha: {valor if valor is not None else _SENHA_TESTE}",
        "whatsapp", account="principal", chat="5511")


# ── 1. senha → reinício ─────────────────────────────────────────────────────

def test_senha_confirma_reinício_pendente(fluxo):
    obj, clock, mails = fluxo
    obj._restart.request_assistant("atualizações")
    assert obj._restart.pending() is not None
    assert _senha(obj) is True
    # O pedido deixou de estar pendente: a senha confirmou (dry run — nada
    # reiniciou de verdade).
    assert obj._restart.pending() is None
    assert any("confirmados: reinício pendente" in log
               for log in obj.ui.logs), obj.ui.logs
    # a senha em si nunca foi logada
    assert all(_SENHA_TESTE not in log for log in obj.ui.logs)


def test_senha_errada_nao_confirma_reinício(fluxo):
    obj, clock, mails = fluxo
    obj._restart.request_assistant("atualizações")
    assert _senha(obj, "senha-errada-x") is True
    assert obj._restart.pending() is not None   # pedido mantido, nada aberto
    assert rootgate.authorized() is False
    assert actiongate.authorized() is False


def test_texto_sem_senha_nao_confirma_reinício(fluxo):
    obj, clock, mails = fluxo
    obj._restart.request_assistant("atualizações")
    # texto digitado comum (mesmo dizendo a frase) não é fator: sem senha e
    # sem voz, o portão de texto devolve False e o pedido continua parado.
    assert main.JarvisLive._owner_text_gate(obj, "confirmo reiniciar",
                                            "whatsapp", account="principal",
                                            chat="5511") is False
    assert obj._restart.pending() is not None
    assert rootgate.authorized() is False


# ── 2. senha → energia ──────────────────────────────────────────────────────

def test_senha_confirma_energia_pendente(fluxo):
    obj, clock, mails = fluxo
    obj._power.request("shutdown", "pedido do dono")
    assert obj._power.pending() is not None
    assert _senha(obj) is True
    assert obj._power.pending() is None
    assert all(_SENHA_TESTE not in log for log in obj.ui.logs)


def test_senha_errada_nao_confirma_energia(fluxo):
    obj, clock, mails = fluxo
    obj._power.request("shutdown", "pedido do dono")
    assert _senha(obj, "senha-errada-x") is True
    assert obj._power.pending() is not None


# ── 3. senha → janela de sudo ───────────────────────────────────────────────

def test_senha_abre_janela_de_sudo(fluxo):
    obj, clock, mails = fluxo
    assert rootgate.authorized() is False
    assert _senha(obj) is True
    assert rootgate.authorized() is True
    # TTL da janela de sudo: expirado → negado.
    clock.advance(rootgate.WINDOW_SECONDS + 1.0)
    assert rootgate.authorized() is False


def test_senha_errada_nao_abre_sudo(fluxo):
    obj, clock, mails = fluxo
    assert _senha(obj, "senha-errada-x") is True
    assert rootgate.authorized() is False


# ── 4. voz do dono → idem (reinício/energia; sudo pela janela do rootgate) ──

def test_voz_do_dono_confirma_reinício(fluxo):
    obj, clock, mails = fluxo
    obj._restart.request_assistant("atualizações")
    obj._owner_voice_ok = lambda pcm, **kw: True
    obj._log_heard = lambda text: None
    handled = main.JarvisLive._handle_restart_confirmation(
        obj, "confirmo reiniciar", pcm=b"\x01\x02" * 512)
    assert handled is True
    assert obj._restart.pending() is None


def test_voz_do_dono_confirma_energia(fluxo):
    obj, clock, mails = fluxo
    obj._power.request("shutdown", "pedido do dono")
    obj._is_owner_pcm = lambda pcm, device=None: True
    obj._log_heard = lambda text: None
    handled = main.JarvisLive._handle_power_confirmation(
        obj, "confirmo desligar", pcm=b"\x01\x02" * 512)
    assert handled is True
    assert obj._power.pending() is None


def test_voz_desconhecida_nao_confirma_reinício(fluxo):
    obj, clock, mails = fluxo
    obj._restart.request_assistant("atualizações")
    obj._owner_voice_ok = lambda pcm, **kw: False
    obj._log_heard = lambda text: None
    handled = main.JarvisLive._handle_restart_confirmation(
        obj, "confirmo reiniciar", pcm=b"\x01\x02" * 512)
    assert handled is True
    assert obj._restart.pending() is not None


# ── 7/8. TTL da janela aberta por senha ─────────────────────────────────────

def test_segunda_chamada_dentro_do_ttl_aprovada(fluxo):
    obj, clock, mails = fluxo
    assert _senha(obj) is True
    assert actiongate.confirm("Confirm 'send_message'", "x") is True
    # janela genérica expira; a janela de envio aberta pela senha cobre o
    # mesmo escopo até o TTL dela.
    clock.advance(actiongate.WINDOW_SECONDS + 60.0)
    assert actiongate.confirm("Confirm 'send_message'", "x") is True
    # expirado o TTL de envio → negado.
    clock.advance(actiongate.CONSENT_TTL_SECONDS)
    assert actiongate.confirm("Confirm 'send_message'", "x") is False


# ── e-mail: mesma regra, fator pleno (mock; nada sai) ───────────────────────

def test_senha_confirma_email_pendente(fluxo):
    obj, clock, mails = fluxo
    mails["pending"] = {"to": "a@b.c", "subject": "s", "body": "x",
                        "account": "principal"}
    assert _senha(obj) is True
    assert mails["confirmed"] and mails["confirmed"][-1] is True


# ── 9. não vazamento ────────────────────────────────────────────────────────

def test_senha_nao_vaza(fluxo):
    obj, clock, mails = fluxo
    obj._restart.request_assistant("atualizações")
    _senha(obj)
    raw = "\n".join(list(obj.ui.logs) + list(obj.said)
                    + [t for (_a, _c, t) in obj.sent])
    assert _SENHA_TESTE not in raw, "A SENHA VAZOU em log/resposta"
