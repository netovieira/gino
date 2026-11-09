"""Obsidian vault mirror: plans, external Markdown, and the memory spider.

WHY THIS EXISTS
    Qoder plans are plain Markdown in ``~/.qoder/plans`` but Obsidian can only
    show files that live inside a vault. This module owns the vault chosen by
    the owner (``~/Jarvis``), mirrors plans into ``Planos/``, copies outside
    Markdown into the vault on demand, writes the memory index into
    ``Indices/`` and opens the result through the ``obsidian://`` URI scheme
    (``xdg-open``), with the desktop binary as fallback.

FAILURE POLICY
    ``mirror_plan`` refuses paths that are neither a Qoder plan nor already in
    the vault (``ValueError``). Copies are atomic and content-aware: an
    unchanged plan is not rewritten, so Obsidian's file watcher stays quiet.
    No Qt and no ``main.py`` imports.

VAULT FILE NAMES (owner's REGRA, 2026-11-07)
    Every new ``.md`` in the vault follows ``TIPO-AAAA-MM-DD-slug.md`` with
    TIPO inside the closed list ``NOTE_TYPES``. ``build_name``/``check_name``
    validate, ``normalize_name`` repairs, and ``_gate_name`` sits on the real
    write path (``write_note``, ``mirror_plan``, ``open_path``). The policy is
    ``JARVIS_VAULT_NAME_POLICY`` = ``registrar`` (default: write and log the
    offense) | ``normalizar`` | ``rejeitar``; asking for a name explicitly
    (``tipo=``) is always strict. ``Indices/Teia.md`` and ``Grafo.json`` are a
    code-locked exception (``CODE_OWNED_NAMES``): ``mirror_index`` rewrites
    them under the fixed names ``memory_index`` and ``guide_panel`` expect.
    Rule text: ``~/Jarvis/REGRA-2026-11-07-padro-de-nomes-de-arquivo.md``.

UP-7 (FLUXO TRABALHO, passo 4)
    Besides mirroring, this module is now the single place that records an
    Obsidian opening: every successful open writes ``event=obsidian_opened``
    with the path to ``logs/activity.log``, and ``open_note`` writes a fresh
    action-plan note into the vault and opens it on screen. When the owner's
    Nextcloud sync folder exists, ``sync_to_nextcloud`` copies the note there
    so the phone can open the very same file. Copying into the owner's own
    sync folder is all it does: no share, no public link, no other account —
    the vault stays exclusive to the owner (skills/actions/SKILL.md).

PASTA POR PLANO E MEMÓRIA (ordens do dono, 09/11/2026)
    Cada plano tem a sua pasta: :func:`create_plan_folder` monta
    ``Planos/<plano>/`` com ``plan.md``, ``status.md``, ``progresso.md``,
    ``historico.md``, ``testes.md``, ``prompt.md`` e os diretórios ``steps/``
    e ``steps/tasks/`` -- os seis nomes vieram por extenso na ordem e são
    exceção declarada à REGRA (mesmo tratamento de ``Indices/Teia.md``), só
    dentro de ``Planos/``. Os arquivos de passo e tarefa NÃO são exceção:
    :func:`create_step` e :func:`create_task` montam o nome em
    :func:`build_name` e validam em :func:`check_name`. O fluxo de memória
    grava em ``Memoria/`` (:func:`write_memory_note`,
    :func:`espelhar_memoria`, :func:`indice_da_memoria`) sempre com wikilinks
    (:func:`wikilink`), porque é o rastro entre arquivos que dá agilidade na
    busca -- a razão que o dono deu para usar o Obsidian. Conteúdo e títulos
    em PT-BR por padrão (ordem 2); o que é ASCII sem acento é só o slug de
    caminho, ditado pela REGRA de nomes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent.parent
VAULT = Path.home() / "Jarvis"
PLANS_DIR = Path.home() / ".qoder" / "plans"
# Pasta de sincronização do dono (cliente Nextcloud). Nada de conta de terceiro.
NEXTCLOUD_DIR = Path.home() / "Nextcloud"
SYNC_FOLDER = "Jarvis"
ACTIVITY_LOG = BASE_DIR / "logs" / "activity.log"
OBSIDIAN_BIN = (
    os.environ.get("JARVIS_OBSIDIAN_BIN")
    or shutil.which("obsidian")
    or ("/snap/bin/obsidian" if Path("/snap/bin/obsidian").exists() else "obsidian")
)

FOLDERS = ("Planos", "Projetos", "Personas", "Indices", "Topicos", "Memoria")

# ── estrutura de pasta por plano (ordem do dono, 09/11/2026) ────────────────
#
# Ordem literal: "CADA PLANO TEM QUE TER SUA PASTA, E DENTRO DELA TER o
# `plan.md`, `status.md`, `progresso.md`, `historico.md`, `testes.md`,
# `prompt.md`, E os DIRETORIOS `steps` E DENTRO DE STEPS `tasks`, E OS
# ARQUIVOS REFERENTES AOS STEPS E TASKS DEVEM SEGUIR O PADRAO/REGRA".
#
# Os seis nomes de arquivo vieram POR EXTENSO na ordem, do mesmo jeito que os
# quatro da vault de obras (`core/obras.py`): valem como exceção declarada à
# REGRA `TIPO-AAAA-MM-DD-slug.md` (mesmo tratamento de ``Indices/Teia.md``), e
# só dentro de ``Planos/<plano>/``. Já os arquivos de ``steps/`` e
# ``steps/tasks/`` NÃO são exceção: eles nascem de :func:`create_step` e
# :func:`create_task`, que montam o nome em :func:`build_name` e validam em
# :func:`check_name` antes de encostar no disco. É assim que "seguir o padrão"
# deixa de ser intenção e vira código.
PLAN_FOLDER_FILES: tuple[str, ...] = (
    "plan.md", "status.md", "progresso.md", "historico.md", "testes.md",
    "prompt.md",
)
PLAN_FOLDER_DIRS: tuple[str, ...] = ("steps", os.path.join("steps", "tasks"))
PLAN_FOLDER_ROOT = "Planos"
STEP_TIPO = "NOTA"
TASK_TIPO = "TAREFA"
PLAN_SLUG_MAX_CHARS = 60

#: Pasta do fluxo de memória no cofre exclusive do assistente. O conteúdo é
#: PT-BR (ordem do dono, 09/11/2026); o nome do arquivo é fixo por tópico
#: porque um tópico não pode ganhar um arquivo novo a cada dia -- é o mesmo
#: motivo pelo qual ``Teia.md`` é travado em código.
MEMORIA_FOLDER = "Memoria"
MEMORIA_INDICE = "Indice-da-memoria.md"
MEMORIA_LIMITE = 50

_LOCK = threading.RLock()


# ── helpers ─────────────────────────────────────────────────────────────────

def _vault() -> Path:
    override = os.environ.get("JARVIS_OBSIDIAN_VAULT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(VAULT).expanduser()


def registered_vault() -> str:
    """O cofre que o Obsidian desta máquina tem registrado ("" se não houver).

    ``obsidian://open?path=`` só mostra um arquivo que esteja num cofre que o
    aplicativo conhece. Sem essa leitura, abrir uma nota de um cofre não
    registrado daria "Abrindo..." na cara do dono e nada na tela.
    """
    config = Path.home() / ".config" / "obsidian" / "obsidian.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - sem config, sem opinião
        return ""
    vaults = data.get("vaults") if isinstance(data, dict) else None
    if not isinstance(vaults, dict) or not vaults:
        return ""
    def score(item):
        entry = item[1] if isinstance(item[1], dict) else {}
        return (bool(entry.get("open")), int(entry.get("ts") or 0))
    try:
        best = max(vaults.items(), key=score)
    except Exception:  # noqa: BLE001 - config corrompida não decide nada
        return ""
    entry = best[1] if isinstance(best[1], dict) else {}
    return str(entry.get("path") or "")


def _inside_registered_vault(target: Path) -> bool:
    """True quando não há config do Obsidian (não sabemos) ou o cofre bate."""
    registered = registered_vault()
    if not registered:
        return True
    try:
        return _resolved(target).is_relative_to(_resolved(Path(registered).expanduser()))
    except Exception:  # noqa: BLE001
        return True


def _plans() -> Path:
    return Path(PLANS_DIR).expanduser()


def _atomic_write(path: Path, text: str) -> None:
    tmp: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_copy(source: Path, destination: Path) -> Path:
    """Copy bytes atomically, skipping the write when content is identical."""
    data = source.read_bytes()
    try:
        if destination.is_file() and destination.read_bytes() == data:
            return destination
    except OSError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return destination


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _slug(value, limit: int = 80) -> str:
    """Nome de arquivo a partir de um título, preservando o português.

    ``\\w`` é unicode aqui: ``ação`` continua ``ação``. Só o que não cabe num
    nome de arquivo (separador de caminho, controle, símbolo estranho) vira
    ``_``; o espaço é legítimo no Obsidian e fica.

    NÃO é o padrão do cofre: o padrão é :func:`build_name`. Este ``_slug``
    legado continua servindo os chamadores antigos (``task_mirror`` escreve
    ``Plano de ação — <título>.md`` e os testes dele contam com isso) e por
    isso ele fica intacto. O que muda é o portão depois dele:
    :func:`_gate_name`.
    """
    text = re.sub(r"\s+", " ", str(value or ""))
    text = re.sub(r"[^\w .()—–-]+", "_", text)
    return text.strip(" ._")[:limit] or "nota"


# ── padrão de nome do cofre (REGRA do dono, 2026-11-07) ─────────────────────
#
# Texto da regra: ``~/Jarvis/REGRA-2026-11-07-padro-de-nomes-de-arquivo.md``.
# Padrão:  TIPO-AAAA-MM-DD-slug.md
#   TIPO  ∈ lista fechada abaixo, caixa-alta;
#   data  = data real do conteúdo (a do arquivo, não a do rename);
#   slug  = minúsculo, sem acento, sem espaço, hífens simples, <= 50 chars.
#
# Vocabulário fechado porque vocabulário aberto vira cada um inventando um
# prefixo: é exatamente o que a tabela de migração desta regra corrige.
NOTE_TYPES = ("PLANO", "REGRA", "TAREFA", "AUDITORIA", "RELATORIO", "NOTA")
SLUG_MAX_CHARS = 50
NAME_MAX_CHARS = 80

#: Pasta que o código já conhece -> TIPO. É isto que torna a normalização
#: inequívoca em vez de adivinhação. ``Indices`` fica fora de propósito:
#: ver :data:`CODE_OWNED_NAMES`.
FOLDER_TYPES = {
    "Planos": "PLANO",
    "Projetos": "NOTA",
    "Personas": "NOTA",
    "Topicos": "NOTA",
}

#: Exceção travada em código: a Teia e o Grafo são reescritos pela aranha de
#: memória com nome fixo (:func:`mirror_index`) e :mod:`core.memory_index` /
#: ``guide_panel`` os procuram por esse nome. Renomeá-los é mudança de
#: contrato, não de nomenclatura, então não passam pelo portão.
CODE_OWNED_NAMES = ("Teia.md", "Grafo.json")

#: Políticas do portão (env ``JARVIS_VAULT_NAME_POLICY``):
#:   ``registrar``  -> escreve como está e loga a infração (default de hoje);
#:   ``normalizar`` -> reescreve para o padrão quando não há ambiguidade;
#:   ``rejeitar``   -> levanta ValueError com a mensagem útil.
NAME_POLICIES = ("registrar", "normalizar", "rejeitar")
NAME_POLICY_DEFAULT = "registrar"

_NAME_RE = re.compile(
    r"^(?P<tipo>[A-Z]+)-(?P<ano>\d{4})-(?P<mes>\d{2})-(?P<dia>\d{2})-"
    r"(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
_MD_SUFFIX_RE = re.compile(r"(?:\.md)+$", re.IGNORECASE)
_DATE_IN_TEXT_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def name_policy() -> str:
    """Política vigente do portão; valor desconhecido volta ao default."""
    raw = os.environ.get("JARVIS_VAULT_NAME_POLICY", "").strip().lower()
    return raw if raw in NAME_POLICIES else NAME_POLICY_DEFAULT


def ascii_slug(value, limit: int = SLUG_MAX_CHARS) -> str:
    """Slug do padrão: minúsculo, ASCII, sem acento, hífens simples.

    Corta no hífen mais próximo do limite para nunca deixar ``-`` sobrando
    nem palavra partida ao meio. Devolve "" quando não sobra nada (o chamador
    decide o que fazer com o vazio; nunca virar "nota" silencioso aqui).
    """
    folded = unicodedata.normalize("NFKD", str(value or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower()
    folded = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if len(folded) > limit:
        head = folded[:limit]
        folded = head.rsplit("-", 1)[0] if "-" in head else head
        folded = folded.strip("-")
    return folded


def name_errors(name) -> list[str]:
    """Lista os problemas do nome contra o padrão; [] quando está dentro.

    Diagnóstico, não só veredito: quem recebe a lista é o dono ou o
    assistente, então cada linha diz o que refazer.
    """
    raw = str(name or "")
    if "/" in raw or "\\" in raw or raw == "..":
        return ["o nome não pode carregar caminho (use folder= para a pasta)"]
    text = Path(raw).name
    if not text:
        return ["nome vazio"]
    if text in CODE_OWNED_NAMES:
        return []

    errors: list[str] = []
    if re.search(r"\s", text):
        errors.append("nome de arquivo não pode ter espaço")
    suffix = _MD_SUFFIX_RE.search(text)
    if suffix is None:
        errors.append("falta a extensão .md")
    elif suffix.group(0).lower() != ".md":
        errors.append("extensão .md duplicada")

    full = _NAME_RE.match(text)
    if full:
        if full["tipo"] not in NOTE_TYPES:
            errors.append(f"tipo {full['tipo']!r} fora da lista fechada "
                          f"{'/'.join(NOTE_TYPES)}")
        try:
            dt.date(int(full["ano"]), int(full["mes"]), int(full["dia"]))
        except ValueError:
            errors.append(
                f"data {full['ano']}-{full['mes']}-{full['dia']} "
                "não existe no calendário")
        if len(full["slug"]) > SLUG_MAX_CHARS:
            errors.append(f"slug com {len(full['slug'])} chars "
                          f"(máximo {SLUG_MAX_CHARS})")
        if len(text) > NAME_MAX_CHARS:
            errors.append(f"nome com {len(text)} chars "
                          f"(máximo {NAME_MAX_CHARS})")
        return errors

    if text.lower().endswith(".md") or text.lower().endswith(".md.md"):
        stem = _MD_SUFFIX_RE.sub("", text)
    else:
        stem = text
    parts = stem.split("-")
    head = parts[0] if parts else ""
    if head in NOTE_TYPES:
        pass
    elif head.upper() in NOTE_TYPES:
        errors.append(f"tipo {head!r} precisa ficar em caixa-alta "
                      f"({head.upper()})")
    elif re.fullmatch(r"[A-Za-z]+", head or ""):
        errors.append(f"tipo {head!r} fora da lista fechada "
                      f"{'/'.join(NOTE_TYPES)}")
    else:
        errors.append("o nome não começa com um tipo da lista fechada "
                      f"{'/'.join(NOTE_TYPES)}; formato "
                      "TIPO-AAAA-MM-DD-slug.md")

    dates = _DATE_IN_TEXT_RE.search(stem)
    if dates:
        try:
            dt.date.fromisoformat(dates.group(0))
        except ValueError:
            errors.append(f"data {dates.group(0)!r} não existe no calendário")
    elif head in NOTE_TYPES:
        errors.append("falta a data AAAA-MM-DD depois do tipo")
    elif re.search(r"\b\d{4}\b", stem):
        errors.append("a data deve vir completa e logo depois do tipo "
                      "(AAAA-MM-DD)")
    else:
        errors.append("falta a data AAAA-MM-DD no nome")

    tail = stem[dates.end():] if dates else stem
    tail = tail.strip("-")
    if not tail:
        if head in NOTE_TYPES:
            errors.append("falta o slug depois da data")
    else:
        if tail != tail.lower():
            errors.append("o slug deve ser todo minúsculo")
        if not tail.isascii():
            errors.append("o slug não pode ter acento (use forma ASCII)")
        if "--" in tail:
            errors.append("o slug não pode ter hífen duplo")
        if tail.startswith("-") or tail.endswith("-"):
            errors.append("o slug não pode começar nem terminar com hífen")
        if len(tail) > SLUG_MAX_CHARS:
            errors.append(f"o slug tem {len(tail)} chars "
                          f"(máximo {SLUG_MAX_CHARS})")
    if len(text) > NAME_MAX_CHARS:
        errors.append(f"nome com {len(text)} chars (máximo {NAME_MAX_CHARS})")
    return errors


def check_name(name) -> str:
    """Valida e devolve o nome; levanta ValueError com o diagnóstico."""
    errors = name_errors(name)
    if errors:
        raise ValueError(
            f"nome {str(name)!r} fora do padrão TIPO-AAAA-MM-DD-slug.md: "
            + "; ".join(errors))
    return str(name)


def build_name(tipo: str, title, day=None) -> str:
    """Monta ``TIPO-AAAA-MM-DD-slug.md`` a partir de um tipo + título.

    Rigoroso por definição: tipo fora da lista ou título que não produz slug
    levanta ValueError em vez de inventar nome.
    """
    token = str(tipo or "").strip().upper()
    if token not in NOTE_TYPES:
        raise ValueError(f"tipo {tipo!r} fora da lista fechada "
                         f"{'/'.join(NOTE_TYPES)}")
    slug = ascii_slug(title)
    if not slug:
        raise ValueError(f"o título {str(title)!r} não produz slug; "
                         "use um título com letras")
    stamp = str(day or "").strip() or dt.date.today().isoformat()
    return check_name(f"{token}-{stamp}-{slug}.md")


def normalize_name(name, *, tipo: str = "", folder: str = "", day=None) -> str:
    """Reescreve um nome fora do padrão para dentro dele.

    Recusa quando a normalização NÃO é inequívoca: sem TIPO conhecido (passado
    ou inferido da pasta) e sem como saber a data, adivinhar seria pior que
    rejeitar -- a data errada conta uma mentira para sempre.
    """
    text = Path(str(name or "")).name
    stem = _MD_SUFFIX_RE.sub("", text)
    parts = stem.split("-")

    token = str(tipo or "").strip().upper()
    if token and token not in NOTE_TYPES:
        raise ValueError(f"tipo {tipo!r} fora da lista fechada "
                         f"{'/'.join(NOTE_TYPES)}")
    if not token:
        token = parts[0].upper() if parts and parts[0].upper() in NOTE_TYPES \
            else ""
    if not token:
        token = FOLDER_TYPES.get(str(folder or ""), "")
    if not token:
        raise ValueError(
            f"não dá para normalizar {text!r} sem tipo: passe tipo= "
            f"({'/'.join(NOTE_TYPES)}) ou escreva numa pasta conhecida "
            f"({', '.join(sorted(FOLDER_TYPES))})")

    stamp = ""
    found = _DATE_IN_TEXT_RE.search(stem)
    if found:
        try:
            # A data que o próprio nome declara é a data do conteúdo: ela
            # ganha do mtime, que muda com qualquer cópia.
            stamp = dt.date.fromisoformat(found.group(0)).isoformat()
        except ValueError:
            stamp = ""
    if not stamp:
        stamp = str(day or "").strip()
        if stamp:
            try:
                stamp = dt.date.fromisoformat(stamp).isoformat()
            except ValueError:
                raise ValueError(f"data {day!r} não é AAAA-MM-DD válida") \
                    from None
    if not stamp:
        stamp = dt.date.today().isoformat()

    residual = "-".join(parts[1:]) if parts and \
        parts[0].upper() in NOTE_TYPES else stem
    residual = _DATE_IN_TEXT_RE.sub(" ", residual)
    slug = ascii_slug(residual) or ascii_slug(stem)
    return check_name(f"{token}-{stamp}-{slug}.md")


def _mtime_day(path: Path) -> str:
    """Data do conteúdo (mtime) no formato AAAA-MM-DD; hoje se não houver."""
    try:
        return dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
    except (OSError, ValueError):
        return dt.date.today().isoformat()


def is_exempt_name(destination: Path) -> bool:
    """Nomes que o dono deu por extenso ou que o código escreve com nome fixo.

    Dois casos, e só estes:

    * ``Memoria/<qualquer>.md`` -- a pasta de memória do cofre exclusive do
      assistente: o arquivo é por tópico e tem de manter o MESMO nome a vida
      inteira, senão cada dia abre uma nota nova e o wikilink quebra.
    * ``Planos/<plano>/<um dos seis nomes do esqueleto>`` -- os arquivos que o
      dono nomeou um por um na ordem de 09/11/2026.

    Fora daqui, tudo passa pela REGRA. ``Indices/Teia.md`` e
    ``Indices/Grafo.json`` já tinham o seu atalho em :data:`CODE_OWNED_NAMES`,
    dentro de :func:`name_errors`.
    """
    try:
        relative = _resolved(Path(destination)).relative_to(_resolved(_vault()))
    except (ValueError, OSError):
        return False
    parts = relative.parts
    if not parts:
        return False
    if parts[0] == MEMORIA_FOLDER and len(parts) >= 2:
        return True
    return (parts[0] == PLAN_FOLDER_ROOT and len(parts) >= 3
            and relative.name in PLAN_FOLDER_FILES)


def _gate_name(destination: Path, *, tipo: str = "", folder: str = "",
               day: str | None = None) -> Path:
    """Portão de nome na escrita do cofre. É aqui que a regra vira código.

    - Nome dentro do padrão: passa direto.
    - Nome dos arquivos do esqueleto de plano (``Planos/<plano>/plan.md`` e
      companhia) e de ``Memoria/``: passa direto -- são nomes que o dono deu
      por extenso ou que o código trava (ver :func:`is_exempt_name`).
    - Nome fora + política ``registrar`` (default de hoje): escreve como está
      e loga ``event=vault_name_off_pattern`` com o diagnóstico. Padrão
      escolhido assim porque normalizar vivo renomearia o que o
      ``task_mirror`` escreve hoje (e os testes dele contam com o nome
      legado) antes do dono autorizar a rodada de renomeio.
    - Política ``normalizar``: troca pelo padrão, usando a data real do
      arquivo quando ele já existe; se a normalização for ambígua, rejeita.
    - Política ``rejeitar``: levanta ValueError com a mensagem útil.
    - Nome pedido explicitamente (``tipo=``): sempre rigoroso -- quem nomeia
      escolhe, então aqui não há modo "registrar".
    """
    if is_exempt_name(destination):
        return destination
    errors = name_errors(destination.name)
    if not errors:
        return destination
    if tipo:
        return destination.with_name(check_name(destination.name))

    policy = name_policy()
    diagnosis = "; ".join(errors)
    if policy == "rejeitar":
        raise ValueError(
            f"nome {destination.name!r} fora do padrão do cofre: {diagnosis} "
            f"(padrão TIPO-AAAA-MM-DD-slug.md, tipos: {'/'.join(NOTE_TYPES)})")
    if policy == "normalizar":
        stamp = day or (_mtime_day(destination) if destination.exists()
                        else None)
        fixed = normalize_name(destination.name, tipo=tipo,
                               folder=folder or destination.parent.name,
                               day=stamp)
        _log_event("vault_name_normalized", src=destination.name, dst=fixed,
                   motivo=diagnosis)
        return destination.with_name(fixed)
    _log_event("vault_name_off_pattern", path=destination, motivo=diagnosis)
    return destination



# ── activity.log ────────────────────────────────────────────────────────────

def _log_event(event: str, **fields) -> None:
    """Uma linha ``event=... chave="valor"`` no activity.log. Nunca levanta.

    É a evidência de que a integração aconteceu (UP-7): sem ela, abrir uma
    nota no Obsidian não deixava rastro nenhum para a auditoria.
    """
    try:
        parts = [f"event={event}"]
        for key, value in fields.items():
            if value is None or value == "":
                continue
            text = str(value).replace('"', "'").replace("\n", " ")
            parts.append(f'{key}="{text}"' if " " in text else f"{key}={text}")
        path = Path(ACTIVITY_LOG).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                     f"{' '.join(parts)}\n")
            fh.flush()
    except Exception:      # noqa: BLE001 - registrar não pode quebrar a abertura
        pass


# ── Nextcloud (só a conta do dono) ──────────────────────────────────────────

def sync_root() -> Path:
    return Path(NEXTCLOUD_DIR).expanduser()


def sync_available() -> bool:
    """True quando a pasta de sincronização do dono existe neste computador."""
    return sync_root().is_dir()


def sync_to_nextcloud(path) -> Path | None:
    """Copia um arquivo do cofre para ``~/Nextcloud/Jarvis/...``.

    Devolve o destino, ou ``None`` quando não há pasta de sincronização ou a
    cópia falhou. Manter a cópia dentro da conta do dono é exigência do
    contrato: nada aqui compartilha, cria link público ou mexe em ACL.
    """
    source = _resolved(Path(path).expanduser())
    if not source.is_file():
        return None
    root = sync_root()
    if not root.is_dir():
        return None
    try:
        relative = source.relative_to(_resolved(_vault()))
    except ValueError:
        relative = Path(source.name)
    destination = root / SYNC_FOLDER / relative
    try:
        return _atomic_copy(source, destination)
    except OSError as exc:  # noqa: BLE001 - o cofre local continua válido
        print(f"[Obsidian] espelho Nextcloud falhou ({source.name}): {exc}")
        return None


def ensure_vault() -> Path:
    """Create the vault, its folders and a minimal ``.obsidian`` config."""
    with _LOCK:
        vault = _vault()
        vault.mkdir(parents=True, exist_ok=True)
        for name in FOLDERS:
            (vault / name).mkdir(parents=True, exist_ok=True)
        settings = vault / ".obsidian"
        settings.mkdir(parents=True, exist_ok=True)
        app = settings / "app.json"
        if not app.is_file():
            _atomic_write(app, "{}")
        return vault


# ── plans ───────────────────────────────────────────────────────────────────

def mirror_plan(path) -> Path:
    """Copy one Qoder plan into ``Planos/<name>.md`` and return the copy."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"Plano não encontrado: {source}")
    source = _resolved(source)
    if not (source.is_relative_to(_resolved(_plans()))
            or source.is_relative_to(_resolved(_vault()))):
        raise ValueError(f"Fora de {_plans()} ou do cofre: {source}")
    destination = _gate_name(ensure_vault() / "Planos" / source.name,
                             folder="Planos", day=_mtime_day(source))
    return _atomic_copy(source, destination)


