"""Caracterização do dispatch do ``whatsapp_control`` — o fim do fallback silencioso.

Defeito real (07/11/2026, relato do dono): o assistente pediu uma nota de voz,
chamou ``whatsapp_control`` com ``action=send_audio`` — nome que não existia —
e o ``run()`` do plugin tinha um ``else`` que rodava ``recent``. Veio a lista
das últimas conversas. A ferramenta não devolveu erro, o modelo não teve como
saber, e o pedido de áudio virou texto na conta errada. É a classe de defeito
mais perigosa do projeto: **resposta que parece sucesso quando nada aconteceu**.

Estes testes fixam os dois lados da linha:

1. ORÁCULO LEGADO — a cadeia de ``HEAD`` (``git show HEAD:plugins...``) é
   reproduzida literal e executada: ``send_audio``/``voice``/lixo caem no
   ``recent``. Se um dia o catálogo reabrir, o teste mostra o que estava errado.
2. COMPORTAMENTO ATUAL — a mesma entrada no ``run()`` de verdade devolve ERRO
   nomeando a action e as actions válidas, e ``recent`` NUNCA é tocado (a
   função que lista mensagens está armada para explodir se for chamada).

Também a ordem dos checks (nada de envio antes de saber o destino e a conta),
e a proibição de converter áudio→texto por conta própria.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from plugins import whatsapp_control as wa

BASE = Path(__file__).resolve().parent.parent


OWNER_NUMBER = "5511999990000"
CONTACTS = {"Joao": "5521999998888"}


class Boom(Exception):
    """Qualquer efeito colateral atingido fora do permitido neste teste."""


class FakeWA:
    """A camada de envio: registra chamadas, e ``recent`` é uma armadilha."""

    def __init__(self, accounts=None, send_ok=True):
        self.accounts = accounts if accounts is not None else [
            {"name": "principal", "uuid": "u1", "default": True,
             "number": OWNER_NUMBER, "paired": True},
        ]
        self.sent: list[tuple] = []
        self.medias: list[tuple] = []
        self.send_ok = send_ok

    # ── leituras proibidas neste caminho ──────────────────────────────────
    def recent(self, *a, **k):
        raise Boom("recent() foi chamado: fallback silencioso voltou")

    def search(self, *a, **k):
        raise Boom("search() foi chamado: fallback silencioso voltou")

    def contact_names(self, *a, **k):
        raise Boom("contact_names() foi chamado: fallback silencioso voltou")

    # ── contas ────────────────────────────────────────────────────────────
    def list_accounts(self):
        return list(self.accounts)

    def default_account_name(self):
        return next((a["name"] for a in self.accounts if a.get("default")), "")

    # ── envios ────────────────────────────────────────────────────────────
    def send(self, recipient, text, account=None, signed=True):
        self.sent.append((recipient, text, account, signed))
        if not self.send_ok:
            return False, "nao conectado"
        return True, f"enviado para {recipient}"

    def send_media(self, recipient, path, caption="", account=None):
        self.medias.append((recipient, path, caption, account))
        return True, f"midia enviada para {recipient}"

    def ffmpeg_ready(self):
        return True

    def current_speaker(self):
        return "Gino"


@pytest.fixture
def fake(monkeypatch):
    """Troca a camada de envio e a síntese; nada toca rede, WhatsApp ou disco."""
    w = FakeWA()
    monkeypatch.setattr(wa, "whatsapp", w)
    monkeypatch.setattr(wa, "get_plugin_setting",
                        lambda ns, key, default=None: (
                            OWNER_NUMBER if key == "owner_chat" else default))
    return w


@pytest.fixture
def synth_ok(monkeypatch, fake):
    """Síntese falsa devolvendo um caminho — o pipeline de verdade não roda aqui."""
    calls: list[str] = []

    def _synthesize(text, **kw):
        calls.append(text)
        return "/tmp/nota-de-teste.ogg", text

    monkeypatch.setattr(wa.voice_note, "synthesize", _synthesize)
    fake.synth_calls = calls
    return fake


# ── 1. o legado, caracterizado (a cadeia que estava em HEAD) ────────────────

def _legacy_dispatch(action: str, table) -> str:
    """Cópia da cadeia de ``HEAD`` em c126be4 (07/11/2026): nomes exatos, else→recent.

    Não é uma paródia: os conjuntos de chaves e a ordem dos ``elif`` são os
    que estavam no ``run()`` de verdade, e o ``else`` final é o ``else`` final
    que estava lá. A cópia mora aqui (e não em ``git show``) para o teste
    continuar dizendo a verdade depois que o commit da correção entrar.
    """
    action = str(action or "").strip().lower()
    for name, keys in (("send_file", ("send_file", "enviar_arquivo",
                                      "mandar_arquivo", "media")),
                       ("send", ("send", "enviar", "manda", "mandar")),
                       ("search", ("search", "buscar", "procurar"))):
        if action in keys:
            return table[name]()
    return table["recent"]()          # o else que engolia tudo


def test_legado_engolia_acao_desconhecida_como_listagem():
    """Prova do defeito: no código antigo, ``send_audio`` devolvia a lista."""
    table = {"send": lambda: "ENVIO", "send_file": lambda: "ARQUIVO",
             "search": lambda: "BUSCA", "recent": lambda: "INBOX-LEGADO"}
    for pedido in ("send_audio", "voice", "audio", "nota_de_voz", "",
                   "transferir_dinheiro"):
        assert _legacy_dispatch(pedido, table) == "INBOX-LEGADO", (
            f"{pedido!r} deveria ter caído no inbox — era o defeito")
    # …e nada disso estava no catálogo antigo: o nome nunca foi reconhecido.
    src = (BASE / "plugins" / "whatsapp_control.py").read_text(encoding="utf-8")
    assert "ACTION_ALIASES" in src, "o catalogo fechado e o que substituiu o else"
    assert "else:  # recent / inbox / ler — o padrão" not in src


# ── 2. o comportamento atual: erro, nunca listagem ──────────────────────────

def test_action_totalmente_desconhecida_devolve_erro_sem_listar(fake):
    out = wa.run({"action": "transferir_dinheiro", "to": "Joao", "text": "oi"})
    assert "transferir_dinheiro" in out, "o erro precisa nomear a action pedida"
    assert "recent" in out and "send_voice" in out, "o erro lista as actions validas"
    assert "envi" in out.lower() or "nada foi" in out.lower()
    assert fake.sent == [] and fake.medias == []


def test_action_ausente_e_erro_nao_inbox(fake):
    out = wa.run({"to": "Joao", "text": "oi"})
    assert "falta a action" in out
    assert fake.sent == [] and fake.medias == []


def test_acao_quase_certida_devolve_a_vizinha(fake):
    """O erro nomeia a action pedida E aponta as mais próximas (dado, não frase)."""
    out = wa.run({"action": "send_voze", "to": "Joao", "text": "oi"})
    assert "send_voze" in out, "tem que dizer o que foi pedido"
    assert "mais próximas" in out and "send_voice" in out
    assert fake.sent == [] and fake.medias == []


def test_acao_sem_vizinho_devolve_o_catalogo_inteiro(fake):
    out = wa.run({"action": "zzz", "to": "Joao"})
    assert "Actions válidas" in out and "send_voice" in out and "recent" in out
    assert fake.sent == [] and fake.medias == []


@pytest.mark.parametrize("alias", ["send_audio", "audio", "voice", "voice_note",
                                   "nota_de_voz", "enviar_audio", "ptt",
                                   "mandar_nota_de_voz", "SEND_VOICE",
                                   "enviar-nota-de-voz"])
def test_apelidos_de_audio_sao_a_rota_de_audio(alias, synth_ok):
    """O que um dia virou inbox agora sintetiza e entrega mídia (nunca texto)."""
    out = wa.run({"action": alias, "to": "Joao", "text": "chega em dez minutos"})
    assert synth_ok.medias, "send_voice tem que sair por send_media"
    assert synth_ok.sent == [], "pedido de audio jamais vira mensagem escrita"
    assert out.startswith("midia enviada")


def test_midi_existente_usa_a_mesma_entrega(synth_ok):
    out = wa.run({"action": "enviar_arquivo", "to": "Joao",
                  "path": "/tmp/boleto.pdf", "caption": "segue"})
    assert synth_ok.medias == [("Joao", "/tmp/boleto.pdf", "segue", None)]
    assert synth_ok.sent == []
    assert "midia enviada" in out


# ── 3. ordem dos checks: nada de efeito antes de saber destino e conta ───────

def test_sem_destino_nao_sintetiza_e_nada_e_enviado(monkeypatch, fake):
    hits: list[str] = []
    monkeypatch.setattr(wa.voice_note, "synthesize",
                        lambda *a, **k: hits.append("tts") or ("/tmp/x.ogg", ""))
    out = wa.run({"action": "send_voice", "text": "oi"})
    assert "para quem" in out
    assert hits == [], "gastar TTS sem destino era rastro fantasma"
    assert fake.medias == []


def test_sem_texto_na_nota_de_voz_e_erro(fake):
    out = wa.run({"action": "send_voice", "to": "Joao"})
    assert "falta o texto" in out or "sem texto" in out
    assert fake.medias == []


def test_conta_errada_e_recusada_antes_da_sintese(monkeypatch, fake):
    monkeypatch.setattr(wa, "whatsapp", FakeWA(accounts=[
        {"name": "principal", "uuid": "u1", "default": True,
         "number": OWNER_NUMBER, "paired": True},
        {"name": "trabalho", "uuid": "u2", "default": False,
         "number": "5511999997777", "paired": True},
    ]))
    hits: list[str] = []
    monkeypatch.setattr(wa.voice_note, "synthesize",
                        lambda *a, **k: hits.append("tts") or ("/tmp/x.ogg", ""))
    out = wa.run({"action": "send_voice", "to": "Joao", "text": "oi",
                  "account": "trbalho"})
    assert "conta de WhatsApp 'trbalho'" in out
    assert "nada foi enviado" in out
    assert hits == [] and wa.whatsapp.medias == []


# ── 4. sem áudio não existe plano B silencioso ──────────────────────────────

def test_sintese_sem_audio_nao_vira_texto(monkeypatch, fake):
    monkeypatch.setattr(wa.voice_note, "synthesize",
                        lambda *a, **k: ("", "texto falado"))
    out = wa.run({"action": "send_voice", "to": "Joao", "text": "oi"})
    assert "NADA foi enviado" in out
    assert fake.medias == [] and fake.sent == [], (
        "trocar audio por texto sem pedir e exatamente o defeito de 07/11")
    assert "mensagem escrita" in out, "oferecer o texto e opcional, nunca automatico"


def test_rota_de_texto_continua_sendo_texto(synth_ok):
    out = wa.run({"action": "send", "to": "Joao", "text": "cheguei"})
    assert synth_ok.sent == [("Joao", "cheguei", None, True)]
    assert synth_ok.medias == []
    assert "enviado" in out


def test_rotulo_do_dono_resolve_no_numero_dono(synth_ok):
    """>  "manda pra mim" cai no owner_chat — nunca no número de um contato."""
    wa.run({"action": "send_voice", "to": "mim", "text": "lembrete"})
    assert synth_ok.medias == [(OWNER_NUMBER, "/tmp/nota-de-teste.ogg", "", None)]
