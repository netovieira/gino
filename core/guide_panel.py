"""Camada de dados do painel GUIA — o espelho das regras, lido do disco.

O painel GUIA (ui.GuideOverlay) é SOMENTE LEITURA e nunca duplica texto de
regra no código: cada seção é montada em tempo de render a partir dos
arquivos-fonte listados abaixo. Mudar o arquivo muda o painel.

Duas obrigações vivem aqui, no ponto de leitura:

* REDAÇÃO — se um arquivo de regra contiver padrão de chave/token/senha,
  CPF ou telefone, o valor é trocado por ``[retido]`` antes de sair do
  módulo. A redação acontece na leitura, nunca na renderização, para que
  nenhum caminho de exibição (texto, cópia, teste) consiga vazar o valor.
* ORIGEM — cada item carrega ``arquivo:linha`` e, quando o texto da regra
  tem data, a data da lei.

Sem Qt, sem ``main.py``: o módulo é puro para poder ser testado e
reutilizado.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

REDACTED = "[retido]"

# ── redação: segredo e dado pessoal nunca saem do arquivo ────────────────────
#
# Ordem importa: primeiro os pares chave=valor (que podem conter qualquer
# caractere), depois os formatos numéricos. Todo padrão é conservador no
# sentido de NÃO vazar: um falso positivo mostra [retido]; um vazamento não.
_PATTERNS: tuple[re.Pattern, ...] = (
    # api_key: xyz / token = xyz / senha: xyz / secret="xyz" / chave=...
    # O valor usa o alfabeto típico de chave (letra/dígito/_-+/=:.), o que já
    # descarta código como ``{token: math.log(...)}`` — ``(`` não é caractere
    # de chave.
    re.compile(
        r"(?i)\b(api[ _-]?key|access[ _-]?key|secret(?:[ _-]?key)?|token|"
        r"password|senha|chave|client[ _-]?secret)(\s*[:=]\s*)"
        r"[\"']?([A-Za-z0-9_\-+/=:.@]{10,})"
    ),
    # sk-... / ghp_... / xoxb-... (chaves de fornecedor já conhecidas)
    re.compile(r"\b(?:sk|ghp|xoxb|ya29)-[A-Za-z0-9_\-]{8,}"),
    # Bearer <token>
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{10,}={0,2}"),
    # blobs longos de base64/hex (32+ chars) com dígito — sha256, keys soltas.
    # Exigir dígito descarta caminhos e palavras compostas sem segredo.
    re.compile(r"\b(?=[A-Za-z0-9+/]{32,}[=]{0,2}\b)(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{32,}={0,2}"),
    # CPF: 123.456.789-01 (e variantes sem pontuação)
    re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-?\d{2}(?!\d)"),
    # telefone BR: +55 (11) 91234-5678, 55 11 91234 5678, (11) 1234-5678
    re.compile(
        r"(?<!\d)(?:\+?55[\s.\-]?)?\(?\d{2}\)?[\s.\-]?\d{4,5}[\s.\-]?\d{4}(?!\d)"
    ),
)


def _value_redact(m: re.Match) -> str:
    """Do par chave=valor só o VALOR é retido: ``api_key: [retido]``.

    group(1) é a chave como escrita, group(2) o separador ``:``/``=`` e o
    resto é o valor suspeito."""
    return m.group(1) + m.group(2) + REDACTED


def redact(text: str) -> str:
    """Troca segredo/dado pessoal por ``[retido]`` preservando o resto."""
    out = _PATTERNS[0].sub(_value_redact, text)
    for pat in _PATTERNS[1:]:
        out = pat.sub(REDACTED, out)
    return out


# ── leitura ──────────────────────────────────────────────────────────────────

_MISSING = "(arquivo ausente — a regra no disco não está onde este painel aponta)"


def read_redacted(path: Path) -> tuple[str, bool]:
    """Lê o arquivo e aplica a redação AQUI, no ponto de leitura.

    Retorna (texto, existia). Ausência não é exceção: o painel espelha o
    disco como ele é, e uma regra que sumiu aparece como ausente.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _MISSING, False
    return redact(raw), True


def _first_content_line(text: str) -> int:
    """Linha da primeira linha de conteúdo (pula front-matter YAML e vazias)."""
    lines = text.splitlines()
    in_front = bool(lines) and lines[0].strip() == "---"
    for idx, line in enumerate(lines, start=1):
        s = line.strip()
        if in_front:
            if idx == 1:
                continue
            if s == "---":
                in_front = False
            continue
        if s:
            return idx
    return 1