def mirror_plans(source_dir=None) -> list[Path]:
    """Mirror every ``*.md`` plan; return destination paths."""
    source = Path(source_dir).expanduser() if source_dir is not None else _plans()
    if not source.is_dir():
        return []
    destinations: list[Path] = []
    for plan in sorted(source.glob("*.md")):
        try:
            destinations.append(mirror_plan(plan))
        except (ValueError, OSError) as exc:
            print(f"[Obsidian] plano ignorado ({plan.name}): {exc}")
    return destinations


# ── index mirror ────────────────────────────────────────────────────────────

def mirror_index(markdown: str, graph: dict) -> Path:
    """Write ``Indices/Teia.md`` and ``Indices/Grafo.json``; return the MD.

    Além da teia, espelha o grafo no fluxo de memória do cofre
    (``Memoria/``: o índice de tópicos com wikilinks e a nota do dia). O
    espelho de memória é melhor-esforço: se ele falhar, a teia continua
    escrita e a aranha não perde o evento -- é a mesma política de
    ``core/memory_index.py:_mirror``.
    """
    with _LOCK:
        indices = ensure_vault() / "Indices"
        teia = indices / "Teia.md"
        grafo = indices / "Grafo.json"
        _atomic_write(teia, str(markdown or ""))
        _atomic_write(
            grafo,
            json.dumps(graph or {}, ensure_ascii=False, indent=2, sort_keys=True),
        )
        try:
            espelhar_memoria(graph or {})
        except Exception as exc:  # noqa: BLE001 - memória não derruba a teia
            _log_event("vault_memoria_espelho_falhou", erro=str(exc)[:200])
        return teia


