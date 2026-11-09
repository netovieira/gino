"""Lei de GRUPOS e de PRIVACIDADE — um ponto só, ordem literal do dono (09/11/2026).

As seis ordens que este módulo codifica:

1. GRUPO: "Agora voce vai poder LER grupos, mas responder APENAS pessoas
   autorizadas". A antiga regra 4 ("grupo nunca é lido nem respondido") foi
   REVOGADA pelo próprio dono. Ler sempre; responder só quem está na allow-list.
2. ALLOW-LIST: "Por contato". Uma vez autorizado, o contato vale em TODOS os
   grupos — não existe autorização por grupo.
3. FORMATO: "Como em chats diretos, espelho da pergunta" — áudio entra, áudio
   sai; texto entra, texto sai. Sem degradação silenciosa (a lei do dono de
   07/11/2026 continua de pé: áudio que não sai é falha dita, não texto).
4. AUTORIZAÇÃO: "Por voz ou texto/senha, e eu já venho autorizado por padrão e
   não posso ter autorização revogada". O dono entra na allow-list sozinho
   (``autorizado_por="padrao"``) e ``revoke_contact`` dele falha sempre.
5. VOZ DESCONHECIDA: registrar a voz como memória da pessoa e dar permissões de
   VISITANTE; upgrade só com autorização explícita do dono (``core.voice_visitors``).
6. PRIVACIDADE (regra GERAL, não só de grupo): "Se eu não estiver no chat, você
   nem vai ver o chat"; nunca falar de projetos; nunca falar do que o dono disse
   a outros; nada do que roda no PC dele; na dúvida, PEDIR PERMISSÃO ANTES NO
   MESMO CHAT/GRUPO; resposta sobre o dono e sobre projetos é SUPERFICIAL e puxa
   uma leve sardinha vendendo o produto sutilmente.

O que isto muda na arquitetura: a decisão de grupo deixa de ser um ``return
False`` espalhado e vira :func:`group_verdict`, uma função pura que devolve o
veredito e o MOTIVO. ``core/whatsapp_watch.py`` consulta o veredito e
``core/group_reply.py`` executa o espelho de formato. Silêncio é sempre
veredito negativo com motivo no log — nunca invenção de resposta.

Nada aqui decide conteúdo de modelo: :func:`law_line` e :func:`superficial_reply`
são o texto de lei que chega ao cérebro, exatamente como ``core/owner_law.py``
faz com a lei da fala do dono.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

BASE_DIR = Path(__file__).resolve().parent.parent

#: Arquivo da allow-list (config versionado: é política, não segredo).
DEFAULT_ALLOWLIST_PATH = BASE_DIR / "config" / "group_allowlist.json"

TIPO_CONTATO = "contato"
#: Quem pôs o dono na lista foi a ordem dele, não uma autorização humana.
AUTORIZADO_POR_PADRAO = "padrao"

_LOCK = threading.Lock()
_PATH: Path | None = None
_LOG: Callable[[str], None] = lambda _m: None
#: Presença do dono por grupo, injetável (testes e vivo). ``None`` = sem fonte.
_presence_source: Callable[[str, str], "bool | None"] | None = None


def configure(*, path: "str | os.PathLike[str] | None" = None,
              log: Callable[[str], None] | None = None,
              presence_source: Callable[[str, str], "bool | None"] | None = None,
              reset_cache: bool = False) -> None:
    """Redireciona arquivo, canal de log e fonte de presença (testes/vivo)."""
    global _PATH, _LOG, _presence_source
    with _LOCK:
        if path is not None:
            _PATH = Path(path)
        if log is not None:
            _LOG = log
        if presence_source is not None:
            _presence_source = presence_source
        if reset_cache:
            _presence_cache.clear()


def allowlist_path() -> Path:
    return _PATH or DEFAULT_ALLOWLIST_PATH


def _log(msg: str) -> None:
    try:
        _LOG(msg)
    except Exception:
        pass


# ── normalização de identidade ──────────────────────────────────────────────

def _fold(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "")).lower()
    return "".join(c for c in raw if not unicodedata.combining(c))


def _digits(text: Any) -> str:
    return re.sub(r"\D", "", str(text or ""))


def normalize_jid(raw: Any) -> str:
    """O contato como chave estável: só dígitos, sem servidor nem pontuação.

    "5517 99999-9999", "+5517999999999", "5517999999999@s.whatsapp.net" e
    "5517999999999@lid" são a mesma pessoa. Sem isso a allow-list dependeria de
    como o WhatsApp decidiu escrever o JID naquele evento.
    """
    return _digits(raw)


def group_jid(chat: Any) -> str:
    """O JID inteiro do grupo (com ``@g.us``), ou '' quando não é grupo."""
    raw = str(chat or "").strip()
    if not raw:
        return ""
    if _is_group_chat(raw):
        return raw
    return ""


def _is_group_chat(chat: Any) -> bool:
    low = str(chat or "").strip().lower()
    return low.endswith("@g.us") or low.endswith("-g.us") or low.endswith("g.us")


def is_group_message(item: Any) -> bool:
    """A mensagem veio de um grupo (flag do neonize ou JID ``@g.us``)?"""
    if not isinstance(item, dict):
        return False
    if item.get("group"):
        return True
    return _is_group_chat(item.get("chat"))


def sender_jid(item: Any) -> str:
    """Quem escreveu num grupo — o autor da mensagem, não o chat.

    Em chat individual o ``chat`` É a pessoa; em grupo o ``chat`` é o grupo e a
    pessoa vem em ``sender`` (campo acrescentado por ``core/whatsapp.py`` a partir
    do ``Author`` do evento). Sem autor reconhecível o veredito é negativo: a
    allow-list é por contato, e contato desconhecido não é contato autorizado.
    Um ``from_me`` do aparelho pareado é o dono (eco dele mesmo).
    """
    if not isinstance(item, dict):
        return ""
    for key in ("sender", "sender_jid", "author"):
        got = normalize_jid(item.get(key))
        if got:
            return got
    if item.get("from_me"):
        # Eco do aparelho pareado: quem escreveu foi o dono.
        mine = sorted(owner_jids())
        return mine[0] if mine else ""
    if not item.get("group") and not _is_group_chat(item.get("chat")):
        return normalize_jid(item.get("chat"))
    return ""


# ── o dono ──────────────────────────────────────────────────────────────────

def owner_jids() -> set[str]:
    """Os números do dono: ``owner_chat`` configurado + números pareados.

    É a fonte do "eu já venho autorizado por padrão": mesmo com o arquivo
    apagado ou corrompido, o dono continua sendo o dono — a ordem dele não
    depende de um JSON existir. A definição única de "número do dono" mora em
    ``core.whatsapp.owner_numbers``; aqui só o fallback quando o WhatsApp não
    está carregado (teste isolado, processo sem neonize).
    """
    try:
        from core import whatsapp as _wa
        got = {normalize_jid(n) for n in _wa.owner_numbers()}
        got.discard("")
        if got:
            return got
    except Exception:
        pass
    out: set[str] = set()
    try:
        from memory.config_manager import get_plugin_setting
        out.add(normalize_jid(get_plugin_setting("whatsapp", "owner_chat", "")))
    except Exception:
        pass
    out.discard("")
    return out


def is_owner(jid: Any) -> bool:
    got = normalize_jid(jid)
    return bool(got and got in owner_jids())


def _owner_entry(now: dt.datetime | None = None) -> dict[str, Any]:
    moment = now or dt.datetime.now()
    jids = sorted(owner_jids())
    nome = ""
    try:
        from memory.config_manager import get_plugin_setting
        nome = str(get_plugin_setting("whatsapp", "owner_name", "") or "").strip()
    except Exception:
        nome = ""
    return {
        "jid": jids[0] if jids else "",
        "nome": nome or "Dono",
        "tipo": TIPO_CONTATO,
        "autorizado_por": AUTORIZADO_POR_PADRAO,
        "data": moment.strftime("%Y-%m-%d"),
        "dono": True,
        "irrevogavel": True,
    }


# ── allow-list (por contato, vale em todos os grupos) ───────────────────────

def _default_doc(now: dt.datetime | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "regra": ("leitura de grupos liberada; resposta apenas a contatos da "
                  "allow-list, em qualquer grupo (ordem do dono 09/11/2026)"),
        "atualizado_em": (now or dt.datetime.now()).strftime("%Y-%m-%d"),
        "contatos": [_owner_entry(now)],
    }


def _read_raw(path: Path) -> tuple[Any, str]:
    """O arquivo como está no disco: ``(objeto|None, "missing"|"corrupt"|"ok")``.

    ``missing`` é instalação nova (a ordem 1 semeia só o dono). ``corrupt`` é
    o que a ordem NÃO permite decidir nada: arquivo ilegível, JSON quebrado ou
    sem a lista ``contatos`` — aí a leitura de grupo desliga (falha fechada).
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
    if not isinstance(raw, dict) or not isinstance(raw.get("contatos"), list):
        return None, "corrupt"
    return raw, "ok"