_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})\b")


def law_date(text: str) -> str | None:
    """Data da lei, se o texto do próprio arquivo a traz (ex.: '05/11/2026')."""
    m = _DATE_RE.search(text)
    return m.group(1) if m else None


def md_block(text: str, title_regex: str) -> tuple[str, int]:
    """Recorta um bloco ``#… TÍTULO`` de um Markdown, com sua linha inicial.

    Usado para o contrato: o bloco FALA e o bloco TRABALHO de FLUXOS.md
    aparecem na seção de quem os cumpre, lidos do mesmo arquivo — nada é
    copiado para o código.
    """
    lines = text.splitlines(keepends=True)
    pat = re.compile(rf"^#+\s*{title_regex}\s*$")
    start = None
    level = 0
    for idx, line in enumerate(lines):
        m = pat.match(line)
        if m:
            start = idx
            level = len(line) - len(line.lstrip("#"))
            break
    if start is None:
        return "", 0
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        m = re.match(r"^(#+)\s", lines[idx])
        if m and len(m.group(1)) <= level:
            end = idx
            break
    return "".join(lines[start:end]).rstrip(), start + 1


# ── as seções fixas, cada uma apontando para seu arquivo ────────────────────

def _vault_dirs() -> list[Path]:
    dirs = []
    try:
        from core.obsidian import VAULT
        dirs.append(Path(VAULT).expanduser())
    except Exception:
        pass
    dirs.append(Path.home() / "Documentos" / "Obsidian Vault")
    return dirs


#: Os documentos do vault que o painel abre, como (legenda, TIPO, título,
#: data do conteúdo, aliases antigos).
#:
#: O nome CANÔNICO não é literal congelado: ele sai do validador de nomes
#: (:func:`core.obsidian.build_name`, a REGRA do dono de 07/11) calculado em
#: tempo de render. Renomear um documento do cofre -- que é exatamente o que a
#: REGRA manda quando o nome está fora do padrão -- passa a ser trocar esta
#: linha, e não caçar um string escondido.
#:
#: Os nomes antigos ficam como ALIASES de reserva: o painel continua abrindo o
#: documento se o cofre ainda estiver com o nome velho (renomeio e citações não
#: são atômicos do ponto de vista do processo vivo) e em máquinas onde a rodada
#: de renomeio ainda não passou.
VAULT_DOCS = (
    ("Contrato do dono no vault", "REGRA", "fluxos", "2026-11-05",
     ("FLUXOS.md",)),
    ("Ordens do dono", "REGRA", "ordens do dono", "2026-11-05",
     ("ORDENS-DO-DONO-2026-11-05.md",)),
    ("Tarefas unificadas", "TAREFA", "tarefas unificadas", "2026-11-05",
     ("TAREFAS-UNIFICADAS.md",)),
)


def vault_doc_names(tipo: str, titulo: str, day: str,
                    aliases: Iterable[str] = ()) -> tuple[str, ...]:
    """Nomes em que procurar: o canônico pelo validador, depois os aliases.

    Se o validador não estiver disponível (ou reprovar o par tipo+título), o
    ponteiro não morre: cai nos aliases. Nome fora do padrão nunca é inventado
    aqui -- quem monta o nome é a mesma função que valida o resto do cofre.
    """
    names: list[str] = []
    try:
        from core.obsidian import build_name
        names.append(build_name(tipo, titulo, day))
    except Exception:
        pass
    names.extend(a for a in aliases if a and a not in names)
    return tuple(names)


def find_vault_doc(filename: str | Iterable[str]) -> Path | None:
    """Achou o documento do vault? Procura os cofres conhecidos (sem varrer o disco).

    Aceita um nome ou uma lista de nomes na ordem de preferência (canônico
    primeiro, aliases depois). A preferência é pelo NOME, não pelo cofre: o
    nome canônico é tentado nos dois cofres antes de se recorrer a um alias.
    """
    names = (filename,) if isinstance(filename, str) else tuple(filename)
    for name in names:
        if not name:
            continue
        for base in _vault_dirs():
            hit = base / name
            if hit.exists():
                return hit
            hits = sorted(base.glob(f"*/{name}"))
            if hits:
                return hits[0]
    return None