# ── opening paths in Obsidian ───────────────────────────────────────────────

def _open_with_obsidian(target: Path, source: str = "open_in") -> str:
    url = "obsidian://open?path=" + quote(str(target), safe="")
    return_code = 1
    try:
        result = subprocess.run(["xdg-open", url], timeout=5)
        return_code = result if isinstance(result, int) else getattr(result, "returncode", 0)
        if not isinstance(return_code, int):
            # A mocked/unknown runner result still means the call happened.
            return_code = 0
    except Exception:  # noqa: BLE001 - any launcher failure falls back
        return_code = 1

    if return_code == 0:
        _log_event("obsidian_opened", path=target, source=source, via="xdg-open")
        return f"Abrindo {target.name} no Obsidian."
    try:
        subprocess.Popen([str(OBSIDIAN_BIN), str(target)])
        _log_event("obsidian_opened", path=target, source=source, via="binario")
        return f"Abrindo {target.name} no Obsidian."
    except Exception as exc:  # noqa: BLE001
        _log_event("obsidian_open_failed", path=target, source=source,
                   error=str(exc)[:200])
        return (
            f"Não consegui abrir {target.name} no Obsidian ({exc}); "
            f"o arquivo está em {target}."
        )


def open_path(path, source: str = "open_in") -> str:
    """Ensure the file is inside the vault, then open it in Obsidian."""
    file_source = Path(path).expanduser()
    if not file_source.exists():
        return f"Arquivo não encontrado: {file_source.name}"
    file_source = _resolved(file_source)
    if file_source.is_dir():
        return f"A pasta {file_source.name} não pode ser aberta no Obsidian."

    vault = ensure_vault()
    vault_resolved = _resolved(vault)
    plans_resolved = _resolved(_plans())

    if file_source.is_relative_to(plans_resolved):
        target = mirror_plan(file_source)
    elif file_source.is_relative_to(vault_resolved):
        target = file_source
    else:
        folder = "Planos" if file_source.suffix.lower() == ".md" else "Projetos"
        target = _atomic_copy(
            file_source,
            _gate_name(vault / folder / file_source.name, folder=folder,
                       day=_mtime_day(file_source)),
        )
    return _open_with_obsidian(target, source=source)


