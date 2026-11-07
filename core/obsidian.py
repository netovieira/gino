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
"""
from __future__ import annotations

import datetime as dt
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

FOLDERS = ("Planos", "Projetos", "Personas", "Indices", "Topicos")

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


def _gate_name(destination: Path, *, tipo: str = "", folder: str = "",
               day: str | None = None) -> Path:
    """Portão de nome na escrita do cofre. É aqui que a regra vira código.

    - Nome dentro do padrão: passa direto.
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
    """Write ``Indices/Teia.md`` and ``Indices/Grafo.json``; return the MD."""
    with _LOCK:
        indices = ensure_vault() / "Indices"
        teia = indices / "Teia.md"
        grafo = indices / "Grafo.json"
        _atomic_write(teia, str(markdown or ""))
        _atomic_write(
            grafo,
            json.dumps(graph or {}, ensure_ascii=False, indent=2, sort_keys=True),
        )
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
