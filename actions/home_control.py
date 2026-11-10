"""Control Home Assistant through its REST API.

This action is deliberately narrow: it reads states, turns devices on/off,
adjusts a few values (brightness, temperature, volume), and activates scenes.
It never exposes Home Assistant configuration, automation deletion or arbitrary
services — those are not reachable from here at all.

The long-lived access token lives in the same vault as the other secrets
(``config/api_keys.json``, mode 600, namespace ``plugin_config.
home_assistant_secrets.token``). It is read at call time and never printed,
logged or returned in an error message.

Name resolution is a machine, not a guess, and it runs in this order:
``(a)`` an explicit alias the owner taught (persistent map in
``config/home_aliases.json``), ``(b)`` the human name in EVERY domain a person
actually names (light, switch, scene, climate, fan, cover, media_player) — not
only the domain the model happened to guess, ``(c)`` loose equality without
accents, punctuation or stopwords, ``(d)`` never a flat denial: if nothing is
found, the similar candidates come back with a question.

The alias map is the answer to the order of 09/11/2026 ("regra do dono vira
arquivo"): "acende a luz do quarto" failed because the only entity named
"Luz do teto" is a *scene*, and the old code only looked in the domain it
inferred from the word "luz". Names the owner teaches must not die in the
conversation, so ``action='apelido'`` writes them to disk and the file is read
from disk on EVERY call — a new alias takes effect without a restart (same
precedent as ``config/flows.json`` and ``config/workflow_gate.json``). The map
holds names and entity ids only; it never contains a secret.

Writes to that map have a gate (decision 2 of the owner, 09/11/2026: "outras
vozes precisam de minha permissão ou autorização momentânea") — see
:func:`_portao_do_apelido`: only his voice, his typed password, a permission
he granted beforehand, or a momentary window he opened may teach a name, and
each entry records who taught it, through which port, and when.

Every network call has a 5 s timeout and every failure becomes a sentence the
assistant can speak instead of a traceback.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import re
import threading
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover - requests is a project dependency
    requests = None

from core import actiongate, voice_grants, voice_identity

BASE_URL = "http://127.0.0.1:8123"
TIMEOUT = 5.0
VAULT_NAMESPACE = "home_assistant_secrets"
VAULT_KEY = "token"
CACHE_TTL = 15.0

BASE_DIR = Path(__file__).resolve().parent.parent
#: Mapa de apelidos do dono: nomes e entity ids, nunca segredo.
DEFAULT_ALIAS_PATH = BASE_DIR / "config" / "home_aliases.json"
ENV_ALIAS_PATH = "JARVIS_HOME_ALIASES"
#: O que o arquivo pode guardar por entrada (falha fechada contra segredo).
#: ``quem`` é rótulo NOMINAL (o dono ou a pessoa), nunca id de voz, embedding,
#: telefone ou dado pessoal; ``fator`` diz o porto exato; ``ensinado_em`` é a
#: data do último ensino (``desde`` segue sendo a primeira vez).
ALIAS_FIELDS = ("apelido", "entidade", "nome", "dominio", "desde", "origem",
                "quem", "fator", "ensinado_em")

# ── Portão de quem ensina apelido (decisão 2 do dono, 09/11/2026) ───────────
#
# "Outras vozes precisam de minha permissão ou autorização momentânea."
# Os mecanismos são os que JÁ existem — nenhum portão paralelo:
#   * fator do dono NA HORA: ``voice_identity.is_owner_turn()`` — voz
#     confirmada por voiceprint OU senha digitada (paridade de 06/11/2026);
#   * permissão prévia EXPLÍCITA: grant de ``core/voice_grants`` (criado só
#     por ele, com ``confirmed_by`` de dono) cobrindo o escopo nominal
#     ``execucao/``:data:`ALIAS_GRANT_SCOPE`;
#   * autorização momentânea NAQUELE turno: a janela de ação de 120 s de
#     ``core/actiongate`` — aberta só pela voz dele ou pela senha dele
#     (``window_factor()`` é vazio quando a janela não é dele).
# Sem nenhum dos três, a escrita é recusada ANTES de qualquer leitura de
# entidade (visitar não usa o ensino como sonda do que existe) e o pedido
# fica estacionado em ``actiongate.mark_pending`` com o rótulo nominal de
# quem pediu — a trilha da falha fechada.
ALIAS_GRANT_SCOPE = "apelidos_da_casa"
#: Porto gravado em ``origem``: fator do dono na hora (voz ou senha — o fator
#: exato fica em ``fator``), grant dele criado antes, ou janela momentânea
#: que ele abriu. Entradas antigas já gravadas como "dono" continuam valendo.
ORIGEM_ALIAS_DONO = "dono"
ORIGEM_ALIAS_GRANT = "permissao_do_dono"
ORIGEM_ALIAS_JANELA = "autorizacao_momentanea"

# Domains the action will toggle. Anything else (sensor, automation, script,
# group, …) is refused before a service call is built.
_TOGGLE_DOMAINS = frozenset({
    "light", "switch", "fan", "climate", "media_player", "humidifier",
    "water_heater", "input_boolean",
})

# Domains counted in the spoken "status" (scenes are not devices).
_DEVICE_DOMAINS = _TOGGLE_DOMAINS | {"cover", "vacuum"}

#: Domains a HUMAN name may live in. Searching only the domain the model
#: guessed is exactly what lost the "luz do quarto" of 09/11/2026: the light is
#: a scene here. A name is looked for in all of them, always.
_SEARCH_DOMAINS = ("light", "switch", "scene", "climate", "fan", "cover",
                   "media_player")

_UNAVAILABLE = frozenset({"unavailable"})
_UNKNOWN = frozenset({"unknown"})

# Words that name a domain in Portuguese and must not be used as a search
# token: "acende a luz da sala" is a light in the sala, not a device literally
# called "luz".
_DOMAIN_WORDS = {
    "luz": "light", "luzes": "light", "lampada": "light", "lampadas": "light",
    "abajur": "light", "abajures": "light", "lustre": "light", "lustres": "light",
    "led": "light", "leds": "light",
    "tomada": "switch", "tomadas": "switch", "interruptor": "switch",
    "interruptores": "switch", "switch": "switch", "plug": "switch",
    "ventilador": "fan", "ventiladores": "fan", "ventoinha": "fan",
    "ar": "climate", "clima": "climate", "condicionado": "climate",
    "termo": "climate", "termostato": "climate", "aquecimento": "climate",
    "tv": "media_player", "televisao": "media_player", "som": "media_player",
    "caixa": "media_player", "speaker": "media_player", "musica": "media_player",
    "cortina": "cover", "cortinas": "cover", "persiana": "cover",
    "persianas": "cover", "portao": "cover", "garagem": "cover",
    "aspirador": "vacuum", "robo": "vacuum",
    "cena": "scene", "cenas": "scene", "scene": "scene",
    "umidificador": "humidifier", "umidade": "humidifier",
}

_STOPWORDS = frozenset({
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "no", "na",
    "nos", "nas", "em", "e", "para", "pra", "por", "com", "meu", "minha",
    "the", "my", "of", "in", "on", "and", "to", "gino", "jarvis",
    "favor", "por", "gentileza",
})

_ACTION_ALIASES = {
    "status": "status", "estado": "status", "situacao": "status",
    "list": "list", "listar": "list", "lista": "list",
    "turn_on": "turn_on", "ligar": "turn_on", "acender": "turn_on",
    "abrir": "turn_on", "liga": "turn_on", "acende": "turn_on",
    "turn_off": "turn_off", "desligar": "turn_off", "apagar": "turn_off",
    "fechar": "turn_off", "desliga": "turn_off", "apaga": "turn_off",
    "set": "set", "ajustar": "set", "ajuste": "set", "ajusta": "set",
    "scene": "scene", "cena": "scene", "ativar_cena": "scene",
    "apelido": "apelido", "registrar_apelido": "apelido",
    "registrar_apelidos": "apelido", "ensinar_apelido": "apelido",
    "ensinar": "apelido", "chamar": "apelido", "renomear": "apelido",
    "nomear": "apelido", "apelid": "apelido",
    "apelidos": "apelidos", "listar_apelidos": "apelidos",
    "quais_apelidos": "apelidos", "mostrar_apelidos": "apelidos",
    "mapa_apelidos": "apelidos",
    "esquecer_apelido": "esquecer_apelido", "esquecer": "esquecer_apelido",
    "remover_apelido": "esquecer_apelido", "desaprender": "esquecer_apelido",
}

_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "states": []}
_alias_lock = threading.Lock()
#: Caminho injetável (teste e vivo). Lido do DISCO a cada chamada: o apelido
#: novo tem de valer sem reiniciar o processo.
_alias_path: Path | None = None


class HomeControlError(Exception):
    """A failure whose message is already safe to show the owner."""


# ── vault / HTTP ─────────────────────────────────────────────────────────────

def _token() -> str:
    """The long-lived token, from the environment or the existing vault."""
    for env_name in ("JARVIS_HA_TOKEN", "HOME_ASSISTANT_TOKEN", "HASS_TOKEN"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()
    try:
        from memory.config_manager import get_plugin_setting
        return str(get_plugin_setting(VAULT_NAMESPACE, VAULT_KEY, "") or "").strip()
    except Exception:
        return ""


def _api(method: str, path: str, payload: dict | None = None):
    """One REST call. Raises HomeControlError with a speakable message."""
    if requests is None:
        raise HomeControlError("A biblioteca requests não está disponível.")
    token = _token()
    if not token:
        raise HomeControlError(
            "Token do Home Assistant não configurado no cofre do JARVIS.")
    url = f"{BASE_URL}{path}"
    kwargs = {
        "headers": {"Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"},
        "timeout": TIMEOUT,
    }
    if payload is not None:
        kwargs["json"] = payload
    try:
        response = requests.request(method, url, **kwargs)
    except requests.Timeout:
        raise HomeControlError(
            f"O Home Assistant não respondeu em {int(TIMEOUT)} segundos.")
    except requests.ConnectionError:
        raise HomeControlError(
            f"Não consegui falar com o Home Assistant em {BASE_URL}.")
    except Exception as e:
        # Exception text from requests never contains the Authorization header.
        raise HomeControlError(f"Falha ao falar com o Home Assistant: {e}")

    if response.status_code in (401, 403):
        raise HomeControlError(
            f"O Home Assistant recusou o token (HTTP {response.status_code}). "
            "Gere outro token e atualize o cofre do JARVIS.")
    if response.status_code == 404:
        raise HomeControlError("Entidade ou serviço não encontrado no Home Assistant.")
    if response.status_code >= 400:
        detail = _error_detail(response)
        raise HomeControlError(
            f"O Home Assistant respondeu HTTP {response.status_code}"
            + (f": {detail}" if detail else "."))
    if not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


def _error_detail(response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            message = body.get("message") or body.get("error")
            if message:
                return " ".join(str(message).split())[:200]
    except Exception:
        pass
    return ""


def _invalidate() -> None:
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["states"] = []


def _states(force: bool = False) -> list[dict]:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["states"]
        if not force and cached and now - float(_cache["at"] or 0.0) < CACHE_TTL:
            return list(cached)
    data = _api("GET", "/api/states")
    if not isinstance(data, list):
        raise HomeControlError("O Home Assistant não devolveu a lista de estados.")
    states = [item for item in data if isinstance(item, dict)
              and isinstance(item.get("entity_id"), str)]
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["states"] = states
    return list(states)


def _call_service(domain: str, service: str, data: dict) -> None:
    _api("POST", f"/api/services/{domain}/{service}", data)
    _invalidate()


# ── names and matching ───────────────────────────────────────────────────────

def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    for char in ("_", "-", "/", ".", ",", ":", ";", "(", ")", "[", "]"):
        text = text.replace(char, " ")
    # Nenhuma pontuação faz parte do nome do aparelho: "Tomada do Escritório!"
    # e "¿luz do quarto?" são os mesmos nomes.
    text = "".join(" " if unicodedata.category(char).startswith("P") else char
                   for char in text)
    return " ".join(text.lower().split())


def _name(entity: dict) -> str:
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    friendly = str(attrs.get("friendly_name") or "").strip()
    return friendly or str(entity.get("entity_id") or "")


def _object_id(entity: dict) -> str:
    return str(entity.get("entity_id") or "").split(".", 1)[-1]


def _domain_of(entity: dict) -> str:
    return str(entity.get("entity_id") or "").split(".", 1)[0].lower()


def _tokens(value: object) -> set[str]:
    return {token for token in _fold(value).split()
            if token not in _STOPWORDS and token not in _DOMAIN_WORDS}


def _infer_domain(value: object) -> str:
    found = ""
    for token in _fold(value).split():
        domain = _DOMAIN_WORDS.get(token)
        if domain and not found:
            found = domain
    return found


def _options(entities: list[dict], limit: int = 5) -> str:
    names = [f"{_name(entity)} ({entity['entity_id']})" for entity in entities[:limit]]
    more = len(entities) - len(names)
    text = ", ".join(names)
    return text + (f" e mais {more}" if more > 0 else "")


def fold_name(value: object) -> str:
    """O nome como chave: sem acento, sem pontuação, sem caixa (uso externo)."""
    return _fold(value)


def _loose(value: object) -> frozenset[str]:
    """O nome sem stopwords: "a luz do quarto" e "luz quarto" são a mesma coisa."""
    return frozenset(token for token in _fold(value).split()
                     if token not in _STOPWORDS)


# ── mapa de apelidos do dono (config/home_aliases.json) ──────────────────────

def configure_aliases(path: "str | os.PathLike[str] | None" = None) -> None:
    """Redireciona o arquivo do mapa (teste e medição); ``None`` volta ao padrão."""
    global _alias_path
    with _alias_lock:
        _alias_path = Path(path) if path not in (None, "") else None


def alias_path() -> Path:
    if _alias_path is not None:
        return _alias_path
    env = os.environ.get(ENV_ALIAS_PATH)
    if env and env.strip():
        return Path(env.strip())
    return DEFAULT_ALIAS_PATH


def _default_alias_doc(now: date | None = None) -> dict:
    return {
        "version": 1,
        "regra": ("nome que o dono usa em fala -> entidade do Home Assistant. "
                  "Regra do dono (09/11/2026): nome ensinado vira arquivo, não "
                  "conversa. Só nomes e entity ids: nenhum segredo."),
        "atualizado_em": (now or date.today()).isoformat(),
        "apelidos": [],
    }


def _read_alias_raw(path: Path) -> tuple[Any, str]:
    """O arquivo como está: ``(documento|None, "ok"|"ausente"|"corrompido")``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "ausente"
    except (OSError, UnicodeDecodeError):
        return None, "corrompido"
    try:
        raw = json.loads(text)
    except ValueError:
        return None, "corrompido"
    if not isinstance(raw, dict) or not isinstance(raw.get("apelidos"), list):
        return None, "corrompido"
    return raw, "ok"


