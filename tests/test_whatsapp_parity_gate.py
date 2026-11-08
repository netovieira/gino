"""Paridade do portão: a nota de voz sai pelo MESMO gate da mensagem escrita.

Ordem do dono (07/11/2026): "se um gate pode você pode". O portão de envio é
o ``core/actiongate.confirm`` ligado ao ``ToolBridge`` — ele não olha a
``action``, olha a FERRAMENTA. Então ``send`` e ``send_voice`` são, por
construção, o mesmo tier e a mesma autorização. Este arquivo não prova isso
por comentário: bate na porta de verdade (``/call`` do bridge real) e exige
que os dois caminhos digam a mesma coisa nos três estados do dono.

Estados testados, sempre iguais para texto e áudio:

  * sem autorização nenhuma  → HTTP 403 ``denied`` (e nada é enviado);
  * janela aberta por VOZ    → aprovado, e a rota correspondente roda;
  * janela aberta por SENHA  → aprovado, mesmo efeito, mesmo recibo.

Nenhum envio real acontece: a camada de mídia/escrita do plugin é falsa.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core import actiongate
from core.owner_auth import FACTOR_TEXT, FACTOR_VOICE
from core.tool_bridge import ACTION_TIERS, ToolBridge, action_risk_tier
from plugins import whatsapp_control as wa

from tests.test_whatsapp_parity_dispatch import OWNER_NUMBER, FakeWA

TOKEN = "gate-parity-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Registry:
    """O suficiente do catálogo: uma ferramenta, ``whatsapp_control``."""

    def get_tool_declarations(self):
        return [{"name": "whatsapp_control", "description": "wa",
                 "parameters": {"type": "OBJECT"}}]

    def has(self, name):
        return name == "whatsapp_control"

    def run(self, name, parameters, ctx=None):
        return wa.run(parameters)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Bridge real + gate real + envio falso. Retorna (client, envio, recibos)."""
    w = FakeWA(accounts=[{"name": "principal", "uuid": "u1", "default": True,
                          "number": OWNER_NUMBER, "paired": True}])
    monkeypatch.setattr(wa, "whatsapp", w)
    monkeypatch.setattr(wa, "get_plugin_setting",
                        lambda ns, key, default=None: (
                            OWNER_NUMBER if key == "owner_chat" else default))
    monkeypatch.setattr(wa.voice_note, "synthesize",
                        lambda text, **kw: ("/tmp/nota.ogg", text))

    audit = tmp_path / "bridge_audit.jsonl"
    bridge = ToolBridge(Registry(), dispatch=None, token=TOKEN,
                        confirm=actiongate.confirm,
                        authorized=actiongate.authorized,
                        audit_path=audit, pending_path=tmp_path / "pending.json",
                        logger=lambda _m: None)
    # Chave de recibo injetada: nada aqui lê config de segredo real.
    bridge._receipts_key = b"chave-de-teste-paridade"
    client = TestClient(bridge.app)
    actiongate.close()
    yield client, w, audit
    client.close()
    actiongate.close()
    actiongate.clear_blocked()


def _call(client, args):
    return client.post("/call", json={"name": "whatsapp_control", "args": args},
                       headers=AUTH)


SEND = {"action": "send", "to": "Joao", "text": "chego em dez minutos"}
VOICE = {"action": "send_voice", "to": "Joao", "text": "chego em dez minutos"}


# ── o tier é o MESMO (é isso que makes o portão único) ─────────────────────

def test_texto_e_audio_no_mesmo_tier():
    assert ACTION_TIERS["whatsapp_control"] == "medium"
    assert action_risk_tier("whatsapp_control", SEND) \
        == action_risk_tier("whatsapp_control", VOICE) == "medium"


# ── negado sem autorização, nos dois caminhos ──────────────────────────────

@pytest.mark.parametrize("args", [SEND, VOICE])
def test_sem_autorizacao_e_negado_e_nada_sai(env, args):
    client, w, _audit = env
    r = _call(client, args)
    assert r.status_code == 403, r.text
    assert r.json()["status"] == "denied"
    assert r.json()["tier"] == "medium"
    assert w.sent == [] and w.medias == [], "negado nao envia"


# ── aprovado por VOZ do dono, nos dois caminhos ────────────────────────────

@pytest.mark.parametrize("args", [SEND, VOICE])
def test_voz_do_dono_aprova(env, args):
    client, w, _audit = env
    actiongate.open_window(factor=FACTOR_VOICE)
    try:
        r = _call(client, args)
        assert r.status_code == 200, r.text
        assert r.json()["approved"] is True
    finally:
        actiongate.close()
    if args["action"] == "send_voice":
        assert len(w.medias) == 1 and w.sent == []
    else:
        assert len(w.sent) == 1 and w.medias == []


