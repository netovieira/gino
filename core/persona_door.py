"""A PORTA do número de uma persona (regra do dono, 10/11/2026).

A palavra dele: "se eu receber uma mensagem no seu número de WhatsApp de um
cliente, por exemplo, que é o meu WhatsApp de trabalho, você não pode responder
esse cliente; mas se eu for direto no seu chat e mandar, você entende que tem
permissão para responder".

O que isto codifica: no número registrado de uma persona a porta é o NÚMERO, e
só entra por ela quem o criador deixa entrar — o próprio criador, ou uma pessoa
que ele tenha permitido PARA AQUELE número. Terceiro não recebe resposta
nenhuma, e a recusa deixa trilha (decisão, motivo, quem, conta). Antes desta
porta uma mensagem individual que caísse na conta atribuída a uma persona era
atendida sem que ninguém perguntasse QUEM escreveu: o fator ``conta_atribuida``
olhava o número de destino e ignorava o autor.

Nada aqui decide conteúdo, modelo nem resposta: é só a porta. O veredito é
positivo para o criador e para o permitido daquele número; negativo para o
restante — e negativo também quando a identidade não existe (mensagem sem
autor reconhecível é terceiro).

Reuso de mecanismos (lei dele: "se um gate pode, você pode"):
* "quem é o criador" é o ``core.group_policy.is_owner`` — a mesma definição de
  número do dono que a allow-list de grupos usa, lida de
  ``core.whatsapp.owner_numbers``; não existe segunda lista;
* "quem escreveu" é o ``core.group_policy.sender_jid`` — autor em grupo, chat
  em individual, dono no eco ``from_me``;
* "quem pode entrar na lista" passa pelo ``core.owner_auth.authorized_factor``,
  o ÚNICO ponto do "o dono autorizou": a voz dele e a senha dele valem pelo
  mesmo caminho, como ele mandou.

O arquivo (``config/persona_door.json``) é lido a cada chamada e começa VAZIO:
vazio é "só o criador", não é "ninguém" e nunca foi preenchido por mim — quem
abre a porta para alguém é ele. Arquivo corrompido fecha a porta para terceiros
(falha fechada) e mantém o criador dentro: a identidade do criador não vem do
arquivo, vem do número pareado, e silenciar o dono por causa de um JSON quebrado
não é o que a regra dele diz.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent.parent

#: Arquivo da porta (config versionado: é política, não segredo).
DEFAULT_DOOR_PATH = BASE_DIR / "config" / "persona_door.json"

#: Quem pôs o criador dentro foi a ordem dele, não uma autorização registrada.
AUTORIZADO_POR_CRIADOR = "criador do numero"

_LOCK = threading.Lock()
_PATH: Path | None = None
_LOG: Callable[[str], None] = lambda _m: None


def configure(*, path: "str | os.PathLike[str] | None" = None,
              log: Callable[[str], None] | None = None) -> None:
    """Redireciona arquivo e canal de log (testes/vivo)."""
    global _PATH, _LOG
    with _LOCK:
        if path is not None:
            _PATH = Path(path)
        if log is not None:
            _LOG = log


def door_path() -> Path:
    return _PATH or DEFAULT_DOOR_PATH


def _log(message: str) -> None:
    try:
        _LOG(message)
    except Exception:
        pass


def _key(account: Any) -> str:
    """A conta como chave estável: o rótulo canônico do cadastro."""
    return str(account or "").strip().casefold()


def _same_jid(left: Any, right: Any) -> bool:
    """Mesma pessoa escrita de dois jeitos (a normalização da allow-list)."""
    try:
        from core import group_policy
        a = group_policy.normalize_jid(left)
        b = group_policy.normalize_jid(right)
    except Exception:
        a = "".join(c for c in str(left or "") if c.isdigit())
        b = "".join(c for c in str(right or "") if c.isdigit())
    return bool(a and b and a == b)


def sender_of(item: Any) -> str:
    """Quem escreveu esta mensagem ('' quando a casa não sabe)."""
    try:
        from core import group_policy
        return group_policy.sender_jid(item)
    except Exception:
        return ""


def is_creator(sender: Any) -> bool:
    """O autor é o criador? Só o número responde — nunca o texto da mensagem."""
    got = str(sender or "")
    if not got:
        return False
    try:
        from core import group_policy
        return group_policy.is_owner(got)
    except Exception:
        return False


# ── o arquivo ───────────────────────────────────────────────────────────────

def _empty_doc() -> dict[str, Any]:
    return {"version": 1,
            "regra": ("porta do numero de cada persona: so o criador, ou quem o "
                      "criador permitir para AQUELE numero (ordem do dono "
                      "10/11/2026)"),
            "atualizado_em": "",
            "numeros": {}}


def _read_raw(path: Path) -> tuple[Any, str]:
    """O arquivo como está no disco: ``(objeto|None, "missing"|"corrupt"|"ok")``.

    ``missing`` é a instalação nova: porta vazia, só o criador — a ausência NÃO
    é falha, é o default que o dono pediu. ``corrupt`` fecha a porta para
    terceiros e nunca é reescrito por cima (política meio escrita dele não é
    minha para adivinhar).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError):
        return None, "corrupt"
    try:
        raw = json.loads(text)
    except ValueError:
        return None, "corrupt"
    if not isinstance(raw, dict) or not isinstance(raw.get("numeros"), dict):
        return None, "corrupt"
    return raw, "ok"