def alias_entries() -> tuple[list[dict], str]:
    """As entradas do mapa lidas do disco AGORA, e o motivo quando faltam.

    ``(entradas, "")`` é o arquivo saudável. ``(vazio, "ausente: ...")`` ou
    ``(vazio, "corrompido: ...")`` nunca derrubam nada: a busca por nome segue
    funcionando, mas o motivo chega na frase e na sonda — esquecimento
    silencioso é o defeito que este mapa existe para matar.
    """
    path = alias_path()
    raw, status = _read_alias_raw(path)
    if status == "ausente":
        return [], f"mapa de apelidos ausente ({path})"
    if status == "corrompido" or raw is None:
        return [], f"mapa de apelidos corrompido ou ilegível ({path})"
    entries: list[dict] = []
    dropped = 0
    for item in raw["apelidos"]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        alias = str(item.get("apelido") or "").strip()
        entity = str(item.get("entidade") or "").strip().lower()
        if not alias or "." not in entity:
            dropped += 1
            continue
        entries.append({
            "apelido": alias,
            "entidade": entity,
            "nome": str(item.get("nome") or "").strip(),
            "dominio": entity.split(".", 1)[0],
            "desde": str(item.get("desde") or ""),
            "origem": str(item.get("origem") or actiongate.ORIGIN_UNDECLARED),
            "quem": str(item.get("quem") or ""),
            "fator": str(item.get("fator") or ""),
            "ensinado_em": str(item.get("ensinado_em") or ""),
        })
    reason = ""
    if dropped:
        reason = f"{dropped} entrada(s) de apelido sem nome ou sem entidade válida"
    return entries, reason