def load(*, seed: bool = True) -> dict[str, Any]:
    """O documento da allow-list; cria o default (só o dono) quando não existe.

    Corrompido NUNCA é reescrito por cima: pode ser política do dono meio
    escrita, e cobri-la com um default deixaria a falha fechada virar aberta.
    """
    path = allowlist_path()
    raw, status = _read_raw(path)
    if status == "missing":
        if not seed:
            return _default_doc()
        doc = _default_doc()
        _write(doc)
        _log("[Grupos] allow-list criada com o dono (autorizado por padrao).")
        return doc
    if status == "corrupt":
        _log("[Grupos] allow-list corrompida ou ilegivel: nenhum grupo sera "
             "lido ate ela voltar (falha fechada).")
        return {"version": 1, "regra": "", "atualizado_em": "",
                "contatos": [], "falha": "allow-list corrompida ou ilegivel"}
    assert raw is not None
    contatos = [c for c in raw["contatos"] if isinstance(c, dict)]
    raw["contatos"] = contatos
    if (seed and contatos
            and not any(c.get("dono") or is_owner(c.get("jid"))
                        for c in contatos)):
        # O dono é irrevogável: se sumiu do arquivo, volta. Lista VAZIA é outra
        # coisa: é "ninguém autorizado", e a ordem manda fechar, não reinserir.
        contatos.insert(0, _owner_entry())
        raw["contatos"] = contatos
        _write(raw)
        _log("[Grupos] dono ausente da allow-list — reinserido por padrao.")
    return raw


def allowlist_status() -> dict[str, Any]:
    """A política está legível e com alguém dentro? ``ok=False`` fecha o grupo.

    Três estados fecham: arquivo corrompido/ilegível, lista ``contatos``
    ausente e lista VAZIA (o dono pode querer ninguém autorizado; aí o Gino lê
    grupo nenhum). Arquivo ausente não fecha: é a instalação nova, onde a
    ordem 1 já diz que a lista é só o dono.
    """
    raw, status = _read_raw(allowlist_path())
    if status == "corrupt":
        return {"ok": False, "contatos": 0,
                "reason": "allow-list corrompida ou ilegivel"}
    if status == "missing":
        return {"ok": True, "contatos": 1, "reason": ""}
    assert raw is not None
    contatos = [c for c in raw["contatos"] if isinstance(c, dict)]
    if not contatos:
        return {"ok": False, "contatos": 0,
                "reason": "allow-list vazia: nenhum contato autorizado"}
    return {"ok": True, "contatos": len(contatos), "reason": ""}


