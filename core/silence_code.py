"""Palavra-código de silêncio — UC-4 (ordem do dono, 2026-11-02 19:26-19:31).

O que faz
    Palavra-código (ex.: "Oklahoma") + nome de projeto silencia a boca para
    aquele projeto. O microfone continua aberto, a memória continua gravando
    e o que a boca diria é retido em fila. Ao liberar ("libera Oklahoma"),
    a boca entrega um resumo de tudo que ficou calado.

Diferença para SilenceGate (core/silence_gate.py)
    SilenceGate: palavra fixa "silêncio", alternância (toggle), descarta
    a fila ao liberar sem produzir resumo falado.
    SilenceCode: palavra configurável, ativa/desativa por projeto, retém
    e entrega resumo falado de tudo que ficou calado.

    Ambos usam a MESMA fila de retenção (memory/silence_queue.json) para
    que nada se perca: quando SilenceCode está ativo, o retain() grava
    na fila do gate; quando o gate está ativo, o retain() dele continua
    valendo. Os dois nunca silenciam a mesma fala duas vezes.

Estado persistido (memory/silence_code.json)
    {
      "version": 1,
      "code_word": "Oklahoma",
      "active_projects": [{"slug": "therotv", "label": "TheroTV"}],
      "started_at": "2026-11-06T18:00:00",
      "updated_at": "2026-11-06T18:00:00"
    }

Lei (c) do contrato (FLUXOS.md): só a voz do dono autoriza ativação/liberação.
O main.py trata disso; este módulo não faz verificação de voz — expõe o hook.

A REGRA ÚNICA DE RECONHECIMENTO (dono, 09/11/2026 16:40: "a palavra é
silêncio") vive em ``recognize()`` abaixo, e é uma só para as duas bocas:

    fala == palavra, isolada (com vocativo, caixa, acento ou pontuação)
        -> "global"   : o modo calado de tudo
    palavra + nome de projeto
        -> "escopado" : só aquele projeto fica mudo; os outros falam
    "libera" + palavra  [+ nome]
        -> "liberar_tudo" | "liberar"
    qualquer outra fala
        -> "none"     : conversa, não ordem

Nada aqui é "começa com". ``silêncio caiu na conversa`` ou ``o silêncio
é um hábito`` têm verbo e função, não têm projeto, e por isso nunca viram
comando: ``_NOME_PROIBIDO`` e o teto de ``_MAX_NOME_TOKENS`` são o que
separa um nome de uma frase.

    Choque das duas palavras (medido, 09/11/2026): quando a palavra-código é
    a MESMA palavra do gate, o ``main.py`` tratava «silêncio Nome» pelo
    SilenceCode antes do SilenceGate, e o escopado ia para um segundo estado.
    ``is_silence_word()`` expõe a colisão para o main.py decidir uma só vez
    pelo gate; aqui a palavra continua configurável e vale para qualquer outra.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from core.memory_index import slugify

BASE = Path(__file__).resolve().parents[1] / "memory"
DEFAULT_STATE_PATH = BASE / "silence_code.json"
DEFAULT_MAX_RETAINED = 40
_MAX_SUMMARY_SUBJECTS = 8
_MAX_SNIPPET = 160

# A palavra do dono para o modo calado (core/silence_gate.py a conhece fixa).
SILENCE_WORD = "silêncio"
# Verbo da liberação, a única gramática do SilenceCode que o gate não tem.
# É o verbo que o dono usa («libera Oklahoma»); nada além dele libera.
LIBERAR_VERBO = "libera"

# Um nome de projeto é curto e não é frase. Estas palavras são função,
# pronome, preposição ou verbo: com qualquer uma nelas o que o microfone
# ouviu é fala corrida — e fala corrida nunca vira comando. Tudo já em
# ``_fold`` (minúsculo, sem acento), porque é assim que se compara.
_NOME_PROIBIDO = frozenset({
    # artigo, pronome, preposição, conjunção
    "o", "a", "os", "as", "um", "uma", "uns", "umas", "e", "ou", "mas",
    "de", "da", "do", "dos", "das", "no", "na", "nos", "nas", "em", "com",
    "sem", "sob", "sobre", "para", "pro", "pra", "ao", "aos", "que", "quem",
    "como", "quando", "onde", "por", "se", "sim", "nao", "eu", "me", "te",
    "lhe", "voce", "ele", "ela", "eles", "elas", "isso", "isto", "aquele",
    "aquela", "este", "esta", "esse", "essa", "todo", "toda", "todos",
    "todas", "mundo", "gente",
    # verbo / estado — o buraco medido: «silêncio caiu na conversa»
    "ser", "sao", "esta", "estou", "estamos", "estiver", "ta", "to", "tamo",
    "ficou", "fica", "fique", "cai", "caiu", "caia", "veio", "vem", "vai",
    "vao", "vou", "tem", "tenho", "foi", "fora", "ha", "quero", "quer",
    "pode", "posso", "deixa", "deixe", "pare", "cala", "calar", "fala",
    "fale", "fico", "faz", "fez", "dar", "dando", "tendo", "sendo",
    # cortesia e tempo, que o gate já descarta como resto
    "favor", "pfv", "agora", "ja", "entao", "ok", "okay", "certo",
    "beleza", "obrigado", "obrigada", "valeu", "bom", "boa", "oi", "ola",
})
# Palavras que querem dizer "tudo", e não "projeto".
_GLOBAL_WORDS = frozenset({"total", "tudo", "geral", "geralzao", "sempre",
                           "o mundo", "todo mundo", "tudo junto"})
# Mais que isto já é frase, não nome.
_MAX_NOME_TOKENS = 4


def _fold(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "")).lower()
    return "".join(c for c in raw if not unicodedata.combining(c))


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


# ── reconhecimento: a regra é uma só ─────────────────────────────────────────

def is_silence_word(word: Any) -> bool:
    """A palavra-código do dono É a própria palavra do silêncio?

    ``memory/silence_code.json`` hoje grava exatamente isso
    (dono, 09/11/2026 16:40: "a palavra é silêncio"). Quando é a mesma
    palavra, o ``main.py`` decide uma vez só pelo SilenceGate — o estado do
    escopado passa a ser um só, e o SilenceCode empresta apenas a liberação,
    que é a única gramática que o gate não tem. Nunca lança.
    """
    return _fold(word).strip() == _fold(SILENCE_WORD).strip()


_BORDA = " \t,;:.!?-–—\"'«»()[]{}"
_VOCATIVO = r"^{nome}\s*[,:!.;-]?\s*"
_CORTESIA_INI_RE = re.compile(
    r"^(?:por\s+favor|pfv|agora|j[aá]|ei|hei)[\s,;:.-]+", re.IGNORECASE)
_CORTESIA_FIM_RE = re.compile(
    r"[\s,;:.!?-]+(?:por\s+favor|pfv|agora|j[aá]|ok|okay|certo|beleza|"
    r"obrigado|obrigada|valeu)$", re.IGNORECASE)
# "o projeto X", "na TheroTV": envolucro de conversa em volta do nome.
_ENVOLUCRO_RES = (
    re.compile(r"^(?:(?:o|a|os|as)\s+)?(?:projeto|assunto|tema|quest[aã]o)\s+",
               re.IGNORECASE),
    re.compile(r"^(?:no|na|nos|nas|do|da|dos|das|de|sobre|para|pro|pra)\s+",
               re.IGNORECASE),
)


def _sem_borda(token: str) -> str:
    """Um token da fala sem a pontuação que a transcrição cola nas bordas."""
    return str(token or "").strip(_BORDA)


def _sem_vocativo(texto: str, addresses: Iterable[str]) -> str:
    """Tira o chamado do assistente no começo ("Gino, silêncio") — o gate já aceita."""
    nomes = [str(a).strip() for a in addresses if str(a or "").strip()]
    nomes += ["gino", "jarvis"]
    sobra = str(texto or "").strip()
    for nome in sorted({n for n in nomes if n}, key=len, reverse=True):
        novo = re.sub(_VOCATIVO.format(nome=re.escape(nome)), "",
                      sobra, count=1, flags=re.IGNORECASE).strip()
        if novo != sobra:
            return novo
    return sobra


def _sem_cortesia(texto: str) -> str:
    sobra = " ".join(str(texto or "").split()).strip().strip(_BORDA)
    anterior = None
    while anterior != sobra:
        anterior = sobra
        sobra = _CORTESIA_INI_RE.sub("", sobra, count=1).strip().strip(_BORDA)
    return _CORTESIA_FIM_RE.sub("", sobra, count=1).strip().strip(_BORDA)


def _nome_de_projeto(resto: str) -> str | None:
    """O que veio depois da palavra: nome, "tudo", ou frase.

    ``str`` vazio  -> não há nome: é o modo calado GERAL.
    ``str`` preenchido -> o nome do projeto (escopado).
    ``None``        -> fala corrida: não é ordem. É esta linha que impede
    a palavra de engolir «silêncio caiu na conversa».
    """
    sobra = _sem_cortesia(resto)
    if not sobra or _fold(sobra) in _GLOBAL_WORDS:
        return ""
    anterior = None
    while anterior != sobra:
        anterior = sobra
        for padrao in _ENVOLUCRO_RES:
            novo = padrao.sub("", sobra, count=1)
            if novo != sobra:
                sobra = " ".join(novo.split()).strip().strip(_BORDA)
    if not sobra or _fold(sobra) in _GLOBAL_WORDS:
        return ""
    tokens = [t for t in sobra.split() if t]
    if not tokens or len(tokens) > _MAX_NOME_TOKENS:
        return None
    dobrado = [_fold(t) for t in tokens]
    if any(t in _NOME_PROIBIDO or t in _GLOBAL_WORDS for t in dobrado):
        # Só cortesia/tempo («silêncio aqui», «silêncio é bom»): não há nome
        # na fala — o dono quer a sala, não um projeto.
        if all(t in _NOME_PROIBIDO for t in dobrado):
            return ""
        # Com verbo ou função MISTURADA a um substantivo («caiu na
        # conversa»): é frase, não ordem.
        return None
    if not any(ch.isalnum() for ch in _fold(sobra)):
        return None
    return " ".join(tokens)[:60]


def recognize(text: str, word: str, *,
              addresses: Iterable[str] = ()) -> dict:
    """A ÚNICA regra de reconhecimento das ordens de silêncio.

    ``{"kind": "global"|"escopado"|"liberar_tudo"|"liberar"|"none",
    "project": str, "slug": str, "word": str}``. Nunca lança.

    global      : a fala INTEIRA é a palavra, isolada (vocativo, caixa,
                  acento, vírgula ou ponto não mudam isso), ou a palavra
                  seguida de "total/tudo/geral".
    escopado    : a palavra + o NOME de um projeto — e só o nome, no máximo
                  ``_MAX_NOME_TOKENS`` tokens sem verbo, sem pronome, sem
                  preposição no meio.
    liberar*    : "libera" antes da palavra; sem nome = tudo, com nome = um.
    none        : qualquer outra fala. Não existe "começa com": sobra que não
                  é nome devolve none e a frase segue para o cérebro.
    """
    raw = " ".join(str(text or "").split()).strip()
    out: dict[str, Any] = {"kind": "none", "project": "", "slug": "",
                           "word": str(word or "")}
    if not raw or "?" in raw or not str(word or "").strip():
        return out
    corpo = _sem_cortesia(_sem_vocativo(raw, addresses))
    liberando = False
    padrao_liberar = rf"^{re.escape(LIBERAR_VERBO)}\b[\s,;:.]*"
    if re.match(padrao_liberar, corpo, re.IGNORECASE):
        liberando = True
        corpo = _sem_cortesia(re.sub(padrao_liberar, "", corpo, count=1,
                                     flags=re.IGNORECASE))
    corpo = _sem_cortesia(_sem_vocativo(corpo, addresses))
    cw_tokens = [_sem_borda(t) for t in str(word).split() if t]
    n = len(cw_tokens)
    tokens = [_sem_borda(t) for t in corpo.split()]
    tokens = [t for t in tokens if t]
    if n == 0 or len(tokens) < n:
        return out
    # Correspondência EXATA: os primeiros tokens têm de ser a palavra, e só
    # ela — nada de "começa com" colando a palavra em outra fala.
    if _fold(" ".join(tokens[:n])) != _fold(" ".join(cw_tokens)):
        return out
    nome = _nome_de_projeto(" ".join(tokens[n:]))
    if nome is None:
        return out
    if not nome:
        out["kind"] = "liberar_tudo" if liberando else "global"
        return out
    out.update({"kind": "liberar" if liberando else "escopado",
                "project": nome, "slug": slugify(nome, "")})
    return out


def parse_silence_code_order(
    text: str,
    code_word: str,
) -> dict:
    """Lê a transcrição e decide se ativa ou libera o silêncio por palavra-código.

    Retorna ``{"action": "activate"|"deactivate"|"deactivate_all"|"none",
    "project": str, "slug": str}``. Nunca lança.

    Finino sobre ``recognize()`` — a regra de reconhecimento é uma só para a
    palavra-código e para a palavra do silêncio. A palavra vale em minúsculas
    sem acentos; sem nome depois dela a ordem é GLOBAL (silencia tudo); com
    nome, é aquele projeto; "libera ..." solta um projeto ou todos.
    """
    decision = recognize(text, code_word)
    out: dict[str, str] = {"action": "none", "project": "", "slug": ""}
    kind = str(decision.get("kind") or "none")
    project = str(decision.get("project") or "")
    slug = str(decision.get("slug") or "")
    if kind in ("global", "escopado"):
        out.update({"action": "activate", "project": project, "slug": slug})
    elif kind == "liberar":
        out.update({"action": "deactivate", "project": project, "slug": slug})
    elif kind == "liberar_tudo":
        out.update({"action": "deactivate_all", "project": "", "slug": ""})
    return out


# ── resumo ───────────────────────────────────────────────────────────────────

def _first_sentence(text: str) -> str:
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    m = re.match(r"(.+?[.!?…])(?:\s|$)", raw)
    snippet = m.group(1) if m else raw
    if len(snippet) > _MAX_SNIPPET:
        snippet = snippet[:_MAX_SNIPPET].rstrip() + "…"
    return snippet


def summarize(entries: list[dict]) -> list[dict]:
    """Agrupa linhas retidas por assunto: contagem + última frase.

    Retorna uma lista de grupos ``{"subject", "count", "latest"}``.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for entry in entries or []:
        slug = str(entry.get("slug") or "")
        key = slug or "-"
        g = groups.get(key)
        if g is None:
            g = {
                "slug": slug,
                "subject": str(
                    entry.get("subject") or entry.get("label")
                    or "sem assunto"),
                "count": 0,
                "latest": "",
            }
            groups[key] = g
            order.append(key)
        g["count"] += 1
        g["latest"] = str(entry.get("text") or g["latest"])
    return [groups[k] for k in order]