def aliases_status() -> dict:
    """Estado honesto do mapa para a sonda: carrega, quantos nomes, o motivo."""
    path = alias_path()
    entries, warning = alias_entries()
    raw, status = _read_alias_raw(path)
    return {"ok": status == "ok", "arquivo": str(path), "estado": status,
            "apelidos": len(entries), "aviso": warning}


def _alias_key_map(entries: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for entry in entries:
        out.setdefault(_fold(entry["apelido"]), []).append(entry)
    return out


#: Palavras que nomeiam o PEDIDO, não a coisa: sobram na fala e não podem
#: atrapalhar o apelido ("acende a luz do quarto" ensina "luz do quarto").
_REQUEST_WORDS = frozenset(_ACTION_ALIASES) | {
    "deixa", "deixar", "coloca", "colocar", "poe", "pode", "consegue",
    "quero", "gostaria", "agora", "ai", "ja", "vamos", "bora", "momento",
}


def _compact(value: object) -> list[str]:
    """A fala reduzida ao que nomeia a coisa: sem stopwords, sem verbo de pedido."""
    return [token for token in _fold(value).split()
            if token not in _STOPWORDS and token not in _REQUEST_WORDS]


def _contains_sequence(words: list[str], sub: list[str]) -> bool:
    """``sub`` aparece como sequência contígua dentro de ``words``?"""
    if not sub or len(sub) > len(words):
        return False
    return any(words[start:start + len(sub)] == sub
               for start in range(0, len(words) - len(sub) + 1))


def alias_for(query: object, entries: list[dict] | None = None
              ) -> tuple[dict | None, str]:
    """O apelido do dono para este pedido: ``(entrada|None, motivo_recusa)``.

    Três leituras, nesta ordem: igualdade dobrada ("luz do quarto"), igualdade
    frouxa sem stopwords ("acende a luz do quarto") e o apelido dentro da frase
    — esta última só vale quando SOBRA NADA além do verbo e da própria frase do
    apelido, para "a luz da sala" nunca ser respondida com a luz do quarto.
    Um apelido gravado para duas entidades DIFERENTES é conflito: nada é
    escolhido em sorteio — a recusa vem com os dois alvos e a saída (``trocar``).
    """
    entries = list(entries) if entries is not None else alias_entries()[0]
    key = _fold(query)
    if not key:
        return None, ""
    by_key = _alias_key_map(entries)
    got = by_key.get(key)
    if got is None:
        loose = _loose(query)
        if loose:
            for folded, items in by_key.items():
                if _loose(folded) == loose:
                    got = items
                    break
    if got is None:
        words = _compact(query)
        best_len = 0
        for folded, items in by_key.items():
            phrase = [token for token in folded.split()
                      if token not in _STOPWORDS and token not in _REQUEST_WORDS]
            if not phrase or not _contains_sequence(words, phrase):
                continue
            residue = [token for token in words if token not in phrase]
            if residue:
                continue
            if len(phrase) > best_len:
                best_len, got = len(phrase), items
    if not got:
        return None, ""
    targets = {str(entry["entidade"]).lower() for entry in got}
    if len(targets) > 1:
        alvos = ", ".join(sorted(
            f"{entry['apelido']} → {entry['entidade']}" for entry in got))
        return None, (f"o apelido '{query}' está gravado para entidades "
                      f"diferentes ({alvos}); diga qual e registre de novo "
                      "com trocar=true.")
    return got[0], ""


def _write_alias_doc(doc: dict) -> bool:
    """Escrita atômica: arquivo meio gravado seria o mapa corrompido de amanhã."""
    path = alias_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _alias_lock:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
        return True
    except OSError:
        try:
            path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return False


def save_alias(alias: object, entity_id: object, nome: str = "", *,
               origem: str = "", trocar: bool = False,
               now: date | None = None, quem: str = "",
               fator: str = "") -> dict:
    """Grava ``apelido -> entidade``. Idempotente; conflito só muda com trocar.

    Um apelido = uma entrada. Regravar o mesmo nome para a MESMA entidade só
    renova o texto; para outra entidade é conflito e precisa de ``trocar`` —
    sorteio entre dois alvos é exatamente o que o dono não perdoa.

    A origem é o que o portão (:func:`_portao_do_apelido`) apurou de verdade:
    ``origem``/``quem``/``fator`` dizem quem ensinou, por qual porto e com que
    fator, e ``ensinado_em`` a data deste ensino (``desde`` segue a primeira).
    Chamado SEM origem (caminho cru, sem portão), o registro não inventa dono:
    mantém a origem que já havia ou grava ``nao_declarada`` — o mapa nunca
    mente sobre quem ensinou.
    """
    key = str(alias or "").strip()
    entity = str(entity_id or "").strip().lower()
    if not key:
        return {"ok": False, "reason": "diga o apelido (ex.: 'luz do quarto').",
                "entry": None}
    if "." not in entity or " " in entity:
        return {"ok": False,
                "reason": f"'{entity}' não é uma entidade do Home Assistant "
                          "(esperado dominio.nome, ex.: scene.fan_backlight).",
                "entry": None}
    path = alias_path()
    raw, status = _read_alias_raw(path)
    if status == "corrompido":
        # Cobrir um mapa podre por cima descartaria o que o dono já ensinou.
        return {"ok": False,
                "reason": f"o mapa de apelidos em {path} está corrompido: "
                          "conserte o arquivo antes de registrar nomes novos.",
                "entry": None}
    doc = raw if status == "ok" and isinstance(raw, dict) else _default_alias_doc(now)
    entries = [item for item in doc.get("apelidos") or [] if isinstance(item, dict)]
    folded = _fold(key)
    same = [item for item in entries if _fold(item.get("apelido")) == folded]
    clash = [item for item in same
             if str(item.get("entidade") or "").strip().lower() != entity]
    if clash and not trocar:
        alvos = ", ".join(sorted({str(item.get("entidade") or "?").lower()
                                  for item in clash}))
        return {"ok": False,
                "reason": f"'{key}' já aponta para {alvos}; para passar a "
                          f"usar {entity} registre com trocar=true.",
                "entry": None}
    moment = (now or date.today()).isoformat()
    old = same[0] if same else {}
    entry = {"apelido": key, "entidade": entity,
             "nome": (str(nome or "").strip()
                      or str(old.get("nome") or "").strip()),
             "dominio": entity.split(".", 1)[0],
             "desde": str(old.get("desde") or moment),
             "origem": str(origem or old.get("origem")
                           or actiongate.ORIGIN_UNDECLARED),
             "quem": str(quem or old.get("quem") or ""),
             "fator": str(fator or old.get("fator") or ""),
             "ensinado_em": moment}
    others = [item for item in entries
              if _fold(item.get("apelido")) != folded]
    doc["apelidos"] = others + [entry]
    doc["atualizado_em"] = moment
    written = _write_alias_doc(doc)
    return {"ok": written,
            "reason": "" if written
            else f"não consegui gravar o mapa de apelidos em {path}",
            "entry": entry}


def remove_alias(alias: object) -> dict:
    """Esquece um apelido. Sem o arquivo legível, nada é reescrito."""
    key = str(alias or "").strip()
    folded = _fold(key)
    if not folded:
        return {"ok": False, "reason": "diga qual apelido esquecer."}
    path = alias_path()
    raw, status = _read_alias_raw(path)
    if status != "ok" or not isinstance(raw, dict):
        return {"ok": False,
                "reason": f"não há mapa de apelidos legível em {path}."}
    entries = [item for item in raw.get("apelidos") or [] if isinstance(item, dict)]
    kept = [item for item in entries if _fold(item.get("apelido")) != folded]
    if len(kept) == len(entries):
        return {"ok": False, "reason": f"'{key}' não é um apelido registrado."}
    raw["apelidos"] = kept
    raw["atualizado_em"] = date.today().isoformat()
    return {"ok": _write_alias_doc(raw),
            "reason": "" if kept else "apelido esquecido e o mapa ficou vazio",
            "entry": None}


def _entity_by_id(states: list[dict], entity_id: str) -> dict | None:
    want = str(entity_id or "").strip().lower()
    for entity in states:
        if str(entity.get("entity_id") or "").lower() == want:
            return entity
    return None


def _strict_name_hit(name: object, states: list[dict]) -> dict | None:
    """Igualdade EXATA de nome dobrado, sem pontuação e sem acento.

    Usada quando o alvo de um apelido sumiu: o nome antigo ainda pode valer
    para a entidade que nasceu com ele. Semelhança não basta — acertar a
    entidade "mais parecida" seria acender outra coisa e dizer que deu certo.
    """
    folded = _fold(name)
    if not folded:
        return None
    hits = [entity for entity in states
            if folded == _fold(_name(entity))
            or folded == _fold(_object_id(entity))]
    return hits[0] if len(hits) == 1 else None



def _search_pool(raw: str, pool: list[dict], *, bare_ok: bool = True,
                 boost: str = "") -> tuple[dict | None, str, str]:
    """Busca um nome dentro de um pool: ``(entidade, mensagem, "ok"|"ask"|"miss")``.

    ``ask`` é pergunta (tem mais de um / não disse qual) e encerra a rodada;
    ``miss`` é "não está aqui" e autoriza alargar a busca para outro pool.
    Confundir os dois foi o que fez o "luz do quarto" virar "não existe".
    """
    if not pool:
        return None, "", "miss"
    folded = _fold(raw)
    if folded:
        exatas = [entity for entity in pool
                  if folded == _fold(_name(entity))
                  or folded == _fold(_object_id(entity))]
        # Dois domínios com o MESMO nome (light.portao e switch.portao): a
        # resposta é a pergunta, nunca o primeiro da lista — isso seria um
        # chute disfarçado de igualdade exata.
        if len(exatas) == 1:
            return exatas[0], "", "ok"
        if len(exatas) > 1:
            return None, (f"Tem mais de um '{raw}': {_options(exatas)}. "
                          "Qual?"), "ask"

    # (c) igualdade frouxa: sem stopwords — "a luz do quarto" = "luz quarto".
    loose = _loose(raw)
    if loose:
        iguais = [entity for entity in pool
                  if loose == _loose(_name(entity))
                  or loose == _loose(_object_id(entity))]
        if len(iguais) == 1:
            return iguais[0], "", "ok"
        if len(iguais) > 1:
            return None, (f"Tem mais de um '{raw}': {_options(iguais)}. "
                          "Qual?"), "ask"

    query_tokens = _tokens(raw)
    if not query_tokens:
        # Um domínio nu ("luz", "tomada") pede o único aparelho daquele tipo.
        # No pool ALARGADO isso é proibido: "luz" viraria lista de TV e tomada.
        if bare_ok and len(pool) == 1:
            return pool[0], "", "ok"
        if bare_ok and len(pool) > 1:
            return None, (f"Qual deles? {_options(pool)}" if len(pool) <= 5
                          else f"Há {len(pool)} opções nesse tipo; diga o "
                               "nome completo."), "ask"
        return None, "", "miss"

    scored: list[tuple[int, dict]] = []
    for entity in pool:
        candidate = _tokens(_name(entity)) | _tokens(_object_id(entity))
        overlap = len(query_tokens & candidate)
        if overlap == 0:
            continue
        if query_tokens <= candidate:
            score = 1000 - len(candidate)
        else:
            score = overlap * 10 - len(query_tokens - candidate)
        if boost and _domain_of(entity) == boost:
            score += 100
        if score > 0:
            scored.append((score, entity))

    if not scored:
        return None, "", "miss"
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = scored[0][0]
    tied = [entity for score, entity in scored if score == best_score]
    if len(tied) > 1:
        return None, f"Tem mais de um '{raw}': {_options(tied)}. Qual?", "ask"
    return scored[0][1], "", "ok"


def _similar(raw: object, pool: list[dict], limit: int = 4) -> list[dict]:
    """Os candidatos que se parecem com o pedido — a matéria da pergunta final."""
    folded = _fold(raw)
    if not folded or not pool:
        return []
    loose = _loose(raw)
    ranked: list[tuple[float, dict]] = []
    for entity in pool:
        name = _fold(_name(entity))
        obj = _fold(_object_id(entity))
        ratio = max(difflib.SequenceMatcher(None, folded, name).ratio(),
                    difflib.SequenceMatcher(None, folded, obj).ratio())
        shared = len(loose & (_loose(_name(entity)) | _loose(_object_id(entity))))
        score = ratio + 0.35 * shared
        if score >= 0.40:
            ranked.append((score, entity))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [entity for _, entity in ranked[:limit]]


def _miss_message(raw: str, pool: list[dict]) -> str:
    """(d) nunca negar em seco: sem nada, devolvem os parecidos e a pergunta."""
    candidates = _similar(raw, pool)
    if not candidates:
        return (f"Não encontrei '{raw}' no Home Assistant e nada com nome "
                "parecido. Diga o nome completo ou a entity_id.")
    return (f"Não encontrei '{raw}' no Home Assistant. O que mais se parece: "
            f"{_options(candidates, 4)}. Era um desses? Se sim, eu anoto o nome.")


def _hint_other_domains(states: list[dict], domain: str) -> str:
    """O complemento do "Nenhuma entidade do domínio X": o que existe em volta."""
    counts: dict[str, int] = {}
    for entity in states:
        other = _domain_of(entity)
        if other and other != domain:
            counts[other] = counts.get(other, 0) + 1
    if not counts:
        return ""
    resumo = ", ".join(f"{name} ({n})" for name, n in
                       sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6])
    return f" Em outros domínios existe: {resumo} — o nome pode estar aí."