def _write(doc: dict[str, Any]) -> bool:
    path = allowlist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        return True
    except OSError as exc:
        _log(f"[Grupos] allow-list nao gravada: {type(exc).__name__}: {exc}")
        return False


def entries() -> list[dict[str, Any]]:
    return list(load().get("contatos") or [])


def contact_for(jid: Any) -> dict[str, Any] | None:
    got = normalize_jid(jid)
    if not got:
        return None
    for entry in entries():
        if normalize_jid(entry.get("jid")) == got:
            return entry
    return None


def is_allowed(jid: Any) -> bool:
    """O contato está na allow-list? (dono vale sempre, mesmo fora do arquivo)"""
    got = normalize_jid(jid)
    if not got:
        return False
    if is_owner(got):
        return True
    return contact_for(got) is not None


def add_contact(jid: Any, nome: str = "", *,
                autorizado_por: str = AUTORIZADO_POR_PADRAO,
                now: dt.datetime | None = None) -> dict[str, Any]:
    """Grava o contato na allow-list. Chamada interna: quem autoriza é
    :func:`authorize_contact`, que exige a prova do dono."""
    got = normalize_jid(jid)
    if not got:
        return {"ok": False, "reason": "sem JID de contato", "entry": None}
    doc = load()
    contatos = doc["contatos"]
    existing = next((c for c in contatos if normalize_jid(c.get("jid")) == got), None)
    moment = (now or dt.datetime.now()).strftime("%Y-%m-%d")
    entry = existing or {"jid": got, "tipo": TIPO_CONTATO, "dono": False}
    entry["nome"] = str(nome or entry.get("nome") or "").strip() or got
    entry["tipo"] = TIPO_CONTATO
    entry["autorizado_por"] = str(autorizado_por or AUTORIZADO_POR_PADRAO)
    entry["data"] = str(entry.get("data") or moment)
    entry["irrevogavel"] = bool(entry.get("dono"))
    if existing is None:
        contatos.append(entry)
    if not any(c.get("dono") for c in contatos):
        contatos.insert(0, _owner_entry(now))
    doc["contatos"] = contatos
    doc["atualizado_em"] = moment
    ok = _write(doc)
    return {"ok": ok, "reason": "" if ok else "arquivo nao gravado",
            "entry": entry}


def authorize_contact(jid: Any, nome: str = "", *, voice_ok: bool = False,
                      password: str = "",
                      now: dt.datetime | None = None) -> dict[str, Any]:
    """Autoriza por voz confirmada OU senha digitada (ordem 4 do dono).

    Sem um dos dois fatores o contato NÃO entra: texto de terceiro não autoriza
    ninguém, e senha errada não é fator. A senha nunca fica nem é logada.
    """
    from core import owner_auth as _auth
    factor = _auth.authorized_factor(password=password, voice_ok=voice_ok)
    if not factor:
        return {"ok": False, "entry": None,
                "reason": ("sem autorizacao do dono: preciso de voz confirmada "
                           "ou senha digitada correta")}
    got = add_contact(jid, nome, autorizado_por=factor, now=now)
    if got["ok"]:
        _log(f"[Grupos] contato {got['entry'].get('nome')} autorizado por "
             f"{factor}.")
    return got


def revoke_contact(jid: Any) -> dict[str, Any]:
    """Tira o contato da allow-list. O dono é irrevogável: recusa sempre."""
    got = normalize_jid(jid)
    entry = contact_for(got) or ({"jid": got, "dono": is_owner(got)} if got else None)
    if not got or entry is None:
        return {"ok": False, "reason": "contato nao esta na allow-list"}
    if entry.get("dono") or is_owner(got):
        return {"ok": False,
                "reason": "o dono ja vem autorizado por padrao e nao pode ter "
                          "autorizacao revogada (ordem 09/11/2026)"}
    doc = load()
    before = len(doc["contatos"])
    doc["contatos"] = [c for c in doc["contatos"]
                      if normalize_jid(c.get("jid")) != got]
    if not doc["contatos"] or not any(c.get("dono") for c in doc["contatos"]):
        doc["contatos"].insert(0, _owner_entry())
    doc["atualizado_em"] = dt.datetime.now().strftime("%Y-%m-%d")
    if len(doc["contatos"]) == before:
        return {"ok": False, "reason": "contato nao esta na allow-list"}
    ok = _write(doc)
    if ok:
        _log(f"[Grupos] contato {got} revogado da allow-list.")
    return {"ok": ok, "reason": "" if ok else "arquivo nao gravado"}


# ── presença do dono no chat (regra 6: "se eu não estiver, nem ver") ────────

_presence_cache: dict[str, "bool | None"] = {}


def note_owner_sighting(group: Any, jid: Any = "") -> None:
    """Registra que o dono falou neste grupo (prova de presença na hora).

    O evento do neonize traz o autor de cada mensagem de grupo; ver o dono
    escrever ali é a forma mais barata e mais honesta de saber que ele está no
    grupo, e é o que entra em cache quando não há consulta de participantes.
    """
    key = str(group or "").strip()
    if not key:
        return
    who = normalize_jid(jid) or sender_jid({"chat": key})
    if who and is_owner(who):
        _presence_cache[key] = True