# ── notas do espelho de execução (UP-7) ─────────────────────────────────────

def write_note(title: str, body: str, folder: str = "Planos",
               tipo: str = "") -> Path:
    """Grava uma nota Markdown no cofre e devolve o caminho.

    ``tipo`` é a porta de entrada da REGRA de nomes: passado, o nome é montado
    como ``TIPO-AAAA-MM-DD-slug.md`` e validado antes de encostar no disco
    (fora da lista fechada -> ``ValueError``). Sem ``tipo``, o nome sai do
    ``_slug`` legado e passa pelo :func:`_gate_name`, que hoje registra a
    infração no activity.log em vez de renomear às escondidas.
    """
    if tipo:
        name = build_name(tipo, title)
    else:
        name = f"{_slug(title)}.md"
    vault = ensure_vault()
    destination = _gate_name(vault / folder / name, tipo=tipo, folder=folder)
    _atomic_write(destination, str(body or ""))
    return destination


def open_note(title: str, body: str, folder: str = "Planos",
              sync: bool = True, source: str = "task_mirror",
              tipo: str = "") -> dict:
    """Escreve a nota, espelha no Nextcloud do dono e abre na tela.

    Devolve ``{"ok", "path", "nextcloud", "registered", "message"}``. Falha de
    cofre ou de abertura não levanta: vira ``ok=False`` com a mensagem honesta,
    porque o dono pediu a nota na tela e um ``ImportError`` não pode derrubar
    a fala.

    ``registered`` diz se o cofre que recebemos é o que o Obsidian desta
    máquina tem aberto. Não é cosmético: ``obsidian://open?path=`` de um cofre
    não registrado corre o risco de não mostrar nada, e dizer "Abrindo" nesse
    caso seria mentira. Nada aqui registra cofre, muda config do Obsidian ou
    redefine a rota do vault do dono — isso é decisão dele.
    """
    try:
        note = write_note(title, body, folder=folder, tipo=tipo)
    except Exception as exc:  # noqa: BLE001 - o cofre pode estar fora do ar
        return {"ok": False, "path": "", "nextcloud": "", "registered": True,
                "message": f"Não consegui escrever a nota no cofre: {exc}"}
    copied = sync_to_nextcloud(note) if sync else None
    message = open_path(note, source=source)
    registered = _inside_registered_vault(note)
    if message.startswith("Abrindo") and not registered:
        message += (f" (atenção: o Obsidian desta máquina tem o cofre "
                    f"{registered_vault()} registrado; o da nota é {_vault()} — "
                    f"registre este cofre no Obsidian ou aponte "
                    f"JARVIS_OBSIDIAN_VAULT para ele)")
    return {"ok": bool(message.startswith("Abrindo")), "path": str(note),
            "nextcloud": str(copied) if copied else "",
            "registered": registered, "message": message}