def resolve_entity(query: object, states: list[dict],
                   domain: str | None = None) -> tuple[dict | None, str]:
    """Resolve an entity id or a human name. Returns (entity, error_message).

    O domínio que o modelo CHUTOU ordena a resposta, nunca fecha a porta: se
    nada bate com o chute, a busca continua em todos os domínios que uma pessoa
    nomeia (:data:`_SEARCH_DOMAINS`). Recusa por domínio só existe quando o
    domínio foi PEDIDO de propósito (``domain=`` explícito, ``action='list'``).
    """
    raw = str(query or "").strip()
    if not raw:
        return None, "Diga qual dispositivo (ou passe entity_id)."

    by_id = {str(entity.get("entity_id", "")).lower(): entity for entity in states}
    lowered = raw.lower()
    if lowered in by_id and (not domain or _domain_of(by_id[lowered]) == domain.lower()):
        return by_id[lowered], ""

    explicit = str(domain or "").strip().lower()
    if explicit:
        pool = [entity for entity in states if _domain_of(entity) == explicit]
        if not pool:
            return None, (f"Nenhuma entidade do domínio '{explicit}' no "
                          "Home Assistant." + _hint_other_domains(states, explicit))
        entity, error, kind = _search_pool(raw, pool)
        if entity is not None:
            return entity, ""
        if kind == "ask":
            return None, error
        return None, _miss_message(raw, pool)

    inferred = _infer_domain(raw)
    if inferred:
        narrow = [entity for entity in states
                  if _domain_of(entity) == inferred]
        entity, error, kind = _search_pool(raw, narrow)
        if entity is not None:
            # O chute só fecha a conta se o MESMO nome não existir em outro
            # domínio: escolher light quando scene tem o nome idêntico é o
            # defeito original com uma moeda lançada por cima.
            rivals = [other for other in _equality_hits(raw, _named_pool(states))
                      if other is not entity]
            if rivals:
                return None, (f"Tem mais de um '{raw}': "
                              f"{_options([entity] + rivals)}. Qual?")
            return entity, ""
        if kind == "ask":
            return None, error

    # (b) o chute não achou: o mesmo nome é procurado em todo domínio relevante.
    pool = _named_pool(states)
    entity, error, kind = _search_pool(raw, pool, bare_ok=not inferred,
                                       boost=inferred)
    if entity is not None:
        return entity, ""
    if kind == "ask":
        return None, error
    return None, _miss_message(raw, pool)


