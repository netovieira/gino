"""Captura do chat direto do dono: inbox + gatilho, com evento do neonize real.

O caso que falhou em produção (~18:05 de 01/11): o dono escreveu do celular no
chat direto e o assistente não viu nem respondeu. O evento de uma mensagem
enviada pelo celular é um MessageEv normal do neonize, mas chega com
``IsFromMe=True`` — e esse era o portão que engolia a mensagem antes de
qualquer resposta. Aqui o evento é montado com os protos do neonize (mesmo
shape do que o whatsapp entrega), injetado no ``_on_message`` real e o inbox +
watcher são conferidos de verdade.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("neonize")
from neonize.proto.Neonize_pb2 import Message  # noqa: E402
from neonize.utils import build_jid  # noqa: E402

from core import whatsapp, whatsapp_watch  # noqa: E402

OWNER = "5517981792464"       # número do dono = conta pareada "principal"
ASSISTANT = "5517992101133"   # número de saída = conta "trabalho"
THIRD = "5511888888888"
GROUP = "120363000000000000@g.us"


def make_event(chat: str, text: str, from_me: bool = False,
               message_id: str = "", ts: float | None = None,
               group: bool = False, lid: str = "",
               sender: str = "") -> Message:
    ev = Message()
    ev.Info.ID = message_id or f"3EB0{time.time_ns():016X}"
    # O neonize entrega MILISSEGUNDOS: é o mesmo número que o pedido de
    # HistorySync manda em ``oldestMsgTimestampMS``. O fixture tem que ter o
    # shape real, senão o teste passa com um timestamp que a produção nunca
    # manda (foi assim que a "entrada morta" de 01/11 passou despercebida).
    ev.Info.Timestamp = int((ts if ts is not None else time.time()) * 1000)
    ev.Info.Pushname = "Anthero Vieira Neto"
    if lid:
        # Shape do WhatsApp depois da migração para LID: o chat é um id interno
        # e o telefone vem no JID alternativo.
        ev.Info.MessageSource.Chat.CopyFrom(build_jid(lid, "lid"))
        if from_me:
            ev.Info.MessageSource.RecipientAlt.CopyFrom(build_jid(chat))
        else:
            ev.Info.MessageSource.SenderAlt.CopyFrom(build_jid(chat))
    else:
        ev.Info.MessageSource.Chat.CopyFrom(build_jid(chat))
    ev.Info.MessageSource.Sender.CopyFrom(
        build_jid(sender or (OWNER if from_me else chat)))
    ev.Info.MessageSource.IsFromMe = from_me
    ev.Info.MessageSource.IsGroup = group
    ev.Message.conversation = text
    return ev


class CaptureCase:
    """Uma conta de mentira com inbox em tmp; nada toca memory/."""

    def __init__(self, tmp_path: Path, name: str, number: str):
        self.account = whatsapp._Account(name, f"capture-{name}", default=True)
        self.account.inbox_file = tmp_path / f"inbox-{name}.jsonl"
        self.account.media_dir = tmp_path / f"media-{name}"
        self.account.me["number"] = number
        self.name = name

    def deliver(self, ev: Message) -> list[dict]:
        whatsapp._on_message(None, ev, self.account)
        if not self.account.inbox_file.exists():
            return []
        return [json.loads(line) for line in
                self.account.inbox_file.read_text().splitlines() if line]


@pytest.fixture
def wire(tmp_path, monkeypatch):
    calls: list[tuple[str, dict]] = []
    whatsapp.drain_owner_pending()
    hooks_before = list(whatsapp._alert_hooks)
    monkeypatch.setattr(whatsapp, "default_account_name",
                        lambda: "trabalho")
    monkeypatch.setattr(
        whatsapp, "account_number",
        lambda account=None: {"principal": OWNER, "trabalho": ASSISTANT}
        .get(str(account or ""), ""))
    monkeypatch.setattr(
        "memory.config_manager.get_plugin_setting",
        lambda _ns, key, default=None: OWNER if key == "owner_chat" else default)

    whatsapp_watch.reset()
    whatsapp_watch.start(lambda account, item: calls.append((account, item))
                         or True,
                         logger=lambda _m: None, assistant_name="Gino",
                         direct_account="trabalho", direct_chat=OWNER)
    try:
        yield calls
    finally:
        for hook in list(whatsapp._alert_hooks):
            if hook not in hooks_before:
                whatsapp.remove_owner_wake_hook(hook)
        whatsapp.drain_owner_pending()
        whatsapp_watch.stop()
        whatsapp_watch.reset()


def test_media_dest_knows_the_path_before_the_download_finishes(tmp_path,
                                                               monkeypatch):
    """media_path="" não pode encerrar a espera: o destino é previsível.

    Foi exatamente o caso do áudio de 02/11: o watcher chegou antes de a
    thread de download criar o arquivo e devolveu "não consegui transcrever".
    """
    acct = whatsapp._Account("espera", "capture-espera")
    acct.media_dir = tmp_path
    monkeypatch.setattr(whatsapp, "_accounts", {"capture-espera": acct})
    monkeypatch.setattr(whatsapp, "_ensure_accounts", lambda: None)
    item = {"ts": 1793626189, "name": "Anthero Vieira Neto",
            "id": "3AB54B686F02A8160839",
            "media": {"kind": "mensagem de voz", "ext": ".ogg"}}

    dest = whatsapp.media_dest(item, "espera")
    assert dest == str(
        tmp_path / "1793626189_anthero_vieira_neto_3AB54B686F02A8160839.ogg")
    assert whatsapp.media_path(item, "espera") == ""

    Path(dest).write_bytes(b"OggS")
    assert whatsapp.media_path(item, "espera") == dest


def test_owner_message_from_phone_lands_in_inbox_and_triggers(wire, tmp_path):
    """O caso de 18:05: dono -> número do assistente, pelo celular dele."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    ev = make_event(OWNER, "Gino, você está aí?", from_me=False)
    items = case.deliver(ev)

    assert len(items) == 1
    assert items[0]["chat"] == OWNER
    assert items[0]["from_me"] is False
    assert [c[0] for c in wire] == ["trabalho"], (
        "mensagem do dono no chat direto tem que pedir resposta mesmo sem o nome")