def load() -> dict[str, Any]:
    """A política da porta, lida AGORA (mudar o arquivo vale sem reiniciar)."""
    raw, status = _read_raw(door_path())
    if status == "ok" and raw is not None:
        numeros = {str(k): (v if isinstance(v, dict) else {})
                   for k, v in raw["numeros"].items()}
        raw["numeros"] = numeros
        return raw
    if status == "corrupt":
        _log("[porta] config da porta ilegivel ou corrompida: no numero de "
             "persona so o criador fala ate ela voltar (falha fechada).")
        return {**_empty_doc(), "numeros": {},
                "falha": "arquivo da porta corrompido ou ilegivel"}
    return _empty_doc()


def _write(doc: dict[str, Any]) -> bool:
    path = door_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        return True
    except OSError as exc:
        _log(f"[porta] arquivo da porta nao gravado: {type(exc).__name__}: {exc}")
        return False


def entries(account: Any) -> list[dict[str, Any]]:
    """Os permitidos daquele número (lista vazia = só o criador)."""
    doc = load()
    block = (doc.get("numeros") or {}).get(_key(account)) or {}
    got = block.get("permitidos")
    if not isinstance(got, list):
        return []
    return [e for e in got if isinstance(e, dict)]


def is_permitted(account: Any, sender: Any) -> bool:
    """O autor está na lista DESSE número? Outro número não empresta permissão."""
    if not str(sender or ""):
        return False
    return any(_same_jid(entry.get("jid"), sender) for entry in entries(account))


def permitted_by(account: Any, sender: Any) -> dict[str, Any] | None:
    """A entrada que autoriza o autor (para a trilha dizer quem permitiu)."""
    for entry in entries(account):
        if _same_jid(entry.get("jid"), sender):
            return entry
    return None


# ── caneta do criador ───────────────────────────────────────────────────────

def authorize(account: Any, jid: Any, nome: str = "", *,
              persona: Any = "", voice_ok: bool = False, password: str = "",
              now: dt.datetime | None = None) -> dict[str, Any]:
    """Permite uma pessoa NAQUELE número. Sem a prova do dono, não entra.

    A prova é o mesmo ``authorized_factor`` de todo portão privilegiado: voz
    confirmada OU senha digitada — as duas portas valem pelo mesmo caminho, e
    a senha nunca fica, é logada ou retornada.
    """
    acct = _key(account)
    if not acct:
        return {"ok": False, "reason": "sem numero (conta) informado",
                "entry": None}
    digits = "".join(c for c in str(jid or "") if c.isdigit())
    if not digits:
        return {"ok": False, "reason": "sem numero de contato", "entry": None}
    from core import owner_auth as _auth
    factor = _auth.authorized_factor(password=password, voice_ok=voice_ok)
    if not factor:
        return {"ok": False, "entry": None,
                "reason": ("sem autorizacao do dono: preciso de voz confirmada "
                           "ou senha digitada correta")}
    doc = load()
    if doc.get("falha"):
        return {"ok": False, "entry": None,
                "reason": f"arquivo da porta indisponivel: {doc['falha']}"}
    numeros = doc["numeros"]
    block = numeros.setdefault(acct, {})
    if persona:
        block["persona"] = str(persona)
    permitidos = block.setdefault("permitidos", [])
    moment = (now or dt.datetime.now()).strftime("%Y-%m-%d")
    existing = next((p for p in permitidos
                     if isinstance(p, dict) and _same_jid(p.get("jid"), digits)),
                    None)
    entry = existing or {"jid": digits, "dono": False}
    entry["nome"] = str(nome or entry.get("nome") or "").strip() or digits
    entry["autorizado_por"] = factor
    entry["data"] = str(entry.get("data") or moment)
    if existing is None:
        permitidos.append(entry)
    doc["atualizado_em"] = moment
    ok = _write(doc)
    if ok:
        _log(f"[porta] {entry['nome']} permitido no numero {acct} por {factor}.")
    return {"ok": ok, "entry": entry,
            "reason": "" if ok else "arquivo nao gravado"}


