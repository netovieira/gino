"""FLUXO REAL: senha digitada no WhatsApp autoriza o ENVIO dentro da janela.

Cobre a falha de 23:07 (13/11/2026): o dono autorizou por senha digitada no
WhatsApp; ``whatsapp_control`` honrou a janela, mas ``send_message`` passou na
primeira chamada (janela genérica de 120 s ainda aberta) e caiu em
"not authorized by the owner's voice yet" nas seguintes, quando só restava a
janela de envio de 15 min — que era cega a ``send_message``.

Casos (na ordem do fluxo, sem monkeypatch no portão):
  1. senha digitada correta → grant armado → send_message APROVADO e enviado
     (envio mockado; nada real sai);
  2. segunda chamada dentro do TTL da janela de envio (> 120 s da janela
     genérica) → APROVADA — mesmo escopo e mesmo TTL de whatsapp_control;
  3. janela expirada → NEGADA;
  4. voz desconhecida (voiceprint negativo) sem grant → voz NÃO arma janela e
     o mesmo pedido é NEGADO; senha errada também não arma nada.
  5. Vazamento: o valor da senha não aparece em log, resposta, activity,
     inbox nem memória.

A senha deste teste é um segredo de teste; ele nunca deve aparecer em log,
relatório ou commit — só o placeholder abaixo circula nas asserções.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from core import actiongate, owner_auth, rootgate
from core.tool_bridge import ToolBridge
from memory import config_manager

# Segredo de teste — placeholder nos relatórios; jamais logar o valor.
_SENHA_TESTE = "senha-de-teste-NAO-LOGAR-9e1c"

TOKEN = "bridge-token-para-teste"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _Clock:
    """Relógio controlado: só monotonic(), como o actiongate usa."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class _Registry:
    def get_tool_declarations(self):
        return [
            {"name": "send_message", "description": "send",
             "parameters": {"type": "OBJECT"}},
            {"name": "whatsapp_control", "description": "wp",
             "parameters": {"type": "OBJECT"}},
        ]

    def has(self, name):
        return any(d["name"] == name for d in self.get_tool_declarations())

    def run(self, name, parameters, ctx=None):
        return f"registry:{name}"


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
    return obj


@pytest.fixture()
def fluxo(tmp_path, monkeypatch):
    """Wiring idêntico ao do app: actiongate real + bridge real, envio mockado."""
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(owner_auth, "is_configured", lambda: True)
    monkeypatch.setattr(owner_auth, "verify",
                        lambda p: p == _SENHA_TESTE)
    # Fator pleno agora confirma também um envio de e-mail pendente; aqui o
    # portão de e-mail é mockado — nenhum SMTP é tocado neste teste.
    from core import email as _mailmod
    monkeypatch.setattr(_mailmod, "pending_send", lambda: None)
    clock = _Clock()
    monkeypatch.setattr(actiongate, "time", clock)
    actiongate.close()
    rootgate.close()

    enviados: list[tuple[str, dict]] = []

    def _dispatch(name, args):
        # MOCK do envio: nada sai de verdade; só registra que passou no portão.
        enviados.append((name, dict(args)))
        return f"Mensagem enviada para {args.get('receiver')}."

    logs: list[str] = []
    bridge = ToolBridge(
        _Registry(), _dispatch, TOKEN,
        logger=logs.append,
        confirm=lambda title, message: actiongate.confirm(title, message),
        authorized=actiongate.authorized,
        pending_path=False,
    )
    client = TestClient(bridge.app)

    def chamar_send(texto: str, receiver: str = "João") -> dict:
        r = client.post("/call", headers=AUTH, json={
            "name": "send_message",
            "args": {"receiver": receiver, "message_text": texto,
                     "platform": "telegram"},
        })
        return {"code": r.status_code, "body": r.json()}

    obj = _live_obj()
    yield obj, clock, chamar_send, enviados, logs
    actiongate.close()
    rootgate.close()
    client.close()


def _armar_senha(obj) -> None:
    assert main.JarvisLive._owner_text_gate(
        obj, f"senha: {_SENHA_TESTE}", "whatsapp",
        account="principal", chat="5511") is True