def test_long_owner_message_reaches_the_watcher_whole(wire, tmp_path):
    """Mensagem longa do dono: o item do inbox não pode parar em 600.

    O teto antigo ``text[:600]`` cortava o texto ANTES do turno, então o
    modelo recebia só o começo de uma mensagem escrita longa do dono.
    """
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    long_text = " ".join(
        f"Ponto {i}: conferir o relatório e registrar a decisão final."
        for i in range(1, 121)) + " FIM DA MENSAGEM DO DONO: última instrução."
    assert len(long_text) > 4000

    items = case.deliver(make_event(OWNER, long_text, from_me=False))

    assert len(items) == 1
    assert items[0]["text"] == long_text
    assert len(wire) == 1
    assert wire[0][1]["text"] == long_text
    assert wire[0][1]["text"].endswith("última instrução.")


def test_owner_own_number_self_chat_is_captured_and_triggers(wire, tmp_path):
    """Dono escrevendo no chat do PRÓPRIO número pareado (from_me=True)."""
    case = CaptureCase(tmp_path, "principal", OWNER)
    ev = make_event(OWNER, "teste do chat direto", from_me=True)
    items = case.deliver(ev)

    assert len(items) == 1, "o inbox não pode descartar a mensagem do dono"
    assert items[0]["from_me"] is True
    assert [c[0] for c in wire] == ["principal"], (
        "chat direto do dono não exige a palavra Gino, nem com from_me")


