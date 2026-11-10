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

* AS FASES E TAREFAS DENTRO DO PLANO (defeito ``DEF-20261110-104454``, ordem
  do dono de 10/11/2026: "quebrar em fases e tasks e executar" -- e a cobrança
  dele, à letra: "o estranho é que nao tem FASES E TASKS de NADA QUE FEZ
  HOJE.. GINO EU DEI A ORDEM DIRETA DE CRIAR FASES E TAREFAS E VOCE DISSE QUE
  IRIA FAZER ISSO.. CADE!???"). A raiz medida no código: ``fase`` só existia no
  nível do PLANO, como ciclo de vida, e a tarefa não tinha fase própria,
  status, commit nem evidência -- o portão sabia que "plano aprovado" e nada
  sobre o que o plano TEM. Hoje a tarefa tem ``fase`` (rótulo dentro do plano),
  ``status`` (:data:`STATUS_DE_TAREFA`), ``commit``, ``evidencia``, ``nota``,
  ``quem``/quando da conclusão; :func:`quebrar_plano` grava a árvore toda
  idempotente, :func:`iniciar_tarefa` / :func:`concluir_tarefa` movem status,
  :func:`quadro` devolve fase → tarefa → status e :func:`resumo` conta por
  status. A Fila (:func:`empurrar_para_fila` / :func:`proxima_da_fila`) existe
  porque ele manda jogar plano na fila -- a ordem é dele, nunca minha.
  Estado antigo, sem esses campos, é lido e normalizado (:func:`_carregar`):
  nada explode e nada é inventado -- tarefa velha vira ``pendente`` sem fase
  declarada, nunca "concluída" por suposição.

* A BRANCH DE TRABALHO (ordem do dono, 10/11/2026: "já implementa no fluxo de
  trabalho para as fases e tasks criadas ter lá qual branch elas devem
  trabalhar... você define a branch daquele plano ou daquela fase.. fica mais
  fácil gerir a worktree"). Vocabulário fechado (:data:`BRANCH_PADRAO_PREFIXO`
  + slug, :data:`BRANCHES_HISTORICAS`, ``""``), validado em
  :func:`validar_branch` com slug no espírito de ``core/obsidian.py``.
  :func:`registrar_plano` já nasce com ``plano/<slug>``,
  :func:`quebrar_plano` grava ramo por fase e por tarefa sem pisar o que está
  registrado, :func:`concluir_tarefa` expõe de onde o commit veio (sem ramo
  declarado é trilha dizendo "branch não declarada", nunca exceção --
  precedente: ``actiongate.ORIGIN_UNDECLARED``) e :func:`definir_branch`
  corrige endereço com nota. O :func:`quadro` e o :func:`resumo` contam por
  branch para a pergunta "o que está em que branch" ter resposta no disco.

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
import unicodedata
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

#: Status fechado da TAREFA dentro do plano (defeito DEF-20261110-104454).
#: Vocabulário fechado de propósito: "quase pronta", "feita depois" e
#: "em andamentozinho" são o que deixou a casa sem saber o que foi entregue
#: hoje. Fora daqui é recusa, não interpretação.
STATUS_DE_TAREFA = ("pendente", "em_progresso", "concluida", "bloqueada")
#: Rótulo do cesto das tarefas velhas que nasceram sem fase declarada. Não é
#: fase inventada: é o nome honesto do buraco que o estado antigo tem.
SEM_FASE = "(sem fase)"
#: O commit curto ou longo que fecha uma tarefa. Texto livre aqui seria o
#: caminho mais curto para um "feito" sem prova entrar no estado.
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
#: Rótulo de fase: curto, de uma linha, lido no celular do dono.
_FASE_MAX_CHARS = 80

#: A BRANCH DE TRABALHO (ordem do dono, WhatsApp 10/11/2026: "ja implementa no
#: fluxo de trabalho para as fases e tasks criadas ter la qual branch elas
#: devem trabalhar... voce define a branch daquele plano ou daquela fase.. fica
#: mais facil gerir a worktree"). Vocabulário FECHADO de propósito, como o do
#: status: ``plano/<slug-do-plano>`` (a convenção, sempre válida), ``main``
#: (fato histórico -- foi nela que entraram as conclusões anteriores a este
#: campo) e ``""`` (não declarado). ``feature/login``, ``hotfix-urgente`` e
#: afins são recusa, não criatividade: quem define a lei do repo é o dono, e um
#: vocabulário aberto transformaria o quadro em depósito de nome que ninguém
#: consegue parear com uma worktree.
BRANCH_PADRAO_PREFIXO = "plano/"
BRANCHES_HISTORICAS = ("main",)
#: O ramo NÃO declarado tem de aparecer na cara do dono, não sumir: os dois
#: rótulos abaixo são o cesto do quadro e a linha da trilha. Precedente no
#: repo: ``actiongate.ORIGIN_UNDECLARED`` (``actions/home_control.py:419``).
RAMO_SEM_DECLARACAO = "(branch não declarada)"
TRILHA_SEM_RAMO = "branch não declarada"
#: Slug no mesmo espírito de ``core/obsidian.py`` (``ascii_slug``): ASCII, sem
#: acento, sem espaço, hífen simples, limite -- aqui o limite do plano
#: (``PLAN_SLUG_MAX_CHARS``) mais o prefixo.
_BRANCH_SLUG_MAX_CHARS = 60
_BRANCH_MAX_CHARS = 80
_BRANCH_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Formato do estado. ``1`` = plano, fase do plano e tarefa plana; ``2`` =
#: tarefa com fase/status/commit/evidencia + ``fases`` no plano + ``fila``;
#: ``3`` = branch por plano/fase/tarefa (``branch``, ``branch_por_fase``).
#: A leitura normaliza o 1 e o 2 para o 3 em memória (:func:`_carregar`), então
#: um estado velho nunca derruba o portão e nunca perde chave.
VERSAO_ESTADO = 3

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
    return {"versao": VERSAO_ESTADO, "tarefa_ativa": "", "planos": {},
            "fila": [], "historico": [], "atualizado": ""}


# ── normalização retrocompatível (defeito DEF-20261110-104454) ──────────────

#: O ciclo de vida do PLANO e a fase de EXECUÇÃO da tarefa têm o mesmo nome
#: na boca do dono ("fase"). Um valor do ciclo colado na tarefa não é fase de
#: obra: é a confusão que o portão tem de recusar em vez de engolir.
_CICLO_DO_PLANO = frozenset(FASES)


def _rotulo_de_fase(item: Any) -> str:
    """O rótulo de uma fase, vindo de texto ou de ``{"fase"|"rotulo"|"titulo"}``."""
    if isinstance(item, dict):
        bruto = (item.get("fase") or item.get("rotulo") or item.get("titulo")
                 or item.get("nome") or "")
    else:
        bruto = item
    return _normalizar(bruto)[:_FASE_MAX_CHARS]


def _validar_rotulo(item: Any, o_que_e: str = "fase") -> str:
    """Rótulo utilizável ou recusa com o nome do que falta (nunca adivinha)."""
    bruto = _rotulo_de_fase(item)
    if not bruto:
        raise ValueError(f"{o_que_e} sem rótulo: passe o nome da fase, ex. "
                         "\"F1 — nucleo do agendador\" (o dono lê isto no "
                         "celular)")
    if bruto.lower() in _CICLO_DO_PLANO:
        raise ValueError(
            f"{o_que_e} {bruto!r} é fase do CICLO DO PLANO (rascunho / "
            "aguardando_aprovacao / aprovado / executando / encerrado), não "
            f"fase de execução dentro do {o_que_e}: dê um rótulo à fase da "
            "obra, ex. \"F1 — nucleo do agendador\"")
    if bruto == SEM_FASE or bruto.startswith("("):
        raise ValueError(f"{o_que_e} {bruto!r} é o cesto de leitura, não um "
                         "rótulo declarável")
    return bruto


def _commit_seguro(texto: Any) -> str:
    """Hash de commit normalizado, ou "" quando não parece hash.

    Leitura nunca levanta: um ``"commit": "feito, viu?"`` colado por engano no
    estado velho volta ao vazio e aparece como falta no :func:`quadro`.
    """
    bruto = str(texto or "").strip().lower()
    return bruto if _COMMIT_RE.match(bruto) else ""


def _validar_commit(texto: Any, o_que_e: str) -> str:
    bruto = str(texto or "").strip()
    if not bruto:
        return ""
    alvo = _commit_seguro(bruto)
    if not alvo:
        raise ValueError(f"{o_que_e} {bruto!r} não é hash de commit (7 a 40 "
                         "hex, minúsculo): evidência de conclusão é commit "
                         "verificável com `git show`, não frase")
    return alvo


def _validar_status(texto: Any, o_que_e: str) -> str:
    bruto = str(texto or "").strip().lower().replace(" ", "_").strip("_")
    if bruto not in STATUS_DE_TAREFA:
        raise ValueError(f"status {texto!r} de {o_que_e} fora do conjunto "
                         f"fechado: {', '.join(STATUS_DE_TAREFA)} "
                         "(fora daqui é recusa, não interpretação)")
    return bruto


def _slug_de_branch(texto: Any, limite: int = _BRANCH_SLUG_MAX_CHARS) -> str:
    """Slug ASCII de um nome livre, no espírito de ``core/obsidian.py:ascii_slug``.

    Sem acento (NFKD + fora os combining), minúsculo, sem espaço, hífen
    simples, e corte no hífen mais próximo do limite para não deixar palavra
    partida nem ``-`` sobrando. Devolve "" quando não sobra nada: quem chama
    decide o que fazer com o vazio (recusa, no portão), nunca inventa nome.
    """
    folded = unicodedata.normalize("NFKD", str(texto or ""))
    folded = "".join(ch for ch in folded
                     if not unicodedata.combining(ch)).lower()
    folded = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if len(folded) > limite:
        head = folded[:limite]
        folded = head.rsplit("-", 1)[0] if "-" in head else head
        folded = folded.strip("-")
    return folded


def _ramo_utilizable(bruto: str) -> bool:
    """O nome já bate no vocabulário fechado? (sem mensagem, só veredito)"""
    if bruto in BRANCHES_HISTORICAS:
        return True
    if not bruto.startswith(BRANCH_PADRAO_PREFIXO):
        return False
    slug = bruto[len(BRANCH_PADRAO_PREFIXO):]
    return bool(slug and _BRANCH_SLUG_RE.match(slug)
                and len(bruto) <= _BRANCH_MAX_CHARS)


def _recusa_de_ramo(texto: Any, o_que_e: str) -> ValueError:
    return ValueError(
        f"branch {str(texto)!r} de {o_que_e} fora do vocabulário fechado: "
        f"{BRANCH_PADRAO_PREFIXO}<slug-do-plano> (slug ASCII, minúsculo, sem "
        f"acento, sem espaço, até {_BRANCH_SLUG_MAX_CHARS} chars), "
        f"{', '.join(BRANCHES_HISTORICAS)} (histórico) ou \"\" (não "
        f"declarado) -- fora daqui é recusa, não interpretação, porque o nome "
        f"do ramo é o que faz a worktree ser gerível")


def validar_branch(texto: Any, o_que_e: str) -> str:
    """Branch dentro do vocabulário fechado, ou recusa nomeando o vocabulário.

    ``""`` É vocabulário: significa "não declarado", não "esqueci de passar".
    Exposição pública porque :func:`quebrar_plano`, :func:`concluir_tarefa` e
    :func:`definir_branch` validam pela mesma porta -- um nome torto escrito
    num canto e bonito noutro é como o quadro deixa de responder "o que está
    em que branch".
    """
    bruto = _normalizar(texto)
    if not bruto:
        return ""
    if not _ramo_utilizable(bruto):
        raise _recusa_de_ramo(texto, o_que_e)
    return bruto


_validar_branch = validar_branch


def _ramo_de_leitura(texto: Any) -> str:
    """Leitura nunca levanta: ramo utilizável ou "" (o cesto do quadro).

    Irmão do :func:`_commit_seguro`: um ``"branch": "tudo em mim"`` colado à
    mão no estado não derruba o portão, volta ao vazio e aparece como
    "não declarada" na cara do dono.
    """
    bruto = _normalizar(texto)
    return bruto if _ramo_utilizable(bruto) else ""


def branch_do_plano(plano) -> str:
    """A convenção do dono para o plano: ``plano/<slug-do-plano>``.

    Derivado do id, nunca do título livre: o id já é o que o portão usa como
    chave, então a branch derivada dele pareia com o plano em qualquer quadro.
    Nome que não produz slug ASCII é recusa nomeando o que falta.
    """
    pid = _validar_id(plano, "id de plano")
    slug = _slug_de_branch(pid)
    if not slug:
        raise ValueError(f"plano {str(plano)!r} não produz slug de branch: a "
                         f"convenção é {BRANCH_PADRAO_PREFIXO}<slug> e o slug "
                         "precisa de letra ou número ASCII (sem acento)")
    return f"{BRANCH_PADRAO_PREFIXO}{slug}"


def _normalizar_tarefa(dado: Any, tid: str = "") -> dict[str, Any]:
    """Uma tarefa velha (id + título) vira a tarefa completa, sem invenção.

    O que o estado antigo NÃO dizia não se deduz: ``status`` de tarefa sem
    status é ``pendente`` (ninguém marcou o contrário), ``fase`` é ``""``
    (o cesto :data:`SEM_FASE` diz isto na cara do :func:`quadro`), commit e
    evidência ficam vazios. Marcara ``concluida`` por suposição era pior que
    o buraco: o dono perguntaria "o que foi feito hoje" e eu responderia com
    uma dedução.
    """
    bruto = dict(dado) if isinstance(dado, dict) else {}
    ident = str(bruto.get("id") or tid or "").strip()
    fase = _rotulo_de_fase(bruto.get("fase"))
    status = str(bruto.get("status") or "").strip().lower().replace(" ", "_")
    if status not in STATUS_DE_TAREFA:
        # leitura não recusa: estado velho volta ao que é verdade por omissão.
        status = "pendente"
    tarefa = {
        "id": ident,
        "titulo": str(bruto.get("titulo") or ""),
        "fase": fase,
        # estado velho não tinha branch: "" é o nome honesto disso, nunca
        # "main" por suposição (a suposição inventaria origem de commit).
        "branch": _ramo_de_leitura(bruto.get("branch")),
        "status": status,
        "commit": _commit_seguro(bruto.get("commit")),
        "evidencia": str(bruto.get("evidencia") or "")[:160],
        "nota": str(bruto.get("nota") or "")[:160],
        "desde": str(bruto.get("desde") or ""),
        "concluida_em": str(bruto.get("concluida_em") or ""),
        "concluida_por": str(bruto.get("concluida_por") or ""),
    }
    if bruto.get("em_progresso_desde"):
        tarefa["em_progresso_desde"] = str(bruto["em_progresso_desde"])
    # Chaves que eu não conheço continuam onde estavam: normalizar não é
    # podar o estado do dono.
    for chave, valor in bruto.items():
        tarefa.setdefault(chave, valor)
    return tarefa


def _normalizar_plano(plano: dict[str, Any]) -> dict[str, Any]:
    """Plano do estado antigo (sem ``fases``) ou novo: sempre as duas chaves.

    Aditivo por desenho: ``dict(plano)`` primeiro, campos novos depois. Uma
    chave que o registro tinha não desaparece porque eu refinei a leitura.
    """
    registro = dict(plano)
    registro["id"] = str(registro.get("id") or "")
    registro["fase"] = str(registro.get("fase") or "")
    registro["branch"] = _ramo_de_leitura(registro.get("branch"))
    fases = registro.get("fases")
    if isinstance(fases, list):
        vistos: list[str] = []
        for item in fases:
            rotulo = _rotulo_de_fase(item)
            if rotulo and rotulo not in vistos:
                vistos.append(rotulo)
        registro["fases"] = vistos
    else:
        registro["fases"] = []
    # branch por fase: dicionário {rótulo: ramo}. ``fases`` continua lista de
    # rótulos (o quadro e os testes leem isto assim desde 910e568); o ramo
    # mora ao lado para a reinscrição não precisar virar objeto.
    ramos: dict[str, str] = {}
    bruto_ramos = registro.get("branch_por_fase")
    if isinstance(bruto_ramos, dict):
        for rotulo, valor in bruto_ramos.items():
            ramo = _ramo_de_leitura(valor)
            if rotulo and ramo:
                ramos[str(rotulo)] = ramo
    registro["branch_por_fase"] = ramos
    tarefas = registro.get("tarefas")
    norm: dict[str, Any] = {}
    if isinstance(tarefas, dict):
        for tid, dado in tarefas.items():
            ident = str(tid or "").strip()
            if not ident:
                continue
            norm[ident] = _normalizar_tarefa(dado, ident)
    elif isinstance(tarefas, (list, tuple)):
        # estado que alguém escreveu à mão como lista: ainda é lido.
        for dado in tarefas:
            bruto = dado if isinstance(dado, dict) else {"id": str(dado)}
            ident = str(bruto.get("id") or "").strip()
            if ident:
                norm[ident] = _normalizar_tarefa(bruto, ident)
    registro["tarefas"] = norm
    return registro


def _carregar() -> dict[str, Any]:
    """Lê o estado; JSON corrompido ou ausente volta ao estado vazio.

    Não levanta de propósito: portão que quebra com estado podre é portão que
    ninguém religa. O histórico perdido é custo menor que execução sem plano.

    A leitura é ADITIVA: ``versao`` 1 (tarefa plana, sem fila) sobe para a
    forma da ``versao`` 2 sem perder plano, tarefa, chave desconhecida nem
    linha de histórico -- é o que permite mexer no estado REAL do dono com
    segurança depois de meses de registro.
    """
    caminho = state_path()
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _vazio()
    if not isinstance(bruto, dict):
        return _vazio()
    estado = _vazio()
    for chave, valor in bruto.items():
        if chave not in ("versao", "tarefa_ativa", "planos", "fila",
                        "historico"):
            estado[chave] = valor
    estado["versao"] = VERSAO_ESTADO
    estado["tarefa_ativa"] = str(bruto.get("tarefa_ativa") or "")
    planos = bruto.get("planos")
    if isinstance(planos, dict):
        for pid, plano in planos.items():
            if isinstance(plano, dict):
                estado["planos"][str(pid)] = _normalizar_plano(plano)
    historico = bruto.get("historico")
    if isinstance(historico, list):
        estado["historico"] = [h for h in historico if isinstance(h, dict)]
    fila = bruto.get("fila")
    if isinstance(fila, list):
        vistos: list[str] = []
        for item in fila:
            ident = str(item or "").strip()
            if ident and ident not in vistos:
                vistos.append(ident)
        estado["fila"] = vistos
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
                    branch: str = "", quando: str | None = None
                    ) -> dict[str, Any]:
    """Cadastra (ou atualiza) um plano no estado do portão e devolve o registro.

    Fase inicial é ``aguardando_aprovacao``: plano recém-escrito NUNCA entra
    já aprovado, e ``fase="aprovado"`` aqui não é atalho -- aprovação se faz
    com :func:`aprovar_plano`, com nome e data de quem aprovou.

    A branch é a outra novidade que o dono pediu (10/11/2026): sem ``branch=``
    o plano já nasce com a convenção ``plano/<slug-do-plano>``, então
    "qual branch deste plano" tem resposta antes da primeira tarefa.
    Reinserção não pisa ramo já registrado; ``branch=""`` aqui NÃO significa
    "apague o ramo" (para isso existe :func:`definir_branch`, com trilha).
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
        registro = dict(anterior)
        registro.update({
            "id": pid,
            "titulo": _redigido(titulo or anterior.get("titulo") or pid) or pid,
            "arquivo": _normalizar(arquivo or anterior.get("arquivo")),
            "fase": fase if not anterior else str(
                anterior.get("fase") or fase),
            "aprovado_por": str(anterior.get("aprovado_por") or ""),
            "aprovado_em": str(anterior.get("aprovado_em") or ""),
            "tarefas": dict(anterior.get("tarefas") or {}),
            "fases": list(anterior.get("fases") or []),
            "branch_por_fase": dict(anterior.get("branch_por_fase") or {}),
            "branch": (_validar_branch(branch, f"plano {pid}")
                       or str(anterior.get("branch") or "")
                       or branch_do_plano(pid)),
            "criado_em": str(anterior.get("criado_em") or hora),
            "atualizado_em": hora,
        })
        for tarefa in tarefas or ():
            registrar_tarefa(pid, tarefa, quando=quando, _estado=(estado, registro))
        estado["planos"][pid] = registro
        _registrar(estado, "plano_registrado", pid, fase=registro["fase"],
                   arquivo=registro["arquivo"],
                   branch=registro["branch"] or TRILHA_SEM_RAMO)
        _salvar(estado)
    _log_event("workflow_gate", decisao="registro", gate="plano_existe",
               plano_id=pid, fase=registro["fase"],
               branch=registro["branch"] or TRILHA_SEM_RAMO)
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
                     quando: str | None = None, fase: str = "",
                     status: str = "", commit: str = "", evidencia: str = "",
                     nota: str = "", branch: str = "",
                     _estado=None) -> dict[str, Any]:
    """Prende uma tarefa a um plano, para o portão responder pelo id da tarefa.

    Aceita o id solto, ``("id", "título")`` ou ``{"id"|"tarefa", "titulo"}`` --
    é o que sai das listas de ``steps/tasks/`` do cofre; os campos novos
    (``fase``, ``status``, ``commit``, ``evidencia``, ``nota``) podem vir no
    dicionário ou nas palavras-chave.

    Duas recusas, e são a lei do DEF-20261110-104454 na porta de escrita:

    * plano que JÁ TEM fases declaradas não ganha tarefa fora de fase -- a
      recusa nomeia as fases disponíveis;
    * ``status="concluida"`` não se alcança por aqui: conclusão é ato com
      ``quem``, ``quando`` e prova (:func:`concluir_tarefa`).

    Registrar de novo a mesma tarefa é aditivo: status, commit e evidência já
    gravados não são pisados por uma reinscrição que não os traz.
    """
    bruto = tarefa
    campo_fase, campo_status = fase, status
    campo_commit, campo_evidencia, campo_nota = commit, evidencia, nota
    campo_ramo = branch
    if isinstance(tarefa, dict):
        titulo = titulo or str(tarefa.get("titulo") or tarefa.get("descricao") or "")
        bruto = tarefa.get("id") or tarefa.get("tarefa") or tarefa.get("task_id") or ""
        campo_fase = fase or str(tarefa.get("fase") or "")
        campo_status = status or str(tarefa.get("status") or "")
        campo_commit = commit or tarefa.get("commit") or ""
        campo_evidencia = evidencia or tarefa.get("evidencia") or ""
        campo_nota = nota or tarefa.get("nota") or ""
        campo_ramo = branch or str(tarefa.get("branch") or "")
    elif isinstance(tarefa, (tuple, list)) and tarefa:
        titulo = titulo or str(tarefa[1] if len(tarefa) > 1 else "")
        bruto = tarefa[0]
    tid = _validar_id(bruto, "id de tarefa")
    hora = _agora(quando)
    rotulo = _validar_rotulo(campo_fase, "fase") if campo_fase else ""
    novo_status = _validar_status(campo_status, tid) if campo_status else ""
    if novo_status == "concluida":
        raise ValueError(f"tarefa {tid} não se conclui por registrar_tarefa: "
                         "conclusão exige quem, quando e prova — use "
                         "concluir_tarefa")
    prova = _validar_commit(campo_commit, f"commit da tarefa {tid}")
    if prova and novo_status != "concluida":
        raise ValueError(f"tarefa {tid} traz commit {prova} sem concluir: "
                         "commit é evidência de conclusão, use "
                         "concluir_tarefa(commit=)")
    evid = _redigido(campo_evidencia)
    if _estado is not None:
        estado, registro = _estado
    else:
        pid = _validar_id(plano, "id de plano")
        estado = _carregar()
        registro = estado["planos"].get(pid)
        if registro is None:
            raise ValueError(f"{MENSAGEM_SEM_PLANO}: plano {pid} não cadastrado")
    declaradas = [str(item) for item in (registro.get("fases") or [])]
    if declaradas:
        if not rotulo:
            raise ValueError(
                f"tarefa {tid} sem fase num plano que já tem fases "
                f"({', '.join(declaradas)}): passe fase=\"...\" ou quebre o "
                "plano com quebrar_plano (defeito DEF-20261110-104454 é "
                "exatamente esta lacuna)")
        if rotulo not in declaradas:
            raise ValueError(f"fase {rotulo!r} da tarefa {tid} não está nas "
                             f"fases declaradas do plano: {', '.join(declaradas)}")
    elif rotulo:
        # o plano ainda não tem fases declaradas: a fase da tarefa declara a
        # primeira, em vez de sumir com o rótulo que o dono ia ler.
        declaradas.append(rotulo)
    anterior = dict((registro.get("tarefas") or {}).get(tid) or {})
    ramo = _validar_branch(campo_ramo, f"tarefa {tid}")
    if not ramo:
        # sem opinião dita, a tarefa trabalha onde o plano trabalha
        ramo = str(anterior.get("branch") or registro.get("branch") or "")
    registro.setdefault("tarefas", {})[tid] = _mesclar_tarefa(
        anterior, {
            "id": tid, "titulo": _redigido(titulo) or anterior.get("titulo", ""),
            "fase": rotulo or anterior.get("fase", ""),
            "branch": ramo,
            "status": novo_status, "commit": prova,
            "evidencia": evid, "nota": _redigido(campo_nota),
            "desde": anterior.get("desde") or hora,
        })
    if declaradas:
        registro["fases"] = declaradas
    if _estado is None:
        estado["planos"][registro["id"]] = registro
        _registrar(estado, "tarefa_registrada", registro["id"], tarefa=tid,
                   fase=rotulo, status=registro["tarefas"][tid]["status"],
                   branch=registro["tarefas"][tid].get("branch") or "")
        _salvar(estado)
    return registro


def _mesclar_tarefa(atual: dict[str, Any], novo: dict[str, Any]) -> dict[str, Any]:
    """Junta a tarefa que já existe com a reinscrição, sem apagar conclusão.

    A regra que o dono pode cobrar: ``concluida`` não se pisa, não se rebaixa
    e não perde o commit -- reinscrever o mapa de um plano é correção de
    mapa, não reescrita de história. Campo vazio em ``novo`` é "sem opinião",
    nunca "apaga". A branch segue a mesma lei: já registrada não some, e a
    reinscrição só acrescenta (é assim que o estado do dono ganha o ramo
    histórico das conclusões anteriores ao campo).
    """
    base = _normalizar_tarefa(atual if atual else novo,
                              str(novo.get("id") or ""))
    if not atual:
        return base
    if base.get("status") == "concluida":
        if novo.get("branch") and not base.get("branch"):
            base["branch"] = novo["branch"]
        return base
    for campo in ("titulo", "nota", "evidencia", "commit", "concluida_em",
                  "concluida_por"):
        if novo.get(campo):
            base[campo] = novo[campo]
    if novo.get("fase") and not base.get("fase"):
        base["fase"] = novo["fase"]
    if novo.get("branch"):
        base["branch"] = novo["branch"]
    if novo.get("status"):
        base["status"] = novo["status"]
    if not base.get("desde"):
        base["desde"] = novo.get("desde") or ""
    return base


def _procurar_tarefa(estado: dict[str, Any], referencia: Any,
                     ) -> tuple[str, str]:
    """``(plano_id, tarefa_id)`` da referência; recusa nomeando o que falta.

    Aceita ``plano:tarefa`` (a forma honesta quando um id vive em dois
    planos), o id solto da tarefa ou uma referência ao plano que a tem.
    Ambiguidade é dita, nunca resolvida em silêncio: foi assim que a casa
    confundiu duas tarefas de planos diferentes.
    """
    bruto = _normalizar(referencia)
    if not bruto:
        raise ValueError("referência de tarefa vazia: passe o id da tarefa ou "
                         "\"plano:tarefa\"")
    if ":" in bruto and not Path(bruto).is_absolute():
        pid, _, tid = bruto.partition(":")
        pid, tid = (_validar_id(pid, "id de plano"),
                    _validar_id(tid, "id de tarefa"))
        registro = estado["planos"].get(pid)
        if registro is None:
            raise ValueError(f"{MENSAGEM_SEM_PLANO}: plano {pid} não cadastrado "
                             f"(referência {bruto!r})")
        if tid not in (registro.get("tarefas") or {}):
            raise ValueError(
                f"tarefa {tid} não está no plano {pid}: declare-a "
                "(quebrar_plano) ou registre com registrar_tarefa")
        return pid, tid
    alvo = _id(bruto)
    achados: list[tuple[str, str]] = []
    for registro in estado["planos"].values():
        pid = str(registro.get("id") or "")
        for chave, dado in (registro.get("tarefas") or {}).items():
            nomes = {_id(chave), _id(str((dado or {}).get("id") or ""))}
            if alvo in nomes:
                achados.append((pid, str(chave)))
                break
    if not achados:
        raise ValueError(f"nenhuma tarefa {referencia!r} registrada: o portão "
                         "não carimba o que não tem mapa — declare a tarefa "
                         "(quebrar_plano) ou registre com registrar_tarefa")
    if len(achados) > 1:
        where = ", ".join(f"{pid}:{tid}" for pid, tid in achados)
        raise ValueError(f"tarefa {alvo} existe em {len(achados)} planos "
                         f"({where}): passe a forma qualificada <plano>:{alvo} "
                         "em vez de deixar o portão escolher")
    return achados[0]


def _exigir_plano_aprovado_para_efeito(registro: dict[str, Any],
                                      acao: str) -> None:
    """Nada se inicia nem se conclui num plano que o dono não aprovou.

    É o portão do lado da ESCRITA: o ``require_plan_approved`` guarda a
    execução, e aqui se recusa o carimbo de "feito" antes da aprovação --
    senão o estado ganharia conclusão que o dono nunca liberou.
    """
    fase = str(registro.get("fase") or "")
    if fase in FASES_APROVADAS:
        return
    raise ValueError(
        f"plano {registro.get('id')} está na fase '{fase or 'sem fase'}': "
        f"nada se {acao} antes da aprovação do dono (aprovar_plano com "
        "quem= e quando=)")


# ── FASES E TAREFAS DENTRO DO PLANO (defeito DEF-20261110-104454) ───────────

def _juntar_fases(atuais: list[str], novos: list[str]) -> list[str]:
    """Fases declaradas não se apagam por reinscrição: a ordem é preservada.

    Uma fase sem tarefa continua no mapa e aparece vazia no :func:`quadro` --
    sumir com o rótulo que o dono leu ontem seria o mesmo defeito de outra
    porta. Reordenar sozinho, idem: a ordem é dele, não minha.
    """
    juntas: list[str] = []
    for rotulo in list(atuais) + list(novos):
        if rotulo and rotulo not in juntas:
            juntas.append(rotulo)
    return juntas


def _fase_e_tarefas(item: Any, posicao: int
                    ) -> tuple[str, str | None, list[Any]]:
    """Uma entrada de ``quebrar_plano``: rótulo, branch e lista de tarefas.

    A branch sai ``None`` quando a fase não disse nada (herda o plano) e
    ``""`` quando ela disse explicitamente "não declarado" -- os dois têm de
    ser distinguíveis, senão corrigir um ramo escrito à mão no mapa viraria
    adivinhação. É o mesmo acordo da chave ``status``: presente é opinião,
    ausente é omissão.
    """
    ramo: str | None = None
    if isinstance(item, dict):
        rotulo = _validar_rotulo(item, f"fase {posicao + 1}")
        if "branch" in item:
            ramo = _validar_branch(item.get("branch"),
                                   f"branch da fase {posicao + 1}")
        tarefas = item.get("tarefas") if item.get("tarefas") is not None else (
            item.get("tasks") if item.get("tasks") is not None else [])
    elif isinstance(item, (tuple, list)) and len(item) == 2:
        rotulo = _validar_rotulo(item[0], f"fase {posicao + 1}")
        tarefas = item[1]
    elif isinstance(item, str):
        # fase declarada sem tarefa: o mapa pode ter fase vazia (é o que o
        # dono lê como "ainda não quebrada"), mas nunca tarefa sem fase.
        rotulo = _validar_rotulo(item, f"fase {posicao + 1}")
        tarefas = []
    else:
        raise ValueError(f"fase {posicao + 1} em formato inutilizável "
                         f"({type(item).__name__}): use "
                         "{\"fase\": \"F1 — ...\", \"tarefas\": [...]}, "
                         "(\"F1 — ...\", [...]) ou o rótulo sozinho")
    if isinstance(tarefas, dict):
        tarefas = [dict(valor, id=chave) if isinstance(valor, dict)
                   else (chave, valor) for chave, valor in tarefas.items()]
    elif isinstance(tarefas, str):
        tarefas = [tarefas]
    if not isinstance(tarefas, (list, tuple)):
        raise ValueError(f"tarefas da fase {rotulo!r} em formato inutilizável "
                         f"({type(tarefas).__name__}): passe lista de ids, "
                         "(id, título) ou dicionários")
    return rotulo, ramo, list(tarefas)


def _tarefa_de_fase(item: Any, rotulo: str, plano_id: str) -> dict[str, Any]:
    """Normaliza e confere UMA tarefa do mapa: fase, status, quem, prova."""
    if isinstance(item, dict):
        bruto = item
    elif isinstance(item, (tuple, list)) and item:
        bruto = {"id": item[0], "titulo": item[1] if len(item) > 1 else ""}
    else:
        bruto = {"id": item}
    tid = _validar_id(bruto.get("id") or bruto.get("tarefa") or "",
                      f"id de tarefa do plano {plano_id}")
    informada = _validar_rotulo(bruto.get("fase"), f"fase da tarefa {tid}") \
        if bruto.get("fase") else ""
    if informada and informada != rotulo:
        raise ValueError(f"tarefa {tid} está na fase {rotulo!r} mas carrega "
                         f"fase={informada!r}: ambiguidade não se resolve "
                         "sozinha -- escolha uma das duas")
    if "status" in bruto:
        # chave presente é opinião dita: ou está no conjunto fechado, ou é
        # recusa. Omitir a chave é "não disse" -> pendente por omissão.
        status = _validar_status(bruto.get("status"), tid)
    else:
        status = "pendente"
    commit = _validar_commit(bruto.get("commit"), f"commit da tarefa {tid}")
    evidencia = _redigido(bruto.get("evidencia"))
    # ``None`` = a tarefa não disse o ramo (herda da fase/plano); ``""`` =
    # disse "não declarado". Distinção idêntica à do status acima.
    ramo = (_validar_branch(bruto.get("branch"), f"branch da tarefa {tid}")
            if "branch" in bruto else None)
    if status == "concluida":
        quem = _normalizar(bruto.get("quem") or bruto.get("concluida_por"))
        if not quem:
            raise ValueError(f"tarefa {tid} marcada concluida sem quem: "
                             "conclusão é ato de alguém (quem= ou "
                             "concluida_por=), nunca estado padrão")
        if not commit and not evidencia:
            raise ValueError(f"tarefa {tid} marcada concluida sem prova: "
                             "passe commit=<hash> e/ou evidencia=\"arquivo:linha, "
                             "nota, teste\" (defeito DEF-20261110-104454 é o "
                             "\"fiz\" sem rastro)")
    return {
        "id": tid,
        "titulo": _redigido(bruto.get("titulo") or bruto.get("descricao")),
        "fase": rotulo,
        "branch": ramo,
        "status": status,
        "commit": commit,
        "evidencia": evidencia,
        "nota": _redigido(bruto.get("nota") or bruto.get("motivo")),
        "concluida_em": (_agora(bruto.get("quando") or bruto.get("concluida_em"))
                         if status == "concluida" else ""),
        "concluida_por": quem if status == "concluida" else "",
    }


def quebrar_plano(plano, fases, *, branch: str = "",
                  quando: str | None = None) -> dict[str, Any]:
    """Grava o mapa fase → tarefa → status (e branch) dentro do plano. Idempotente.

    Ordem do dono, WhatsApp 10/11/2026: "quebrar em fases e tasks e
    executar" e a cobrança "CADE!?". É o que faltava no módulo: ``fase`` só
    existia no ciclo de vida do plano, então nenhuma entrega podia virar
    "fase 2, tarefa 2.1, concluída no commit ``x``".

    Formas aceitas (as duas origens reais do mapa): lista de
    ``{"fase": ..., "tarefas": [...]}``, de ``("fase", [...])``, de rótulos
    soltos, e um dicionário ``{rótulo: [tarefas]}``. Cada tarefa aceita id
    solto, ``(id, título)`` ou dicionário com ``titulo``, ``status``,
    ``commit``, ``evidencia``, ``nota``, ``quem``/``quando``.

    Branch (ordem do dono de 10/11/2026): o mapa pode dizer ``branch=`` no
    plano, ``"branch": ...`` na fase e ``"branch": ...`` na tarefa. Quem não
    fala herda a fase, e quem não fala de fase herda o plano -- então
    "qual branch esta fase/tarefa deve trabalhar" tem resposta sem eu
    inventar nada. Chave ``branch`` ausente = herança; ``branch=""`` dito =
    "não declarado", que é vocabulário.

    As três garantias que fazem o re-rodar ser seguro no estado do dono:

    1. **não duplica**: tarefa já registrada no mesmo plano é atualizada no
       lugar, não criada outra vez;
    2. **não pisa o que está concluído**: status, commit, evidência, quem e
       quando de uma tarefa ``concluida`` são preservados mesmo se a nova
       chamada não os menciona ou menciona outra coisa;
    3. **não apaga**: tarefa fora do mapa continua no plano; se estava sem
       fase, fica no cesto :data:`SEM_FASE` do :func:`quadro`.

    Recusas (fail-closed): fase sem rótulo, rótulo do ciclo de vida do plano
    colado como fase de obra, tarefa sem id, status fora de
    :data:`STATUS_DE_TAREFA`, commit que não é hash, fase que discorda da
    tarefa, ``concluida`` sem ``quem`` e sem prova.
    """
    pid = _validar_id(plano, "id de plano")
    hora = _agora(quando)
    if isinstance(fases, dict):
        entradas = [{**valor, "fase": chave} if isinstance(valor, dict)
                    else {"fase": chave, "tarefas": valor}
                    for chave, valor in fases.items()]
    elif isinstance(fases, (list, tuple)):
        entradas = list(fases)
    else:
        raise ValueError(f"fases de {plano!r} em formato inutilizável "
                         f"({type(fases).__name__}): passe a lista de fases")
    if not entradas:
        raise ValueError(f"quebrar_plano({pid}, []) não quebra nada: passe as "
                         "fases com suas tarefas (ou o plano fica plano, que "
                         "é o defeito DEF-20261110-104454)")
    rotulos: list[str] = []
    ramos_de_fase: dict[str, str | None] = {}
    mapa: list[tuple[str, dict[str, Any]]] = []
    vistos: set[str] = set()
    for posicao, item in enumerate(entradas):
        rotulo, ramo_fase, tarefas = _fase_e_tarefas(item, posicao)
        if rotulo in rotulos:
            raise ValueError(f"fase {rotulo!r} declarada duas vezes: junte as "
                             "tarefas numa só fase")
        rotulos.append(rotulo)
        ramos_de_fase[rotulo] = ramo_fase
        for bruto in tarefas:
            tarefa = _tarefa_de_fase(bruto, rotulo, pid)
            if tarefa["id"] in vistos:
                raise ValueError(f"tarefa {tarefa['id']} aparece em duas fases "
                                 f"({rotulo} e outra): uma tarefa, uma fase")
            vistos.add(tarefa["id"])
            mapa.append((rotulo, tarefa))
    novas: list[str] = []
    preservadas: list[str] = []
    atualizadas: list[str] = []
    with _LOCK:
        estado = _carregar()
        registro = estado["planos"].get(pid)
        if registro is None:
            raise ValueError(f"{MENSAGEM_SEM_PLANO}: plano {pid} não cadastrado "
                             "-- registre o plano antes de quebrá-lo em fases")
        # Branch do plano: o mapa pode declarar, mas reescrever um ramo já
        # registrado por aqui seria apagar a origem das tarefas que estão
        # nele. Correção de ramo é ato com trilha (definir_branch).
        ramo_decl = _validar_branch(branch, f"plano {pid}")
        ramo_atual = str(registro.get("branch") or "")
        if ramo_decl and ramo_atual and ramo_decl != ramo_atual:
            raise ValueError(
                f"plano {pid} já trabalha na branch {ramo_atual!r} e o mapa "
                f"pede {ramo_decl!r}: isto reescreveria o endereço de todas "
                "as tarefas registradas -- corrija com definir_branch("
                f"{pid!r}, {ramo_decl!r}) para a trilha guardar as duas "
                "pontas")
        ramo_base = ramo_decl or ramo_atual
        # O mapa pode ter tarefa pendente e bloqueada em qualquer fase -- ter
        # o mapa não é executar. Mas status de ENTREGA (concluida,
        # em_progresso) escrito pelo mapa é a mesma afirmação que
        # :func:`concluir_tarefa` exige aprovada: sem aprovação do dono,
        # recusa. É a porta dos fundos que faltava fechar.
        pedidas = [t["id"] for _, t in mapa
                   if t["status"] in ("concluida", "em_progresso")]
        fase_plano = str(registro.get("fase") or "")
        if pedidas and fase_plano not in FASES_APROVADAS:
            raise ValueError(
                f"plano {pid} está na fase '{fase_plano or 'sem fase'}': "
                f"nenhuma tarefa se conclui nem inicia antes da aprovação do "
                f"dono -- {len(pedidas)} pediam status de entrega "
                f"({', '.join(pedidas[:6])})")
        registro["fases"] = _juntar_fases(list(registro.get("fases") or []),
                                          rotulos)
        registro["branch"] = ramo_base
        ramos_grifados = registro.setdefault("branch_por_fase", {})
        for rotulo, ramo_fase in ramos_de_fase.items():
            if ramo_fase is not None:
                ramos_grifados[rotulo] = ramo_fase
        registro.setdefault("tarefas", {})
        for _, tarefa in mapa:
            if tarefa.get("branch") is None:
                herdado = ramos_de_fase.get(str(tarefa.get("fase") or ""))
                tarefa["branch"] = ramo_base if herdado is None else herdado
            anterior = registro["tarefas"].get(tarefa["id"])
            if anterior and str((anterior or {}).get("status") or "") \
                    == "concluida":
                preservadas.append(tarefa["id"])
            elif anterior:
                atualizadas.append(tarefa["id"])
            else:
                novas.append(tarefa["id"])
            merged = _mesclar_tarefa(dict(anterior or {}), tarefa)
            if not merged.get("desde"):
                merged["desde"] = hora
            registro["tarefas"][tarefa["id"]] = merged
        registro["atualizado_em"] = hora
        estado["planos"][pid] = registro
        _registrar(estado, "plano_quebrado", pid,
                   fases=len(rotulos), tarefas=len(mapa), novas=len(novas),
                   atualizadas=len(atualizadas), preservadas=len(preservadas),
                   branch=ramo_base or TRILHA_SEM_RAMO,
                   ramos=len({str(dado.get("branch") or "")
                              for dado in registro["tarefas"].values()}))
        _salvar(estado)
    _log_event("workflow_gate", decisao="quebrar", gate="fases_e_tarefas",
               plano_id=pid, fases=len(rotulos), tarefas=len(mapa),
               novas=len(novas), preservadas=len(preservadas),
               branch=ramo_base or TRILHA_SEM_RAMO)
    return {"plano": pid, "titulo": str(registro.get("titulo") or pid),
            "fase_do_plano": str(registro.get("fase") or ""),
            "branch": ramo_base,
            "branch_do_plano": str(registro.get("branch") or ""),
            "branch_por_fase": dict(registro.get("branch_por_fase") or {}),
            "ramos": sorted({str(dado.get("branch") or "")
                             for dado in (registro["tarefas"] or {}).values()}),
            "fases": rotulos, "tarefas": len(registro["tarefas"]),
            "novas": novas, "atualizadas": atualizadas,
            "preservadas": preservadas,
            "sem_fase": sorted(tid for tid, dado
                               in (registro["tarefas"] or {}).items()
                               if not dado.get("fase"))}


def iniciar_tarefa(referencia, *, quando: str | None = None) -> dict[str, Any]:
    """Carimba ``em_progresso`` na tarefa e passa a apontar para ela.

    É o "estou fazendo isto agora" registrado em disco: sem ele, o portão
    saberia que o plano está aprovado mas não qual pedaço do mapa está vivo.
    A tarefa ativa é escrita NO MESMO estado desta mudança -- chamar
    :func:`definir_tarefa_ativa` aqui releria o disco e jogaria fora o que
    esta função acabou de acumular em memória (bug medido no smoke test: o
    ``tarefa_ativa`` voltava vazio).
    """
    hora = _agora(quando)
    with _LOCK:
        estado = _carregar()
        pid, tid = _procurar_tarefa(estado, referencia)
        registro = estado["planos"][pid]
        _exigir_plano_aprovado_para_efeito(registro, "inicie")
        tarefa = _normalizar_tarefa(registro["tarefas"][tid], tid)
        if tarefa.get("status") == "concluida":
            raise ValueError(f"tarefa {tid} já está concluída "
                             f"(commit {tarefa.get('commit') or 'sem hash'})"
                             ": iniciar de novo apagaria a conclusão -- "
                             "reabra com mudar_status(..., \"pendente\", "
                             "nota=<motivo>)")
        tarefa["status"] = "em_progresso"
        tarefa["em_progresso_desde"] = hora
        registro["tarefas"][tid] = tarefa
        registro["atualizado_em"] = hora
        estado["planos"][pid] = registro
        estado["tarefa_ativa"] = f"{pid}:{tid}"[:120]
        _registrar(estado, "tarefa_iniciada", pid, tarefa=tid,
                   fase=tarefa.get("fase") or "",
                   branch=tarefa.get("branch") or "", quando=hora)
        _salvar(estado)
    _log_event("workflow_gate", decisao="iniciar", gate="status_tarefa",
               plano_id=pid, tarefa_id=tid, status="em_progresso")
    return tarefa


def concluir_tarefa(referencia, *, commit: str = "", evidencia: str = "",
                    quem: str = "", quando: str | None = None,
                    nota: str = "", branch: str = "") -> dict[str, Any]:
    """Carimba ``concluida`` com quem, quando e prova. Sem prova, recusa.

    É a alavanca que faltava para a pergunta do dono ("o que foi feito
    hoje?") ter resposta em disco: a tarefa passa a levar o commit e a
    evidência do que a fecha, e a recusa nomeia exatamente o que falta.
    Conclusão não se faz por :func:`registrar_tarefa` nem por
    :func:`quebrar_plano` sem ``quem`` -- e num plano não aprovado, nem com.

    A branch entra junto do hash (ordem do dono de 10/11/2026): o commit só
    quer dizer algo se soubermos de onde ele veio. Quem chama sem ``branch=``
    não quebra -- o que já estava declarado no mapa fica, e o que não está
    vira ``""`` com a trilha dizendo "branch não declarada", no mesmo
    espírito de ``actiongate.ORIGIN_UNDECLARED``. Não herdo o ramo do plano
    calado aqui: a branch de onde o commit VEIO é fato, não convenção.
    """
    aprovador = _normalizar(quem)
    if not aprovador:
        raise ValueError("conclusão sem quem não existe: passe quem=<nome de "
                         "quem entregou> (o assistente com nome serve, o dono "
                         "não é carimbado por dedução)")
    if not _commit_seguro(commit) and not _normalizar(evidencia):
        raise ValueError(
            f"conclusão de {referencia!r} sem prova: passe commit=<hash> "
            "e/ou evidencia=\"arquivo:linha | nota do vault | teste\" — "
            "\"está pronto\" não é evidência (defeito DEF-20261110-104454)")
    hora = _agora(quando)
    prova = _validar_commit(commit, "commit")
    with _LOCK:
        estado = _carregar()
        pid, tid = _procurar_tarefa(estado, referencia)
        registro = estado["planos"][pid]
        _exigir_plano_aprovado_para_efeito(registro, "conclua")
        tarefa = _normalizar_tarefa(registro["tarefas"][tid], tid)
        se_ja = (tarefa.get("status") == "concluida")
        if se_ja and prova and tarefa.get("commit") \
                and prova != tarefa["commit"]:
            raise ValueError(
                f"tarefa {tid} já está concluída no commit {tarefa['commit']}: "
                f"concluir de novo em {prova} apagaria a evidência antiga -- "
                "reabra com mudar_status(..., \"pendente\", nota=<motivo>) e "
                "conclua em seguida, para o histórico guardar as duas pontas")
        ramo = _validar_branch(branch, f"branch da tarefa {tid}")
        ramo_antes = str(tarefa.get("branch") or "")
        if se_ja and ramo and ramo_antes and ramo != ramo_antes:
            raise ValueError(
                f"tarefa {tid} já declarou conclusão na branch "
                f"{ramo_antes!r} e você aponta {ramo!r}: a origem do commit "
                "não se reescreve em silêncio -- corrija com definir_branch"
                f"({pid!r}, {ramo!r}, tarefa={tid!r}, nota=<motivo>)")
        if se_ja:
            if not ramo or ramo == ramo_antes:
                # idempotente: repetir a conclusão com a mesma prova não
                # reescreve nada; o que muda é o texto do commit novo, e esse
                # caso já foi recusado acima.
                return tarefa
            # ramo que ainda não existia: acrescentar o endereço de onde o
            # commit veio NÃO reescreve a conclusão -- é exatamente o que o
            # campo novo precisa conseguir no estado que já tinha "feito"
            # sem branch nenhuma (as 22 conclusões históricas do dono).
            tarefa["branch"] = ramo
            registro["tarefas"][tid] = tarefa
            registro["atualizado_em"] = hora
            estado["planos"][pid] = registro
            _registrar(estado, "tarefa_ramo_acrescentado", pid, tarefa=tid,
                       branch=ramo, commit=str(tarefa.get("commit") or ""),
                       quando=_agora(tarefa.get("concluida_em") or hora))
            _salvar(estado)
            _log_event("workflow_gate", decisao="ramo", gate="status_tarefa",
                       plano_id=pid, tarefa_id=tid, branch=ramo,
                       status="concluida")
            return tarefa
        tarefa["status"] = "concluida"
        tarefa["commit"] = prova or tarefa.get("commit", "")
        if ramo:
            tarefa["branch"] = ramo
        if _normalizar(evidencia):
            tarefa["evidencia"] = _redigido(evidencia)
        if _normalizar(nota):
            tarefa["nota"] = _redigido(nota)
        tarefa["concluida_em"] = hora
        tarefa["concluida_por"] = aprovador[:80]
        registro["tarefas"][tid] = tarefa
        registro["atualizado_em"] = hora
        estado["planos"][pid] = registro
        _registrar(estado, "tarefa_concluida", pid, tarefa=tid,
                   commit=prova, quem=aprovador, quando=hora,
                   branch=ramo or ramo_antes or TRILHA_SEM_RAMO,
                   evidencia=str(tarefa.get("evidencia") or "")[:80])
        _salvar(estado)
    _log_event("workflow_gate", decisao="concluir", gate="status_tarefa",
               plano_id=pid, tarefa_id=tid, status="concluida", commit=prova,
               branch=ramo or ramo_antes or TRILHA_SEM_RAMO, quem=aprovador)
    return tarefa


def definir_branch(plano, branch, *, fase: str = "", tarefa: str = "",
                   nota: str = "", quando: str | None = None
                   ) -> dict[str, Any]:
    """Declara ou corrige a branch do plano, de uma fase ou de uma tarefa.

    Existe porque consertar um ENDEREÇO não é concluir nem rebaixar status:
    :func:`concluir_tarefa` recusa hash trocado e :func:`mudar_status` recusa
    reabrir sem nota, e nenhum dos dois serve para "esta fase passa a
    trabalhar em ``plano/x``". Corrigir um ramo já registrado exige ``nota``
    dizendo o porquê -- a mesma lei da reabertura, porque é o mesmo risco:
    reescrever rastro sem motivo.

    ``branch=""`` é vocabulário ("não declarado"), não apagar nada: as
    tarefas passam a aparecer no cesto :data:`RAMO_SEM_DECLARACAO` do
    :func:`quadro`, que é o jeito honesto de o dono ver o buraco.
    """
    pid = _validar_id(plano, "id de plano")
    ramo = _validar_branch(branch, f"plano {pid}")
    rotulo = _validar_rotulo(fase, "fase") if fase else ""
    tid = _validar_id(tarefa, "id de tarefa") if tarefa else ""
    motivo = _redigido(nota)
    hora = _agora(quando)
    with _LOCK:
        estado = _carregar()
        registro = estado["planos"].get(pid)
        if registro is None:
            raise ValueError(f"{MENSAGEM_SEM_PLANO}: plano {pid} não cadastrado "
                             "para declarar branch")
        declaradas = [str(item) for item in (registro.get("fases") or [])]
        antes = ""
        alvo = ""
        if tid:
            dado = (registro.get("tarefas") or {}).get(tid)
            if dado is None:
                raise ValueError(
                    f"tarefa {tid} não está no plano {pid}: declare-a "
                    "(quebrar_plano) ou registre com registrar_tarefa antes "
                    "de apontar branch")
            antes = str(dado.get("branch") or "")
            alvo = tid
            if antes and ramo and antes != ramo and not motivo:
                raise ValueError(f"tarefa {tid} já trabalha na branch "
                                 f"{antes!r} e você aponta {ramo!r}: mude com "
                                 "nota=<motivo> para a trilha guardar as duas "
                                 "pontas")
            dado["branch"] = ramo
            registro["tarefas"][tid] = dado
        elif rotulo:
            if rotulo not in declaradas:
                raise ValueError(f"fase {rotulo!r} não está nas fases "
                                 f"declaradas do plano {pid}: "
                                 f"{', '.join(declaradas) or '(nenhuma)'} — "
                                 "quebre o plano primeiro")
            grifados = registro.setdefault("branch_por_fase", {})
            antes = str(grifados.get(rotulo) or "")
            alvo = rotulo
            if antes and ramo and antes != ramo and not motivo:
                raise ValueError(f"a fase {rotulo!r} já trabalha na branch "
                                 f"{antes!r} e você aponta {ramo!r}: mude com "
                                 "nota=<motivo>")
            grifados[rotulo] = ramo
        else:
            antes = str(registro.get("branch") or "")
            if antes and ramo and antes != ramo and not motivo:
                raise ValueError(f"plano {pid} já trabalha na branch "
                                 f"{antes!r} e você aponta {ramo!r}: mude com "
                                 "nota=<motivo>")
            registro["branch"] = ramo
        registro["atualizado_em"] = hora
        estado["planos"][pid] = registro
        _registrar(estado, "branch_definida", pid,
                   branch=ramo or TRILHA_SEM_RAMO, antes=antes,
                   nivel=tid and "tarefa" or (rotulo and "fase" or "plano"),
                   alvo=alvo, motivo=motivo[:80], quando=hora)
        _salvar(estado)
    _log_event("workflow_gate", decisao="branch", gate="ramo_de_trabalho",
               plano_id=pid, branch=ramo or TRILHA_SEM_RAMO, alvo=alvo)
    return {"plano": pid, "branch": ramo, "antes": antes,
            "nivel": tid and "tarefa" or (rotulo and "fase" or "plano"),
            "alvo": alvo}


def mudar_status(referencia, status: str, *, nota: str = "",
                 quem: str = "", quando: str | None = None) -> dict[str, Any]:
    """Move status que não têm alavanca própria: ``pendente`` e ``bloqueada``.

    ``concluida`` e ``em_progresso`` passam por :func:`concluir_tarefa` e
    :func:`iniciar_tarefa`, porque os dois exigem prova ou plano aprovado.
    Reabrir uma tarefa concluída é permitido com ``nota`` dizendo o motivo --
    sem nota, recusa: conclusão desfeita por acaso é como se perde rastro.
    """
    alvo = _validar_status(status, str(referencia))
    if alvo in ("concluida", "em_progresso"):
        raise ValueError(f"status {alvo!r} não se alcança por mudar_status: "
                         "use concluir_tarefa (com prova) ou iniciar_tarefa")
    hora = _agora(quando)
    motivo = _redigido(nota)
    with _LOCK:
        estado = _carregar()
        pid, tid = _procurar_tarefa(estado, referencia)
        registro = estado["planos"][pid]
        tarefa = dict(registro["tarefas"][tid])
        if tarefa.get("status") == "concluida" and not motivo:
            raise ValueError(f"tarefa {tid} está concluída: reabrir sem nota "
                             "dizendo o porquê é apagar rastro -- passe "
                             "nota=<motivo>")
        if tarefa.get("status") == "concluida":
            # reabrir apaga a conclusão de propósito, com o motivo na nota:
            # o histórico do estado guarda a linha da conclusão original.
            for campo in ("commit", "evidencia", "concluida_em",
                          "concluida_por"):
                tarefa[campo] = ""
        tarefa["status"] = alvo
        if motivo:
            tarefa["nota"] = motivo
        if _normalizar(quem):
            tarefa["movida_por"] = _normalizar(quem)[:80]
        tarefa["status_desde"] = hora
        registro["tarefas"][tid] = tarefa
        registro["atualizado_em"] = hora
        estado["planos"][pid] = registro
        _registrar(estado, "tarefa_status", pid, tarefa=tid, status=alvo,
                   motivo=motivo[:80], quando=hora)
        _salvar(estado)
    _log_event("workflow_gate", decisao="status", gate="status_tarefa",
               plano_id=pid, tarefa_id=tid, status=alvo)
    return tarefa


def _balde_de_ramo() -> dict[str, int]:
    """Um balde de contagem por branch: os quatro status + o total."""
    balde = {chave: 0 for chave in STATUS_DE_TAREFA}
    balde["tarefas"] = 0
    return balde


def _contar_por_ramo(ramos: dict[str, dict[str, int]],
                     linha: dict[str, Any]) -> None:
    """Conta uma linha do quadro no balde da sua branch.

    O cesto :data:`RAMO_SEM_DECLARACAO` existe pela mesma razão do
    :data:`SEM_FASE`: "não declarado" é um fato que o dono precisa VER, não
    uma linha que desaparece da contagem.
    """
    chave = str(linha.get("branch") or "") or RAMO_SEM_DECLARACAO
    balde = ramos.setdefault(chave, _balde_de_ramo())
    status = str(linha.get("status") or "pendente")
    balde[status] = balde.get(status, 0) + 1
    balde["tarefas"] = balde.get("tarefas", 0) + 1


def quadro(plano_id) -> dict[str, Any]:
    """A árvore fase → tarefa → status (e branch) do plano: o que o dono lê
    no celular. ``por_branch`` responde "o que está em que branch".
    """
    estado = _carregar()
    bruto = _normalizar(plano_id) or str(estado.get("tarefa_ativa") or "")
    registro = _plano_do_estado(bruto, estado) if bruto else None
    if registro is None:
        raise ValueError(f"{MENSAGEM_SEM_PLANO}: não há plano "
                         f"{plano_id!r} no estado do portão para montar o "
                         "quadro")
    pid = str(registro.get("id") or "")
    declaradas = [str(item) for item in (registro.get("fases") or [])]
    ramo_do_plano = str(registro.get("branch") or "")
    bruto_grifados = registro.get("branch_por_fase")
    grifados: dict[str, str] = {
        str(chave): str(valor) for chave, valor in bruto_grifados.items()
        if chave and valor} if isinstance(bruto_grifados, dict) else {}
    galhos: dict[str, list[dict[str, Any]]] = {rotulo: [] for rotulo in declaradas}
    por_status = {chave: 0 for chave in STATUS_DE_TAREFA}
    por_branch: dict[str, dict[str, int]] = {}
    sem_fase: list[dict[str, Any]] = []
    for tid, dado in sorted((registro.get("tarefas") or {}).items()):
        rotulo = str(dado.get("fase") or "")
        linha = {"id": tid, "titulo": str(dado.get("titulo") or ""),
                 "fase": rotulo,
                 "branch": _ramo_de_leitura(dado.get("branch")),
                 "status": str(dado.get("status") or "pendente"),
                 "commit": str(dado.get("commit") or ""),
                 "evidencia": str(dado.get("evidencia") or ""),
                 "nota": str(dado.get("nota") or ""),
                 "desde": str(dado.get("desde") or ""),
                 "status_desde": str(dado.get("status_desde") or ""),
                 "em_progresso_desde": str(dado.get("em_progresso_desde") or ""),
                 "concluida_em": str(dado.get("concluida_em") or ""),
                 "concluida_por": str(dado.get("concluida_por") or "")}
        por_status[linha["status"]] = por_status.get(linha["status"], 0) + 1
        _contar_por_ramo(por_branch, linha)
        if rotulo in galhos:
            galhos[rotulo].append(linha)
        else:
            sem_fase.append(linha)
    fases = [{"fase": rotulo, "tarefas": galhos[rotulo],
              "branch": grifados.get(rotulo) or ramo_do_plano,
              "total": len(galhos[rotulo]),
              "concluidas": sum(1 for linha in galhos[rotulo]
                                if linha["status"] == "concluida")}
             for rotulo in declaradas]
    if sem_fase:
        fases.append({"fase": SEM_FASE, "tarefas": sem_fase,
                      "total": len(sem_fase),
                      "concluidas": sum(1 for linha in sem_fase
                                        if linha["status"] == "concluida")})
    return {"plano": pid, "titulo": str(registro.get("titulo") or pid),
            "fase_do_plano": str(registro.get("fase") or ""),
            "branch": ramo_do_plano, "branch_por_fase": grifados,
            "por_branch": por_branch, "ramos": sorted(por_branch),
            "arquivo": str(registro.get("arquivo") or ""),
            "aprovado_por": str(registro.get("aprovado_por") or ""),
            "aprovado_em": str(registro.get("aprovado_em") or ""),
            "fases": fases, "tarefas": len(registro.get("tarefas") or {}),
            "por_status": por_status,
            "na_fila": pid in [str(item) for item in
                               (estado.get("fila") or [])]}


# ── a fila de planos (ordem do dono: "jogue como próximo da fila") ─────────

def fila() -> list[str]:
    """A fila explícita, na ordem dita. Vazia é fila vazia, não suposição."""
    return list(_carregar().get("fila") or [])


def empurrar_para_fila(plano_id, posicao: int = 0, *,
                       quando: str | None = None) -> list[str]:
    """Põe o plano na fila na posição pedida e devolve a fila nova.

    A ORDEM É DELE. Este módulo nunca escolhe prioridade, nunca reordena por
    data, fase ou tamanho: :func:`proxima_da_fila` devolve o que está no topo
    porque alguém -- o dono -- mandou pôr ali. ``posicao`` fora do intervalo é
    recusada, não cortada: "último" dito como número errado tem de dar erro,
    não um plano em lugar inesperado.
    """
    pid = _validar_id(plano_id, "id de plano")
    with _LOCK:
        estado = _carregar()
        if pid not in estado["planos"]:
            raise ValueError(f"{MENSAGEM_SEM_PLANO}: plano {pid} não cadastrado "
                             "para entrar na fila")
        try:
            alvo = int(posicao)
        except (TypeError, ValueError):
            raise ValueError(f"posição {posicao!r} na fila não é número") from None
        total = len(estado.get("fila") or [])
        if alvo < 0 or alvo > total:
            raise ValueError(
                f"posição {alvo} fora da fila (0 a {total} com {total} itens): "
                "diga onde o dono quer o plano -- 0 é o próximo da fila")
        fila_atual = [item for item in (estado.get("fila") or [])
                      if item != pid]
        fila_atual.insert(alvo, pid)
        estado["fila"] = fila_atual
        _registrar(estado, "fila_mudou", pid, posicao=alvo,
                   quando=_agora(quando))
        _salvar(estado)
    _log_event("workflow_gate", decisao="fila", gate="fila_de_planos",
               plano_id=pid, posicao=alvo)
    return fila_atual


def tirar_da_fila(plano_id) -> list[str]:
    """Tira o plano da fila. Ele continua registrado: fila não é arquivo."""
    pid = _validar_id(plano_id, "id de plano")
    with _LOCK:
        estado = _carregar()
        estado["fila"] = [item for item in (estado.get("fila") or [])
                          if item != pid]
        _registrar(estado, "fila_mudou", pid, saiu=True)
        _salvar(estado)
    return estado["fila"]


def proxima_da_fila() -> str:
    """O id do próximo da fila, ou "" quando a fila está vazia.

    Id na fila que não bate com plano registrado é pulado e dito no log --
    fila velha não pode fazer o assistente executar nome que não existe.
    """
    estado = _carregar()
    planos = estado.get("planos") or {}
    for item in estado.get("fila") or []:
        if item in planos:
            return str(item)
        if item:
            _log_event("workflow_gate", decisao="fila_fantasma",
                       gate="fila_de_planos", plano_id=item)
    return ""


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
    """Contagens por fase do plano E por status de tarefa + fila: painel e auditoria.

    As duas leituras que faltavam para a pergunta do dono ter resposta ("quantas
    fases, quantas tarefas, o que está concluído"): :data:`STATUS_DE_TAREFA`
    contado tarefa a tarefa e o total de fases declaradas dentro dos planos. A
    fila aparece porque "próximo da fila" é fato do estado, não da conversa.
    """
    estado = _carregar()
    por_fase = {fase: 0 for fase in FASES}
    por_status = {chave: 0 for chave in STATUS_DE_TAREFA}
    por_branch: dict[str, dict[str, int]] = {}
    planos_por_branch: dict[str, list[str]] = {}
    tarefas = 0
    fases_de_obra = 0
    sem_fase = 0
    sem_branch = 0
    for plano in estado["planos"].values():
        por_fase[str(plano.get("fase") or "")] = \
            por_fase.get(str(plano.get("fase") or ""), 0) + 1
        fases_de_obra += len(plano.get("fases") or [])
        ramo_do_plano = str(plano.get("branch") or "")
        planos_por_branch.setdefault(
            ramo_do_plano or RAMO_SEM_DECLARACAO, []).append(
            str(plano.get("id") or ""))
        for dado in (plano.get("tarefas") or {}).values():
            tarefas += 1
            status = str(dado.get("status") or "pendente")
            por_status[status] = por_status.get(status, 0) + 1
            if not dado.get("fase"):
                sem_fase += 1
            ramo = _ramo_de_leitura(dado.get("branch"))
            if not ramo:
                sem_branch += 1
            _contar_por_ramo(por_branch, {"branch": ramo, "status": status})
    saida_por_branch = {chave: por_branch[chave] for chave in sorted(por_branch)}
    return {"planos": len(estado["planos"]), "por_fase": por_fase,
            "tarefa_ativa": estado.get("tarefa_ativa", ""),
            "tarefas": tarefas, "por_status": por_status,
            "por_branch": saida_por_branch,
            "ramos": sorted(por_branch),
            "planos_por_branch": {chave: sorted(planos_por_branch[chave])
                                  for chave in sorted(planos_por_branch)},
            "tarefas_sem_branch": sem_branch,
            "fases": fases_de_obra, "tarefas_sem_fase": sem_fase,
            "fila": list(estado.get("fila") or []),
            "proxima_da_fila": proxima_da_fila(),
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
                                    "execute tarefa por tarefa" + (
                                       f" -- ATENÇÃO: o plano não tem fases "
                                       f"nem tarefas declaradas "
                                       f"(quebrar_plano), ordem do dono de "
                                       f"10/11/2026"
                                       if not (registro.get("fases")
                                               or registro.get("tarefas"))
                                       else ""))
    _log_event("workflow_gate", decisao="liberado", gate="plano_aprovado",
               referencia=alvo, plano_id=pid, fase=fase,
               fases=len(registro.get("fases") or []),
               tarefas=len(registro.get("tarefas") or {}))
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