def owner_present(*, chat: Any = "", group: Any = "", account: str = "",
                  direct: bool = False) -> "bool | None":
    """True/False/None (None = não sei). ``None`` nunca vira "pode"."""
    if direct:
        return True
    key = str(group or chat or "").strip()
    if not key:
        return None
    if key in _presence_cache:
        return _presence_cache[key]
    if _presence_source is not None:
        try:
            got = _presence_source(key, str(account or ""))
        except Exception:
            got = None
        if got is not None:
            _presence_cache[key] = bool(got)
        return None if got is None else bool(got)
    try:
        from core import whatsapp as _wa
        got = _wa.owner_in_group(key, account)
    except Exception:
        got = None
    if got is not None:
        _presence_cache[key] = bool(got)
    return got


# ── contexto: pergunta direta x trecho de música x narração ─────────────────

_INTERROGATIVE_RE = re.compile(
    r"\b(o que|que|quem|como|qual|quais|quando|onde|por ?que|porque|por que|"
    r"quanto|quantos|voce|vc|da|da pra|daria|pode|podes|consegue|conseguiria|"
    r"sera que|vale|sabe|tem|existem?|e ?que|pra ?que)\b"
)
_MUSIC_NOTE_RE = re.compile(r"[♪🎵♩🎶]")
_MUSIC_WORD_RE = re.compile(r"\b(trecho|verso|refrao|estribilho|letra|"
                            r"musica|musiquinha|cancao|song|lyrics)\b")
_CHORUS_RE = re.compile(r"(?:\b(?:la|oh|oi|ei|na|nhé|té)\b[\s,!?-]*){2,}",
                        re.IGNORECASE)
_NARRATION_RE = re.compile(
    r"\b(ai a gente|entao (a gente|eu|nos)|dai (eu|a gente)|a[ií] que|"
    r"ele (falou|disse)|ela (falou|disse)|fui|vim|cheguei)\b"
)

#: Idiom exclamativos: têm "que"/"como" mas NÃO perguntam nada. Em grupo isso é
#: papo de terceiros ("que loucura esse jogo"), e papo de terceiros é silêncio.
_EXCLAMATION_RE = re.compile(
    r"^(?:nao ?acredito que|que|quao|nem que|se)\s+"
    r"(?:loucura|isso|nada|bom|boa|ruim|ruins|legal|absurdo|bizarrice|susto|"
    r"cansaco|dificil|facil|tarde|cedo|pessoa|genio|medo|vergonca|bagunca|"
    r"barulho|prazer|feio|lindo|bonito|caro|barato|mesmo|se|fosse)"
    r"\b|^(?:quem sabe|sei la|sei nao|nossa|caraca|caramba|meu deus|ai ai|"
    r"obrigad[oa]|valeu|parabens|bom dia|boa noite|boa tarde|adeus|tchau)\b"
    r"|\b(?:k{3,}|ha{3,}|rs{2,})\b"
)


#: Aspas que carregam fala de OUTRO: letra de música, verso de música, fala de
#: filme, citação do que alguém disse. O nome do assistente DENTRO delas não é
#: chamada — é matéria citada (ordem 1 do dono: "se era letra de música ou
#: alguém narrando"). Por isso o miolo citado some antes de qualquer decisão.
_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'), ("“", "”"), ("‟", "”"), ("„", "“"), ("«", "»"),
    ("'", "'"), ("‘", "’"), ("「", "」"), ("『", "』"),
)


def _quoted_spans(text: str) -> list[str]:
    out: list[str] = []
    for open_q, close_q in _QUOTE_PAIRS:
        out += re.findall(re.escape(open_q) + r"(.*?)" + re.escape(close_q),
                          text, re.DOTALL)
    return out


def _blank_quotes(text: str) -> str:
    """A frase sem o que está entre aspas (o citado deixa de ser endereçamento)."""
    out = str(text or "")
    for open_q, close_q in _QUOTE_PAIRS:
        out = re.sub(re.escape(open_q) + r".*?" + re.escape(close_q), " ",
                     out, flags=re.DOTALL)
    return out


def _address_names() -> list[str]:
    """Os nomes que chamam alguém: o do assistente vivo + o das personas."""
    out: list[str] = []
    try:
        from core import whatsapp_watch as _watch
        got = str(_watch.trigger_name() or "").strip()
        if got:
            out.append(got)
    except Exception:
        pass
    try:
        from core import whatsapp_watch as _watch
        for persona in _watch._live_personas():
            got = str((persona or {}).get("name") or "").strip()
            if got:
                out.append(got)
    except Exception:
        pass
    return out


#: Verbos de fala/citação que antecedem o nome: "o refrão diz Gino ...",
#: "ele falou Gino some daqui". Sem aspas, é o mesmo caso: a frase é de outro.
_REPORTED_SPEECH_RE = re.compile(
    r"(?:diz|dizia|dizem|diria|cant[oa]|cantava|cantou|grit[ao]|gritava|gritou|"
    r"fal[ao]|falava|falou|escrev[ea]|escrevia|escreveu|repet[ea]|repetia|"
    r"repetiu|cit[ao]|citava|citou|letra|refrao|verso|estribilho|musica|"
    r"cancao|filme|musical|novela)\s*[:\-]?\s*$",
    re.IGNORECASE)


def _reported_speech(text: Any) -> bool:
    """O nome vem logo depois de um "diz / falou / refrão"? Então é fala de outro."""
    raw = _fold(str(text or ""))
    for name in _address_names():
        folded = _fold(name)
        if not folded:
            continue
        for hit in re.finditer(rf"\b{re.escape(folded)}\b", raw):
            window = raw[max(0, hit.start() - 30):hit.start()]
            if "?" in window:
                continue
            if _REPORTED_SPEECH_RE.search(window):
                return True
    return False


