"""Flags de áudio nas linhas do Activity Log (``logs/activity.log``).

O log é texto por linha, uma linha por evento::

    [2026-11-01 17:10:02] Gino: Bom dia, chefe.

Quem lê hoje (core/comm_review.py, scripts, grep do dono) depende desse
prefixo. Por isso as flags vão como um SUFIXO opcional no fim da linha, em
JSON de uma linha só::

    [2026-11-01 17:10:02] Gino: Bom dia, chefe. [audio={"turn":12,...}]

Regras do formato:

* o prefixo ``[data hora] Fonte: texto`` não muda — leitor antigo continua
  vendo a mesma fonte e o mesmo texto (com o sufixo junto, como texto);
* o sufixo é identificado pelo marcador `` [audio={...}]`` no FIM da linha;
  ``strip()`` devolve a linha como ela era antes desta instrumentação;
* ``tts`` só aparece quando o texto falado difere do texto exibido — é
  exatamente a divergência que o dono quer auditar;
* ``lang`` vem de ``core.transcript.detect_lang`` (stopwords, sem modelo):
  ``pt``, ``en`` ou ausente quando não dá para dizer;
* ``sanitized`` é a lista de tipos que ``core.speech_guard`` trocou antes do
  TTS (``tool``, ``path``, ``command``, ``json``, ``lang``, unidos por ``+``
  na ordem canônica). É a medição honesta do vazamento: a revisão de 6 h
  conta a flag em vez de adivinhar pelo texto;
* ``cut`` marca a fala que foi cortada no meio (TTS falhou ou um novo turno
  substituiu a resposta). ``audio_generated=false`` com ``cut=true`` diz
  "começou a sair e parou"; sem ``cut``, "nunca saiu".

Chaves (sempre nesta ordem):

    turn              índice inteiro da vez nesta sessão (rastreável)
    chan              voz | texto | whatsapp | persona | tarefa | sistema
    audio_requested   o áudio DEVERIA ser gerado para esta mensagem?
    audio_generated   o áudio foi de fato sintetizado e enfileirado?
    cut               a fala foi cortada no meio? (só quando sim)
    latency_ms        pergunta → primeiro áudio da vez (inteiro, ms)
    lang              idioma da fala (quando trivial)
    sanitized         tipos de vazamento trocados antes do TTS
    origin            rótulo curto da origem (narração, portão, persona…)
    tts               texto exatamente como foi para o TTS, se difere do exibido

Sem Qt, sem main.py e sem modelo — testável isoladamente.
"""
from __future__ import annotations

import json
import re
from typing import Optional

META_RE = re.compile(r"\s\[audio=(\{.*\})\]\s*$")

_CHANNELS = frozenset({
    "voz", "texto", "whatsapp", "persona", "tarefa", "sistema",
})

# A ordem fixa deixa a linha legível e os testes estáveis.
_KEY_ORDER = ("turn", "chan", "audio_requested", "audio_generated", "cut",
              "latency_ms", "lang", "sanitized", "origin", "tts")

# Os mesmos nomes de core/speech_guard.KINDS; repetidos aqui para o formato do
# log não depender da importação do módulo de regex.
_SANITIZED_KINDS = ("tool", "path", "command", "json", "lang")

_MAX_LATENCY_MS = 10 * 60 * 1000      # 10 min: acima disso é relógio errado


def lang_of(text: str) -> str:
    """Idioma da fala por stopwords ('pt'/'en'/'') — sem modelo local.

    Delega para core/transcript, que já faz exatamente esta conta para
    escolher o idioma da resposta. Falha aqui nunca pode quebrar o log.
    """
    try:
        from core import transcript as _tr
        return _tr.detect_lang(text) or ""
    except Exception:
        return ""


def make_meta(*, turn: Optional[int] = None, chan: str = "",
              requested: Optional[bool] = None,
              generated: Optional[bool] = None,
              cut: Optional[bool] = None,
              latency_ms: Optional[int] = None,
              sanitized: str = "",
              spoken: Optional[str] = None, shown: Optional[str] = None,
              origin: str = "") -> dict:
    """Monta o dicionário de flags; campos vazios/None não entram."""
    meta: dict = {}
    if isinstance(turn, int):
        meta["turn"] = turn
    chan = str(chan or "").strip().lower()
    if chan in _CHANNELS:
        meta["chan"] = chan
    if requested is not None:
        meta["audio_requested"] = bool(requested)
    if generated is not None:
        meta["audio_generated"] = bool(generated)
    if cut:
        meta["cut"] = True
    if latency_ms is not None:
        try:
            ms = int(round(float(latency_ms)))
        except (TypeError, ValueError):
            ms = None
        if ms is not None and 0 <= ms <= _MAX_LATENCY_MS:
            meta["latency_ms"] = ms
    lang = lang_of(spoken if spoken is not None else (shown or ""))
    if lang:
        meta["lang"] = lang
    kinds = sanitized_kinds(sanitized)
    if kinds:
        meta["sanitized"] = kinds
    origin = " ".join(str(origin or "").split())[:40]
    if origin:
        meta["origin"] = origin
    if (spoken is not None and shown is not None
            and str(spoken).strip() and str(spoken) != str(shown)):
        meta["tts"] = str(spoken)
    return {k: meta[k] for k in _KEY_ORDER if k in meta}


def sanitized_kinds(value) -> str:
    """Normaliza ``sanitized`` para ``tipo+tipo`` na ordem canônica.

    Aceita o que vier do main.py (string já montada) e ignora nome
    desconhecido em vez de gravar lixo no log.
    """
    names = [part.strip().lower() for part in str(value or "").split("+")]
    found = [name for name in _SANITIZED_KINDS if name in names]
    return "+".join(found)


def suffix(meta: dict | None) -> str:
    """Sufixo `` [audio={...}]`` (vazio quando não há flag nenhuma)."""
    if not meta:
        return ""
    ordered = {k: meta[k] for k in _KEY_ORDER if k in meta}
    if not ordered:
        return ""
    try:
        blob = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return f" [audio={blob}]"


def with_meta(line: str, meta: dict | None) -> str:
    """Linha do log com as flags no fim (nunca duplica: substitui)."""
    base = strip(str(line or ""))
    return base + suffix(meta)


def strip(line: str) -> str:
    """A mesma linha sem o sufixo — o formato antigo, byte a byte no prefixo."""
    return META_RE.sub("", str(line or ""))


def parse(line: str) -> Optional[dict]:
    """Flags de uma linha, ou None quando a linha não tem (linhas antigas)."""
    m = META_RE.search(str(line or ""))
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def split(line: str) -> tuple[str, Optional[dict]]:
    """(texto exibido, flags) de uma linha — útil para scripts de auditoria."""
    return strip(line), parse(line)
