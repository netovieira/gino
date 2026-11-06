"""Grants persistentes por pessoa, dados por FRASE DO DONO (FLUXOS.md, ramificação *).

O contrato (skills/fluxos/FLUXOS.md) diz: "USUARIOS VISITANTES PODEM SER
ELEVADOS PARA PERMISSOES ESPECIFICAS OU GERAL", em frases como
"dê acesso total a Valmira" ou "pode responder e executar tudo que o usuário
Valmira pedir no projeto SITE2". Hoje só existem janelas temporárias de risco
(core/actiongate.py); nada persiste permissão por pessoa.

Invariantes deste módulo (a lei do portão):
  * CRIAR grant exige veredito privilegiado do dono — quem chama ``add_grant``
    precisa passar ``owner_ok=True``, e este módulo exige ``owner_ok is True``
    E ``confirmed_by`` em {voiceprint_owner, senha_digitada}. Nada mais cria.
  * O padrão é NEGAÇÃO: sem grant ativo para aquela pessoa + escopo nominal,
    ``allows`` devolve False. Voz desconhecida nunca tem pessoa, nunca tem grant.
  * Grant NUNCA toca o perfil de voz: escrita de voz, cadastro, substituição
    de amostras e autorização de voz (SpeakerId) estão fora do alcance de
    qualquer grant — ver ``PROFILE_WRITE_SCOPE`` recusado em ``add_grants``.
  * A identidade da pessoa é um RÓTULO NOMINAL (ex.: "valmira"), nunca um id
    de voz, embedding, telefone, e-mail ou CPF. Chamar ``allows`` com um dado
    desses é recusado por formato (``_valid_person``).
  * ``describe`` fala o rótulo e o escopo; nunca um segredo, nunca um id.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE = _ROOT / "memory" / "voice_grants.json"

# Formas aceitas para ``confirmed_by`` na criação de um grant.
CONFIRMED_BY_OWNER = frozenset({"voiceprint_owner", "owner_voiceprint",
                                "senha_digitada", "owner_password"})

# Escopo de escrita do perfil de voz — FORA do alcance de qualquer grant.
PROFILE_WRITE_SCOPE = "perfil_de_voz"

# Rótulos canônicos de escopo (nominais, faláveis, nunca dados pessoais).
SCOPE_PROJECT = "projeto"
SCOPE_EMAIL = "email"
SCOPE_WHATSAPP = "whatsapp"
SCOPE_READ = "leitura"
SCOPE_EXECUTE = "execucao"
SCOPE_ALL = "tudo"
_KNOWN_SCOPES = frozenset({SCOPE_PROJECT, SCOPE_EMAIL, SCOPE_WHATSAPP,
                           SCOPE_READ, SCOPE_EXECUTE, SCOPE_ALL})

# Duração padrão de um grant: 30 dias. Revogável por frase a qualquer hora.
DEFAULT_TTL_SECONDS = 30 * 24 * 3600.0
MAX_TTL_SECONDS = 365 * 24 * 3600.0

# Um rótulo de pessoa tem de ser nominal e curto. Números longos (telefone),
# e-mails, CPF e ids de voz são recusados aqui — a chave nunca é um dado.
_PERSON_MAX = 40
# 5 ou mais dígitos seguidos não são nome de pessoa (telefone/CPF/RG).
_DIGIT_RUN_RE = re.compile(r"\d{5,}")
_EMAILISH_RE = re.compile(r"[@#]\S")
_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,38}[a-z0-9])?")


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def person_key(label: Any) -> str:
    """Slug nominal de um rótulo de pessoa ('Valmira' -> 'valmira').

    Recusa (devolve '') rótulos vazios, com dado pessoal embutido (dígitos
    longos, '@') ou acima do comprimento de um nome. Retornar '' significa
    'isto não é uma pessoa' — o chamador deve negar, nunca adivinhar.
    """
    folded = _fold(label)
    if not folded or len(folded) > _PERSON_MAX:
        return ""
    if _DIGIT_RUN_RE.search(folded) or _EMAILISH_RE.search(folded):
        return ""
    slug = re.sub(r"[^a-z0-9_-]+", "-", folded).strip("-")
    return slug if _SLUG_RE.fullmatch(slug) else ""


def _valid_person(person: Any) -> bool:
    key = _fold(person)
    return bool(key) and _SLUG_RE.fullmatch(key) is not None


def _valid_scope(scope: Any) -> bool:
    return _fold(getattr(scope, "kind", scope) or "") in _KNOWN_SCOPES


class Scope:
    """Escopo nominal de um grant: categoria + alvo opcional ('site2')."""

    __slots__ = ("kind", "target")

    def __init__(self, kind: str, target: str = "") -> None:
        self.kind = _fold(kind)
        self.target = _fold(target)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target}

    def __repr__(self) -> str:
        return f"Scope({self.kind}:{self.target or '*'})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Scope):
            return (self.kind, self.target) == (other.kind, other.target)
        return NotImplemented


def _as_scope(value: Any) -> Scope:
    if isinstance(value, Scope):
        return value
    if isinstance(value, dict):
        return Scope(str(value.get("kind") or ""), str(value.get("target") or ""))
    return Scope(str(value or ""))


class GrantStore:
    """Armazena e consulta grants; a leitura NÃO concede sozinha nada."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else DEFAULT_STORE
        self._lock = threading.RLock()

    # ── persistência ───────────────────────────────────────────────────────
    def load(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return []
            grants = raw.get("grants") if isinstance(raw, dict) else None
            return [g for g in (grants or []) if isinstance(g, dict)]

    def _save(self, grants: list[dict[str, Any]]) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"grants": grants}, ensure_ascii=False,
                                           indent=1), encoding="utf-8")
                tmp.replace(self._path)
            except OSError:
                pass  # leitura continua funcionando; gravação é que falha

    # ── escrita (única porta: dono privilegiado) ────────────────────────────
    def add_grant(self, person: Any, scope: Any, *, owner_ok: bool,
                  confirmed_by: str = "", ttl_seconds: float = DEFAULT_TTL_SECONDS,
                  reason: str = "", now: float | None = None) -> dict[str, Any]:
        """Cria um grant. Sem ``owner_ok=True`` E ``confirmed_by`` de dono,
        a resposta é recusada — não existe outro caminho para esta função."""
        if not owner_ok:
            return {"granted": False, "why": "sem veredito privilegiado do dono"}
        if _fold(confirmed_by) not in CONFIRMED_BY_OWNER:
            return {"granted": False,
                    "why": "origem do privilégio não é voz do dono nem senha"}
        key = person_key(person)
        if not key:
            return {"granted": False, "why": "rótulo de pessoa inválido"}
        sc = _as_scope(scope)
        if _fold(sc.kind) == _fold(PROFILE_WRITE_SCOPE) or "perfil" in sc.kind \
                or "voz" in sc.kind:
            # (d) escrita de perfil de voz está FORA do alcance de qualquer
            # grant — só a senha digitada ou o voiceprint do dono no ato.
            return {"granted": False,
                    "why": "escrita de perfil de voz não é concedível"}
        if sc.kind not in _KNOWN_SCOPES:
            return {"granted": False, "why": f"escopo desconhecido: {sc.kind}"}
        if sc.kind == SCOPE_PROJECT and not sc.target:
            return {"granted": False, "why": "projeto sem nome"}
        ttl = max(5.0, min(MAX_TTL_SECONDS, float(ttl_seconds or 0.0)
                           or DEFAULT_TTL_SECONDS))
        moment = time.monotonic() if now is None else float(now)
        record = {"person": key, "label": str(person)[:_PERSON_MAX],
                  "scope": sc.to_dict(), "granted_at": moment,
                  "expires_at": moment + ttl,
                  "confirmed_by": _fold(confirmed_by),
                  "reason": str(reason or "")[:80]}
        with self._lock:
            grants = self.load()
            grants = [g for g in grants
                      if not (g.get("person") == key
                              and (g.get("scope") or {}).get("kind") == sc.kind
                              and (g.get("scope") or {}).get("target")
                              == sc.target)]
            grants.append(record)
            self._save(grants)
        return {"granted": True, "record": record}

    def add_grants(self, person: Any, scopes: list[Any], **kwargs) -> dict[str, Any]:
        out = [self.add_grant(person, scope, **kwargs) for scope in scopes]
        granted = [o for o in out if o.get("granted")]
        return {"granted": bool(granted), "granted_count": len(granted),
                "results": out,
                "why": "" if granted else "; ".join(
                    str(o.get("why") or "") for o in out if o.get("why"))}

    # ── consulta (o padrão é NEGAR) ─────────────────────────────────────────
    def active(self, person: Any, now: float | None = None) -> list[dict[str, Any]]:
        key = _fold(person)
        if not key:
            return []
        moment = time.monotonic() if now is None else float(now)
        return [g for g in self.load()
                if g.get("person") == key
                and float(g.get("expires_at") or 0.0) > moment]

    def allows(self, person: Any, scope: Any, *,
               now: float | None = None) -> bool:
        """True apenas com grant ATIVO que cubra o escopo pedido.

        Cobertura: kind igual com target igual ou grant sem target; e o
        super-escopo 'tudo' cobre as categorias de acesso ('email',
        'whatsapp', 'projeto'). 'leitura'/'execucao' NÃO são super-escopos:
        só cobrem a si mesmas (target '*' cobre o alvo).
        """
        if not _valid_person(person):
            return False
        if not _valid_scope(scope):
            return False
        want = _as_scope(scope)
        if want.kind == _fold(PROFILE_WRITE_SCOPE):
            return False
        moment = time.monotonic() if now is None else float(now)
        for g in self.active(person, now=moment):
            gs = _as_scope(g.get("scope"))
            if gs.kind == SCOPE_ALL and want.kind in (
                    SCOPE_PROJECT, SCOPE_EMAIL, SCOPE_WHATSAPP):
                return True
            if gs.kind != want.kind:
                continue
            if not gs.target or gs.target == want.target:
                return True
        return False

    def revoke(self, person: Any, scope: Any = None, *,
               now: float | None = None) -> int:
        """Revoga grants falados. scope None = todos da pessoa."""
        key = _fold(person)
        if not key:
            return 0
        want = None if scope is None or not str(scope) else _as_scope(scope)
        moment = time.monotonic() if now is None else float(now)
        with self._lock:
            grants = self.load()
            if want is None:
                keep = [g for g in grants
                        if g.get("person") != key
                        or float(g.get("expires_at") or 0.0) <= moment]
            else:
                keep = [g for g in grants
                        if not (g.get("person") == key
                                and _as_scope(g.get("scope")).kind == want.kind
                                and (not want.target
                                     or _as_scope(g.get("scope")).target
                                     == want.target))
                        or float(g.get("expires_at") or 0.0) <= moment]
            removed = len(grants) - len(keep)
            if removed:
                self._save(keep)
            return removed

    # ── fala/log: só rótulo e escopo nominal ────────────────────────────────
    def describe(self, person: Any, now: float | None = None) -> str:
        key = _fold(person)
        items = self.active(key, now=now)
        if not items:
            return ""
        parts: list[str] = []
        for g in items:
            sc = _as_scope(g.get("scope"))
            left = max(0.0, float(g.get("expires_at") or 0.0)
                       - (time.monotonic() if now is None else float(now)))
            label = str(g.get("label") or key)[:_PERSON_MAX]
            days = int(left // 86400)
            parts.append(f"{label}: {sc.kind}"
                         + (f"/{sc.target}" if sc.target else "")
                         + (f" ({days} dias)" if days else ""))
        return "; ".join(parts)

    def clear(self) -> None:
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass


_store: GrantStore | None = None
_store_lock = threading.Lock()


def default_store() -> GrantStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = GrantStore()
        return _store


def set_store(store: GrantStore | None) -> None:
    """Injeção para testes; None volta ao padrão do processo."""
    global _store
    with _store_lock:
        _store = store


def allows(person: Any, scope: Any, *, now: float | None = None) -> bool:
    return default_store().allows(person, scope, now=now)


# ── parser da frase de elevação ─────────────────────────────────────────────
# Formas do contrato: "dê acesso total a VALMIRA", "pode responder e executar
# tudo que o usuário VALMIRA pedir no projeto SITE2". O nome é capturado até
# o fim da frase; nada aqui conhece ou grava dados pessoais — é rótulo.
_FULL_ACCESS_RE = re.compile(
    r"(?:d[êe]|da(?:\-me)?|conced[ea]|autorize|libere)\s+(?:a\s+)?"
    r"(?:acesso|permiss[ãa]o)\s+(?:total|geral|plena)"
    r"\s+(?:a|para|pra|ao)\s+(?P<pessoa>[\w\u00c0-\u017f][\w \u00c0-\u017f-]{1,40})",
    re.IGNORECASE)
_SCOPED_GRANT_RE = re.compile(
    r"(?:pode|p[oe]de|poder[âa]?)\s+(?:responder\s+e\s+executar|fazer|executar"
    r"|responder|usar|acessar)\s+tudo\s+que\s+(?:o|a)?\s*"
    r"(?:usu[áa]rio|usu[áa]ria|pessoa)?\s*(?P<pessoa>[\w\u00c0-\u017f][\w \u00c0-\u017f-]{1,40}?)"
    r"\s+(?:pedir|pedir(?:em)?|solicitar|quiser)(?:\s+(?:no|na|para|pra|em)\s+"
    r"(?:o\s+)?(?:projeto\s+)?(?P<projeto>[\w\u00c0-\u017f][\w \u00c0-\u017f.-]{1,40}))?",
    re.IGNORECASE)
_REVOKE_RE = re.compile(
    r"(?:revog[ae]|cancel[ae]|retir[ae]|tire(?:\s+(?:o|a)\s+(?:acesso|permiss[ãa]o))?)"
    r"\s+(?:o\s+)?(?:acesso|permiss[ãa]o|grant)?\s*(?:de|do|da|para|pra|da\s)?"
    r"(?P<pessoa>[\w\u00c0-\u017f][\w \u00c0-\u017f-]{1,40})",
    re.IGNORECASE)
_REVOKE_ALL_RE = re.compile(
    r"(?:revog[ae]|cancel[ae]|retir[ae])\s+todos\s+os\s+acessos", re.IGNORECASE)

_STOPWORDS = {"usuario", "usuaria", "pessoa", "o", "a", "de", "do", "da",
              "projeto", "acesso", "total", "que", "pedir", "solicitar"}


def _clean_person(raw: str) -> str:
    words = [w for w in _fold(raw).split()
             if w and w not in _STOPWORDS]
    return " ".join(words).strip()


def parse_grant_phrase(text: str) -> dict[str, Any] | None:
    """Nada reconhecível vira None; a frase comum segue para o cérebro.

    Retorna {"action": "grant_full"|"grant_scoped", "person": <rótulo>,
    "scopes": [Scope, ...]} ou None.
    """
    line = str(text or "").strip()
    if not line:
        return None
    m = _FULL_ACCESS_RE.search(line)
    if m:
        person = _clean_person(m.group("pessoa"))
        key = person_key(person)
        if key:
            return {"action": "grant_full", "person": key, "label": person,
                    "scopes": [Scope(SCOPE_ALL)]}
    m = _SCOPED_GRANT_RE.search(line)
    if m:
        person = _clean_person(m.group("pessoa"))
        key = person_key(person)
        if key:
            projeto = _fold(m.group("projeto") or "")
            if projeto.startswith("projeto "):
                projeto = projeto[len("projeto "):]
            if projeto and projeto in _STOPWORDS:
                projeto = ""
            scopes = [Scope(SCOPE_READ), Scope(SCOPE_EXECUTE)]
            if projeto:
                scopes.append(Scope(SCOPE_PROJECT, projeto))
            return {"action": "grant_scoped", "person": key, "label": person,
                    "scopes": scopes, "project": projeto}
        return None
    m = _REVOKE_ALL_RE.search(line)
    if m:
        return {"action": "revoke_all", "person": "", "label": "",
                "scopes": []}
    m = _REVOKE_RE.search(line)
    if m:
        person = _clean_person(m.group("pessoa"))
        key = person_key(person)
        # "cancela" sozinho é verbo de tarefa (actiongate/rootgate), não
        # revogação de acesso: exige um rótulo de pessoa capturado.
        if key:
            return {"action": "revoke", "person": key, "label": person,
                    "scopes": []}
    return None


def grant_from_phrase(text: str, *, owner_ok: bool, confirmed_by: str,
                      store: GrantStore | None = None,
                      now: float | None = None,
                      ttl_seconds: float = DEFAULT_TTL_SECONDS) -> dict[str, Any] | None:
    """Parser + porta dura. None = a frase não é de elevação (segue normal).

    Toda criação passa por ``GrantStore.add_grant``, que exige owner_ok e
    confirmed_by de dono. 'sim' falado sem dono privilegiado cai no 'why'.
    """
    plan = parse_grant_phrase(text)
    if plan is None:
        return None
    st = store or default_store()
    if plan["action"] == "revoke_all":
        removed = 0
        for g in st.load():
            removed += st.revoke(g.get("person"), now=now) or 0
        return {"handled": True, "action": "revoke_all", "removed": removed,
                "granted": False}
    if plan["action"] == "revoke":
        removed = st.revoke(plan["person"], now=now)
        return {"handled": True, "action": "revoke", "person": plan["person"],
                "label": plan["label"], "removed": removed, "granted": False}
    result = st.add_grants(plan["person"], plan["scopes"], owner_ok=owner_ok,
                           confirmed_by=confirmed_by, now=now,
                           reason="frase do dono", ttl_seconds=ttl_seconds)
    result["handled"] = True
    result["action"] = plan["action"]
    result["person"] = plan["person"]
    result["label"] = plan["label"]
    return result


__all__ = ["GrantStore", "Scope", "SCOPE_ALL", "SCOPE_EMAIL", "SCOPE_EXECUTE",
           "SCOPE_PROJECT", "SCOPE_READ", "SCOPE_WHATSAPP",
           "PROFILE_WRITE_SCOPE", "DEFAULT_STORE", "DEFAULT_TTL_SECONDS",
           "allows", "add_grant", "grant_from_phrase", "parse_grant_phrase",
           "person_key", "default_store", "set_store"]


def add_grant(person: Any, scope: Any, **kwargs) -> dict[str, Any]:
    return default_store().add_grant(person, scope, **kwargs)
