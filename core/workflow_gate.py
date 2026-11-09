"""core/workflow_gate.py — o portão que não deixa executar sem plano aprovado.

Ordem do dono, literal (09/11/2026): "O GATE DEVOLVE ALGO COMO 'PRECISO DE UM
PLANO PRA ISSO' FORCANDO O GINO A LEMBRAR QUE ELE TEM QUE CRIAR O PLANO E NÃO
EXECUTAR DIRETAMENTE". A dor real está diagnosticada no
``~/Jarvis/PLANO-2026-11-07-fluxo-plano-aprovacao-tarefa-e-vault-de-obras.md``
(seção 3.3): o ciclo plano → aprovação → tarefas → execução vivia só em texto
de prompt e na RAM do processo. No reinício não havia portão nem registro de
"aguardando aprovação", e todo turno virava execução direta.

O que este módulo é:

* ESTADO PERSISTENTE em ``memory/workflow_gate.json`` (``JARVIS_WORKFLOW_GATE_STATE``
  por cima para teste): plano, fase, quem aprovou, quando, que arquivo do
  cofre é o plano, e as tarefas. Sobrevive a reinício de propósito — é isso
  que falta para o portão valer no vivo.
* O PORTÃO: :func:`require_plan_approved` devolve :class:`GateResult`. Sem
  plano, ou com plano ainda não aprovado, ``ok=False`` e a mensagem é
  :data:`MENSAGEM_SEM_PLANO` -- "Preciso de um plano pra isso". Quem chama
  decide o que fazer com o resultado; :func:`exigir_plano_aprovado` levanta
  :class:`SemPlanoAprovado` para quem prefere não esquecer de conferir.
* A ALAVANCA: :func:`ativo` lê ``config/workflow_gate.json`` a CADA chamada
  (precedente: ``core/flows/config.py``, ``core/flow_ops/rules.py``), então
  desligar não pede reinício. Regra do dono cumprida nas duas pontas:
  desligado = o comportamento de hoje (``ok=True``, nada bloqueado); ligado =
  nunca quebra o que já funciona -- erro interno meu dentro do portão sai como
  liberação com ``motivo="erro_interno:..."`` no log, nunca como traceback.

* A LINHA DE EVENTO: cada decisão escreve ``event=workflow_gate`` no
  ``logs/activity.log`` com veredito, ids e motivo. Nunca conteúdo de conversa
  e nunca segredo: só referência, fase, quem aprovou e o nome do arquivo.

O que este módulo NÃO é (e é decisão, não esquecimento):

* Não aprova nada sozinho. :func:`aprovar_plano` exige ``quem=`` com o nome de
  quem aprovou e recusa aprovação em nome do próprio assistente: aprovação é
  ato do dono (mesmo quando ele delega, fica registrado a quem se delegou).
* Não bloqueia por força bruta: quem executa é quem consulta. O plug-in no
  executor (``main.py`` / ``core/flows/live.py``) está pendente e declarado
  como asserção no ``core/dev_audit.py`` -- portão sem chamador em produção é
  exatamente o defeito que a auditoria de 07/11 pegou no UP-8/UP-10.
* Não lê segredo. Referência de chave em documento é
  ``[key:<nome>, tipo:<tipo>]`` (``core/obras.py``).

Sem Qt, sem ``main.py``, sem rede.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
ACTIVITY_LOG = BASE_DIR / "logs" / "activity.log"
DEFAULT_STATE = BASE_DIR / "memory" / "workflow_gate.json"
_ENV_STATE = "JARVIS_WORKFLOW_GATE_STATE"

#: Alavanca do portão (ordem do dono, 09/11/2026): "gate desligado = hoje;
#: gate ligado nunca pode quebrar o que já funciona". O valor mora num JSON
#: lido a CADA chamada -- o mesmo precedente de ``core/flows/config.py`` (e de
#: ``core/flow_ops/rules.py:reload_if_changed``): editar o arquivo vale sem
#: reiniciar o processo. ``JARVIS_WORKFLOW_GATE_CONFIG`` aponta para outro
#: arquivo (teste), e ``JARVIS_WORKFLOW_GATE`` desliga/liga na marra sem
#: mexer em disco.
DEFAULT_CONFIG = BASE_DIR / "config" / "workflow_gate.json"
_ENV_CONFIG = "JARVIS_WORKFLOW_GATE_CONFIG"
_ENV_ATIVO = "JARVIS_WORKFLOW_GATE"
#: Sem arquivo de config, vale a lei, não a conveniência: o portão vem LIGADO.
ATIVO_PADRAO = True
_VERDADES = ("1", "true", "sim", "on", "ligado", "ativo", "verde")
_FALSO = ("0", "false", "nao", "não", "off", "desligado", "inativo")

#: A resposta do portão quando não há plano aprovado para o que se pediu.
#: Literal na ordem do dono: o gate TEM de dizer isto, em PT-BR.
MENSAGEM_SEM_PLANO = "Preciso de um plano pra isso"

#: Fase fechada. ``rascunho`` e ``aguardando_aprovacao`` não executam;
#: ``aprovado`` em diante executam, tarefa por tarefa.
FASES = ("rascunho", "aguardando_aprovacao", "aprovado", "executando",
         "encerrado")
FASES_APROVADAS = ("aprovado", "executando", "encerrado")

#: Quem NÃO pode constar como aprovador: o assistente não se autoaprova.
_APELADOS_DO_ASSISTENTE = {"gino", "jarvis", "assistente", "modelo", "agente",
                           "ai", "eu"}

_ID_RE = re.compile(r"^[\w][\w.:-]{0,99}$")
_LIMITE_HISTORICO = 200

#: Nomes do esqueleto de pasta de plano (``core/obsidian.py:PLAN_FOLDER_FILES``).
#: Num caminho, o que identifica o plano é a PASTA, não o arquivo: todos os
#: planos têm ``plan.md``.
_NOMES_DO_ESQUELETO = frozenset({
    "plan", "status", "progresso", "historico", "testes", "prompt", "steps",
    "tasks",
})

#: Onde a aprovação foi dita. Vocabulário fechado de propósito: o campo
#: serve para a auditoria saber em que canal o dono respondeu, não para
#: guardar recado. Texto livre aqui é o caminho mais curto para um segredo
#: ou uma fala do dono parar no estado do portão.
FONTES_DE_APROVACAO = ("whatsapp", "voz", "terminal", "email",
                       "arquivo-do-cofre", "painel", "outro")

_LOCK = threading.RLock()


@dataclass
class GateResult:
    """O veredito de um portão: liga/desliga + o motivo legível."""

    ok: bool
    gate: str
    motivo: str = ""
    referencia: str = ""
    plano_id: str = ""
    fase: str = ""
    mensagem: str = ""
    plano: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.ok)

    def texto(self) -> str:
        """O que o assistente devolve ao dono/quem pediu -- nunca um traceback."""
        return self.mensagem or (
            f"plano {self.plano_id} aprovado (fase {self.fase}); pode executar"
            if self.ok else MENSAGEM_SEM_PLANO)


class SemPlanoAprovado(RuntimeError):
    """Levantado por :func:`exigir_plano_aprovado` quando o portão fecha."""


# ── estado em disco ─────────────────────────────────────────────────────────

def state_path() -> Path:
    """O arquivo de estado (o env ``JARVIS_WORKFLOW_GATE_STATE`` vence)."""
    override = os.environ.get(_ENV_STATE, "").strip()
    return Path(override).expanduser() if override else Path(DEFAULT_STATE)


# ── a alavanca ──────────────────────────────────────────────────────────────

#: Último valor BOM lido do config. Serve só para o caso de o arquivo podre no
#: meio do caminho: sem isto, um JSON quebrado desligaria o portão em silêncio
#: -- que é a reclamação original do dono ("executa sem plano").
_ultimo_ativo: bool | None = None
#: Último problema lido no config ("" quando o config está limpo). Serve para
#: o painel e para a auditoria dizerem POR QUE o portão está como está.
_ultimo_problema: str = ""


def config_path() -> Path:
    """O JSON da alavanca (o env ``JARVIS_WORKFLOW_GATE_CONFIG`` vence)."""
    override = os.environ.get(_ENV_CONFIG, "").strip()
    return Path(override).expanduser() if override else Path(DEFAULT_CONFIG)


def _ler_ativo() -> tuple[bool, str]:
    """(valor, problema). Lê o JSON A CADA CHAMADA -- sem cache de processo."""
    global _ultimo_ativo
    marra = os.environ.get(_ENV_ATIVO, "").strip().lower()
    if marra in _VERDADES or marra in _FALSO:
        valor = marra in _VERDADES
        _ultimo_ativo = valor
        return valor, ""
    caminho = config_path()
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Sem config não se desliga nada: volta o padrão declarado acima.
        _ultimo_ativo = ATIVO_PADRAO
        return ATIVO_PADRAO, ""
    except (OSError, ValueError) as exc:
        problema = f"config_inutilizavel:{type(exc).__name__}"
        return (bool(_ultimo_ativo) if _ultimo_ativo is not None
                else ATIVO_PADRAO), problema
    if not isinstance(bruto, dict):
        return (bool(_ultimo_ativo) if _ultimo_ativo is not None
                else ATIVO_PADRAO), "config_inutilizavel:formato"
    campo = bruto.get("ativo", ATIVO_PADRAO)
    if isinstance(campo, bool):
        valor = campo
    elif isinstance(campo, str) and campo.strip().lower() in (
            _VERDADES + _FALSO):
        valor = campo.strip().lower() in _VERDADES
    else:
        # "ativo": "mais ou menos" não é liga nem desliga: não decide nada.
        return (bool(_ultimo_ativo) if _ultimo_ativo is not None
                else ATIVO_PADRAO), "config_inutilizavel:valor"
    _ultimo_ativo = valor
    return valor, ""


def ativo() -> bool:
    """Se o portão está ligado AGORA. Nunca levanta, nunca travou nada."""
    global _ultimo_problema
    valor, problema = _ler_ativo()
    _ultimo_problema = problema
    if problema:
        _log_event("workflow_gate", decisao="config_recusada",
                   motivo=problema, caminho=str(config_path()),
                   mantido=str(valor))
    return valor


def problema_config() -> str:
    """O problema da última leitura ("" quando o JSON está limpo)."""
    return _ultimo_problema


def _vazio() -> dict[str, Any]:
    return {"versao": 1, "tarefa_ativa": "", "planos": {},
            "historico": [], "atualizado": ""}


def _carregar() -> dict[str, Any]:
    """Lê o estado; JSON corrompido ou ausente volta ao estado vazio.

    Não levanta de propósito: portão que quebra com estado podre é portão que
    ninguém religa. O histórico perdido é custo menor que execução sem plano.
    """
    caminho = state_path()
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _vazio()
    if not isinstance(bruto, dict):
        return _vazio()
    estado = _vazio()
    estado["versao"] = bruto.get("versao", 1)
    estado["tarefa_ativa"] = str(bruto.get("tarefa_ativa") or "")
    planos = bruto.get("planos")
    if isinstance(planos, dict):
        for pid, plano in planos.items():
            if isinstance(plano, dict):
                estado["planos"][str(pid)] = plano
    historico = bruto.get("historico")
    if isinstance(historico, list):
        estado["historico"] = [h for h in historico if isinstance(h, dict)]
    return estado


def _salvar(estado: dict[str, Any]) -> None:
    caminho = state_path()
    estado["atualizado"] = time.strftime("%Y-%m-%d %H:%M:%S")
    estado["historico"] = estado.get("historico", [])[-_LIMITE_HISTORICO:]
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(f".{caminho.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=2,
                                  sort_keys=True), encoding="utf-8")
        os.replace(tmp, caminho)
    finally:
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _registrar(estado: dict[str, Any], evento: str, plano_id: str,
               **campos: Any) -> None:
    estado.setdefault("historico", []).append({
        "quando": time.strftime("%Y-%m-%d %H:%M:%S"),
        "evento": evento,
        "plano_id": plano_id,
        **{k: str(v)[:160] for k, v in campos.items() if v not in (None, "")},
    })


def _log_event(evento: str, **campos: Any) -> None:
    """Uma linha ``event=workflow_gate ...`` no activity.log. Nunca levanta."""
    try:
        partes = [f"event={evento}"]
        for chave, valor in campos.items():
            if valor is None or valor == "":
                continue
            texto = re.sub(r'["\n\r]', " ", str(valor))[:160]
            partes.append(f'{chave}="{texto}"' if " " in texto
                          else f"{chave}={texto}")
        caminho = Path(ACTIVITY_LOG).expanduser()
        caminho.parent.mkdir(parents=True, exist_ok=True)
        with caminho.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                     f"{' '.join(partes)}\n")
    except Exception:  # noqa: BLE001 - registrar não pode quebrar o portão
        pass


# ── normalização de referência ──────────────────────────────────────────────

def _normalizar(texto: Any) -> str:
    """Texto livre colapsado num espaço só, sem barras sobrando no fim.

    NUNCA tira a barra do começo: ``/home/anthero/...`` é caminho absoluto, e
    um portão que come a primeira barra do caminho passa a achar que o plano
    do dono não existe.
    """
    bruto = " ".join(str(texto or "").split())
    return bruto.strip().rstrip("/\\")


def _id(texto: Any) -> str:
    """Id estável de plano/tarefa a partir de nome, caminho ou título.

    ``PLANO-2026-11-07-personas-de-trabalho.md`` e
    ``~/Jarvis/Planos/personas-de-trabalho/plan.md`` viram o mesmo id: é o
    que permite aprovar o plano pelo arquivo do cofre e cobrar a execução
    pela tarefa, sem duplicar registro. Em arquivo do esqueleto da pasta
    (``plan.md``, ``status.md``...) quem identifica é a PASTA: senão todo
    plano teria id ``plan``.
    """
    bruto = _normalizar(texto)
    if not bruto:
        return ""
    caminho = Path(bruto) if ("/" in bruto or "\\" in bruto) else None
    nome = (caminho.name if caminho is not None else bruto)
    nome = re.sub(r"\.(?:md|markdown)$", "", nome, flags=re.I)
    if caminho is not None and nome.lower() in _NOMES_DO_ESQUELETO:
        nomes = [p for p in caminho.parts if p not in ("/", ".", "..")]
        nome = nomes[-2] if len(nomes) >= 2 else nome
    candidato = re.sub(r"[^A-Za-z0-9_.:-]+", "-", nome).strip("-").lower()
    return candidato[:100]


def _validar_id(texto: Any, o_que_e: str) -> str:
    """Id de plano/tarefa: estável, sem caminho, sem vazio.

    Espaço vira hífen (é assim que "montar o esqueleto" vira um id útil), mas
    barra é recusada: caminho é referência, não identidade, e um id com "/"
    duplicaria o mesmo plano em duas chaves do estado.
    """
    bruto = _normalizar(texto)
    if "/" in bruto or "\\" in bruto:
        raise ValueError(f"{o_que_e} {texto!r} carrega caminho: passe só o "
                         "id (a referência do portão aceita caminho, o id "
                         "não)")
    alvo = _id(bruto)
    if not alvo or not _ID_RE.match(alvo):
        raise ValueError(f"{o_que_e} {texto!r} inutilizável: use letras, "
                         "números, '.', ':' ou '-'")
    return alvo


def _plano_do_estado(referencia: Any, estado: dict[str, Any]) -> dict[str, Any] | None:
    """Procura o plano por id, por tarefa registrada ou pelo nome do arquivo."""
    bruto = _normalizar(referencia)
    if not bruto:
        return None
    alvo = _id(bruto)
    planos = estado.get("planos") or {}
    if alvo in planos:
        return planos[alvo]
    nome = re.sub(r"\.(?:md|markdown)$", "", Path(bruto).name,
                  flags=re.I).lower()
    for plano in planos.values():
        tarefas = plano.get("tarefas") or {}
        if alvo in tarefas:
            return plano
        arquivo = str(plano.get("arquivo") or "")
        if arquivo and nome and _id(arquivo) == alvo:
            return plano
        if nome and nome.lower() == str(plano.get("id") or "").lower():
            return plano
    return None


# ── registro de plano e aprovação ───────────────────────────────────────────

def _redigido(texto: Any, teto: int = 160) -> str:
    """Texto livre que vai para o estado: o redator da vault antes do disco.

    Título de plano é escrito pelo modelo a partir do pedido do dono, e pedido
    de dono às vezes cola chave. O estado do portão não é lugar de segredo:
    ``[key:<nome>, tipo:<tipo>]`` ou ``[retido]`` é que ficam.
    """
    bruto = _normalizar(texto)
    if not bruto:
        return ""
    try:
        from core import obras
        bruto = obras.referenciar(bruto)
    except Exception:  # noqa: BLE001 - sem redator, não se grava texto cru
        return "[retido] (redator de segredo indisponível)"
    return bruto[:teto]


def registrar_plano(plano, titulo: str = "", *, arquivo: Any = "",
                    fase: str = "aguardando_aprovacao", tarefas=(),
                    quando: str | None = None) -> dict[str, Any]:
    """Cadastra (ou atualiza) um plano no estado do portão e devolve o registro.

    Fase inicial é ``aguardando_aprovacao``: plano recém-escrito NUNCA entra
    já aprovado, e ``fase="aprovado"`` aqui não é atalho -- aprovação se faz
    com :func:`aprovar_plano`, com nome e data de quem aprovou.
    """
    if fase not in FASES:
        raise ValueError(f"fase {fase!r} fora do conjunto fechado: "
                         + ", ".join(FASES))
    if fase in FASES_APROVADAS:
        raise ValueError(
            "registrar_plano não aprova: use aprovar_plano(quem=...) depois "
            f"da resposta do dono (fase pedida: {fase!r})")
    pid = _validar_id(plano, "id de plano")
    hora = _agora(quando)
    with _LOCK:
        estado = _carregar()
        anterior = estado["planos"].get(pid) or {}
        registro = {
            "id": pid,
            "titulo": _redigido(titulo or anterior.get("titulo") or pid) or pid,
            "arquivo": _normalizar(arquivo or anterior.get("arquivo")),
            "fase": fase if not anterior else str(
                anterior.get("fase") or fase),
            "aprovado_por": str(anterior.get("aprovado_por") or ""),
            "aprovado_em": str(anterior.get("aprovado_em") or ""),
            "tarefas": dict(anterior.get("tarefas") or {}),
            "criado_em": str(anterior.get("criado_em") or hora),
            "atualizado_em": hora,
        }
        for tarefa in tarefas or ():
            registrar_tarefa(pid, tarefa, quando=quando, _estado=(estado, registro))
        estado["planos"][pid] = registro
        _registrar(estado, "plano_registrado", pid, fase=registro["fase"],
                   arquivo=registro["arquivo"])
        _salvar(estado)
    _log_event("workflow_gate", decisao="registro", gate="plano_existe",
               plano_id=pid, fase=registro["fase"])
    return registro


def _agora(quando: str | None) -> str:
    bruto = _normalizar(quando)
    if not bruto:
        return time.strftime("%Y-%m-%d %H:%M:%S")
    return bruto[:32]


def _fonte(texto: Any) -> str:
    """Canal de aprovação dentro do vocabulário fechado; "" quando não dito."""
    bruto = _normalizar(texto).lower().replace(" ", "-").strip("-")
    if not bruto:
        return ""
    if bruto not in FONTES_DE_APROVACAO:
        raise ValueError(f"fonte {texto!r} fora do vocabulário fechado: "
                         + ", ".join(FONTES_DE_APROVACAO)
                         + " (o campo diz ONDE o dono respondeu, não o que "
                         "ele respondeu)")
    return bruto


def aprovar_plano(plano, *, quem: str, quando: str | None = None,
                  fonte: str = "") -> dict[str, Any]:
    """Registra a aprovação do dono e libera o portão para aquele plano.

    ``quem`` é obrigatório e não pode ser o próprio assistente: a lei do dono
    é que ele aprova. ``fonte`` é o canal (:data:`FONTES_DE_APROVACAO`), e é
    fechado justamente para o estado do portão não virar depósito de recado
    ou de chave colada na conversa.
    """
    pid = _validar_id(plano, "id de plano")
    canal = _fonte(fonte)
    aprovador = _normalizar(quem)
    if not aprovador:
        raise ValueError("aprovação sem quem aprovou não existe: passe "
                         "quem=<nome do dono ou de quem ele delegou>")
    if aprovador.lower() in _APELADOS_DO_ASSISTENTE:
        raise ValueError(
            f"aprovação em nome de {quem!r} recusada: quem aprova é o dono "
            "(ou quem ele delegou, com o nome dele em quem=); o assistente "
            "não se autoaprova")
    hora = _agora(quando)
    with _LOCK:
        estado = _carregar()
        registro = estado["planos"].get(pid)
        if registro is None:
            raise ValueError(f"{MENSAGEM_SEM_PLANO}: não há plano {pid} "
                             "cadastrado para aprovar — escreva o plano "
                             "(registrar_plano) e mostre ao dono antes")
        if not registro.get("arquivo"):
            raise ValueError(
                f"plano {pid} aprovado sem arquivo no cofre: o dono aprova um "
                "documento, não uma intenção — passe arquivo=~/Jarvis/…")
        registro["fase"] = "aprovado"
        registro["aprovado_por"] = aprovador[:80]
        registro["aprovado_em"] = hora
        registro["fonte_aprovacao"] = canal
        registro["atualizado_em"] = hora
        estado["planos"][pid] = registro
        _registrar(estado, "plano_aprovado", pid, quem=aprovador,
                   quando=hora, fonte=canal)
        _salvar(estado)
    _log_event("workflow_gate", decisao="aprovacao", gate="plano_aprovado",
               plano_id=pid, quem=aprovador, quando=hora)
    return registro


def definir_fase(plano, fase: str, *, quando: str | None = None) -> dict[str, Any]:
    """Move o plano no ciclo. Só se avança para fase aprovada por aprovação."""
    if fase not in FASES:
        raise ValueError(f"fase {fase!r} fora do conjunto fechado: "
                         + ", ".join(FASES))
    if fase in FASES_APROVADAS:
        raise ValueError("fase aprovada se alcança com aprovar_plano(quem=), "
                         "não com definir_fase")
    pid = _validar_id(plano, "id de plano")
    with _LOCK:
        estado = _carregar()
        registro = estado["planos"].get(pid)
        if registro is None:
            raise ValueError(f"{MENSAGEM_SEM_PLANO}: plano {pid} não cadastrado")
        registro["fase"] = fase
        registro["atualizado_em"] = _agora(quando)
        estado["planos"][pid] = registro
        _registrar(estado, "fase_mudou", pid, fase=fase)
        _salvar(estado)
    return registro


def registrar_tarefa(plano, tarefa, titulo: str = "", *,
                     quando: str | None = None, _estado=None) -> dict[str, Any]:
    """Prende uma tarefa a um plano, para o portão responder pelo id da tarefa.

    Aceita o id solto, ``("id", "título")`` ou ``{"id"|"tarefa", "titulo"}`` --
    é o que sai das listas de ``steps/tasks/`` do cofre.
    """
    bruto = tarefa
    if isinstance(tarefa, dict):
        titulo = titulo or str(tarefa.get("titulo") or tarefa.get("descricao") or "")
        bruto = tarefa.get("id") or tarefa.get("tarefa") or tarefa.get("task_id") or ""
    elif isinstance(tarefa, (tuple, list)) and tarefa:
        titulo = titulo or str(tarefa[1] if len(tarefa) > 1 else "")
        bruto = tarefa[0]
    tid = _validar_id(bruto, "id de tarefa")
    hora = _agora(quando)
    if _estado is not None:
        estado, registro = _estado
    else:
        pid = _validar_id(plano, "id de plano")
        estado = _carregar()
        registro = estado["planos"].get(pid)
        if registro is None:
            raise ValueError(f"{MENSAGEM_SEM_PLANO}: plano {pid} não cadastrado")
    registro.setdefault("tarefas", {})[tid] = {
        "id": tid, "titulo": _redigido(titulo), "desde": hora,
    }
    if _estado is None:
        estado["planos"][registro["id"]] = registro
        _registrar(estado, "tarefa_registrada", registro["id"], tarefa=tid)
        _salvar(estado)
    return registro


def definir_tarefa_ativa(referencia) -> str:
    """Marca qual é a tarefa/turno corrente (o que o portão assume quando chamado sem referência)."""
    alvo = _normalizar(referencia)
    with _LOCK:
        estado = _carregar()
        estado["tarefa_ativa"] = alvo[:120]
        _salvar(estado)
    return alvo


def plano_de(referencia) -> dict[str, Any] | None:
    """O plano que atende a referência (id do plano, id de tarefa ou arquivo)."""
    estado = _carregar()
    alvo = _normalizar(referencia) or estado.get("tarefa_ativa", "")
    return _plano_do_estado(alvo, estado)


def resumo() -> dict[str, Any]:
    """Contagens por fase + a tarefa ativa: para painel e para a auditoria."""
    estado = _carregar()
    por_fase = {fase: 0 for fase in FASES}
    for plano in estado["planos"].values():
        por_fase[str(plano.get("fase") or "")] = \
            por_fase.get(str(plano.get("fase") or ""), 0) + 1
    return {"planos": len(estado["planos"]), "por_fase": por_fase,
            "tarefa_ativa": estado.get("tarefa_ativa", ""),
            "ativo": ativo(), "arquivo_config": str(config_path()),
            "arquivo_estado": str(state_path())}


# ── O PORTÃO ────────────────────────────────────────────────────────────────

def _bloqueado(gate: str, motivo: str, referencia: str, *,
               plano_id: str = "", fase: str = "",
               plano: dict[str, Any] | None = None,
               dica: str = "") -> GateResult:
    """Monta o veredito fechado e grava a linha de evento correspondente.

    A mensagem é sempre a do dono (:data:`MENSAGEM_SEM_PLANO`), com a dica
    depois do travessão: o motivo técnico fica em ``motivo`` para o log e
    para a auditoria, e o que o assistente lê é a instrução.
    """
    mensagem = MENSAGEM_SEM_PLANO + (f" — {dica}" if dica else "")
    resultado = GateResult(ok=False, gate=gate, motivo=motivo,
                           referencia=referencia[:120], plano_id=plano_id,
                           fase=fase, mensagem=mensagem, plano=plano or {})
    _log_event("workflow_gate", decisao="bloqueado", gate=gate,
               motivo=motivo, referencia=referencia, plano_id=plano_id,
               fase=fase)
    return resultado


def _desligado(referencia: Any, motivo: str = "gate_desligado_por_config",
               detalhe: str = "") -> GateResult:
    """Veredito do portão desligado: PASSA, e é a mesma cara do comportamento de hoje.

    Desligar é opção do dono; o que não é opção é o portão mentir. Por isso o
    bloqueio aqui nunca vem embrulhado em ``ok=False`` -- quem consulta recebe
    ``ok=True``, o log recebe ``decisao=liberado gate=desligado`` e a auditoria
    consegue contar quantos pedidos passaram sem plano porque o portão estava
    aberto.
    """
    resultado = GateResult(ok=True, gate="desligado", motivo=motivo,
                           referencia=_normalizar(referencia)[:120],
                           mensagem="portão desligado na config; seguindo "
                                    "como antes" + (f" ({detalhe})" if detalhe
                                                    else ""))
    _log_event("workflow_gate", decisao="liberado", gate="desligado",
               motivo=motivo, referencia=_normalizar(referencia),
               detalhe=detalhe)
    return resultado


def require_plan_approved(referencia: Any = "") -> GateResult:
    """O portão: só passa quem tem plano APROVADO para o que vai executar.

    ``referencia`` é o id do plano, o id de uma tarefa dele, ou um caminho de
    arquivo do cofre. Vazio, vale a tarefa ativa
    (:func:`definir_tarefa_ativa`).

    Devolve :class:`GateResult`. Nos casos de bloqueio a mensagem é sempre
    :data:`MENSAGEM_SEM_PLANO` -- "Preciso de um plano pra isso" -- com o
    motivo curto ao lado, porque o ponto do portão é lembrar o assistente de
    QUE ele tem de criar o plano (e mostrar ao dono) em vez de executar
    direto. Ordem do dono, 09/11/2026.

    As duas saídas que NÃO bloqueiam, e por quê:

    * :func:`ativo` desligado (``config/workflow_gate.json``, lido a cada
      chamada) -- a regra do dono é "gate desligado = hoje".
    * erro interno embaixo do portão (estado ilegível de um jeito que a
      leitura não previu, disco sumindo no meio, bug meu) -- "gate ligado
      nunca pode quebrar o que já funciona". O erro é logado com o tipo e o
      veredito sai ``ok=True`` com ``gate="desligado"``,
      ``motivo="erro_interno:..."``. Um portão que derruba o assistente é um
      portão que vão desligar no susto, e a lei morre ali.
    """
    if not ativo():
        return _desligado(referencia)
    try:
        return _decidir(referencia)
    except Exception as exc:  # noqa: BLE001 - ver docstring: fail-open medido
        _log_event("workflow_gate", decisao="erro_interno",
                   motivo=f"{type(exc).__name__}:{str(exc)[:160]}",
                   referencia=_normalizar(referencia))
        return _desligado(referencia, motivo=f"erro_interno:{type(exc).__name__}",
                          detalhe=str(exc)[:160])


def _decidir(referencia: Any) -> GateResult:
    """O corpo do portão: estado em disco → plano → fase → arquivo."""
    estado = _carregar()
    alvo = _normalizar(referencia) or str(estado.get("tarefa_ativa") or "")
    if not alvo:
        return _bloqueado("plano_aprovado", "sem_referencia_e_sem_tarefa_ativa",
                          "", dica="diga de qual tarefa você está falando "
                                    "(definir_tarefa_ativa) ou passe a "
                                    "referência")
    registro = _plano_do_estado(alvo, estado)
    if registro is None:
        return _bloqueado("plano_existe", "nenhum_plano_para_a_referencia",
                          alvo,
                          dica="escreva o plano no cofre "
                               "(~/Jarvis/Planos/<plano>/plan.md), registre com "
                               "registrar_plano e peça a aprovação do dono")
    pid = str(registro.get("id") or "")
    fase = str(registro.get("fase") or "")
    if fase not in FASES_APROVADAS:
        return _bloqueado("plano_aprovado", f"plano_na_fase_{fase or 'vazia'}",
                          alvo, plano_id=pid, fase=fase, plano=registro,
                          dica=f"o plano {pid} existe mas está em "
                               f"'{fase or 'sem fase'}': nada executa antes "
                               "da aprovação do dono")
    arquivo = str(registro.get("arquivo") or "")
    if not arquivo:
        return _bloqueado("plano_aprovado", "aprovacao_sem_arquivo_de_plano",
                          alvo, plano_id=pid, fase=fase, plano=registro,
                          dica="aprovação sem documento não é aprovação: "
                               "registre o arquivo do cofre no plano")
    if not Path(arquivo).expanduser().is_file():
        return _bloqueado("plano_aprovado", "arquivo_de_plano_inexistente",
                          alvo, plano_id=pid, fase=fase, plano=registro,
                          dica=f"o plano aprovado aponta para {arquivo}, que "
                               "não existe no disco")
    resultado = GateResult(ok=True, gate="plano_aprovado", motivo="",
                           referencia=alvo[:120], plano_id=pid, fase=fase,
                           plano=registro,
                           mensagem=f"plano {pid} aprovado por "
                                    f"{registro.get('aprovado_por') or '?'} "
                                    f"em {registro.get('aprovado_em') or '?'}; "
                                    "execute tarefa por tarefa")
    _log_event("workflow_gate", decisao="liberado", gate="plano_aprovado",
               referencia=alvo, plano_id=pid, fase=fase)
    return resultado


def exigir_plano_aprovado(referencia: Any = "") -> dict[str, Any]:
    """Versão que levanta: para quem não pode esquecer de conferir o veredito.

    O ``RuntimeError`` carrega exatamente a frase do dono, então um stack trace
    no log já diz o que faltava: plano, não execução.
    """
    resultado = require_plan_approved(referencia)
    if not resultado:
        raise SemPlanoAprovado(resultado.texto())
    return resultado.plano
