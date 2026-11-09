"""Voz desconhecida = VISITANTE com memória própria e permissão mínima.

Ordem literal do dono (09/11/2026), regra 5:

    "Quando alguém que voce não conhece perguntar algo por voz, REGISTRE A VOZ
     COMO MEMORIA PARA O MESMO, E DE PERMISSOES DE VIZITANTE, E SE EU
     PEDIR/AUTORIZAR VOCE PODERA DAR MAIS PERMISSOES A ELE"

Então, quando uma nota de voz chega e a voz NÃO é de ninguém cadastrado
(``core/speaker_id`` devolve "voz desconhecida"), o caminho é:

* guardar o vetor da voz como memória DAQUELA pessoa (o contato — JID/nome do
  grupo, ou o rótulo nominal dado pelo dono), para a próxima vez ela deixar de
  ser "uma voz no ar" e passar a ser "a fulano, que fala assim";
* dar permissões de visitante: pode ser respondido na mesma conversa e pode
  ter memória própria. Não pode NADA privilegiado — nem ação, nem dado do dono,
  nem dado de projeto, nem outro chat, nem escrita do perfil de voz;
* subir o nível SÓ com autorização explícita do dono (:func:`upgrade`, que
  exige voz confirmada ou senha digitada — o mesmo fator de ``core.owner_auth``
  e de ``core.voice_grants``), e o upgrade vira um grant nominal.

Invariantes:
  * vetor de voz nunca sai em log, nunca vira rótulo, nunca vira chave — a
    chave é um rótulo nominal ou o JID normalizado do contato;
  * texto digitado de terceiro jamais chama :func:`register` com privilégio;
    registro de visitante não abre portão nenhum;
  * nada aqui toca o cadastro privilegiado do ``SpeakerId``: visitante não entra
    no conjunto de vozes que o portão de ações escuta;
  * sem fonte de embedding (modelo não instalado, áudio ruim) o registro é
    recusado com motivo — não se inventa identidade de voz.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STORE = BASE_DIR / "memory" / "voice_visitors.json"

LEVEL_VISITOR = "visitante"
LEVEL_AUTORIZADO = "autorizado"

#: Máximo de amostras da mesma pessoa (o teto do cadastro privilegiado é
#: menor; aqui é memória de reconhecimento, não fator de portão).
MAX_SAMPLES = 3

#: Similaridade de corte para "é a mesma voz de um visitante já registrado".
MATCH_THRESHOLD = 0.55

#: Confirmação de dono para criar/elevar: os mesmos fatores de always
#: (``core.owner_auth.FACTORS``), nunca texto solto de terceiro.
CONFIRMED_BY_OWNER = frozenset({"voiceprint_owner", "owner_voiceprint",
                                "senha_digitada", "owner_password",
                                "voz do dono", "senha digitada"})

#: O que um visitante PODE, por padrão. Todo o resto é negado.
VISITOR_PERMISSIONS: dict[str, bool] = {
    "ser_respondido_na_conversa": True,
    "ter_memoria_propria": True,
    "executar_acoes": False,
    "enviar_mensagem_em_nome_do_dono": False,
    "falar_dados_do_dono": False,
    "falar_de_projetos": False,
    "falar_do_que_o_dono_disse": False,
    "ver_tela_arquivos_ou_apps_do_pc": False,
    "escrever_perfil_de_voz": False,
    "mudar_permissoes_de_quer_que_seja": False,
}

_LABEL_MAX = 48

_LOCK = threading.Lock()
_STORE: Path | None = None
_LOG: Callable[[str], None] = lambda _m: None


def configure(*, store: "str | os.PathLike[str] | None" = None,
              log: Callable[[str], None] | None = None) -> None:
    global _STORE, _LOG
    with _LOCK:
        if store is not None:
            _STORE = Path(store)
        if log is not None:
            _LOG = log


def store_path() -> Path:
    return _STORE or DEFAULT_STORE


def _log(msg: str) -> None:
    try:
        _LOG(msg)
    except Exception:
        pass


def _fold(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "")).lower()
    return "".join(c for c in raw if not unicodedata.combining(c))


def _slug(text: Any) -> str:
    got = _fold(text)
    got = re.sub(r"[^a-z0-9]+", "-", got).strip("-")
    return got[:_LABEL_MAX]


def _digits(text: Any) -> str:
    return re.sub(r"\D", "", str(text or ""))


def label_for(*, jid: Any = "", name: Any = "") -> str:
    """A chave da pessoa: rótulo nominal; na falta dele, o contato normalizado.

    Nunca é um vetor, um id de voz ou um dado pessoal além do JID de contato.
    """
    got = _slug(name)
    if got:
        return got
    digits = _digits(jid)
    return f"contato-{digits[-8:]}" if digits else ""


def _load() -> dict[str, Any]:
    path = store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("visitantes"), list):
        return raw
    return {"version": 1, "visitantes": []}


def _save(doc: dict[str, Any]) -> bool:
    path = store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            path.write_text(json.dumps(doc, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        return True
    except OSError as exc:
        _log(f"[Visitantes] registro nao gravado: {type(exc).__name__}: {exc}")
        return False


def _normalize(vec: Any) -> list[float]:
    vals = [float(v) for v in (vec or []) if isinstance(v, (int, float))]
    if not vals:
        return []
    norm = math.sqrt(sum(v * v for v in vals))
    return [v / norm for v in vals] if norm else vals


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def embedding_of(pcm16: Any = None, *, embedding: Any = None) -> list[float]:
    """O vetor da fala: o embedding já calculado, ou o do ``core.speaker_id``.

    Sem áudio e sem vetor devolve ``[]`` — e quem chama registra nada quando o
    vetor vem vazio. Não existe "visitar sem voz": inventar identidade de voz
    é pior do que não registrar.
    """
    if embedding is not None:
        return _normalize(embedding)
    if pcm16 is None:
        return []
    try:
        from core.speaker_id import SpeakerId
        return _normalize(SpeakerId().embedding(pcm16))
    except Exception as exc:
        _log(f"[Visitantes] embedding indisponivel: {exc}")
        return []


def visitors() -> list[dict[str, Any]]:
    return list(_load().get("visitantes") or [])


def find(embedding: Any) -> dict[str, Any] | None:
    """O visitante mais próximo dessa voz acima do corte, ou None."""
    vec = _normalize(embedding)
    if not vec:
        return None
    best: dict[str, Any] | None = None
    best_score = 0.0
    for entry in visitors():
        for sample in entry.get("amostras") or []:
            score = _cosine(vec, _normalize(sample))
            if score > best_score:
                best_score, best = score, entry
    if best is None or best_score < MATCH_THRESHOLD:
        return None
    got = dict(best)
    got["score"] = round(best_score, 4)
    return got


def register(name: Any = "", *, jid: Any = "", pcm16: Any = None,
             embedding: Any = None, group: Any = "", now: dt.datetime | None = None,
             source: str = "voz") -> dict[str, Any]:
    """Registra a voz desconhecida como memória da pessoa, nível visitante.

    Devolve ``{"registered": bool, "reason": str, "entry": dict|None}`` — nunca
    levanta exceção: a conversa com o contato continua enquanto o registro é
    melhor esforço. Se a voz já é de um visitante conhecido, só acrescenta a
    amostra (memória atualizada), sem duplicar a pessoa.
    """
    vec = embedding_of(pcm16, embedding=embedding)
    if not vec:
        return {"registered": False, "reason": "sem vetor de voz", "entry": None}
    label = label_for(jid=jid, name=name)
    if not label:
        return {"registered": False, "reason": "sem rotulo da pessoa",
                "entry": None}
    doc = _load()
    pessoas = doc["visitantes"]
    moment = (now or dt.datetime.now()).isoformat(timespec="seconds")
    hit = find(vec)
    if hit is not None and _slug(hit.get("label")) == _slug(label):
        entry = next((p for p in pessoas
                      if _slug(p.get("label")) == _slug(label)), None)
        if entry is not None:
            entry["amostras"] = (entry.get("amostras") or [])[-(MAX_SAMPLES - 1):]
            entry["amostras"].append(vec)
            entry["updated"] = moment
            doc["visitantes"] = pessoas
            return {"registered": _save(doc), "reason": "amostra nova",
                    "entry": entry}
    entry = {
        "label": label,
        "nome": str(name or "").strip()[:_LABEL_MAX],
        "jid": _digits(jid),
        "nivel": LEVEL_VISITOR,
        "permissoes": dict(VISITOR_PERMISSIONS),
        "amostras": [vec],
        "origem": str(source or "voz"),
        "grupo": str(group or "")[:64],
        "criado_em": moment,
        "updated": moment,
    }
    pessoas.append(entry)
    doc["visitantes"] = pessoas
    ok = _save(doc)
    if ok:
        _log(f"[Visitantes] voz desconhecida registrada como visitante: {label}.")
    return {"registered": ok, "reason": "" if ok else "arquivo nao gravado",
            "entry": entry}


def visitor_of(name: Any = "", *, jid: Any = "") -> dict[str, Any] | None:
    label = _slug(name if str(name or "").strip() else label_for(jid=jid, name=name))
    if not label:
        return None
    for entry in visitors():
        if _slug(entry.get("label")) == label:
            return entry
    return None


def permissions(name: Any = "", *, jid: Any = "") -> dict[str, bool]:
    """O que a pessoa pode. Visitante é a lista mínima; nada é inferido."""
    entry = visitor_of(name, jid=jid)
    if entry is None:
        return dict(VISITOR_PERMISSIONS)
    merged = dict(VISITOR_PERMISSIONS)
    for key, value in (entry.get("permissoes") or {}).items():
        merged[str(key)] = bool(value)
    if str(entry.get("nivel") or LEVEL_VISITOR) != LEVEL_AUTORIZADO:
        # O nível visitante é o teto: um arquivo editado na mão não sobe
        # ninguém por aqui.
        merged.update({k: v for k, v in VISITOR_PERMISSIONS.items()
                       if not v and k not in (entry.get("granted_scopes") or {})})
    return merged


def allows(name: Any = "", *, jid: Any = "", scope: str = "") -> bool:
    return bool(permissions(name, jid=jid).get(str(scope or "")))


def upgrade(name: Any = "", *, jid: Any = "", scope: str = "tudo",
            owner_ok: bool = False, confirmed_by: str = "",
            ttl_seconds: float | None = None) -> dict[str, Any]:
    """Sobe as permissões de um visitante. Só com a palavra do dono.

    ``owner_ok`` e ``confirmed_by`` são a mesma prova dura de
    ``core.voice_grants``: voz confirmada ou senha digitada. O efeito é um
    grant nominal naquela pessoa (persistido no livro de grants, revogável) e o
    nível ``autorizado`` no registro de visitantes — o portão privilegiado
    continua consultando o grant, não este arquivo.
    """
    entry = visitor_of(name, jid=jid)
    if entry is None:
        return {"ok": False, "reason": "visitante nao registrado", "grant": None}
    if not owner_ok or str(confirmed_by or "").strip().lower() not in CONFIRMED_BY_OWNER:
        return {"ok": False, "reason": ("upgrade exige autorizacao explicita do "
                                        "dono (voz confirmada ou senha digitada)"),
                "grant": None}
    try:
        from core import voice_grants as _grants
        grant = _grants.add_grant(entry.get("label"), scope,
                                  owner_ok=True, confirmed_by=confirmed_by,
                                  ttl_seconds=ttl_seconds,
                                  reason="upgrade de visitante por ordem do dono")
        if not grant.get("granted"):
            return {"ok": False, "reason": grant.get("why") or "grant recusado",
                    "grant": grant}
    except Exception as exc:
        return {"ok": False, "reason": f"grant indisponivel: {exc}", "grant": None}
    doc = _load()
    for person in doc["visitantes"]:
        if _slug(person.get("label")) == _slug(entry.get("label")):
            person["nivel"] = LEVEL_AUTORIZADO
            person.setdefault("granted_scopes", []).append(str(scope))
            person["updated"] = dt.datetime.now().isoformat(timespec="seconds")
            entry = person
            break
    ok = _save(doc)
    _log(f"[Visitantes] {entry.get('label')} elevado por {confirmed_by} "
         f"(escopo={scope}).")
    return {"ok": bool(ok), "reason": "" if ok else "arquivo nao gravado",
            "grant": grant, "entry": entry}


def law_line() -> str:
    """A regra como contexto para o modelo, na fala de quem atende."""
    return ("LEI DA VOZ DESCONHECIDA (ordem do dono 09/11/2026): voz que voce "
            "nao conhece vira memoria daquela pessoa com permissao de "
            "VISITANTE - responde na mesma conversa, mais nada. Acao, dado do "
            "dono, dado de projeto e escrita de perfil de voz estao fora ate o "
            "dono autorizar por voz confirmada ou senha digitada.")