def revoke(account: Any, jid: Any) -> dict[str, Any]:
    """Tira a pessoa do número. O criador não sai: a porta dele não é licença."""
    acct = _key(account)
    digits = "".join(c for c in str(jid or "") if c.isdigit())
    if is_creator(digits):
        return {"ok": False,
                "reason": "o criador nao precisa de licenca neste numero e nao "
                          "pode ser revogado dele"}
    doc = load()
    block = (doc.get("numeros") or {}).get(acct) or {}
    permitidos = block.get("permitidos")
    if not isinstance(permitidos, list):
        return {"ok": False, "reason": "essa pessoa nao esta permitida neste numero"}
    before = len(permitidos)
    block["permitidos"] = [p for p in permitidos
                           if not (isinstance(p, dict)
                                   and _same_jid(p.get("jid"), digits))]
    if len(block["permitidos"]) == before:
        return {"ok": False, "reason": "essa pessoa nao esta permitida neste numero"}
    doc["atualizado_em"] = dt.datetime.now().strftime("%Y-%m-%d")
    ok = _write(doc)
    if ok:
        _log(f"[porta] {digits} retirado do numero {acct}.")
    return {"ok": ok, "reason": "" if ok else "arquivo nao gravado"}


# ── o veredito ──────────────────────────────────────────────────────────────

@dataclass
class DoorVerdict:
    """A decisão da porta de um número de persona, com o motivo falável."""

    applies: bool = False
    allowed: bool = True
    decision: str = ""
    reason: str = ""
    account: str = ""
    persona: str = ""
    persona_name: str = ""
    sender: str = ""
    sender_name: str = ""
    autorizado_por: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def persona_owning(account: Any) -> dict[str, Any] | None:
    """A ÚNICA persona dona desta conta (lê o cadastro vivo; None se não há).

    Zero ou duas personas na conta não é porta: sem porta, a rota normal decide
    como sempre decidiu — nada aqui abre ou fecha conversa fora de um número de
    persona.
    """
    try:
        from core import whatsapp_watch as _watch
        return _watch.persona_for_account(str(account or ""))
    except Exception:
        return None


def decide(account: Any, item: Any, *, persona: dict | None = None,
           direct: bool = False) -> DoorVerdict:
    """O veredito de UMA mensagem individual na conta ``account``.

    ``direct=True`` é o chat direto do dono na conta de saída: a entrada dele
    não é licença nenhuma e não passa por porta alguma (a ordem dele é de
    09/11/2026, e a decisão pendente sobre "qualquer chat" incluir o DM dele é
    dele, não minha).
    """
    account = str(account or "").strip()
    persona = persona if persona is not None else persona_owning(account)
    if direct or not persona:
        return DoorVerdict(account=account, applies=False, allowed=True,
                           decision="sem_porta",
                           reason=("chat direto do dono" if direct
                                   else "a conta nao e o numero de uma persona"))
    slug = str(persona.get("id") or persona.get("slug")
               or persona.get("name") or "").strip()
    verdict = DoorVerdict(applies=True, account=account, persona=slug,
                          persona_name=str(persona.get("name") or slug),
                          sender=sender_of(item),
                          sender_name=str((item or {}).get("sender_name")
                                          or (item or {}).get("name") or ""))
    if not verdict.sender:
        verdict.allowed = False
        verdict.decision = "recusa"
        verdict.reason = ("autor da mensagem nao identificado: sem quem escreveu "
                          "nao ha permissao de resposta")
        return verdict
    if is_creator(verdict.sender):
        verdict.allowed = True
        verdict.decision = "resposta"
        verdict.reason = "autor e o criador do numero"
        verdict.autorizado_por = AUTORIZADO_POR_CRIADOR
        return verdict
    entry = permitted_by(account, verdict.sender)
    if entry is not None:
        verdict.allowed = True
        verdict.decision = "resposta"
        verdict.reason = "autor esta na lista deste numero"
        verdict.autorizado_por = str(entry.get("autorizado_por") or "")
        return verdict
    verdict.allowed = False
    verdict.decision = "recusa"
    verdict.reason = ("terceiro no numero da persona: so o criador ou quem ele "
                      "permitir para este numero recebe resposta")
    if (load().get("falha")):
        verdict.reason += " (arquivo da porta ilegivel: falha fechada)"
    return verdict


def describe(verdict: DoorVerdict) -> str:
    """Uma linha honesta de trilha: decisão, motivo, quem, conta.

    ``quem`` é a pessoa pelo rótulo que o WhatsApp entrega e ``contato`` é o
    número normalizado — o que a caneta dele precisa para permitir. Nunca traz
    o texto da mensagem: o que chegou de terceiro é conteúdo, não log.
    """
    got = verdict.as_dict()
    return (f"[porta persona] conta={got.get('account') or '?'} "
            f"persona={got.get('persona_name') or got.get('persona') or '?'} "
            f"quem={got.get('sender_name') or got.get('sender') or '?'} "
            f"contato={got.get('sender') or '?'} "
            f"decisao={got.get('decision') or '-'}"
            + (f" autorizado_por={got.get('autorizado_por')}"
               if got.get("autorizado_por") else "")
            + (f" motivo={got.get('reason')}" if got.get("reason") else ""))