def test_from_me_outside_direct_chat_without_the_name_never_triggers(
        wire, tmp_path):
    """Dono mandando para um terceiro pelo celular: inbox sim, resposta não."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    items = case.deliver(make_event(THIRD, "oi, tudo bem?", from_me=True))
    assert len(items) == 1
    assert wire == []


def test_from_me_outside_direct_chat_naming_the_assistant_is_an_order(
        wire, tmp_path):
    """O caso real da Carol: ordem escrita do celular no chat de terceiro."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    items = case.deliver(make_event(
        THIRD, "Gino, fala pra Carol como você se sente", from_me=True,
        message_id="3EB02F87F80819AA795C2A"))
    assert len(items) == 1
    assert [c[0] for c in wire] == ["trabalho"]


def test_owner_echo_order_is_one_turn_per_message_id(wire, tmp_path):
    """O mesmo evento reentregue não pode gerar uma segunda resposta."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    ev = make_event(THIRD, "Gino, fala pra Carol", from_me=True,
                    message_id="3EB0ORDERDEDUP01")
    case.deliver(ev)
    case.deliver(ev)
    assert [c[0] for c in wire] == ["trabalho"]


def test_direct_owner_message_is_not_announced_in_the_room(wire, tmp_path):
    """O turno do dono sai no WhatsApp; nada de uma segunda resposta falada."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    announced: list[dict] = []
    case.account.sink = announced.append
    case.deliver(make_event(OWNER, "e aí, tudo certo?", from_me=False))

    assert announced == [], "aviso de sala duplicaria o turno do chat direto"
    assert [c[0] for c in wire] == ["trabalho"]


def test_third_party_message_still_gets_the_room_announcement(wire, tmp_path):
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    announced: list[dict] = []
    case.account.sink = announced.append
    case.deliver(make_event(THIRD, "oi, tudo bem?", from_me=False))

    assert len(announced) == 1
    assert wire == []


def test_without_the_watcher_the_room_keeps_the_announcement(wire, tmp_path):
    """Sem turno autônomo armado, silenciar a sala perderia a mensagem."""
    whatsapp_watch.stop()
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    announced: list[dict] = []
    case.account.sink = announced.append
    case.deliver(make_event(OWNER, "sem assistente autônomo", from_me=False))

    assert len(announced) == 1
    assert wire == []


def test_echo_of_our_own_send_never_triggers(wire, tmp_path, monkeypatch):
    """Anti-laço: o eco de um envio deste processo é reconhecido pelo ID."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    sent_id = "3EB0OWNREPLY0001"
    monkeypatch.setattr(whatsapp, "sent_by_me",
                        lambda mid: str(mid or "") == sent_id)
    items = case.deliver(make_event(OWNER, "resposta do assistente",
                                    from_me=True, message_id=sent_id))
    assert len(items) == 1
    assert wire == []


def test_third_party_naming_the_assistant_still_triggers(wire, tmp_path):
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    case.deliver(make_event(THIRD, "Gino, me ajuda?", from_me=False))
    assert [c[0] for c in wire] == ["trabalho"]


def test_group_message_naming_the_assistant_is_ignored(wire, tmp_path):
    """Grupo NUNCA vira turno — nem com o nome (decisão do dono)."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    case.deliver(make_event(GROUP, "Gino, olha isso", from_me=False,
                            group=True, sender=OWNER))
    assert wire == []


def test_group_event_is_not_logged(wire, tmp_path, monkeypatch):
    """Sem rastro de conversa de grupo no log: grupo não é monitorado."""
    logs: list[str] = []
    monkeypatch.setattr(whatsapp, "_logger", logs.append)
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    case.deliver(make_event(GROUP, "Gino, olha isso", from_me=False,
                            group=True, sender=OWNER))
    assert logs == []
    assert wire == []


def test_group_message_without_the_name_is_ignored(wire, tmp_path):
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    case.deliver(make_event(GROUP, "olha isso", from_me=False,
                            group=True, sender=OWNER))
    assert wire == []