# ── estrutura de pasta por plano (ordem do dono, 09/11/2026) ────────────────

def plan_folder_slug(nome) -> str:
    """Slug estável da pasta do plano, a partir do nome do arquivo ou do título.

    Aceita as três formas que hoje circulam no cofre:
    ``PLANO-2026-11-07-personas-de-trabalho....md``, ``personas-de-trabalho``
    e um título livre com acento. TIPO e data saem antes do slug: pasta não é
    arquivo -- a REGRA vale para nomes de ``.md`` e aqui o que se quer é um
    caminho curto, sem acento e estável entre execuções.
    """
    texto = Path(str(nome or "")).name
    texto = _MD_SUFFIX_RE.sub("", texto)
    partes = texto.split("-")
    if partes and partes[0].upper() in NOTE_TYPES:
        partes = partes[1:]
    resto = _DATE_IN_TEXT_RE.sub(" ", "-".join(partes))
    slug = (ascii_slug(resto, limit=PLAN_SLUG_MAX_CHARS)
            or ascii_slug(texto, limit=PLAN_SLUG_MAX_CHARS))
    if not slug or not re.search(r"[a-z]", slug):
        # Data e pontuação sozinhas não dão nome a uma pasta: "2026-11-09"
        # viraria uma pasta chamada 2026-11-09, indecidível entre execuções e
        # igual a qualquer outro plano do mesmo dia. Recusa, não adivinha.
        raise ValueError(
            f"nome sem matéria para pasta de plano: {nome!r} -- o slug precisa "
            "de letras; use o título do plano, não só a data")
    return slug


def _nome_bruto_plano(nome) -> str:
    """O nome do plano como arquivo: sem caminho, sem ``.md``."""
    return _MD_SUFFIX_RE.sub("", Path(str(nome or "")).name).strip()


def cofre_do_dono() -> Path:
    """O cofre que o Obsidian desta máquina tem registrado.

    Fato medido (``~/.config/obsidian/obsidian.json``): o dono abre
    ``/home/anthero/Documentos/Obsidian Vault`` -- e ``~/Jarvis`` é o cofre
    exclusive do assistente (decisão 2 do plano de fluxo, 07/11/2026). Os dois
    têm usos diferentes e o código precisa dos dois nomes para não afirmar ao
    dono que algo apareceu na tela dele quando apareceu na minha. Sem config
    do Obsidian, vale o cofre do código.
    """
    registrado = registered_vault()
    return Path(registrado).expanduser() if registrado else _vault()


def _pasta_existente_do_plano(vault: Path, slug: str, bruto: str) -> Path | None:
    """A pasta que JÁ É deste plano, seja qual for o nome que lhe escreveram.

    A ordem do dono deu a cada plano uma pasta; o nome da pasta não foi dado.
    Hoje o cofre tem ``Planos/PLANO-2026-11-09-<slug>/`` (montada à mão na
    rodada anterior) enquanto o código produz ``Planos/<slug>/``. Deixar as
    duas nascerem é exatamente o bug "duas cópias divergindo": o dono lê uma
    pasta, o assistente escreve na outra, e nobody sabe qual é a verdade.
    Procura-se, por esta ordem, antes de criar seja o que for:

    1. pasta com o slug canónico;
    2. pasta com o nome do arquivo do plano (``PLANO-AAAA-MM-DD-slug``);
    3. pasta cujo próprio nome normalize no mesmo slug -- data diferente
       incluida, porque plano renomeado não ganha casa nova.

    Nada é movido, copiado nem renomeado: a pasta que existe vence, porque o
    dono pode ter editado nela.
    """
    raiz = vault / PLAN_FOLDER_ROOT
    try:
        candidatas = sorted(p for p in raiz.iterdir() if p.is_dir())
    except OSError:
        return None
    for pasta in candidatas:
        if pasta.name == slug:
            return pasta
    if bruto:
        for pasta in candidatas:
            if pasta.name.casefold() == bruto.casefold():
                return pasta
    normalizadas = []
    for pasta in candidatas:
        try:
            if plan_folder_slug(pasta.name) == slug:
                normalizadas.append(pasta)
        except ValueError:
            continue
    return normalizadas[0] if normalizadas else None


def plan_folder(nome) -> Path:
    """O caminho da pasta do plano (resolve, não cria nada).

    Resolve a pasta já existente quando o mesmo plano foi gravado com outro
    nome (:func:`_pasta_existente_do_plano`); sem pasta, devolve o caminho
    canónico ``Planos/<slug>/``. É por esta função que ``create_plan_folder``,
    ``create_step`` e ``create_task`` chegam ao mesmo lugar -- se cada uma
    montasse o caminho por conta, um plano teria duas casas.
    """
    vault = _vault()
    slug = plan_folder_slug(nome)
    achada = _pasta_existente_do_plano(vault, slug, _nome_bruto_plano(nome))
    return achada if achada is not None else (vault / PLAN_FOLDER_ROOT / slug)


def _garantir_pasta(pasta: Path) -> None:
    """mkdir com diagnóstico: colisão de nome e permissão negada nomeados.

    ``FileExistsError`` cru faria o chamador achar que o plano existe; um
    ``PermissionError`` cru não diz qual cofre está podre. Os dois casos
    tinham de ter frase legível porque os dois acontecem: o cofre do dono é
    outro usuário, e um arquivo com o nome da pasta já foi escrito à mão aqui.
    """
    if pasta.is_dir():
        return
    if pasta.exists() or pasta.is_symlink():
        _log_event("vault_pasta_colisao", path=pasta)
        raise ValueError(
            f"colisão de nome no cofre: {pasta} existe e NÃO é pasta. "
            "Nada foi escrito em cima: renomeie o arquivo (ou aponte o nome "
            "do plano) e rode de novo -- os seus dados não se movem sozinhos")
    try:
        pasta.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        _log_event("vault_pasta_sem_permicao", path=pasta, erro=str(exc)[:200])
        raise OSError(
            f"sem permissão para criar {pasta}: o cofre {_vault()} não é "
            f"escrevível por este processo ({exc.strerror or exc})") from None
    except OSError as exc:
        _log_event("vault_pasta_falhou", path=pasta, erro=str(exc)[:200])
        raise OSError(f"não deu para criar {pasta}: {exc}") from None


def _escrever_esqueleto(alvo: Path, texto: str) -> None:
    """:func:`_atomic_write` com a mesma cara de erro do portão de pastas."""
    try:
        _atomic_write(alvo, texto)
    except PermissionError as exc:
        _log_event("vault_esqueleto_sem_permicao", path=alvo,
                   erro=str(exc)[:200])
        raise OSError(
            f"sem permissão para escrever {alvo}: o cofre {_vault()} está "
            f"fechado para este processo ({exc.strerror or exc})") from None