def test_fluxo_senha_abre_envio_e_expira(fluxo):
    obj, clock, chamar_send, enviados, logs = fluxo

    # 1. senha digitada → grant armado → primeiro envio APROVADO
    _armar_senha(obj)
    r1 = chamar_send("oi, tudo bem?")
    assert r1["code"] == 200, f"caso 1 negado: {r1['body']}"
    assert r1["body"]["approved"] is True
    assert len(enviados) == 1

    # 2. segunda chamada DENTRO do TTL da janela de envio, depois dos 120 s
    #    da janela genérica — é aqui que o defeito caía ("voice yet").
    clock.advance(actiongate.WINDOW_SECONDS + 60.0)
    assert actiongate._window_open() is False   # janela de 120 s expirada
    r2 = chamar_send("reforçando o recado")
    assert r2["code"] == 200, (
        f"caso 2 negado dentro da janela autorizada por senha: {r2['body']}")
    assert r2["body"]["approved"] is True
    assert len(enviados) == 2

    # 3. expirado o TTL da janela de envio → NEGADA
    clock.advance(actiongate.CONSENT_TTL_SECONDS)
    r3 = chamar_send("agora já era")
    assert r3["code"] == 403, f"caso 3 aprovado fora do TTL: {r3['body']}"
    assert r3["body"]["status"] == "denied"
    assert len(enviados) == 2   # nada saiu depois da expiração


def test_voz_desconhecida_sem_grant_e_negada(fluxo):
    obj, clock, chamar_send, enviados, logs = fluxo

    # Voz desconhecida tentando armar pela consent phrase: sem voiceprint de
    # dono o matcher de consentimento não abre janela nenhuma.
    obj._is_speaking = False
    obj._reply_active = False
    obj._looks_like_own_voice = lambda text: False
    obj._owner_speaker_confirmed = lambda text, pcm, device=None: (
        False, 0.10, "voz não identificada como dono")
    recusadas: list = []
    obj._consent_speaker_refused = lambda score, detail, kind: recusadas.append(
        (score, detail, kind))
    armed = main.JarvisLive._try_action_consent(obj, "pode enviar",
                                                pcm=b"\x01\x02" * 512)
    # True aqui só significa "a fala foi consumida, não vira turno"; o que
    # importa é o portão: NENHUMA janela foi aberta pela voz desconhecida.
    assert recusadas, "a recusa da voz desconhecida deveria ser auditada"
    assert actiongate.authorized() is False
    assert actiongate.send_window_factor() == ""

    r = chamar_send("pedido sem grant")
    assert r["code"] == 403
    assert r["body"].get("status") == "denied"
    assert enviados == []   # nada foi enviado por esta voz
    # senha ERRADA também não arma nada:
    actiongate.close()
    assert main.JarvisLive._owner_text_gate(
        obj, "senha: valor-errado-qualquer", "whatsapp",
        account="principal", chat="5511") is True
    assert actiongate.authorized() is False
    assert rootgate.authorized() is False   # senha ERRADA não abre nada


def test_senha_nao_vaza_em_lugar_nenhum(fluxo):
    obj, clock, chamar_send, enviados, logs = fluxo

    # erro de senha (loga recusa), acerto (loga sessão), resposta ao dono:
    main.JarvisLive._owner_text_gate(obj, "senha: errado-tambem-nao-loga",
                                     "ui")
    _armar_senha(obj)
    chamar_send("oi")

    vistos = []
    vistos += obj.ui.logs
    vistos += obj.said
    vistos += [t for (_a, _c, t) in obj.sent]
    vistos += logs
    raw = "\n".join(str(v) for v in vistos)
    assert _SENHA_TESTE not in raw, "A SENHA VAZOU em log/resposta"

    # inbox do WhatsApp grava só o marcador redigido
    from core import whatsapp as wa
    inbox_file = Path("/tmp") / "jarvis-inbox-senha-teste.jsonl"
    if inbox_file.exists():
        inbox_file.unlink()
    acct = type("Acct", (), {"inbox_file": inbox_file, "appended": 0})()
    item = {"text": f"senha: {_SENHA_TESTE}", "chat": "dono"}
    wa._append_inbox(item, acct)
    gravado = inbox_file.read_text(encoding="utf-8")
    assert _SENHA_TESTE not in gravado
    assert owner_auth.REDACTED in gravado
    inbox_file.unlink()


def test_whatsapp_control_e_janelas_por_senha(fluxo):
    """Sem enfraquecer: whatsapp_control honra a janela como antes; a senha
    é fator PLENO (decisão do dono, 06/11/2026) e abre também a janela de
    sudo/root — não existe mais classe em que a senha seja banida."""
    obj, clock, chamar_send, enviados, logs = fluxo
    _armar_senha(obj)
    clock.advance(actiongate.WINDOW_SECONDS + 60.0)
    # whatsapp_control segue dentro da janela de envio (comportamento antigo):
    assert actiongate.confirm("Confirm 'whatsapp_control'", "x") is True
    # fator pleno: a janela de sudo foi aberta pela mesma senha:
    assert rootgate.authorized() is True
