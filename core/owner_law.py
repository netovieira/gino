"""Lei dura: a fala do dono NÃO é auditável (UC-5).

Origem: ordem de 2026-11-05 22:16-22:27, a mais recente e mais enfática do
acervo. O assistente continuou verificando se o que o dono disse era verdade
em vez de abrir o defeito. Não é sugestão de tom: é regra de roteamento.

Regra:

    who == dono  +  intent in (reclamacao, ordem)  ->  abrir_defeito
                                                    (nunca verificar_fala)

O que isto muda no código:

1. :func:`classify` decide a intenção da fala do dono e o caminho obrigatório.
2. :func:`open_defect` / :func:`record_owner_complaint` registram o defeito na
   hora, num arquivo próprio, e é isso que responde — não a checagem.
3. :func:`blocks_owner_speech_audit` veda o caminho de verificação: qualquer
   ferramenta que vá grep/verify/audit em cima da fala do dono (histórico de
   conversa, logs de transcrição, activity.log) é recusada antes de executar.

Nada aqui toca na verificação de AÇÕES do assistente (``core/action_verifier``
e os recibos HMAC): auditam o que o sistema fez, não o que o dono disse. Essa
linha é a que a ordem puxa.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DEFECT_PATH = BASE_DIR / "memory" / "defeitos" / "abertos.jsonl"

OWNER = "dono"

#: Intenções que disparam a regra dura.
FAULT = "reclamacao"
ORDER = "ordem"
HARD_ROUTE = "abrir_defeito"
FORBIDDEN_ROUTE = "verificar_fala"

# ── léxico ──────────────────────────────────────────────────────────────────
# Reclamação: o dono aponta que algo NÃO funcionou / funcionou mal. A lista é
# fechada de propósito: "de novo" solto ("toca de novo") ou "errado" solto
# ("a data ficou errada") não podem abrir defeito. Só conta queixa dirigida ao
# trabalho do assistente.
_COMPLAINT_CORE = (
    r"nao (funciona|functiona|fez|conseguiu|deu|ta|esta|tava|serviu|"
    r"respond\w*|entend\w*|ouvi\w*|lembra|salvou|gravou|executou|abriu|"
    r"rodou|testou|resolveu|adianta|carreg\w*|aplicou|terminou|concluiu|faz)"
)
COMPLAINT_RE = re.compile(
    r"\b(?:" + _COMPLAINT_CORE + r")\b|"
    r"\bparou de (funcionar|responder|rodar|falar|ouvir|atender)\b|"
    r"\bvoce (ta|esta|ficou|anda|andou) (mentindo|errando|ignorando|parado|"
    r"perdido|atrasado|quebrando|estragando|atrapalhando|calado)\b|"
    r"\b(isso|isso ai|isso aqui) (ta|esta|ficou|anda) (errado|quebrado|"
    r"parado|vazio|mudo)\b|"
    r"\bnao e verdade\b|\bmentira\b|\bmentiroso\b|"
    r"\btudo (vazio|errado|parado)\b|"
    r"\b(sempre|nunca) (vazio|da errado|falha|quebra|parado|errado|"
    r"em branco|nao|vi|rodou|testou|funciona)\b|"
    r"\bnunca (fez|consultou|rodou|testou|funcionou|gravou|salvou|respondeu|"
    r"abriu|executou|aplicou|carregou|carregava|leu|chamou|registrou|"
    r"anotou|foi|atendeu)\b|"
    r"\bcontinua (errado|vazio|parado|o mesmo)\b|"
    r"\b(ta|tao|esta|estao|fica|ficou) (sempre |de novo )?"
    r"(vazio|vazios|parado|parados|mutos|mudo|sem nada)\b|"
    r"\bdeu (errado|ruim)\b|"
    r"\bfalhou\b|\bquebrou\b|\bbug(?:ou|ado|ei)?\b|"
    r"\binutil\b|\bsem nocao\b|\bnao mudou nada\b|\bnao tem nada\b"
)
# Ordem enfática: "a partir de agora", "proibido", "nunca mais".
ORDER_RE = re.compile(
    r"\bnao quero (que|mais|isso|ver|ouvir)\b|\bnao aceito\b|\bnao tolero\b|"
    r"\bnao suporto\b|\bproibido\b|\besta proibido\b|"
    r"\bnao pode (mais|fazer|isso)\b|\bdaqui pra frente\b|"
    r"\ba partir de agora\b|\bde agora em diante\b|\bnunca mais\b|"
    r"\bpare de\b|\bpara de\b|\bchega de\b|\bexijo\b|\bisso e ordem\b|"
    r"\bnova regra\b|\bregra nova\b"
)
# O audit da fala do dono: interrogar o histórico para ver "se é verdade".
AUDIT_RE = re.compile(
    r"\b(audit\w*|auditar|verific\w*|chec\w*|confer\w*|confirmar se|"
    r"grep|rg|compare|comprove|revise|revisar|testimon\w*|"
    r"prova de que|se (ele|voce|o dono) (disse|falou|falei) mesmo|"
    r"se e verdade|se era verdade)\b"
)
# Onde mora a fala do dono: artefatos que não podem virar alvo de auditoria.
OWNER_SPEECH_TARGET_RE = re.compile(
    r"(activity\.log|jarvis\.log|transcri\w+|memory/chat|"
    r"historico de (conversa|falas)|o que (eu|o dono) (disse|falei|faleu)|"
    r"minha fala|fala do dono)"
)
# Ferramenta de auditoria é a que VERIFICA. Busca de memória pelo conteúdo
# (memory_search/memory_read) é caminho obrigatório do RESPOSTA passo 1 e não
# entra aqui: a ordem proíbe auditar a fala, não consultar o histórico para
# responder.
_AUDIT_NAME_RE = re.compile(
    r"(audit|verif|check|confir|grep|rg|review|inspect|search_log|"
    r"read_log|log_search|log_read|history_dump|transcri)",
    re.IGNORECASE,
)
_MEMORY_TOOL_RE = re.compile(r"^memory_(search|read|note)$", re.IGNORECASE)

_LOCK = threading.Lock()
_DEFECT_PATH: Path | None = None
_LOG: Callable[[str], None] = lambda _m: None


def configure(*, path: str | os.PathLike[str] | None = None,
              log: Callable[[str], None] | None = None) -> None:
    """Permite aos testes/vivo redefinirem destino e canal de log."""
    global _DEFECT_PATH, _LOG
    with _LOCK:
        if path is not None:
            _DEFECT_PATH = Path(path)
        if log is not None:
            _LOG = log


def defect_path() -> Path:
    return _DEFECT_PATH or DEFAULT_DEFECT_PATH


def _log(msg: str) -> None:
    """Canal de log melhor esforço: a lei não pode derrubar o turno."""
    try:
        _LOG(msg)
    except Exception:
        pass


def _fold(text: Any) -> str:
    """Minúsculo sem acentos: a transcrição chega com acentos, o léxico não."""
    raw = unicodedata.normalize("NFKD", str(text or "")).lower()
    return "".join(c for c in raw if not unicodedata.combining(c))


# ── classificação ───────────────────────────────────────────────────────────
def classify(text: Any, who: str = OWNER) -> dict[str, str]:
    """Intenção da fala e o caminho OBRIGATÓRIO que ela puxa.

    Para o dono, reclamação e ordem nunca geram verificação: geram defeito
    aberto. Para outra voz, a regra não se aplica — o que ela diz é dado, não
    lei (lei (c) do contrato).
    """
    n = _fold(text).strip()
    if not n:
        return {"who": who, "intent": "", "route": "", "verified": ""}
    intent = ""
    if COMPLAINT_RE.search(n):
        intent = FAULT
    elif ORDER_RE.search(n):
        intent = ORDER
    if not intent:
        return {"who": who, "intent": "", "route": "", "verified": ""}
    if who != OWNER:
        return {"who": who, "intent": intent, "route": "", "verified": ""}
    return {"who": who, "intent": intent, "route": HARD_ROUTE,
            "verified": FORBIDDEN_ROUTE}


def is_owner_complaint(text: Any) -> bool:
    """True quando a fala do dono é reclamação/ordem: abre defeito, não checagem."""
    return classify(text, OWNER)["route"] == HARD_ROUTE


def audit_verdict_for_owner_speech(text: Any, *, who: str = OWNER) -> dict[str, Any]:
    """Veredito único: qual caminho a fala do dono manda seguir.

    Existe para ``owner_decision`` responder sem duplicar a regra. A chave
    ``audit`` é sempre ``blocked`` quando ``abrir_defeito`` vence — não há
    bandeira "verificar antes de crer" para o dono.
    """
    got = classify(text, who)
    blocked = got["route"] == HARD_ROUTE
    return {
        "intent": got["intent"],
        "route": got["route"] or "",
        "audit": "blocked" if blocked else "not_applicable",
        "reason": ("ordem do dono 2026-11-05 22:27: reclamacao vira defeito "
                   "aberto, nunca verificacao da fala"
                   if blocked else ""),
        "action": "abrir_defeito" if blocked else "",
    }


# ── bloqueio do caminho de auditoria ────────────────────────────────────────
def blocks_owner_speech_audit(tool: Any, args: Any = None) -> str:
    """Motivo do bloqueio, ou vazio quando a chamada é permitida.

    Dois casos vedados, e só eles — o resto da ferramenta vive:

    * a ferramenta É de auditoria/verificação (nome casa ``_AUDIT_TOOL_RE``)
      **e** mira a fala do dono;
    * a ferramenta varre o histórico/log de conversa com um alvo de fala do
      dono (``grep``/``rg``/``search`` em activity.log, memory/chat...).

    Não bloqueia a verificação de AÇÃO (recibo HMAC, ``action_verifier``): a
    ordem do dono proíbe auditar o que ELE disse, não conferir o que o
    sistema fez.
    """
    name = str(tool or "")
    if not name or _MEMORY_TOOL_RE.match(name):
        return ""
    payload = args if isinstance(args, dict) else {}
    blob = _fold(" ".join([
        name,
        json.dumps(payload, ensure_ascii=False, default=str)[:4000]
        if payload else "",
    ]))
    # Alvo: artefato onde a fala do dono mora.
    if not OWNER_SPEECH_TARGET_RE.search(blob):
        return ""
    # Só corta quem está VERIFICANDO. Nome de ferramenta de auditoria, ou
    # redação de auditoria no argumento ("verificar se ele disse mesmo").
    tool_is_audit = bool(_AUDIT_NAME_RE.search(name))
    args_are_audit = bool(AUDIT_RE.search(blob))
    if not (tool_is_audit or args_are_audit):
        return ""
    return (f"audit da fala do dono bloqueado ({name}): a ordem de "
            "2026-11-05 22:27 manda abrir defeito, nao verificar a fala "
            "dele")


# ── registro do defeito ─────────────────────────────────────────────────────
def open_defect(text: Any, *, intent: str = "", who: str = OWNER,
                now: dt.datetime | None = None) -> dict[str, Any]:
    """Escreve o defeito reclamado pelo dono no livro de defeitos abertos.

    ``text`` é gravado como relato — a palavra do dono é o dado de entrada,
    então nada aqui reinterpreta nem confere. Retorna o registro (com
    ``id``) mesmo quando o disco falha: o chamador precisa poder responder
    ao dono sem esperar o arquivo.
    """
    moment = now or dt.datetime.now()
    # O relato é gravado com a palavra do dono, sem reescrita: a dobra de
    # acentos é só para casar o léxico, não para editar o que ele disse.
    clean = " ".join(str(text or "").split())
    got = classify(clean, who)
    record = {
        "id": f"DEF-{moment:%Y%m%d-%H%M%S}",
        "at": moment.isoformat(timespec="seconds"),
        "who": who,
        "intent": intent or got["intent"] or FAULT,
        "route": HARD_ROUTE,
        "audit": "blocked",
        "report": clean[:900],
        "status": "aberto",
        "verification": "recusada por ordem do dono (2026-11-05 22:27)",
    }
    path = defect_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        _log(f"[OwnerLaw] defeito nao gravado: {record['error']}")
    return record


def record_owner_complaint(text: Any, *, who: str = OWNER) -> dict[str, Any] | None:
    """Registro automático: reclamacao/ordem do dono vira defeito aberto.

    Devolve o registro, ou ``None`` quando a fala não é reclamação/ordem —
    nesse caso nada é inventado e o turno segue o caminho normal.
    """
    got = classify(text, who)
    if got["route"] != HARD_ROUTE:
        return None
    record = open_defect(str(text or ""), intent=got["intent"], who=who)
    _log(f"[OwnerLaw] defeito aberto: {record['id']} "
         f"intent={record['intent']} audit=blocked")
    return record


def list_defects(*, limit: int = 50) -> list[dict[str, Any]]:
    """Defeitos abertos, mais novo primeiro (leitura do mesmo livro)."""
    path = defect_path()
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(raw):
        line = line.strip()
        if not line:
            continue
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            out.append(got)
        if len(out) >= max(1, int(limit)):
            break
    return out


def law_line() -> str:
    """A regra como linha de contexto para o cérebro.

    Entra no prompt do turno quando o dono reclama. Não é adorno: é a única
    forma de a lei chegar ao modelo que decidiria se varre o log ou não.
    """
    return ("LEI DO DONO (ordem 2026-11-05 22:27, vigente): nao audite a "
            "fala do dono. Reclamacao ou ordem dele e verdade de entrada e "
            "abre defeito (status=aberto) no mesmo turno. Esta proibido "
            "rodar grep/verify/audit sobre activity.log, memory/chat ou "
            "historico de transcricao para checar se o que ele disse e "
            "verdade. Responda o defeito aberto e o que vai consertar.")