def _item(base_dir: Path, label: str, rel: str) -> dict:
    path = base_dir / rel
    text, existed = read_redacted(path)
    origin = f"{rel}:1"
    date = None
    if existed:
        origin = f"{rel}:{_first_content_line(text)}"
        date = law_date(text)
    return {"label": label, "path": str(path), "origin": origin,
            "date": date, "text": text, "missing": not existed}


def _contract_item(base_dir: Path, label: str, title_regex: str) -> dict:
    """Um bloco do contrato FLUXOS.md, recortado do arquivo na render."""
    rel = "skills/fluxos/FLUXOS.md"
    path = base_dir / rel
    raw, existed = read_redacted(path)
    if not existed:
        return {"label": label, "path": str(path), "origin": f"{rel}:?",
                "date": None, "text": raw, "missing": True}
    block, line = md_block(raw, title_regex)
    return {"label": label, "path": str(path), "origin": f"{rel}:{line or '?'}",
            "date": law_date(block), "text": block or raw, "missing": False}


def _link(label: str, path: Path | str, exists: bool | None = None) -> dict:
    p = Path(path)
    ok = p.exists() if exists is None else exists
    return {"label": label, "path": str(p), "exists": ok}


# ── FASE3-TASK4: marcadores de execução concreta ────────────────────────────

def unverified_markers() -> list[dict]:
    """Retorna marcadores de turnos UNVERIFIED para o painel GUIA.

    Cada marcador indica uma ação que foi executada sem prova material
    (recibo HMAC + verificação externa). O painel GUIA exibe esses marcadores
    para que o dono veja quais ações não foram verificadas.
    """
    from core.flows.executor import _ACTIVITY_LOG_PATH, _is_observer_mode
    from datetime import datetime, timezone

    markers = []
    observer = _is_observer_mode()

    # Lê as últimas 50 linhas do activity log procurando TURN_UNVERIFIED.
    try:
        if not _ACTIVITY_LOG_PATH.exists():
            return markers
        lines = _ACTIVITY_LOG_PATH.read_text(encoding="utf-8").splitlines()
        for line in lines[-50:]:
            if "TURN_UNVERIFIED" not in line:
                continue
            # Extrai task_id e tool do log.
            parts = line.split()
            task_id = tool = reason = "?"
            for part in parts:
                if part.startswith("task_id="):
                    task_id = part.split("=", 1)[1]
                elif part.startswith("tool="):
                    tool = part.split("=", 1)[1]
                elif part.startswith("reason="):
                    reason = part.split("=", 1)[1]
            markers.append({
                "task_id": task_id,
                "tool": tool,
                "reason": reason,
                "observer_mode": observer,
                "timestamp": line.split("]")[0].lstrip("["),
            })
    except Exception:
        pass

    return markers[-10:]  # últimos 10 marcadores