def test_our_own_group_answer_never_retriggers(wire, tmp_path):
    """from_me=True em grupo (nossa resposta com "Gino:") nunca dispara."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    case.deliver(make_event(GROUP, "Gino: já respondi", from_me=True,
                            group=True))
    assert wire == []


def test_group_answer_from_the_other_paired_account_never_retriggers(
        wire, tmp_path):
    """A resposta da conta "trabalho" no grupo chega na "principal" como
    mensagem de membro; o remetente pareado é o que barra o laço."""
    case = CaptureCase(tmp_path, "principal", OWNER)
    case.deliver(make_event(GROUP, "Gino, tudo bem?", from_me=False,
                            group=True, sender=ASSISTANT))
    assert wire == []


def test_empty_owner_chat_falls_back_to_the_paired_number(wire, tmp_path,
                                                          monkeypatch):
    """Sem owner_chat no config, o chat direto é o número da própria conta."""
    monkeypatch.setattr(
        "memory.config_manager.get_plugin_setting",
        lambda _ns, key, default=None: default)
    whatsapp_watch.set_direct_chat("trabalho", "")
    case = CaptureCase(tmp_path, "principal", OWNER)

    items = case.deliver(make_event(OWNER, "sem nome e sem owner_chat",
                                    from_me=True))
    assert len(items) == 1
    assert [c[0] for c in wire] == ["principal"]


def test_owner_chat_on_the_outbound_account_is_still_direct(wire, tmp_path):
    """Regra antiga preservada: dono -> conta de saída, remetente = dono."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    assert whatsapp_watch.is_direct(
        "trabalho", {"chat": OWNER, "from_me": False}) is True
    assert whatsapp_watch.is_direct(
        "principal", {"chat": OWNER, "from_me": False}) is True
    assert whatsapp_watch.is_direct(
        "trabalho", {"chat": THIRD, "from_me": False}) is False


# ── o que só apareceu em produção: timestamp em ms e chat em LID ────────────

def test_event_timestamp_is_read_in_milliseconds(wire, tmp_path):
    """O evento real do neonize chega em ms; lido como segundos virava 1970."""
    assert whatsapp._stamp_seconds(1793566902000) == 1793566902
    # Compatibilidade: um evento em segundos (build antiga/testes) vale igual.
    assert whatsapp._stamp_seconds(1793566902) == 1793566902

    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    items = case.deliver(make_event(OWNER, "cheguei", from_me=False))
    assert len(items) == 1, "mensagem fresca tem que entrar no inbox"
    assert abs(items[0]["ts"] - time.time()) < 120
    assert [c[0] for c in wire] == ["trabalho"], (
        "sem o timestamp certo o watcher via tudo como velho e nada respondia")