def _named_pool(states: list[dict]) -> list[dict]:
    """Os domínios que uma pessoa nomeia ao pedir um aparelho."""
    found = [entity for entity in states if _domain_of(entity) in _SEARCH_DOMAINS]
    return found or list(states)


def _equality_hits(raw: object, pool: list[dict]) -> list[dict]:
    """Toda entidade cujo nome/id bate com o pedido (exata ou sem stopwords)."""
    folded = _fold(raw)
    loose = _loose(raw)
    hits: list[dict] = []
    for entity in pool:
        if folded and (folded == _fold(_name(entity))
                       or folded == _fold(_object_id(entity))):
            hits.append(entity)
        elif loose and (loose == _loose(_name(entity))
                        or loose == _loose(_object_id(entity))):
            hits.append(entity)
    return hits


#: "light.sala" já é uma entidade; "luz do quarto" é fala do dono.
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")


def _vanished_alias_message(entry: dict, states: list[dict],
                           warning: str = "") -> str:
    """Apelido vivo apontando para entidade morta: dito, com os parecidos."""
    alias = entry.get("apelido") or "?"
    target = entry.get("entidade") or "?"
    text = (f"O apelido '{alias}' aponta para {target}, que não existe mais "
            "no Home Assistant.")
    pool = ([entity for entity in states if _domain_of(entity) in _SEARCH_DOMAINS]
            or list(states))
    candidates = _similar(str(entry.get("nome") or "").strip() or alias, pool)
    if candidates:
        text += (f" O que existe de parecido agora: {_options(candidates, 3)}. "
                 "É um desses? Me diga qual e eu regravo o apelido.")
    else:
        text += " Não há nada com nome parecido no Home Assistant agora."
    if warning:
        text += f" (e o mapa está indisponível: {warning})"
    return text


def resolve(query: object, states: list[dict],
            domain: str | None = None) -> tuple[dict | None, str]:
    """A resolução completa, na ordem da ordem do dono.

    ``(a)`` apelido explícito do mapa → ``(b)`` nome humano em todo domínio
    relevante → ``(c)`` igualdade frouxa → ``(d)`` nunca negar em seco. O
    caminho antigo (só o domínio chutado) continua existindo como peça dentro
    de ``(b)``, mas nunca como porta de entrada.
    """
    raw = str(query or "").strip()
    if not raw:
        return None, "Diga qual dispositivo (ou passe entity_id)."
    if _ENTITY_ID_RE.match(raw.lower()):
        return resolve_entity(raw, states, domain=domain)
    entries, warning = alias_entries()
    entry, conflict = alias_for(raw, entries)
    if conflict:
        return None, f"Não apliquei o apelido: {conflict}"
    stale = None
    if entry is not None:
        entity = _entity_by_id(states, entry["entidade"])
        if entity is not None:
            return entity, ""
        # O alvo do apelido sumiu da casa. O NOME ainda vale, mas só por
        # igualdade estrita: trocar de entidade por semelhança seria acender
        # outra coisa e dizer que deu certo.
        stale = entry
        for probe in (raw, str(entry.get("nome") or "").strip()):
            achado = _strict_name_hit(probe, states)
            if achado is not None:
                return achado, ""
        return None, _vanished_alias_message(stale, states, warning)
    entity, error = resolve_entity(raw, states, domain=domain)
    if entity is None and warning:
        error = f"{error} (e o mapa de apelidos está indisponível: {warning})"
    return entity, error


# ── formatting ───────────────────────────────────────────────────────────────

def _is_on(entity: dict) -> bool:
    state = str(entity.get("state") or "").lower()
    domain = _domain_of(entity)
    if domain in ("light", "switch", "fan", "input_boolean", "humidifier",
                  "water_heater"):
        return state == "on"
    if domain == "climate":
        return state not in ("off",) and state not in _UNAVAILABLE | _UNKNOWN
    if domain == "media_player":
        return state not in ("off", "idle", "standby") and state not in _UNAVAILABLE | _UNKNOWN
    if domain == "cover":
        return state in ("open", "opening")
    if domain == "vacuum":
        return state in ("cleaning", "returning")
    return state == "on"


def _names_text(entities: list[dict], limit: int = 8) -> str:
    names = [_name(entity) for entity in entities[:limit]]
    more = len(entities) - len(names)
    return ", ".join(names) + (f" e mais {more}" if more > 0 else "")


def _status(states: list[dict]) -> str:
    devices = [entity for entity in states if _domain_of(entity) in _DEVICE_DOMAINS]
    if not devices:
        return (f"Home Assistant tem {len(states)} entidades, mas nenhuma "
                "controlável (luz, tomada, clima ou similar).")
    on = [entity for entity in devices if _is_on(entity)]
    unavailable = [entity for entity in devices
                   if str(entity.get("state") or "").lower() in _UNAVAILABLE]
    unknown = [entity for entity in devices
               if str(entity.get("state") or "").lower() in _UNKNOWN]
    off = len(devices) - len(on) - len(unavailable) - len(unknown)
    parts = [f"{len(devices)} dispositivos: {len(on)} ligados, {max(0, off)} "
             f"desligados, {len(unavailable)} offline"]
    if unknown:
        parts.append(f"{len(unknown)} sem estado")
    text = "Casa: " + parts[0]
    if len(parts) > 1:
        text += ", " + parts[1]
    text += "."
    if on:
        text += f" Ligados: {_names_text(on)}."
    if unavailable:
        text += f" Offline: {_names_text(unavailable)}."
    if unknown:
        text += f" Sem estado: {_names_text(unknown)}."
    return text