def _sha_de_arquivo(path: Path) -> str:
    """Resumo curto do conteúdo; "" quando o arquivo não se lê."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def copias_do_plano(nome) -> list[dict]:
    """Toda cópia conhecida do texto de um plano, com hash e data reais.

    É a medição por trás de "cópia, não migração": o original fica onde está
    (raiz do cofre do dono, raiz do meu cofre) e a pasta do plano recebe a
    cópia. Para isso não virar duas verdades, a comparação existe como função
    -- sha256 + mtime de cada arquivo, nada de "está igual, acredito".
    """
    bruto = _nome_bruto_plano(nome)
    vistos: list[Path] = []
    alvo = plan_folder(nome) / "plan.md"
    vistos.append(alvo)
    for raiz in (_vault(), cofre_do_dono()):
        if not bruto:
            continue
        for candidato in (raiz / f"{bruto}.md", raiz / bruto):
            if candidato not in vistos:
                vistos.append(candidato)
    saida: list[dict] = []
    for caminho in vistos:
        existe = caminho.is_file()
        saida.append({"caminho": str(caminho), "existe": existe,
                      "sha": _sha_de_arquivo(caminho) if existe else "",
                      "bytes": caminho.stat().st_size if existe else 0,
                      "dia": _mtime_day(caminho) if existe else ""})
    return saida


def divergem_copias_do_plano(nome) -> dict:
    """Veredito da comparação: ``iguais`` só quando os hashes batem todos."""
    copias = [c for c in copias_do_plano(nome) if c["existe"]]
    hashes = {c["sha"] for c in copias if c["sha"]}
    iguais = len(hashes) <= 1
    return {"plano": _nome_bruto_plano(nome) or str(nome),
            "pasta": str(plan_folder(nome)), "copias": copias,
            "iguais": iguais,
            "motivo": "" if iguais else "copias_divergindo"}


def _titulo_do_plano(texto: str, fallback) -> str:
    """O H1 do plano, ou um nome legível derivado do arquivo."""
    for linha in str(texto or "").splitlines():
        if linha.startswith("# "):
            return linha[2:].strip() or str(fallback)
    return str(fallback or "").strip() or "plano"


def _link_de_arquivo(nome) -> str:
    """Wikilink para o arquivo do cofre a partir do nome (com ou sem .md)."""
    stem = _MD_SUFFIX_RE.sub("", Path(str(nome or "")).name)
    return wikilink(stem)


def _corpo_plan(titulo: str, slug: str, *, original: str, fonte: str,
                carimbo: str) -> str:
    """O ``plan.md``: o plano em si + o mapa da pasta."""
    cabeca = (f"# {titulo}\n\n"
              f"- **pasta do plano:** `{PLAN_FOLDER_ROOT}/{slug}/`\n"
              f"- **montado em:** {carimbo}\n"
              f"- **esta pasta:** {wikilink('status')} · "
              f"{wikilink('progresso')} · {wikilink('historico')} · "
              f"{wikilink('testes')} · {wikilink('prompt')}\n")
    if fonte:
        cabeca += (f"- **o documento do plano:** "
                   f"{_link_de_arquivo(fonte)}\n")
    mapa = (
        cabeca + "\n"
        "## Como esta pasta se usa\n\n"
        "| arquivo / pasta | o que entra aqui |\n"
        "| --- | --- |\n"
        "| `plan.md` | o plano (este arquivo) |\n"
        "| `status.md` | a foto de agora: o que está aberto, fechado, bloqueado |\n"
        "| `progresso.md` | o que já foi feito, em ordem, com data |\n"
        "| `historico.md` | os commits reais desta obra |\n"
        "| `testes.md` | os testes desta obra e o que cada um cobre |\n"
        "| `prompt.md` | o prompt que mandou executar (redigido) |\n"
        f"| `{PLAN_FOLDER_ROOT}/<plano>/steps/` | um arquivo por passo do plano |\n"
        "| `steps/tasks/` | um arquivo por tarefa de cada passo |\n\n"
        "### O padrão dos arquivos de `steps/` e `steps/tasks/`\n\n"
        "Eles seguem a REGRA do cofre "
        "(`" + "TIPO-AAAA-MM-DD-slug.md" + "`), e é código que monta o nome: "
        f"{wikilink('REGRA-2026-11-07-padro-de-nomes-de-arquivo')}. Passo é "
        f"`{STEP_TIPO}-AAAA-MM-DD-step-NN-slug.md` "
        "(`core.obsidian.create_step`); tarefa é "
        f"`{TASK_TIPO}-AAAA-MM-DD-task-NN-slug.md` "
        "(`core.obsidian.create_task`).\n\n"
        "### Segredo em documento de plano\n\n"
        "Nenhum valor de chave de API, token, senha ou credencial entra aqui. "
        "O documento cita a chave pelo nome e pelo tipo, "
        "`[key:<nome>, tipo:<tipo>]`, e quem executa lê o valor pelo portão de "
        f"segredo (`core/obras.py`), nunca deste arquivo. Ver "
        f"{wikilink('prompt')}.\n"
    )
    if original.strip():
        return mapa + "\n---\n\n" + original.rstrip("\n") + "\n"
    return mapa + "\n## Plano\n\n*(ainda não escrito)*\n"


def _corpo_status(titulo: str, slug: str, carimbo: str) -> str:
    return (f"# status — {titulo}\n\n"
            f"- **pasta:** `Planos/{slug}/`\n"
            f"- **plano:** {wikilink('plan')}\n"
            f"- **fase atual:** em aberto\n"
            f"- **registrado:** {carimbo}\n\n"
            "O que está de pé agora. Uma linha por fase, com data de entrada e "
            "de saída; nada aqui é história acumulada (isso é o "
            f"{wikilink('progresso')}).\n\n"
            "## Fases\n\n"
            "| fase | estado | desde | fechou quando | por quê |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| *(preencher)* | em aberto | — | — | — |\n")


def _corpo_progresso(titulo: str, slug: str, carimbo: str) -> str:
    return (f"# progresso — {titulo}\n\n"
            f"- **pasta:** `Planos/{slug}/`\n"
            f"- **plano:** {wikilink('plan')}\n"
            f"- **registrado:** {carimbo}\n\n"
            "O que já foi feito, na ordem em que foi feito, com data e hora. "
            "Linha nova a cada tarefa fechada; linha nunca apagada.\n\n"
            "## Andamento\n\n"
            f"- [{carimbo}] pasta criada com o esqueleto do plano "
            "(`plan.md`, `status.md`, `progresso.md`, `historico.md`, "
            "`testes.md`, `prompt.md`, `steps/`, `steps/tasks/`) — "
            f"ver {wikilink('historico')}.\n")


def _corpo_historico(titulo: str, slug: str, carimbo: str) -> str:
    return (f"# histórico — {titulo}\n\n"
            f"- **pasta:** `Planos/{slug}/`\n"
            f"- **plano:** {wikilink('plan')}\n"
            f"- **registrado:** {carimbo}\n\n"
            "Commits REAIS desta obra, hash saído do `git log` do repositório "
            "de código -- nunca hash inventado, nunca commit que só existe no "
            "plano.\n\n"
            "## Commits\n\n"
            "- *(nenhum commit registrado ainda)*\n")


def _corpo_testes(titulo: str, slug: str, carimbo: str) -> str:
    return (f"# testes — {titulo}\n\n"
            f"- **pasta:** `Planos/{slug}/`\n"
            f"- **plano:** {wikilink('plan')}\n"
            f"- **registrado:** {carimbo}\n\n"
            "Testes desta obra, um por linha, com o resultado REAL da execução "
            "(comando + contagem), não a intenção de testar.\n\n"
            "## Testes\n\n"
            "- *(nenhum teste registrado ainda)*\n")


def _corpo_prompt(titulo: str, slug: str, texto: str, carimbo: str) -> str:
    sem_prompt = "*(prompt ainda não registrado)*"
    try:
        from core import obras as _obras
        corpo = _obras.referenciar(texto).strip() if str(texto or "").strip() \
            else sem_prompt
    except Exception:  # noqa: BLE001 - sem o redator, o prompt não entra cru
        corpo = "*(retido: o redator de segredo não estava disponível)*"
    return (f"# prompt — {titulo}\n\n"
            f"- **pasta:** `Planos/{slug}/`\n"
            f"- **plano:** {wikilink('plan')}\n"
            f"- **registrado:** {carimbo}\n\n"
            "O prompt que mandou executar isto. Segredo não entra: o valor "
            "some e fica o endereço da chave, `[key:<nome>, tipo:<tipo>]` "
            "(política de redação em `core/obras.py`).\n\n"
            "---\n\n"
            f"{corpo}\n")

_CORPOS = {
    "status.md": _corpo_status,
    "progresso.md": _corpo_progresso,
    "historico.md": _corpo_historico,
    "testes.md": _corpo_testes,
}


def _carimbo(dia: str | None) -> str:
    """Data AAAA-MM-DD validada; hoje quando o chamador não tem opinião."""
    bruto = str(dia or "").strip()
    if not bruto:
        return dt.date.today().isoformat()
    try:
        return dt.date.fromisoformat(bruto).isoformat()
    except ValueError:
        raise ValueError(f"data {dia!r} não é AAAA-MM-DD válida") from None


def create_plan_folder(nome, *, conteudo: str | None = None, fonte=None,
                       prompt: str = "", dia: str | None = None) -> Path:
    """Monta o esqueleto da pasta de um plano e devolve o caminho.

    ``Planos/<plano>/`` com os seis arquivos nomeados pelo dono
    (:data:`PLAN_FOLDER_FILES`), o diretório ``steps/`` e o ``steps/tasks/``
    (:data:`PLAN_FOLDER_DIRS`). É idempotente por arquivo: o que já existe é
    respeitado e nunca reescrito -- quem mexeu numa nota do Obsidian não perde
    o texto na próxima passada, e rodar duas vezes não cria pasta gêmea.

    O que a função RECUSA em vez de improvisar (cada recusa tem teste):

    * ``fonte=`` apontando para caminho inexistente ou para pasta -- melhor
      parar do que gravar um ``plan.md`` vazio dizendo que copiou o plano;
    * colisão de nome: ``Planos/<plano>`` ocupado por arquivo, ou um dos seis
      nomes ocupado por pasta;
    * permissão negada no cofre: ``OSError`` com o caminho e o cofre na frase,
      não um errno solto;
    * nome sem matéria para slug (:func:`plan_folder_slug` levanta).

    Nada aqui move nem apaga original: os arquivos que já vivem na raiz do
    cofre ficam onde estão, e a divergência entre eles e a cópia da pasta é
    MEDIDA (:func:`divergem_copias_do_plano`) e logada, não jurada.
    """
    with _LOCK:
        vault = ensure_vault()
        slug = plan_folder_slug(nome)
        bruto = _nome_bruto_plano(nome)
        achada = _pasta_existente_do_plano(vault, slug, bruto)
        pasta = achada if achada is not None else (vault / PLAN_FOLDER_ROOT / slug)
        fonte_caminho = Path(fonte).expanduser() if fonte is not None else None
        if fonte_caminho is not None:
            if fonte_caminho.is_dir():
                raise ValueError(
                    f"a fonte do plano é uma pasta, não um arquivo: "
                    f"{fonte_caminho}")
            if not fonte_caminho.is_file():
                raise ValueError(
                    f"fonte do plano inexistente: {fonte_caminho} — nada foi "
                    "copiado e nenhuma nota vazia sai no lugar do plano")
            if conteudo is None:
                try:
                    conteudo = fonte_caminho.read_text(encoding="utf-8",
                                                       errors="replace")
                except OSError as exc:
                    raise ValueError(
                        f"não deu para ler a fonte {fonte_caminho}: {exc}") \
                        from None
        original = str(conteudo or "")
        titulo = _titulo_do_plano(original, nome)
        carimbo = _carimbo(dia)
        fonte_nome = (fonte_caminho.name if fonte_caminho is not None
                      else str(nome or ""))
        _garantir_pasta(pasta)
        nome_pasta = pasta.name

        alvo_plan = _gate_name(pasta / "plan.md", folder=PLAN_FOLDER_ROOT)
        if alvo_plan.is_dir():
            raise ValueError(
                f"colisão de nome no cofre: {alvo_plan} é pasta e o dono "
                "pediu ali um arquivo plan.md")
        if not alvo_plan.exists():
            _escrever_esqueleto(
                alvo_plan, _corpo_plan(titulo, nome_pasta, original=original,
                                       fonte=fonte_nome, carimbo=carimbo))
        for nome_arquivo in PLAN_FOLDER_FILES:
            if nome_arquivo == "plan.md":
                continue
            alvo = _gate_name(pasta / nome_arquivo, folder=PLAN_FOLDER_ROOT)
            if alvo.is_dir():
                raise ValueError(
                    f"colisão de nome no cofre: {alvo} é pasta e o dono pediu "
                    f"ali o arquivo {nome_arquivo}")
            if alvo.exists():
                continue
            if nome_arquivo == "prompt.md":
                _escrever_esqueleto(alvo, _corpo_prompt(titulo, nome_pasta,
                                                        prompt, carimbo))
            else:
                _escrever_esqueleto(alvo, _CORPOS[nome_arquivo](titulo,
                                                                nome_pasta,
                                                                carimbo))
        for subpasta in PLAN_FOLDER_DIRS:
            _garantir_pasta(pasta / subpasta)
        veredito = divergem_copias_do_plano(nome)
        existentes = [c for c in veredito["copias"] if c["existe"]]
        _log_event("vault_pasta_de_plano", pasta=pasta, plano=slug,
                   alias=str(pasta.name != slug).lower(),
                   registradas=str(_inside_registered_vault(pasta)).lower(),
                   copias=len(existentes), fonte=str(fonte_nome))
        if not veredito["iguais"]:
            _log_event("vault_copias_divergindo", plano=slug, pasta=pasta,
                       arquivos="; ".join(c["caminho"] for c in existentes))
        return pasta


def _passo_nome(numero, titulo: str, prefixo: str, tipo: str,
                dia: str | None) -> str:
    """Nome de arquivo de passo/tarefa: sempre montado e validado pela REGRA."""
    sequencia = str(numero or "").strip() or "01"
    try:
        sequencia = f"{int(sequencia):02d}"
    except (TypeError, ValueError):
        pass
    bruto = f"{prefixo}-{sequencia} {titulo}"
    return build_name(tipo, bruto, day=_carimbo(dia))


def create_step(nome_plano, numero, titulo: str, *, corpo: str = "",
                dia: str | None = None) -> Path:
    """Cria ``steps/<NOTA-AAAA-MM-DD-step-NN-slug>.md`` do plano e devolve o caminho.

    O nome sai de :func:`build_name` e passa por :func:`check_name`: passo que
    não cabe na REGRA não entra na pasta. Existe? Não reescreve.
    """
    with _LOCK:
        pasta = plan_folder(nome_plano)
        nome = _passo_nome(numero, titulo, "step", STEP_TIPO, dia)
        alvo = _gate_name(pasta / "steps" / nome, folder=PLAN_FOLDER_ROOT)
        if not alvo.exists():
            alvo.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(alvo, _corpo_passo(titulo, nome, "passo", numero,
                                             corpo))
        return alvo


def create_task(nome_plano, numero, titulo: str, *, passo: str = "",
                corpo: str = "", dia: str | None = None) -> Path:
    """Cria ``steps/tasks/<TAREFA-AAAA-MM-DD-task-NN-slug>.md`` e devolve o caminho.

    ``passo=`` é o título ou o nome do passo a que a tarefa pertence: entra no
    corpo como wikilink, e é isso que faz o gráfico do Obsidian ligar tarefa,
    passo e plano sem procura.
    """
    with _LOCK:
        pasta = plan_folder(nome_plano)
        nome = _passo_nome(numero, titulo, "task", TASK_TIPO, dia)
        alvo = _gate_name(pasta / "steps" / "tasks" / nome,
                          folder=PLAN_FOLDER_ROOT)
        if not alvo.exists():
            alvo.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(alvo, _corpo_passo(titulo, nome, "tarefa", numero,
                                             corpo, passo=passo))
        return alvo


def _corpo_passo(titulo: str, nome: str, que: str, numero, corpo: str,
                 *, passo: str = "") -> str:
    descricao = str(corpo or "").strip() or (
        "*(descrever a execução desta unidade: o que muda, onde, como se "
        "prova)*")
    liga = f"- **plano:** {wikilink('plan')}\n"
    if passo:
        liga += f"- **passo:** {_link_de_arquivo(passo)}\n"
    return (f"# {que} {numero} — {titulo}\n\n"
            f"- **arquivo:** `{nome}`\n"
            f"{liga}"
            f"- **estado:** em aberto\n"
            f"- **registrado:** {dt.date.today().isoformat()}\n\n"
            "## O que faz\n\n"
            f"{descricao}\n\n"
            "## Prova de que fechou\n\n"
            "- *(citar comando, teste ou arquivo:linha -- sem prova a "
            f"unidade não fecha; ver {wikilink('testes')})*\n\n"
            "## Chaves usadas\n\n"
            "- *(só `[key:<nome>, tipo:<tipo>]`; o valor nunca entra aqui)*\n")


# ── fluxo de memória no cofre (ordem do dono, 09/11/2026) ──────────────────

def wikilink(nome, texto: str = "") -> str:
    """Wikilink do Obsidian: ``[[nome]]`` ou ``[[nome|texto]]``.

    O que não cabe num link (``[] | # ^``) sai; o resto fica como está,
    inclusive o acento -- conteúdo PT-BR é ordem do dono. ``texto`` (o rótulo)
    também entra posicional porque é assim que se lê num fôlego:
    ``wikilink('topico-vault', 'vault')``.
    """
    limpo = re.sub(r"[\[\]|#^]", "", str(nome or "")).strip()
    rotulo = re.sub(r"[\[\]|#^]", "", str(texto or "")).strip()
    if not limpo:
        return rotulo or ""
    return f"[[{limpo}|{rotulo}]]" if rotulo else f"[[{limpo}]]"


def memoria_root() -> Path:
    """A pasta de memória do cofre exclusive (criada se faltar)."""
    raiz = ensure_vault() / MEMORIA_FOLDER
    raiz.mkdir(parents=True, exist_ok=True)
    return raiz


def _slug_memoria(topico) -> str:
    """Nome de nota de memória: legível, estável, sem acento no caminho."""
    slug = ascii_slug(topico, limit=SLUG_MAX_CHARS)
    if not slug:
        raise ValueError(f"tópico sem matéria para nota de memória: {topico!r}")
    return slug


def nota_de_topico(topico, *, prefixo: str = "topico") -> Path:
    """``Memoria/<prefixo>-<slug>.md`` -- o caminho da nota de um tópico."""
    return memoria_root() / f"{prefixo}-{_slug_memoria(topico)}.md"


#: Marca deixada quando nem o redator da vault de obras pôde carregar: a
#: memória existe, o conteúdo do trecho não.
RETIDA = "[retido]"
FMT_HORA_VAULT = "%Y-%m-%d %H:%M"


def _redator():
    """O redator de segredo da vault de obras, com fallback conservador.

    A memória espelha fala e resumo de conversa: se o redator não carregar, o
    texto NÃO entra cru -- entra a marca de retido. Falso positivo é
    aceitável; vazar chave no cofre não é.
    """
    try:
        from core import obras as _obras
    except Exception:  # noqa: BLE001 - sem obras, nada entra cru
        return lambda texto: f"{RETIDA} (redator indisponível)"
    return getattr(_obras, "referenciar", None) or _obras.redigir


def write_memory_note(topico, texto, *, quando=None, refs=(),
                      plano=None) -> Path:
    """Acrescenta uma linha datada à nota do tópico, com wikilinks.

    Uma nota por tópico, nome estável (é isso que faz o wikilink ligar os
    arquivos e a busca ficar rápida -- a razão que o dono deu para usar o
    Obsidian). O texto passa pelo redator antes do disco. ``refs=`` e
    ``plano=`` viram wikilinks na linha, então a teia se costura sozinha.
    """
    with _LOCK:
        alvo = nota_de_topico(topico)
        momento = str(quando or "").strip() or time.strftime(FMT_HORA_VAULT)
        liga = [wikilink(MEMORIA_INDICE[:-3])]
        if plano:
            liga.append(wikilink(plano))
        for ref in refs or ():
            nome = Path(str(ref)).name if ref else ""
            if nome:
                liga.append(wikilink(_MD_SUFFIX_RE.sub("", nome)))
        linha = f"- [{momento}] {_redator()(texto)} — " + " ".join(liga)
        base_existente = alvo.read_text(encoding="utf-8") if alvo.is_file() else ""
        base = base_existente.rstrip("\n") if base_existente else (
            f"# memória — {str(topico).strip()}\n\n"
            f"- **índice:** {wikilink(MEMORIA_INDICE[:-3])}\n"
            f"- **criada:** {time.strftime(FMT_HORA_VAULT)}\n\n"
            "Uma linha por registro, com data e hora, ligada ao que toca. "
            f"Está em `{MEMORIA_FOLDER}/` no cofre exclusive do assistente; o "
            "vault do dono continua o mesmo, a pasta é nova.\n\n"
            "## Registros")
        _atomic_write(alvo, base + "\n\n" + linha + "\n")
        return alvo


def _facetas_do_grafo(graph: dict) -> dict[str, dict[str, int]]:
    """Conta os eventos por faceta (subject/persona/project) do grafo."""
    saida: dict[str, dict[str, int]] = {}
    eventos = (graph or {}).get("events")
    if not isinstance(eventos, dict):
        return saida
    for ev in eventos.values():
        if not isinstance(ev, dict):
            continue
        for faceta in ("subject", "persona", "project"):
            valor = str(ev.get(faceta) or "").strip()
            if not valor:
                continue
            contas = saida.setdefault(faceta, {})
            contas[valor] = contas.get(valor, 0) + 1
    return saida


def _eventos_do_dia(graph: dict, dia: str) -> list[dict]:
    """Eventos do grafo cujo timestamp cai no dia pedido, mais recentes primeiro."""
    eventos = (graph or {}).get("events")
    if not isinstance(eventos, dict):
        return []
    achados = [ev for ev in eventos.values()
               if isinstance(ev, dict)
               and str(ev.get("at") or "").startswith(dia)]
    achados.sort(key=lambda ev: str(ev.get("at") or ""), reverse=True)
    return achados[:MEMORIA_LIMITE]


def indice_da_memoria(graph: dict | None = None, *, dia: str | None = None) -> Path:
    """Reescreve ``Memoria/Indice-da-memoria.md``: o hub de wikilinks da memória."""
    with _LOCK:
        alvo = memoria_root() / MEMORIA_INDICE
        carimbo = time.strftime(FMT_HORA_VAULT)
        dia_atual = str(dia or "").strip() or dt.date.today().isoformat()
        linhas = [
            "# índice da memória",
            "",
            f"- **gerado:** {carimbo}",
            f"- **nota do dia:** {wikilink(f'dia-{dia_atual}')}",
            "",
            "Um link por faceta conhecida. É por aqui que a busca entra na "
            "memória: o gráfico do Obsidian mostra o que já está ligado a quê "
            "sem precisar ler arquivo por arquivo.",
            "",
        ]
        facetas = _facetas_do_grafo(graph or {})
        titulos = {"subject": "Assuntos", "persona": "Personas",
                   "project": "Projetos"}
        escritas = 0
        for chave in ("subject", "persona", "project"):
            contas = facetas.get(chave) or {}
            linhas += [f"## {titulos[chave]}", ""]
            if not contas:
                linhas.append("_Nenhum registro._")
                linhas.append("")
                continue
            itens = sorted(contas.items(), key=lambda kv: (-kv[1], kv[0]))
            for slug, total in itens[:MEMORIA_LIMITE]:
                linhas.append(f"- {wikilink(f'topico-{slug}', slug)} — "
                              f"{total} registro(s)")
                escritas += 1
            linhas.append("")
        pastas = sorted(p.name[:-3] for p in memoria_root().glob("topico-*.md"))
        if pastas:
            linhas += ["## Notas escritas", ""]
            for nome in pastas[:MEMORIA_LIMITE]:
                linhas.append(f"- {wikilink(nome)}")
            linhas.append("")
        alvo.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(alvo, "\n".join(linhas) + "\n")
        _log_event("vault_memoria_indice", path=alvo, links=escritas)
        return alvo


def nota_do_dia(graph: dict, *, dia: str | None = None) -> Path:
    """Reescreve ``Memoria/dia-<AAAA-MM-DD>.md`` com os eventos daquele dia."""
    with _LOCK:
        carimbo_dia = str(dia or "").strip() or dt.date.today().isoformat()
        alvo = memoria_root() / f"dia-{carimbo_dia}.md"
        linhas = [f"# memória do dia {carimbo_dia}", "",
                  f"- **índice:** {wikilink(MEMORIA_INDICE[:-3])}", "",
                  "## Registros", ""]
        eventos = _eventos_do_dia(graph or {}, carimbo_dia)
        if not eventos:
            linhas.append("_Nenhum evento indexado neste dia._")
        redator = _redator()
        for ev in eventos:
            hora = str(ev.get("at") or "")[11:16] or "--:--"
            faceta = str(ev.get("subject") or "geral")
            linhas.append(f"- [{hora}] {wikilink(f'topico-{faceta}', faceta)}: "
                          f"{redator(ev.get('text'))}")
        alvo.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(alvo, "\n".join(linhas) + "\n")
        return alvo


def espelhar_memoria(graph: dict) -> list[Path]:
    """Espelha o grafo da aranha em ``Memoria/``: índice + nota do dia.

    Chutado por :func:`mirror_index`, que é o que ``core/memory_index.py`` já
    chama a cada evento. São dois arquivos por passada, de propósito: nota por
    tópico se escreve quando o tópico merece página própria
    (:func:`write_memory_note`), e não a cada evento do mundo.
    """
    with _LOCK:
        hoje = dt.date.today().isoformat()
        indice = indice_da_memoria(graph or {}, dia=hoje)
        dia = nota_do_dia(graph or {}, dia=hoje)
        _log_event("vault_memoria_espelhada", indice=indice, dia=dia)
        return [indice, dia]