def sections(base_dir: Path) -> list[dict]:
    """As seis seções fixas do GUIA, montadas do disco, agora."""
    base = Path(base_dir)

    minhas = [
        _item(base, "Conversa — o que responder e quando", "skills/conversation/SKILL.md"),
        _item(base, "Execução — como trabalhar em ordem", "skills/execution/SKILL.md"),
        _item(base, "Escopo do pedido — cumprimento literal do que o dono pediu",
              "skills/escopo-do-pedido/SKILL.md"),
        _item(base, "Ações — o menu de ferramentas", "skills/actions/SKILL.md"),
        _item(base, "Voz — como falar", "skills/voice/SKILL.md"),
        _item(base, "Personas — quem é cada um", "skills/personas/SKILL.md"),
        _item(base, "Fluxos — o contrato do dono, vale como lei", "skills/fluxos/SKILL.md"),
        _item(base, "WhatsApp — a rota das mensagens", "skills/whatsapp/SKILL.md"),
        _item(base, "Memória — o que guardar", "skills/memory/SKILL.md"),
    ]

    boca = [
        _item(base, "Guarda da boca — leis de asserção, número, vazamento", "core/flows/guard.py"),
        _item(base, "Boca — quem atende e responde", "core/flows/mouth.py"),
        _item(base, "Config da boca — contexto de 15 min, modelo local", "core/flows/config.py"),
        _contract_item(base, "Contrato — bloco FLUXO FALA", r"FLUXO\s+FALA"),
    ]

    cerebro = [
        _item(base, "Live — a boca nunca é esperada", "core/flows/live.py"),
        _item(base, "Roteador — o que vai pra boca, o que vai pro cérebro", "core/flows/router.py"),
        _item(base, "Orquestrador de fluxos", "core/flows/orchestrator.py"),
        _item(base, "Política de modelos gratuitos", "core/free_models.py"),
    ]

    gates = [
        _item(base, "Rootgate — o portão da raiz", "core/rootgate.py"),
        _item(base, "Actiongate — o portão de ação", "core/actiongate.py"),
        _item(base, "Job grant — a autorização de trabalho", "core/job_grant.py"),
        _item(base, "Owner decision — decisão que é só do dono", "core/owner_decision.py"),
        _item(base, "Restart — o que só abre por fala do dono", "core/restart.py"),
        _contract_item(base, "Contrato — bloco FLUXO TRABALHO", r"FLUXO\s+TRABALHO"),
    ]

    memoria = [
        _item(base, "Memória de chat — o que é coletado e indexado", "core/chat_memory.py"),
        _item(base, "Índice de memória — como se busca", "core/memory_index.py"),
    ]

    # FASE3-TASK4: seção de execução concreta com marcadores UNVERIFIED.
    unverified = unverified_markers()
    execution_items = [
        _item(base, "Executor — execução concreta obrigatória",
              "core/flows/executor.py"),
        _item(base, "Verificador externo — prova material",
              "core/action_verifier.py"),
        _item(base, "Recibos HMAC — integridade do tool call",
              "core/tool_receipts.py"),
    ]
    # Adiciona marcadores UNVERIFIED como itens dinâmicos.
    for marker in unverified:
        execution_items.append({
            "label": f"[UNVERIFIED] {marker['tool']} (task {marker['task_id']})",
            "path": f"activity_log:{marker['timestamp']}",
            "origin": f"TURN_UNVERIFIED:{marker['task_id']}",
            "date": marker.get("timestamp"),
            "text": f"Ação sem prova material: {marker['reason']}",
            "missing": False,
            "unverified": True,
            "observer_mode": marker.get("observer_mode", False),
        })

    links: list[dict] = []
    for sec in (minhas, boca, cerebro, gates, memoria):
        for it in sec:
            links.append(_link(it["label"], it["path"], exists=not it["missing"]))
    fluxos_repo = base / "skills" / "fluxos" / "FLUXOS.md"
    links.append(_link("Contrato (cópia literal no projeto)", fluxos_repo))
    for label, tipo, titulo, day, aliases in VAULT_DOCS:
        names = vault_doc_names(tipo, titulo, day, aliases)
        found = find_vault_doc(names)
        links.append(_link(label,
                           found if found else f"(ausente no vault: {', '.join(names)})",
                           exists=found is not None))

    return [
        {"title": "MINHAS REGRAS (o assistente/orquestador)",
         "short": "MINHAS",
         "note": "As skills que dizem o que responder, como responder e quando responder — é o guia que este painel espelha.",
         "items": minhas},
        {"title": "REGRAS DA BOCA",
         "short": "BOCA",
         "note": "O que está em código: as leis de anúncio/asserção/número/vazamento, o contexto de 15 minutos, o modelo local e o contrato do bloco FALA.",
         "items": boca},
        {"title": "REGRAS DO CÉREBRO",
         "short": "CÉREBRO",
         "note": "A boca nunca é esperada; o roteador decide o caminho; o cérebro usa os modelos gratuitos da política.",
         "items": cerebro},
        {"title": "REGRAS DO ORQUESTRADOR / GATES",
         "short": "GATES",
         "note": "Os portões (root, ação, grant, decisão do dono, restart — o que só abre por fala do dono) e o bloco TRABALHO do contrato.",
         "items": gates},
        {"title": "MEMÓRIA",
         "short": "MEMÓRIA",
         "note": "O que deve ser coletado/indexado e o que não entra — a fronteira está nos próprios módulos.",
         "items": memoria},
        {"title": "EXECUÇÃO CONCRETA (FASE3-TASK4)",
         "short": "EXEC",
         "note": "Ações com efeito colateral exigem prova material (recibo HMAC + verificação externa). Turnos UNVERIFIED são marcados explicitamente. Modo observador (2 semanas): loga mas não bloqueia.",
         "items": execution_items,
         "unverified_count": len(unverified),
         "observer_mode": len(unverified) > 0 and unverified[0].get("observer_mode", False) if unverified else False},
        {"title": "LINKS",
         "short": "LINKS",
         "note": "Cada regra acima aponta para o arquivo-fonte; os documentos do vault ficam por último.",
         "items": links},
    ]