def is_citation(text: Any) -> bool:
    """Ninguém chamou ninguém: o nome é matéria citada (letra, fala de outro).

    Dois formatos: o nome aparece SÓ dentro de aspas ("Gino, me leva no
    altar") ou vem logo depois de um verbo de citação ("o refrão diz Gino ...").
    O ``?`` de dentro da citação não é pergunta para o assistente — é a
    pontuação do outro. Se o nome também está fora do citado, o endereçamento é
    real e nada aqui se aplica.
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    if _quoted_spans(raw):
        inside = _fold(raw)
        outside = _fold(_blank_quotes(raw))
        for name in _address_names():
            folded = _fold(name)
            if not folded:
                continue
            pattern = rf"\b{re.escape(folded)}\b"
            if re.search(pattern, inside) and not re.search(pattern, outside):
                return True
    return _reported_speech(raw)


def _strip_vocative(folded: str) -> str:
    """Tira o nome do assistente e o começo de frase, sobra o pedido nu.

    "gino, que loucura esse jogo" vira "que loucura esse jogo" — e aí o idiom
    exclamativo pega. O nome nunca é parte da pergunta, nunca deve escondê-la.
    """
    out = str(folded or "")
    try:
        from core import whatsapp_watch as _watch
        name = _fold(_watch.trigger_name())
        if name:
            out = re.sub(rf"\b{re.escape(name)}\b[\s,.:;!?-]*", "", out)
    except Exception:
        pass
    out = re.sub(r"^(?:oi|eai|e ai|ola|opa|ei|psiu|tudo bem|[!?.\s,-])+", "", out)
    return out.strip()


def classify_context(text: Any) -> str:
    """O que a mensagem É: ``pergunta``, ``trecho_de_musica``, ``citacao`` ou ``narracao``.

    Só ``pergunta`` responde. Em grupo, a maior parte do tráfego é conversa
    entre as pessoas, letra de música, citação do que outro disse ou narração
    do que está acontecendo — e cada uma dessas continua sendo lida em
    silêncio. A ordem é essa de propósito: partitura/linha repetida é música
    mesmo com "?", o citado ("Gino, me leva no altar" entre aspas, "o refrão
    diz Gino...") é fala de outro mesmo com "?", o "?" é a prova mais forte de
    pergunta, e sem "?" o idiom exclamativo ("que loucura") e a narração ("ai
    a gente chegou") vencem do interrogativo solto. Dúvida vira silêncio, nunca
    pergunta inventada.
    """
    raw = str(text or "")
    body = " ".join(raw.split())
    if not body:
        return "narracao"
    folded = _fold(body)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    lowered = [ln.lower() for ln in lines]
    if _MUSIC_NOTE_RE.search(raw):
        return "trecho_de_musica"
    if len(lines) >= 2 and len(set(lowered)) < len(lowered):
        return "trecho_de_musica"
    if len(_CHORUS_RE.findall(body)) >= 2:
        return "trecho_de_musica"
    if is_citation(raw):
        return "citacao"
    if "?" in body:
        return "pergunta"
    clause = _strip_vocative(folded)
    if _MUSIC_WORD_RE.search(clause):
        return "trecho_de_musica"
    if _EXCLAMATION_RE.search(clause) or _NARRATION_RE.search(clause):
        return "narracao"
    if _INTERROGATIVE_RE.search(clause):
        return "pergunta"
    return "narracao"


# ── privacidade (regra 6, global) ───────────────────────────────────────────

TOPICO_DONO = "dono"
TOPICO_PROJETO = "projeto"
TOPICO_DISPOSITIVO = "dispositivo"
TOPICO_FALAS = "falas"

SENSITIVE_PATTERNS: dict[str, tuple[str, ...]] = {
    # O que o dono disse/fez para outras pessoas, e a vida dele.
    TOPICO_FALAS: (
        r"o que (eu|ele|ela|o dono|a dona) (falei|falei pra|disse|disseram|"
        r"falou|contou|pedi|pediu)",
        r"(conversa|historico|transcri[cç][aã]o|di[aá]logo|recado) (anterior|"
        r"de antes|do dono|com o dono)",
        r"(ele|ela|o dono) (me|pra mim) (disse|falou|contou)",
        r"o que (voce|vc) (ouvi|ouuiu|escutou|escutou) (sobre|dele|dela)",
    ),
    # Dados do próprio dono.
    TOPICO_DONO: (
        r"\b(sal[aá]rio|dinheiro|conta banc[aá]ria|cart[aã]o|cpf|rg|senha|"
        r"endere[cç]o|telefone|n[uú]mero (dele|dela|do dono)|e-?mail (dele|dela|"
        r"do dono)|agenda|consulta|m[eé]dic[oa]s|sa[uú]de|dieta|termo (de |do )?"
        r"(compromisso|casamento)|namorad[ao]|esposa|marido|fam[ií]lia (dele|dela))\b",
        r"(onde|como) (ele|ela|o dono) (est[aá]|trabalha|mora|andou)",
        r"(fale|diga|mostra|manda|revela|lista|conte)(-me| me)? (algo|tudo|o que|"
        r"sobre) (sobre )?(mim|o dono|ele|ela)",
    ),
    # Projetos e segredos de negócio.
    TOPICO_PROJETO: (
        r"\b(projetos?|c[oó]digo|reposit[oó]rio|repo|git|commit|deploy|prazo|"
        r"or[cç]amento|cliente[sS]?|contrato|fatura(?:mento)?|cakto|nextcloud|"
        r"retransmitter|site ?2|api[_ ]?key|token|segredo|backdoor|vulnerab|"
        r"roadmap|espec(?:ifica)?c?ao|arquitetura|budget)\b",
        r"no ?(que (voc[eê]|voce|gino|jarvis) (est[aá] (trabalhando|fazendo|codando)|"
        r"andando|construindo|developando))",
        r"(planilha|dor?ma?nte|vault|obsidian|dossi[eê]|dosser|anota[çc][õo]es|"
        r"minuta|rascunho)s? (do dono|do gino|do jarvis|do projeto|meus?|minhas?)",
    ),
    # A máquina do dono: o que está aberto, rodando, visível.
    TOPICO_DISPOSITIVO: (
        r"\b(apps?|aplicativos?|programas?|abas?|janelas?|processos?|tarefas?|"
        r"downloads?|archiv[oa]s?|arquivos?|prints?|capturas?|telas?|webcam|"
        r"microfone|teclado|hist[oó]rico (do |no )?(navegador|chrome|pc))\b"
        r".{0,24}\b(aberto|abertos|rodando|aberta|abertas|ativos|vis[ií]vel|"
        r"espa?lhando|na tela|no pc|no computador|do dono|do meu pc|"
        r"do meu computador)\b",
        r"(abertos?|rodando|na tela|no meu pc|no computador|aqui do pc)"
        r".{0,24}\b(apps?|programas?|arquivos?|abas?|janelas?)\b",
        r"(o que (tem|esta| roda|rodando|aberto)s? (no|aqui) (meu |do )?"
        r"(pc|computador|m[aá]quina))",
        r"(lista|mostre|mostra|diga|fala)(-me| me)? (tudo|o que|os) (aberto|"
        r"rodando|no meu (pc|computador))",
    ),
}

_SENSITIVE_RES: dict[str, tuple[re.Pattern, ...]] = {
    kind: tuple(re.compile(p, re.IGNORECASE) for p in pats)
    for kind, pats in SENSITIVE_PATTERNS.items()
}

#: Marcas de "está em dúvida": pedido vago, tangencial, ou pergunta sobre
#: mim/projetos sem alvo claro — aí a lei manda PERGUNTAR AO DONO.
_DOUBT_RE = re.compile(
    r"\b(sei l[aá]|talvez| acho que|me parece|n[aã]o sei se|ser[aá] que (posso|"
    r"devo)|posso falar|pode contar|conta s[oó]?|fala s[oó]?|um pouco|mais ou menos|"
    r"sem contar muito)\b",
    re.IGNORECASE)

#: A "leve sardinha": o produto é o próprio assistente. O texto mora no config
#: para o dono trocar sem tocar em código; nada aqui inventa preço ou link.
DEFAULT_PITCH = ("E olha: isso aqui e um assistente que roda no PC dele, faz "
                 "isso e muito mais. Quer saber como, fala com o Anthero.")
_PITCH_SETTING = "group_pitch"


def pitch() -> str:
    """A frase de venda sutil (config ``whatsapp.group_pitch``; default discreto)."""
    try:
        from memory.config_manager import get_plugin_setting
        got = str(get_plugin_setting("whatsapp", _PITCH_SETTING, "") or "").strip()
        if got:
            return got
    except Exception:
        pass
    return DEFAULT_PITCH


def sensitive_topic(text: Any) -> str:
    """A família do dado sensível pedido, ou '' quando não é sensível."""
    body = str(text or "")
    if not body.strip():
        return ""
    folded = _fold(body)
    for kind, patterns in _SENSITIVE_RES.items():
        if any(p.search(folded) for p in patterns):
            return kind
    return ""


def is_doubtful(text: Any) -> bool:
    """O texto abre dúvida? (a lei manda pedir permissão ao dono)"""
    body = str(text or "")
    if not body.strip():
        return False
    if _DOUBT_RE.search(_fold(body)):
        return True
    # "sobre o dono / sobre o projeto" sem pedido claro de dado específico.
    folded = _fold(body)
    return bool(re.search(r"\b(sobre (o dono|a dona|mim|o projeto|os projetos|"
                           r"a empresa|o negocio))\b", folded)
                and not sensitive_topic(folded))


@dataclass
class PrivacyVerdict:
    """O veredito de privacidade de UMA conversa (qualquer canal)."""

    see: bool = True
    allowed: bool = True
    topic: str = ""
    action: str = "responder"
    reason: str = ""
    answer: str = ""
    ask_owner: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def privacy_verdict(text: Any, *, owner_present_in_chat: bool = False,
                    third_party: bool = False,
                    direct_owner_chat: bool = False) -> PrivacyVerdict:
    """A regra 6 inteira numa função: ver, não falar, pedir permissão, ser leve.

    * dono ausente do chat  -> ``see=False``: o chat nem é visto (lei literal).
    * dado sensível em chat de terceiro -> nada é dito; pede-se permissão ao
      dono NO MESMO CHAT/GRUPO quando ele está ali, senão silêncio.
    * assunto sobre o dono/projeto permitido de dizer -> resposta SUPERFICIAL
      com a sardinha leve.
    """
    body = str(text or "")
    if not direct_owner_chat and not owner_present_in_chat:
        return PrivacyVerdict(see=False, allowed=False, action="invisivel",
                              reason="o dono nao esta no chat: nem vejo o chat")
    if third_party:
        topic = sensitive_topic(body)
        if topic:
            ask = bool(owner_present_in_chat)
            return PrivacyVerdict(
                allowed=False, topic=topic,
                action="pedir_permissao" if ask else "silencio",
                ask_owner=ask,
                reason=(f"dado sensivel ({topic}) em chat de terceiro: "
                        + ("peco permissao ao dono no mesmo chat"
                           if ask else "o dono nao esta aqui, silencio")))
        if is_doubtful(body):
            ask = bool(owner_present_in_chat)
            return PrivacyVerdict(
                allowed=not ask, topic="",
                action="pedir_permissao" if ask else "silencio",
                ask_owner=ask,
                reason="duvida sobre poder responder: a lei manda pedir "
                       "permissao antes")
    if third_party and (re.search(r"\b(voce|gino|jarvis|assistente)\b", _fold(body))
                        and re.search(r"\b(de quem|quem (e|te) (criou|fez)|"
                                      r"com quem|meu dono|criador)\b", _fold(body))):
        return PrivacyVerdict(allowed=True, topic=TOPICO_DONO,
                              action="responder_superficial",
                              answer=superficial_reply(TOPICO_DONO),
                              reason="sobre o dono: resposta superficial")
    return PrivacyVerdict(allowed=True, action="responder")


def superficial_reply(topic: str = "", *, question: Any = "") -> str:
    """A resposta sobre o dono/projetos: SUPERFICIAL e com a leve sardinha.

    Existe para o caminho sem modelo (e para o teste provar a regra): nenhum
    dado, nenhum nome, nenhum número — só o genérico que vende o produto.
    """
    lead = {
        TOPICO_PROJETO: "Trabalho em coisas por aqui, nada de detalhes.",
        TOPICO_DONO: "Do dono eu so digo o que ele mesmo fala.",
        TOPICO_FALAS: "Conversa alheia eu nao repito.",
        TOPICO_DISPOSITIVO: "A tela e os arquivos dele nao saem daqui.",
    }.get(topic, "Sobre isso eu nao entro em detalhe.")
    return f"{lead} {pitch()}".strip()


def law_line() -> str:
    """A lei como linha de contexto para o cérebro (turnos de terceiros).

    Sem isto a regra não chega ao modelo: ela mora no código que decide o
    veredito, mas a REDAÇÃO da resposta é do modelo — e é ele que pode vazar.
    """
    return (
        "LEI DE PRIVACIDADE (ordem do dono, 09/11/2026, vigente em QUALQUER "
        "chat): se o dono nao esta no chat, voce nem ve o chat. Em conversa "
        "com terceiros: nunca fale de projetos, codigos, prazos, clientes ou "
        "valores; nunca diga o que o dono falou, ouviu ou fez para outra "
        "pessoa; nunca descreva apps abertos, arquivos, tela ou historico do "
        "PC dele. Informacao sobre o dono ou sobre projetos e sempre "
        "SUPERFICIAL e puxa uma leve sardinha vendendo o produto sutilmente. "
        "Na duvida se pode responder, peca permissao ao dono ANTES, no mesmo "
        "chat/grupo, e nao responda nada enquanto ele nao dizer."
    )


# ── o veredito de grupo (B) ─────────────────────────────────────────────────

@dataclass
class GroupVerdict:
    allowed: bool = False
    reason: str = ""
    kind: str = ""
    reply_format: str = "text"
    sender: str = ""
    sender_name: str = ""
    group: str = ""
    addressed: str = ""
    owner_present: "bool | None" = None
    ask_owner: bool = False
    needs_transcript: bool = False
    privacy: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _persona_by_slug(slug: Any) -> dict | None:
    """O cadastro de uma persona pelo slug/id/nome (lê, nunca escreve)."""
    want = _fold(slug)
    if not want:
        return None
    try:
        from core import whatsapp_watch as _watch
        for persona in _watch._live_personas():
            for key in ("slug", "id", "name"):
                if _fold((persona or {}).get(key)) == want:
                    return persona
    except Exception:
        return None
    return None


def persona_reachable_by_number(persona: Any) -> bool:
    """A persona tem número próprio? (ordem 3 do dono, 09/11/2026)

    ``direct_whatsapp`` é o que dá o número; sem ele a persona NÃO pode ser
    alcançada por número nenhum — nem por grupo, onde a mensagem chega no
    número do assistente e a resposta sairia nele fingindo ser ela. Ler o
    cadastro falhou é o mesmo que não ter número: falha fechada.
    """
    if not isinstance(persona, dict):
        return False
    try:
        from core import personas as _personas
        return bool(_personas.direct_whatsapp(persona))
    except Exception:
        return False


def _addressee(text: Any, item: dict, account: str) -> dict[str, str]:
    """Quem foi chamado: ``{"name": "assistente"|slug|"", "reason": recusa}``.

    Persona SEM número próprio é recusada aqui, com o MOTIVO dito (ordem 3 do
    dono, 09/11/2026): em grupo a mensagem chega no número do assistente, e
    responder por ela naquele número seria alcançá-la (e falar por ela) por um
    número que não é dela. O vocativo dela no chat do assistente continua
    vivo — é o ``main.py``/voz quem decide, não esta lei de grupo.
    """
    try:
        from core import whatsapp_watch as _watch
        if _watch.calls_assistant(dict(item or {}, text=text), account):
            return {"name": "assistente", "reason": ""}
    except Exception:
        pass
    try:
        from core import whatsapp_watch as _watch
        persona = _watch.addressed_persona(text, account)
    except Exception:
        return {"name": "", "reason": ""}
    if not persona:
        return {"name": "", "reason": ""}
    slug = str(persona.get("slug") or persona.get("name") or "persona")
    if str(persona.get("factor") or "") == "conta_atribuida":
        # Já passou pelo crivo do número próprio em ``personas.own_account``.
        return {"name": slug, "reason": ""}
    if not persona_reachable_by_number(_persona_by_slug(slug)):
        return {"name": "",
                "reason": (f"persona {slug} nao tem whatsapp proprio: nao e "
                           "alcancavel por numero nenhum, nem em grupo")}
    return {"name": slug, "reason": ""}


def _mentions_assistant(text: Any, item: dict, account: str) -> str:
    """'' quando ninguém chamou o assistente; senão ``assistente``/persona."""
    return _addressee(text, item, account)["name"]


def mirror_format(item: Any) -> str:
    """Espelho da pergunta: áudio entra → ``audio``; texto entra → ``text``."""
    try:
        from core import whatsapp_watch as _watch
        if _watch.is_voice_item(item):
            return "audio"
    except Exception:
        media = (item or {}).get("media") or {}
        if str(media.get("kind") or "").lower() in ("áudio", "audio",
                                                    "mensagem de voz"):
            return "audio"
    return "text"


def group_verdict(item: Any, *, account: str = "", text: Any = None,
                  fresh: bool = True,
                  owner_present: "bool | None" = None,
                  heard: Any = None) -> GroupVerdict:
    """O veredito completo de UMA mensagem de grupo. Silêncio é ``allowed=False``.

    Ordem literal das regras: ler sempre → allow-list legível (falha fechada)
    → dono presente → contato na allow-list → chamou o assistente/persona que
    tem número → é pergunta direta (não letra, não citação, não narração) →
    privacidade do conteúdo. Formato é sempre o espelho.

    ``heard`` é o transcript de uma nota de voz: quando ele vem, o nome, o
    contexto e a privacidade são julgados no que foi OUVIDO (regra do dono para
    áudio, a mesma do chat individual); quando não vem, o veredito para em
    ``needs_transcript`` e quem executa volta aqui depois de transcrever.
    """
    if not isinstance(item, dict):
        return GroupVerdict(reason="sem mensagem")
    group = str(item.get("chat") or "").strip()
    verdict = GroupVerdict(group=group,
                           reply_format=mirror_format(item),
                           sender=sender_jid(item),
                           sender_name=str(item.get("sender_name")
                                           or item.get("name") or ""))
    if not is_group_message(item):
        verdict.reason = "nao e grupo"
        return verdict
    if not fresh:
        verdict.reason = "mensagem antiga (historysync)"
        return verdict
    gate = allowlist_status()
    if not gate["ok"]:
        # Falha FECHADA: sem política legível não se lê grupo nenhum — nem do
        # dono. O chat direto dele continua normal (não passa por aqui).
        verdict.reason = f"allow-list indisponivel: {gate['reason']}"
        return verdict
    present = (owner_present if owner_present is not None
               else owner_present_in_group(group, account))
    verdict.owner_present = present
    if present is not True:
        verdict.reason = ("dono nao esta no grupo: nem vejo o chat"
                          if present is False else "presenca do dono desconhecida")
        return verdict
    if not verdict.sender:
        verdict.reason = "remetente do grupo nao identificado"
        return verdict
    if not is_allowed(verdict.sender):
        verdict.reason = "remetente nao esta na allow-list"
        return verdict
    body = str(item.get("text") if text is None else text or "")
    if verdict.reply_format == "audio" and heard is None:
        # Nota de voz não nomeia ninguém ANTES de ser ouvida: o rótulo de mídia
        # no texto ("[Áudio]") não é conversa. Os gates duros (dono presente,
        # contato na allow-list) já passaram; nome, contexto e privacidade são
        # julgados no transcript, exatamente como no chat individual.
        verdict.needs_transcript = True
        verdict.allowed = True
        verdict.kind = "audio"
        return verdict
    body = str(heard) if heard is not None else body
    who = _addressee(body, item, account)
    verdict.addressed = who["name"]
    if not verdict.addressed:
        verdict.reason = (who["reason"]
                          or "ninguem chamou o assistente nem uma persona")
        return verdict
    verdict.kind = classify_context(body)
    if verdict.kind != "pergunta":
        verdict.reason = {
            "trecho_de_musica": "trecho de musica: conversa alheia",
            "citacao": "citacao/letra entre aspas: o nome e de outro, nao "
                       "uma chamada",
        }.get(verdict.kind, "narracao de conversa alheia")
        return verdict
    privacy = privacy_verdict(body, owner_present_in_chat=True,
                              third_party=not is_owner(verdict.sender),
                              direct_owner_chat=False)
    verdict.privacy = privacy.as_dict()
    verdict.ask_owner = privacy.ask_owner
    if not privacy.allowed:
        verdict.allowed = False
        verdict.reason = privacy.reason or f"privacidade: {privacy.topic}"
        return verdict
    verdict.allowed = True
    verdict.reason = ""
    if privacy.action == "responder_superficial" and privacy.answer:
        verdict.privacy["answer"] = privacy.answer
    return verdict


def owner_present_in_group(group: Any, account: str = "") -> "bool | None":
    """Quem sabe a presença é ``core/whatsapp`` (participantes) ou o cache."""
    return owner_present(chat=group, account=account)


def describe(verdict: GroupVerdict) -> str:
    """Uma linha honesta de log: o que foi decidido e por quê."""
    got = verdict.as_dict()
    return (f"grupo={got.get('group') or '?'} "
            f"quem={got.get('sender_name') or got.get('sender') or '?'} "
            f"formato={got.get('reply_format')} "
            f"contexto={got.get('kind') or '-'} "
            f"decisao={'responde' if got.get('allowed') else 'silencio'}"
            + (f" motivo={got.get('reason')}" if got.get('reason') else ""))


def is_group_allowed(item: Any, *, account: str = "", text: Any = None,
                     fresh: bool = True,
                     owner_present: "bool | None" = None) -> bool:
    """Atalho booleano para ``core/whatsapp_watch`` (mesmo veredito, sem objeto)."""
    return group_verdict(item, account=account, text=text, fresh=fresh,
                         owner_present=owner_present).allowed