def test_lid_chat_resolves_to_the_owner_number(wire, tmp_path):
    """Chat `@lid` + SenderAlt = telefone: é o chat direto do dono, não terceiro."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    items = case.deliver(make_event(OWNER, "e aí?", from_me=False,
                                    lid="28381194227763"))
    assert len(items) == 1
    assert items[0]["chat"] == OWNER, "a resposta tem que ir para o telefone"
    assert items[0]["chat_lid"] == "28381194227763"
    assert [c[0] for c in wire] == ["trabalho"]


def test_lid_self_chat_from_the_phone_is_a_turn(wire, tmp_path):
    """Dono falando com o próprio número; o evento chega com chat em LID."""
    case = CaptureCase(tmp_path, "principal", OWNER)
    items = case.deliver(make_event(OWNER, "nota: comprar café", from_me=True,
                                    lid="181609135390893"))
    assert len(items) == 1
    assert items[0]["chat"] == OWNER
    assert [c[0] for c in wire] == ["principal"]


def test_third_party_lid_chat_still_needs_the_name(wire, tmp_path):
    """Resolver o LID não transforma qualquer contato em chat do dono."""
    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    items = case.deliver(make_event(THIRD, "oi, tudo bem?", from_me=False,
                                    lid="116659918508160"))
    assert items[0]["chat"] == THIRD
    assert wire == []


def test_the_same_sent_message_on_both_accounts_is_one_turn(wire, tmp_path):
    """Um envio do dono aparece nas duas contas: só a que RECEBEU responde."""
    principal = CaptureCase(tmp_path, "principal", OWNER)
    trabalho = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    mid = "3AAA799927A23D194860"
    principal_items = principal.deliver(
        make_event(ASSISTANT, "Gino, chegou o pedido?", from_me=True,
                   message_id=mid, lid="181609135390893"))
    trabalho_items = trabalho.deliver(
        make_event(OWNER, "Gino, chegou o pedido?", from_me=False,
                   message_id=mid, lid="28381194227763"))

    assert len(principal_items) == 1 and len(trabalho_items) == 1
    assert [c[0] for c in wire] == ["trabalho"], (
        "responder pela conta do dono seria uma segunda resposta ao mesmo pedido")


def test_the_same_owner_event_delivered_twice_is_one_turn(wire, tmp_path):
    """Reconexão reentrega o mesmo evento: id é a chave do dedupe."""
    case = CaptureCase(tmp_path, "principal", OWNER)
    ev = make_event(OWNER, "acorda", from_me=True, message_id="DUP-OWNER-1")
    first = case.deliver(ev)
    second = case.deliver(ev)

    assert len(first) == 1
    assert len(second) == 1, "o inbox não pode ganhar a mesma linha duas vezes"
    assert [c[0] for c in wire] == ["principal"]


# ── a gaveta: aviso do dono enquanto o assistente dorme ─────────────────────

def test_owner_message_waits_in_the_drawer_until_the_next_wake(wire, tmp_path):
    case = CaptureCase(tmp_path, "principal", OWNER)
    case.deliver(make_event(OWNER, "acorda, preciso de você", from_me=True,
                            message_id="OWNER-SLEEP-1"))

    queued = whatsapp.pending_owner()
    assert [q["text"] for q in queued] == ["acorda, preciso de você"]
    assert queued[0]["account"] == "principal"
    assert queued[0]["id"] == "OWNER-SLEEP-1"

    drained = whatsapp.drain_owner_pending()
    assert [d["id"] for d in drained] == ["OWNER-SLEEP-1"]
    assert whatsapp.pending_owner() == []


def test_wake_hook_receives_the_owner_message(wire, tmp_path):
    seen: list[tuple[str, dict]] = []
    whatsapp.add_owner_wake_hook(
        lambda account, item: seen.append((account, item)) or True)

    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    case.deliver(make_event(OWNER, "cheguei em casa", from_me=False,
                            message_id="OWNER-WAKE-1"))

    assert [s[0] for s in seen] == ["trabalho"]
    assert seen[0][1]["chat"] == OWNER
    assert whatsapp.pending_owner() == [], "aviso entregue não vai para a gaveta"


def test_third_party_message_never_uses_the_owner_wake_hook(wire, tmp_path):
    seen: list[dict] = []
    whatsapp.add_owner_wake_hook(
        lambda account, item: seen.append(item) or True)

    case = CaptureCase(tmp_path, "trabalho", ASSISTANT)
    case.deliver(make_event(THIRD, "Gino, tudo bem?", from_me=False))

    assert seen == []
    assert whatsapp.pending_owner() == []


def test_echo_of_our_own_reply_does_not_enter_the_drawer(wire, tmp_path,
                                                         monkeypatch):
    case = CaptureCase(tmp_path, "principal", OWNER)
    monkeypatch.setattr(whatsapp, "sent_by_me",
                        lambda mid: str(mid or "") == "OWN-ECHO-1")
    case.deliver(make_event(OWNER, "resposta do assistente", from_me=True,
                            message_id="OWN-ECHO-1"))

    assert whatsapp.pending_owner() == []
    assert wire == []