def _list(states: list[dict], domain: str) -> str:
    domain = str(domain or "").strip().lower()
    if domain:
        pool = [entity for entity in states if _domain_of(entity) == domain]
        if not pool:
            # Verdade dita, mas nunca sozinha: "não tem light" não pode mais
            # significar "a casa não tem luz" (09/11/2026).
            return (f"Nenhuma entidade do domínio '{domain}' no Home Assistant."
                    + _hint_other_domains(states, domain)
                    + " Se você procurava um nome, diga-o que eu busco em tudo.")
        lines = [f"{_name(entity)} ({entity['entity_id']}): "
                 f"{entity.get('state', '?')}" for entity in pool[:60]]
        more = len(pool) - len(lines)
        if more > 0:
            lines.append(f"... e mais {more}.")
        return f"{len(pool)} entidade(s) em {domain}:\n" + "\n".join(lines)

    grouped: dict[str, list[dict]] = {}
    for entity in states:
        grouped.setdefault(_domain_of(entity), []).append(entity)
    if not grouped:
        return "O Home Assistant não devolveu entidades."
    lines = []
    for name in sorted(grouped):
        items = grouped[name]
        lines.append(f"{name} ({len(items)}): {_names_text(items, 6)}")
    return f"{len(states)} entidades em {len(grouped)} domínios:\n" + "\n".join(lines)


def _state_label(entity: dict, verb: str) -> str:
    state = str(entity.get("state") or "").lower()
    if state in _UNAVAILABLE:
        return (f"{_name(entity)} está indisponível no Home Assistant "
                f"({verb} enviado, mas o aparelho não está online).")
    return f"{_name(entity)}: {verb}."


# ── actions ──────────────────────────────────────────────────────────────────

def _target(parameters: dict) -> str:
    for key in ("entity_id", "target", "name", "device"):
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _turn(parameters: dict, turn_on: bool) -> str:
    target = _target(parameters)
    if not target:
        return ("Diga qual dispositivo ligar/desligar "
                "(entity_id ou nome, ex.: 'luz da sala').")
    domain = str(parameters.get("domain") or "").strip().lower() or None
    states = _states()
    entity, error = resolve(target, states, domain=domain)
    if entity is None:
        return error
    entity_domain = _domain_of(entity)
    if entity_domain == "scene":
        # "acende a luz do quarto" == ativar a cena que atende por esse nome.
        if not turn_on:
            return (f"{_name(entity)} é uma cena: cena não se desliga, se "
                    "ativa. Diga o que apagar (luz ou tomada) ou qual cena "
                    "ativar no lugar.")
        _call_service("scene", "turn_on", {"entity_id": entity["entity_id"]})
        return f"Cena {_name(entity)} ativada."
    if entity_domain not in _TOGGLE_DOMAINS:
        return (f"Não sei ligar/desligar {_name(entity)} "
                f"(domínio {entity_domain}).")
    service = "turn_on" if turn_on else "turn_off"
    _call_service(entity_domain, service, {"entity_id": entity["entity_id"]})
    return _state_label(entity, "ligado" if turn_on else "desligado")


_LIGHT_KEYS = frozenset({
    "brightness", "brightness_pct", "brightness_step", "brightness_step_pct",
    "color_name", "color_temp", "color_temp_kelvin", "rgb_color", "rgbw_color",
    "rgbww_color", "hs_color", "xy_color", "effect", "flash", "transition",
})
_LIGHT_NUMERIC_KEYS = frozenset({
    "brightness", "brightness_pct", "brightness_step", "brightness_step_pct",
    "color_temp", "color_temp_kelvin", "transition",
})


