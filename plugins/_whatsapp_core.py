"""Adaptador entre actions/send_message.py e o WhatsApp de verdade.

send_message.py foi escrito para procurar exatamente este arquivo: se ele
existir e `get()` entregar um transporte, o envio do WhatsApp passa por aqui em
vez de o app do WhatsApp ser aberto e digitado às cegas. O contrato é o que
aquele arquivo já espera:

    transport, why = get()
    sent, failure  = transport.send_message_to("João", "texto")

Quando o WhatsApp ainda não está pareado, o transporte EXISTE e recusa. Poderia
parecer melhor devolver None (e send_message cairia no modo cego), mas digitar
às cegas é justamente o que produz "enviado" para uma mensagem que nunca saiu.
Recusar com o motivo é mais honesto e é o que o próprio send_message.py espera
de um driver que já rodou.
"""
from __future__ import annotations


class _Transport:
    def send_message_to(self, receiver: str, message: str) -> tuple[bool, str]:
        from core import whatsapp
        ok, why = whatsapp.send(receiver, message)
        return ok, "" if ok else why


def get() -> tuple[object | None, str]:
    """(transporte, motivo). None só quando não há como nem tentar."""
    from core import whatsapp
    if not whatsapp.available():
        return None, "neonize não está instalado (pip install neonize)"
    return _Transport(), ""
