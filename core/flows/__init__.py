"""Arquitetura de fluxos do Gino (orquestrador, roteador, boca, supervisor).

O pacote nasceu da PoC em ``/tmp/gino-poc-arquitetura`` e é o único lugar em
que as decisões de fluxo vivem. O pipeline ao vivo (``main.py``, sidecar
``qoder_live``) apenas entrega eventos a :class:`core.flows.live.FlowsRuntime`;
quem decide o que fazer com cada evento é o orquestrador.

Leis aplicadas em código (seção 4 de ``PLANO.md``):

a) a boca nunca afirma resultado sem fato devolvido por um trabalhador;
b) quem chama ferramenta não confirma: o pedido vira ``pending_confirm``;
c) evento de chat alheio (``trust="untrusted"``) nunca vira ordem nem task;
d) todo chat persistente tem orçamento e prazo no supervisor; o chat de
   raciocínio é compactado, nunca morto.

Nada aqui importa ``main.py``, Qt ou o sidecar: o pacote é importável por
testes e por scripts de medição.
"""
from .config import DEFAULT_CONFIG_PATH, Budget, FlowsConfig, load_config
from .orchestrator import Orchestrator, TurnPlan
from .router import Decision, Router

__all__ = [
    "Budget",
    "DEFAULT_CONFIG_PATH",
    "Decision",
    "FlowsConfig",
    "Orchestrator",
    "Router",
    "TurnPlan",
    "load_config",
]