def _values(parameters: dict) -> dict:
    raw = parameters.get("params", parameters.get("values"))
    values: dict = {}
    if isinstance(raw, dict):
        values.update(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                values.update(parsed)
        except Exception:
            for part in text.replace(";", ",").split(","):
                key, sep, value = part.partition("=")
                if not sep:
                    continue
                key = key.strip().lower()
                value = value.strip()
                try:
                    value = float(value) if "." in value else int(value)
                except ValueError:
                    pass
                values[key] = value
    for key in ("brightness", "brightness_pct", "color_temp_kelvin", "temperature",
                "target_temp", "hvac_mode", "fan_mode", "swing_mode",
                "preset_mode", "position", "volume_level", "priority"):
        if key in parameters and key not in values:
            values[key] = parameters[key]
    if "value" in parameters and "value" not in values:
        values["value"] = parameters["value"]
    return values


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _light_data(entity: dict, values: dict) -> dict | None:
    data = {"entity_id": entity["entity_id"]}
    for key in _LIGHT_KEYS:
        if key not in values:
            continue
        value = values[key]
        if key in _LIGHT_NUMERIC_KEYS:
            number = _number(value)
            if number is None:
                return None
            value = number
        data[key] = value
    if "value" in values and "brightness_pct" not in data and "brightness" not in data:
        level = _number(values["value"])
        if level is not None:
            if 0 <= level <= 100:
                data["brightness_pct"] = level
            elif level <= 255:
                data["brightness"] = level
    return data if len(data) > 1 else None


def _set(entity: dict, values: dict) -> tuple[str, str, dict] | tuple[None, str, None]:
    """(domain, service, data) for one entity, or a clear refusal."""
    domain = _domain_of(entity)
    eid = entity["entity_id"]

    if domain == "light":
        data = _light_data(entity, values)
        if data is None:
            return None, ("Para luz use brightness_pct, color_temp_kelvin, "
                          "rgb_color ou value (0–100)."), None
        return "light", "turn_on", data

    if domain == "climate":
        if any(key in values for key in ("temperature", "target_temp", "value")):
            raw = values.get("temperature", values.get("target_temp",
                                                        values.get("value")))
            temp = _number(raw)
            if temp is None:
                return None, "A temperatura precisa ser um número.", None
            return "climate", "set_temperature", {"entity_id": eid,
                                                  "temperature": temp}
        for key, service in (("hvac_mode", "set_hvac_mode"),
                             ("mode", "set_hvac_mode"),
                             ("fan_mode", "set_fan_mode"),
                             ("swing_mode", "set_swing_mode"),
                             ("preset_mode", "set_preset_mode")):
            if key in values:
                field = "hvac_mode" if key == "mode" else key
                return "climate", service, {"entity_id": eid, field: values[key]}
        return None, ("Para clima use temperature, hvac_mode, fan_mode, "
                      "swing_mode ou preset_mode."), None

    if domain == "media_player":
        if "volume_level" in values or "volume" in values:
            raw = values.get("volume_level", values.get("volume"))
            level = _number(raw)
            if level is None:
                return None, "O volume precisa ser um número.", None
            if level > 1:
                level = level / 100.0
            level = min(1.0, max(0.0, level))
            return "media_player", "volume_set", {
                "entity_id": eid, "volume_level": level}
        if "source" in values:
            return "media_player", "select_source", {
                "entity_id": eid, "source": values["source"]}
        return None, "Para o player use volume_level (0–1) ou source.", None

    if domain == "fan":
        if "percentage" in values or "value" in values:
            raw = values.get("percentage", values.get("value"))
            level = _number(raw)
            if level is None:
                return None, "A velocidade precisa ser um número de 0 a 100.", None
            return "fan", "set_percentage", {
                "entity_id": eid, "percentage": int(min(100, max(0, level)))}
        if "preset_mode" in values or "speed" in values:
            preset = values.get("preset_mode", values.get("speed"))
            return "fan", "set_preset_mode", {
                "entity_id": eid, "preset_mode": preset}
        return None, "Para ventilador use percentage (0–100) ou preset_mode.", None

    if domain == "cover":
        if "position" in values or "value" in values:
            raw = values.get("position", values.get("value"))
            position = _number(raw)
            if position is None:
                return None, "A posição precisa ser um número de 0 a 100.", None
            return "cover", "set_cover_position", {
                "entity_id": eid, "position": int(min(100, max(0, position)))}
        return None, "Para cortina/portão use position (0–100).", None

    if domain == "humidifier":
        raw = values.get("humidity", values.get("target_humidity",
                                                 values.get("value")))
        humidity = _number(raw)
        if humidity is None:
            return None, "A umidade precisa ser um número de 0 a 100.", None
        return "humidifier", "set_humidity", {
            "entity_id": eid, "humidity": int(min(100, max(0, humidity)))}

    if domain == "water_heater":
        raw = values.get("temperature", values.get("value"))
        temp = _number(raw)
        if temp is None:
            return None, "A temperatura precisa ser um número.", None
        return "water_heater", "set_temperature", {
            "entity_id": eid, "temperature": temp}

    return None, f"Não sei ajustar valores de {_name(entity)} (domínio {domain}).", None


def _set_action(parameters: dict) -> str:
    target = _target(parameters)
    if not target:
        return "Diga qual dispositivo ajustar (entity_id ou nome)."
    values = _values(parameters)
    if not values:
        return ("Diga o que ajustar: brightness_pct, temperature, "
                "volume_level, position ou value.")
    domain = str(parameters.get("domain") or "").strip().lower() or None
    entity, error = resolve(target, _states(), domain=domain)
    if entity is None:
        return error
    service_domain, service, data = _set(entity, values)
    if service_domain is None:
        return service  # the refusal sentence
    _call_service(service_domain, service, data)
    return f"{_name(entity)} ajustado ({service})."


def _scene(parameters: dict) -> str:
    name = _target(parameters)
    states = _states()
    scenes = [entity for entity in states if _domain_of(entity) == "scene"]
    if not name:
        if not scenes:
            return ("Nenhuma cena cadastrada no Home Assistant."
                    + _hint_other_domains(states, "scene"))
        return f"Cenas disponíveis: {_options(scenes, 10)}."
    # O nome vem do mapa de apelidos primeiro: "acende a luz do quarto" é cena.
    entity, error = resolve(name, states)
    if entity is None:
        return error
    if _domain_of(entity) != "scene":
        return (f"{_name(entity)} ({entity['entity_id']}) não é cena, é "
                f"{_domain_of(entity)}: para esse use ligar, desligar ou "
                f"ajustar. Cenas que existem: {_options(scenes, 6)}.")
    _call_service("scene", "turn_on", {"entity_id": entity["entity_id"]})
    return f"Cena {_name(entity)} ativada."


# ── o mapa de apelidos pela boca do dono ─────────────────────────────────────

_ALIAS_KEYS = ("alias", "apelido", "nome", "chamar")


def _alias_of(parameters: dict) -> str:
    for key in _ALIAS_KEYS:
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _portao_do_apelido(*, apagar: bool = False) -> dict:
    """QUEM pode escrever no mapa — decisão 2 do dono, 09/11/2026, à letra:
    "outras vozes precisam de minha permissão ou autorização momentânea".

    Consulta, nesta ordem, só mecanismos que JÁ existem (nenhum portão
    paralelo): o fator do dono NA HORA (voz por voiceprint ou senha digitada —
    os dois fatores plenos de ``core/owner_auth``, já refletidos em
    ``voice_identity``); a permissão prévia EXPLÍCITA dele (grant nominal de
    ``core/voice_grants``, que só ele cria); e a autorização momentânea
    NAQUELE turno (a janela de ação de ``core/actiongate``, que só a voz dele
    ou a senha dele abre). APAGAR é caneta dele e de mais ninguém: nem grant
    nem janela substituem o dono na eliminação ("nada apaga apelido existente
    sem ele confirmar").

    Devolve ``{"ok", "origem", "quem", "fator", "resposta"}``. Na recusa,
    ``resposta`` nomeia quem pediu (rótulo nominal) e o pedido fica
    estacionado em ``actiongate.mark_pending`` — a trilha da falha fechada;
    quando o dono autorizar, a janela dele abre e o pedido pode ser repetido
    dentro do prazo. O ``pending`` do dono não é sobrescrito por terceiro.
    """
    snap = voice_identity.recent()
    if voice_identity.is_owner_turn():
        por_texto = str(snap.get("confirmed_by") or "") in (
            "senha_digitada", "owner_password")
        return {"ok": True, "origem": ORIGEM_ALIAS_DONO, "quem": "dono",
                "fator": "owner_password" if por_texto else "voz_do_dono",
                "resposta": ""}
    quem = voice_grants.person_key(snap.get("person") or "")
    if not apagar and quem and voice_grants.allows(
            quem, voice_grants.Scope(voice_grants.SCOPE_EXECUTE,
                                     ALIAS_GRANT_SCOPE)):
        return {"ok": True, "origem": ORIGEM_ALIAS_GRANT, "quem": quem,
                "fator": "permissao_do_dono", "resposta": ""}
    if not apagar and actiongate.window_factor():
        return {"ok": True, "origem": ORIGEM_ALIAS_JANELA,
                "quem": quem or "visitante", "fator": "janela_do_dono",
                "resposta": ""}
    rotulo = voice_identity.describe(snap)
    if apagar:
        return {"ok": False, "origem": actiongate.ORIGIN_UNDECLARED,
                "quem": quem or "visitante", "fator": "",
                "resposta": ("o mapa da casa só se apaga pelo dono — nem "
                             f"permissão antiga nem janela momentânea apagam "
                             f"({rotulo})")}
    if not actiongate.has_pending():
        # Trilha sem pisar no pedido pendente do PROPRIO dono: um slot só.
        actiongate.mark_pending(
            "home_control",
            f"ensinar apelido da casa — {rotulo}")
    return {"ok": False, "origem": actiongate.ORIGIN_UNDECLARED,
            "quem": quem or "visitante", "fator": "",
            "resposta": ("o mapa da casa é ensinado só pelo dono; pedi a "
                         f"autorização dele ({rotulo})")}


def _register_alias(parameters: dict) -> str:
    """Grava ``apelido -> entidade`` no arquivo. A regra do dono vira config.

    O portão vem ANTES da resolução da entidade: visitante não usa o ensino
    como sonda do que existe na casa — e o que o dono ensina pela boca grava
    com a origem real (quem, porto, fator, quando).
    """
    alias = _alias_of(parameters)
    if not alias:
        return ("Diga o apelido e a entidade, ex.: apelido='luz do quarto', "
                "entidade='Luz do teto'.")
    alvo = str(parameters.get("entidade") or parameters.get("entity_id")
               or parameters.get("target") or parameters.get("device")
               or "").strip()
    if not alvo:
        entries, _reason = alias_entries()
        entry, conflict = alias_for(alias, entries)
        if conflict:
            return conflict
        if entry is None:
            return (f"O que é '{alias}'? Diga a entidade (ex.: 'Luz do teto' "
                    "ou scene.fan_backlight) que eu anoto.")
        return (f"'{alias}' está anotado como {entry['nome'] or '?'} "
                f"({entry['entidade']}).")

    portao = _portao_do_apelido()
    if not portao["ok"]:
        return f"Não anotei '{alias}': {portao['resposta']}"
    states = _states()
    entity, error = resolve(alvo, states)
    if entity is None:
        # Prevenir é o ponto: apelido nasce apontando para algo que existe.
        return f"Não anotei '{alias}': {error}"
    trocar = bool(parameters.get("trocar") or parameters.get("replace")
                  or parameters.get("substituir"))
    got = save_alias(alias, entity["entity_id"], _name(entity), trocar=trocar,
                     origem=portao["origem"], quem=portao["quem"],
                     fator=portao["fator"])
    if not got["ok"]:
        return f"Não anotei '{alias}': {got['reason']}"
    entry = got["entry"]
    return (f"Anotado: '{entry['apelido']}' é {entry['nome'] or entry['entidade']} "
            f"({entry['entidade']}). Vale daqui pra frente, sem você repetir.")


def _forget_alias(parameters: dict) -> str:
    """Esquece um apelido — caneta do dono, com confirmação para o existente.

    Ordem fixa: portão (só o fator do dono apaga), depois a pergunta — nada é
    apagado sem ele confirmar, precedente do ``scheduler.cancelar``.
    """
    alias = _alias_of(parameters) or str(_target(parameters) or "").strip()
    portao = _portao_do_apelido(apagar=True)
    if not portao["ok"]:
        return f"Não esqueci '{alias}': {portao['resposta']}"
    confirmar = bool(parameters.get("confirmar") or parameters.get("confirm")
                     or parameters.get("confirma"))
    if not confirmar:
        entries, _reason = alias_entries()
        existente, _conflict = alias_for(alias, entries)
        if existente is not None:
            return (f"Vou esquecer '{alias}' ({existente['entidade']}). "
                    "Confirma? Repita o pedido com confirmação.")
    got = remove_alias(alias)
    if not got["ok"]:
        return f"Não esqueci '{alias}': {got['reason']}"
    return f"'{alias}' não é mais atalho para nada."


def _alias_report(parameters: dict) -> str:
    entries, reason = alias_entries()
    if reason and not entries:
        return f"Sem mapa de apelidos legível: {reason}."
    if not entries:
        return ("Nenhum apelido registrado. Diga 'a luz do quarto é a cena "
                "Luz do teto' que eu anoto.")
    lines = [f"{entry['apelido']} → {entry['nome'] or '?'} "
             f"({entry['entidade']})" for entry in entries[:40]]
    more = len(entries) - len(lines)
    text = f"{len(entries)} apelido(s) da casa:\n" + "\n".join(lines)
    if more > 0:
        text += f"\n... e mais {more}."
    if reason:
        text += f"\nObs.: {reason}"
    return text


def home_control(parameters: dict, player=None, speak=None,
                 session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().lower()
    action = _ACTION_ALIASES.get(action, action)
    try:
        if action == "status":
            return _status(_states())
        if action == "list":
            domain = str(params.get("domain") or "").strip().lower()
            return _list(_states(), domain)
        if action == "turn_on":
            return _turn(params, True)
        if action == "turn_off":
            return _turn(params, False)
        if action == "set":
            return _set_action(params)
        if action == "scene":
            return _scene(params)
        if action == "apelido":
            return _register_alias(params)
        if action == "apelidos":
            return _alias_report(params)
        if action == "esquecer_apelido":
            return _forget_alias(params)
        return (f"Ação desconhecida: '{action}'. Use status, list, turn_on, "
                "turn_off, set, scene, apelido, apelidos ou "
                "esquecer_apelido.")
    except HomeControlError as e:
        return str(e)
    except Exception as e:  # a tool must always answer, never crash the call
        return f"Falha no controle do Home Assistant: {e}"


def auto_authorized(parameters: dict | None = None) -> bool:
    """True for the simple house actions that need no spoken confirmation.

    The bridge uses this as a narrow standing grant: only the actions this
    tool actually implement qualify. Destructive Home Assistant operations
    (deleting automations, editing configuration) are not implemented here at
    all, so there is nothing else to grant.

    ``apelido``/``esquecer_apelido`` stay pre-authorized so the owner's own
    teaching phrase writes on the spot — asking him to confirm his vocabulary
    would be the friction this map exists to remove. That standing grant
    covers DISPATCH only: WHO may write is decided inside the handler by
    :func:`_portao_do_apelido` (decision 2, 09/11/2026 — "outras vozes
    precisam de minha permissão ou autorização momentânea"), so a call from
    any other voice reaches the map's gate and is refused there with a trail.
    """
    action = str((parameters or {}).get("action") or "status").strip().lower()
    return _ACTION_ALIASES.get(action, action) in {
        "status", "list", "turn_on", "turn_off", "set", "scene",
        "apelido", "apelidos", "esquecer_apelido"}


def bridge_preauthorized(name: str, args: dict | None = None) -> bool:
    """The same narrow grant with the bridge's ``(name, args)`` signature."""
    if str(name) != "home_control":
        return False
    return auto_authorized(args if isinstance(args, dict) else {})


TOOL = {
    "name": "home_control",
    "description": (
        "Controls the owner's Home Assistant: reports which devices are on or "
        "offline, lists entities, turns lights/switches/fans/climate/scenes on, "
        "adjusts brightness, temperature, volume or cover position, and "
        "activates scenes. Targets may be an entity_id or a human name in "
        "Portuguese (e.g. 'luz da sala'): the name is looked up in the alias "
        "map first and then in EVERY relevant domain, so never claim 'the house "
        "has no X' from one empty domain. When the owner teaches a name "
        "('a luz do quarto é a cena Luz do teto'), persist it with "
        "action='apelido' — that rule is his, and a name that is not written "
        "down is lost. Only the owner's voice or typed password (or a "
        "permission he granted beforehand, or a momentary window he opened) "
        "may teach a name; other voices are refused with a trail, and only "
        "he erases (action='esquecer_apelido' needs his confirmation for an "
        "existing alias). action='apelidos' lists the map. "
        "Use action='status' for "
        "'quais luzes estão ligadas?' and action='list' with a domain for "
        "'lista as tomadas'. This action cannot change Home Assistant "
        "configuration or delete automations; do not promise that."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("status, list, turn_on, turn_off, set, scene, "
                                "apelido, apelidos or esquecer_apelido."),
                "enum": ["status", "list", "turn_on", "turn_off", "set",
                         "scene", "apelido", "apelidos", "esquecer_apelido"],
            },
            "entity_id": {
                "type": "STRING",
                "description": ("Entity id (e.g. light.luz_da_sala) or human "
                                "name in Portuguese (e.g. 'luz da sala')."),
            },
            "target": {
                "type": "STRING",
                "description": "Alias for entity_id: the device or scene name.",
            },
            "name": {
                "type": "STRING",
                "description": "For action='scene': the scene name.",
            },
            "alias": {
                "type": "STRING",
                "description": ("For action='apelido'/'esquecer_apelido': the "
                                "name the owner uses, e.g. 'luz do quarto'."),
            },
            "entidade": {
                "type": "STRING",
                "description": ("For action='apelido': what the alias points "
                                "to — entity_id or human name, e.g. "
                                "'Luz do teto' or scene.fan_backlight."),
            },
            "trocar": {
                "type": "BOOLEAN",
                "description": ("For action='apelido': replace an alias that "
                                "already points somewhere else."),
            },
            "confirmar": {
                "type": "BOOLEAN",
                "description": ("For action='esquecer_apelido': the owner's "
                                "confirmation to forget an EXISTING alias — "
                                "nothing is erased without it."),
            },
            "domain": {
                "type": "STRING",
                "description": ("Optional domain filter: light, switch, climate, "
                                "media_player, fan, cover, scene."),
            },
            "params": {
                "type": "OBJECT",
                "description": ("For action='set': brightness_pct, color_temp_kelvin, "
                                "temperature, hvac_mode, fan_mode, volume_level, "
                                "position or value."),
            },
            "value": {
                "type": "STRING",
                "description": ("Convenience value for action='set': light 0–100, "
                                "climate temperature, volume 0–100, cover 0–100."),
            },
        },
    },
    "handler": home_control,
}