# ── aprovado por SENHA digitada, mesmo efeito ──────────────────────────────

@pytest.mark.parametrize("args", [SEND, VOICE])
def test_senha_digitada_aprova_igual_voz(env, args):
    client, w, _audit = env
    actiongate.open_window(factor=FACTOR_TEXT)
    try:
        r = _call(client, args)
        assert r.status_code == 200, r.text
        assert r.json()["approved"] is True
    finally:
        actiongate.close()
    assert (len(w.medias), len(w.sent)) == (
        (1, 0) if args["action"] == "send_voice" else (0, 1))


def test_os_dois_fatores_aceitos_pela_mesma_rota_de_audio(env):
    """A rota chamável de áudio não tem portão próprio: é o gate da ferramenta.

    Sequência: voz aprova → senha aprova → sem fator nenhum nega. A terceira
    recusa é o que prova que nada foi afrouxado para o áudio.
    """
    client, w, _audit = env
    actiongate.open_window(factor=FACTOR_VOICE)
    try:
        assert _call(client, VOICE).status_code == 200
    finally:
        actiongate.close()
    actiongate.open_window(factor=FACTOR_TEXT)
    try:
        assert _call(client, VOICE).status_code == 200
    finally:
        actiongate.close()
    assert _call(client, VOICE).status_code == 403
    assert len(w.medias) == 2, "as duas aprovadas, a recusa de fora"


# ── o recibo HMAC é o mesmo para os dois canais ────────────────────────────

@pytest.mark.parametrize("args", [SEND, VOICE])
def test_recibo_hmac_emitido_para_os_dois(env, args):
    client, _w, audit = env
    actiongate.open_window(factor=FACTOR_TEXT)
    try:
        assert _call(client, args).status_code == 200
    finally:
        actiongate.close()
    lines = [json.loads(ln) for ln in
             audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
    receipts = [d for d in lines if "hmac" in d]
    assert receipts, "execucao aprovada tem que ter prova material"
    assert receipts[-1]["tool"] == "whatsapp_control"
    assert receipts[-1]["hmac"].startswith("sha256:")


def test_negado_nao_gera_recibo(env):
    client, _w, audit = env
    assert _call(client, VOICE).status_code == 403
    lines = [json.loads(ln) for ln in
             audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert not [d for d in lines if "hmac" in d], "recibo e prova de execução"


# ── a pré-autorização autônoma também é paritária (mesmo escopo, dois formatos) ─

def test_grant_de_resposta_cobre_texto_e_voz_mesmo_chat():
    """A janela curta da resposta autônoma vale para ``send`` E para ``send_voice``.

    Espelha a lei do app: o que chega em áudio é respondido em áudio. Recusar
    a nota de voz enquanto o texto do MESMO chat está liberado seria um portão
    paralelo — mais forte para a rota de voz, contra "se um gate pode você
    pode". O escopo não muda: mesma conta, mesmo contato, mesmo TTL.
    """
    actiongate.allow_whatsapp_reply("principal", "Joao", name="Joao")
    try:
        same = {"to": "Joao", "account": "principal"}
        assert actiongate.whatsapp_reply_allowed(
            "whatsapp_control", {**same, "action": "send"})
        assert actiongate.whatsapp_reply_allowed(
            "whatsapp_control", {**same, "action": "send_voice"})
        # o apelido falado pelo modelo é a mesma action (catálogo do plugin)
        assert actiongate.whatsapp_reply_allowed(
            "whatsapp_control", {**same, "action": "nota de voz"})
        # fora do escopo: outro contato, outra action, outra conta
        assert not actiongate.whatsapp_reply_allowed(
            "whatsapp_control", {"to": "Maria", "account": "principal",
                                 "action": "send_voice"})
        assert not actiongate.whatsapp_reply_allowed(
            "whatsapp_control", {"to": "Joao", "account": "principal",
                                 "action": "send_file"})
        assert not actiongate.whatsapp_reply_allowed(
            "whatsapp_control", {"to": "Joao", "account": "trabalho",
                                 "action": "send_voice"})
    finally:
        actiongate.revoke_whatsapp_reply("principal", "Joao")
    assert not actiongate.whatsapp_reply_allowed(
        "whatsapp_control", {"to": "Joao", "account": "principal",
                             "action": "send_voice"}), "revogada nao vale mais"