def summary_lines(entries: list[dict], dropped: int = 0,
                  max_subjects: int = _MAX_SUMMARY_SUBJECTS) -> list[str]:
    """Linhas curtas de resumo para a boca falar na liberação."""
    groups = summarize(entries)
    lines: list[str] = []
    for g in groups[:max(1, int(max_subjects))]:
        count = int(g.get("count") or 0)
        plural = "s" if count > 1 else ""
        line = f"Sobre {g['subject']}: {count} fala{plural} guardada{plural}."
        latest = _first_sentence(g.get("latest") or "")
        if latest:
            line += f" A última: {latest}"
        lines.append(line)
    extra = len(groups) - max(1, int(max_subjects))
    if extra > 0:
        lines.append(
            f"Mais {extra} assunto{'s' if extra > 1 else ''} "
            "também teve fala guardada.")
    if int(dropped or 0) > 0:
        lines.append(
            f"Descartei {int(dropped)} fala"
            f"{'s' if int(dropped) > 1 else ''} antiga"
            f"{'s' if int(dropped) > 1 else ''} por limite de espaço.")
    return lines


# ── módulo principal ─────────────────────────────────────────────────────────

class SilenceCode:
    """Palavra-código de silêncio com retenção e resumo.

    ``code_word``  : a palavra definida pelo dono (ex.: "Oklahoma").
    ``on_retain``  : callback para reter fala no SilenceGate (fila compartilhada).
    ``logger``     : console/output; nunca obrigatório.

    Estado ativo (memória + disco):
        ``active_projects`` : lista de {"slug": str, "label": str}.
    """

    def __init__(
        self,
        code_word: str = "",
        *,
        state_path: str | Path | None = None,
        on_retain: Callable[[str, str, str], None] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._code_word: str = code_word
        self._state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._on_retain = on_retain or (lambda t, s, sl: None)
        self._log_fn = logger or (lambda _m: None)
        self._active_projects: list[dict] = []
        self._started_at: str = ""
        self._dropped: int = 0
        self._local_entries: list[dict] = []  # when no on_retain wired
        self._load()

    # ── propriedade ─────────────────────────────────────────────────────────

    @property
    def code_word(self) -> str:
        with self._lock:
            return self._code_word

    @code_word.setter
    def code_word(self, value: str) -> None:
        with self._lock:
            self._code_word = " ".join(str(value or "").split())[:40]

    @property
    def active(self) -> bool:
        """True quando pelo menos um projeto está sob palavra-código."""
        with self._lock:
            return bool(self._active_projects)

    # ── consultas ───────────────────────────────────────────────────────────

    def is_active_for(self, project: str = "", text: str = "") -> bool:
        """A boca deve ficar calada para este projeto/texto agora?

        Retorna True se o projeto está na lista de ativos. Se ``project`` vazio,
        tenta inferir de ``text`` pelos slugs ativos.
        """
        with self._lock:
            if not self._active_projects:
                return False
            subj_slug = slugify(str(project or ""), "") if project else ""
            for item in self._active_projects:
                if subj_slug and subj_slug == item["slug"]:
                    return True
                if text and item["slug"] and item["slug"] in _fold(text):
                    return True
            # Se "todos" está ativo (slug vazio), qualquer fala é retida.
            for item in self._active_projects:
                if not item["slug"]:
                    return True
            return False

    def active_projects(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._active_projects]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "code_word": self._code_word,
                "active_projects": [dict(i) for i in self._active_projects],
                "started_at": self._started_at,
            }

    # ── ativação / desativação ───────────────────────────────────────────────

    def activate(self, project: str = "") -> dict:
        """Silencia a boca para o projeto. Retorna o registro do projeto.

        ``project`` vazio = silencia tudo (slug "" = projeto global).
        """
        label = " ".join(str(project or "").split())[:60]
        slug = slugify(label, "") if label else ""
        with self._lock:
            if slug and any(i["slug"] == slug for i in self._active_projects):
                # já ativo: idempotente
                return {"action": "already_active", "project": label, "slug": slug}
            if not slug and any(not i["slug"] for i in self._active_projects):
                return {"action": "already_active", "project": "tudo", "slug": ""}
            if not self._active_projects:
                self._started_at = _now_iso()
            item = {"slug": slug, "label": label or "tudo"}
            self._active_projects.append(item)
            self._save_locked()
        self._log(f"[PalavraCodigo] silêncio ativado: {label or 'tudo'}")
        return {"action": "activate", "project": label, "slug": slug}

    def deactivate(self, project_slug: str | None = None) -> dict:
        """Libera o silêncio (um projeto, ou todos se slug=None).

        Retorna {"action": ..., "entries": [...], "dropped": int,
        "summary_lines": [str]}.  A fila local é zerada ao liberar.
        """
        with self._lock:
            if project_slug is None or project_slug == "":
                # libera tudo
                self._active_projects = []
                action = "deactivate_all"
            else:
                before = len(self._active_projects)
                self._active_projects = [
                    i for i in self._active_projects if i["slug"] != project_slug
                ]
                action = ("deactivate"
                          if len(self._active_projects) < before
                          else "not_active")
            entries = list(self._local_entries)
            self._local_entries = []
            dropped = self._dropped
            self._dropped = 0
            if not self._active_projects:
                self._started_at = ""
            self._save_locked()
        lines = summary_lines(entries, dropped)
        self._log(f"[PalavraCodigo] silêncio liberado ({action}): "
                  f"{len(entries)} linhas retidas")
        return {"action": action, "entries": entries,
                "dropped": dropped, "summary_lines": lines}

    # ── retenção ─────────────────────────────────────────────────────────────

    def retain(self, text: str, *, subject: str = "",
               slug: str = "") -> dict:
        """Retém o que a boca diria. Delega ao on_retain se configurado.

        Retorna o registro da entrada local (com id e timestamp).
        """
        text_clean = str(text or "").strip()
        if not text_clean:
            return {}
        subj = " ".join(str(subject or "").split())[:60]
        entry_slug = slug or (slugify(subj, "") if subj else "")
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "text": text_clean,
            "subject": subj or "sem assunto",
            "slug": entry_slug,
            "at": _now_iso(),
            "origin": "silence_code",
        }
        with self._lock:
            self._local_entries.append(entry)
            while len(self._local_entries) > DEFAULT_MAX_RETAINED:
                self._local_entries.pop(0)
                self._dropped += 1
        # Delega ao callback compartilhado (fila do SilenceGate)
        try:
            self._on_retain(text_clean, subj, entry_slug)
        except Exception as exc:
            self._log(f"[PalavraCodigo] on_retain falhou: {exc}")
        return dict(entry)

    def pending_entries(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._local_entries]

    # ── processar fala do dono (ativação/liberação por transcrição) ──────────

    def handle_transcription(self, text: str) -> dict | None:
        """Detecta ordem de palavra-código na transcrição e age.

        Retorna o resultado de activate/deactivate, ou None se a fala
        não é ordem de palavra-código.
        """
        if not self._code_word:
            return None
        order = parse_silence_code_order(text, self._code_word)
        action = order.get("action", "none")
        if action == "none":
            return None
        if action == "activate":
            return self.activate(order.get("project", ""))
        if action == "deactivate":
            return self.deactivate(order.get("slug", ""))
        if action == "deactivate_all":
            return self.deactivate(None)
        return None

    # ── persistência ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        with self._lock:
            cw = str(data.get("code_word") or "")
            if cw:
                self._code_word = cw
            items = data.get("active_projects") or []
            self._active_projects = [
                {"slug": str(i.get("slug") or ""),
                 "label": str(i.get("label") or i.get("slug") or "tudo")}
                for i in items if isinstance(i, dict)
            ]
            self._started_at = str(data.get("started_at") or "")

    def _save_locked(self) -> None:
        payload = json.dumps({
            "version": 1,
            "code_word": self._code_word,
            "active_projects": list(self._active_projects),
            "started_at": self._started_at,
            "updated_at": _now_iso(),
        }, ensure_ascii=False)
        try:
            _atomic_write(self._state_path, payload)
        except Exception as exc:
            self._log(f"[PalavraCodigo] nao consegui salvar estado: {exc}")

    def _log(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            pass
