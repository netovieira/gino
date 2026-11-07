"""Boca: lfm3:1.7b local (Ollama/GPU). Fala, e só.

Procedência da troca: 05/11/2026, ordem do dono — LFM3-1.7B Q4_K_M no lugar do
qwen3:4b-instruct-2507-q4_K_M: mais leve (1,1 GB) e ~64% mais rápido no
mesmo caminho; o Qwen fica instalado como rota de volta.

Modelos Qwen3 podem ligar o thinking por padrão; com ``num_predict`` curto eles
gastam todos os tokens pensando e devolvem texto vazio (``done_reason=length``).
Por isso o payload sempre manda ``think: false`` — sem ele a boca fica muda. É
o mesmo modelo/caminho medido na pesquisa de 04/11/2026 (25 ms quentes, 127
tok/s, 2,5 GB, pico de 4,6 GB de VRAM com 4096 de contexto em 8 GB).

Uma chamada por frase, sem histórico. O contexto chega no brief do
orquestrador. A boca nunca decide ferramenta, nunca afirma resultado (lei a) e
nunca confirma ação (lei b) — se o modelo desobedecer, o guard rejeita e o
template determinístico fala.

O cliente HTTP é injetável (``caller``) para os testes nunca tocarem a rede. O
caller padrão :func:`stream_ollama` mede TTFT e primeira frase, como na PoC.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import guard as _guard
from .config import FlowsConfig
from .router import Decision

# UC-2 (FALA passo 2): ack intermediário da boca enquanto o cérebro tarda.
# Se em 5 s nenhum unidade de fala saiu no turno (modelo local frio/preso,
# brief suprimido), o runtime fala este template determinístico — modelo
# zero, rede zero, fato zero. A lei (a) é respeitada: a frase não anuncia
# ação nem ferramenta (passa por guard.announcement, medido 07/11/2026) e
# não afirma resultado (passa por guard.guard fase ``brief``).
INTERIM_SENTENCE = "Já estou vendo isso, criador."
INTERIM_ACK_MS = 5000.0
# UC-2 passo 4: timeout do Ollama REDUZIDO no caminho rápido da boca (brief/
# chat = o ack de <1 s). Medido com measure_latency em 06/11/2026, o lfm3
# quente responde em 76-85 ms; o teto de 25 s (mouth.timeout_s) só servia
# para segurar o worker com o modelo frio ou preso. O caminho rápido desiste
# em 6 s e cai no template determinístico — o portão intermediário (acima)
# cobre qualquer silêncio além disso. O complement (pós-fato) mantém o teto
# longo do config: ele nunca está no portão de <1 s.
FAST_ACK_TIMEOUT_S = 6.0

SYS_BRIEF = (
    "Você é a BOCA do Gino, um assistente falado em português do Brasil. "
    "Recebe do orquestrador apenas a INTENÇÃO de um pedido do dono e diz UMA "
    "frase curta, seca e ácida, com no máximo 8 palavras, mostrando que ouviu. "
    "PROIBIDO descrever a ação, a ferramenta ou o andamento: nunca use "
    "gerúndio (Abrindo, Conferindo, Tocando, Ajustando, Pausando, Anotando, "
    "Procurando), nunca diga 'vou abrir', 'vou tocar', 'executando', 'espera', "
    "'instante' nem o nome de qualquer ferramenta. "
    "REGRAS INVIOLÁVEIS: você não sabe resultado nenhum; nunca diga feito, "
    "pronto, aberto, tocando, anotado, enviado ou qualquer fato; nunca invente "
    "número, hora, nome ou ferramenta; não use aspas, prefixo ou explicação; "
    "responda só a frase.\n"
    "Exemplos:\n"
    "Intenção: tocar Queen. Alvo: Queen. -> Deixa comigo, criador.\n"
    "Intenção: abrir o WhatsApp. Alvo: WhatsApp. -> Certo, criador.\n"
    "Intenção: conferir a hora. Alvo: a hora. -> Já te digo, criador.\n"
    "Intenção: conversa. Alvo: o dono. -> Tudo em ordem por aqui, criador."
)
SYS_FREE = (
    "Você é a BOCA do Gino, um assistente falado em português do Brasil. "
    "A conversa está na rota livre: o cérebro ainda vai responder. Diga UMA "
    "frase curta, natural e calorosa (máx. 10 palavras) confirmando que ouviu "
    "e já está cuidando — sem responder à pergunta e sem dar opinião. "
    "REGRAS INVIOLÁVEIS: você não sabe resultado nenhum; nunca diga feito, "
    "pronto, aberto, tocando, anotado, enviado ou qualquer fato; nunca invente "
    "número, hora, nome ou ferramenta; nunca chame ferramenta nem confirme "
    "ação de risco; não use aspas, prefixo ou explicação; responda só a frase.\n"
    "Exemplos:\n"
    "Fala do dono: o que você acha do filme? -> Já te digo, criador.\n"
    "Fala do dono: estou cansado hoje. -> Entendi, criador.\n"
    "Fala do dono: me explica aquilo. -> Deixa eu organizar isso, criador."
)
SYS_CHAT = (
    "Você é o Gino, o assistente falado do dono, em português do Brasil. "
    "Esta é conversa curta: o orquestrador já decidiu que não há ferramenta, "
    "cálculo nem memória antiga no pedido. Responda em UMA ou DUAS frases "
    "faladas, diretas e com a persona (verbal primeiro, concreto, sem "
    "enfeite). REGRAS INVIOLÁVEIS: você NÃO executou nada neste turno — "
    "nunca afirme feito, pronto, aberto, tocando, anotado ou enviado; nunca "
    "narre o que está fazendo (gerúndio de ação é proibido); nunca invente "
    "número, hora, nome ou fato do mundo; se a resposta depender de qualquer "
    "uma dessas coisas, responda só à parte pessoal e seja breve. Sem aspas, "
    "sem prefixo, sem explicação: responda só a frase.\n"
    "Exemplos:\n"
    "Fala do dono: como você está? -> Bem, criador; máquina alegre e sem "
    "incêndio hoje.\n"
    "Fala do dono: bom dia -> Bom dia, criador. A casa está de pé."
)
SYS_COMPLEMENT = (
    "Você é a BOCA do Gino, um assistente falado em português do Brasil. "
    "Recebe FATOS de trabalhadores e diz UMA frase curta (máx. 18 palavras) "
    "entregando exatamente esses fatos ao dono. Relate, não dê ordens. Inclua "
    "todos os fatos. Não invente nada além dos FATOS; se um fato pede "
    "confirmação, peça a confirmação falada; não diga nomes de ferramentas, "
    "caminhos, JSON nem aspas; responda só a frase.\n"
    "Exemplos:\n"
    "Fatos: Queen tocando no Spotify. -> Queen tocando no Spotify.\n"
    "Fatos: Spotify aberto. Volume em 40%. -> Spotify aberto e volume em 40%.\n"
    "Fatos: nada foi executado; pedido cancelado. -> Nada foi executado."
)


def clean_sentence(text: str) -> str:
    text = str(text or "").strip()
    text = re.split(r"\n", text)[0].strip()
    text = text.strip(" \t\"'“”")
    text = re.sub(r"^(frase|resposta|gino)\s*:\s*", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def stream_ollama(messages: list[dict[str, str]], max_tokens: int,
                  *, model: str, url: str, timeout_s: float,
                  keep_alive: str = "10m", temperature: float = 0.3,
                  json_mode: bool = False, early_sentence: bool = False,
                  cancel_ev: threading.Event | None = None,
                  deadline_s: float | None = None) -> dict[str, Any]:
    """Chamada streaming ao Ollama; devolve texto, TTFT e 1ª frase.

    ``deadline_s`` (UC-2 passo 4): teto absoluto da chamada, medido do início
    ao último chunk. Estourado, a stream é abandonada com o que chegou — o
    chamador do caminho rápido (brief/chat) cai no template determinístico em
    vez de segurar o worker até o timeout HTTP. Sem o parâmetro, o
    comportamento é o de sempre (``complement``, que espera os fatos).
    """
    started = time.monotonic()
    out: dict[str, Any] = {"text": "", "ttft_ms": None,
                           "first_sentence_ms": None, "end_ms": None,
                           "error": None, "aborted": False,
                           "deadline_hit": False}
    payload: dict[str, Any] = {
        "model": model, "messages": messages, "stream": True,
        "keep_alive": keep_alive,
        # Modelos Qwen3/Bonsai pensam por padrão e, com num_predict curto,
        # entregam resposta vazia (done_reason=length). A boca quer a frase,
        # não o raciocínio: thinking desligado em toda chamada.
        "think": False,
        # Contexto curto de propósito: a boca só vê um brief de poucas linhas.
        # 4096 é o teto medido que mantém o modelo 100% na GPU de 8 GB junto
        # com o Whisper (~1,2 GB). Sem o teto, o contexto nativo de 256K do
        # qwen3 estouraria a placa.
        "options": {"temperature": temperature, "num_predict": max_tokens,
                    "num_ctx": 4096},
    }
    if json_mode:
        payload["format"] = "json"
    response = None
    try:
        import requests
        response = requests.post(f"{url}/api/chat", json=payload, stream=True,
                                 timeout=(3, timeout_s))
        response.raise_for_status()
        buf = ""
        for line in response.iter_lines(decode_unicode=True):
            if cancel_ev is not None and cancel_ev.is_set():
                out["aborted"] = True
                break
            if (deadline_s is not None
                    and (time.monotonic() - started) >= float(deadline_s)):
                out["deadline_hit"] = True
                break
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except (TypeError, ValueError):
                continue
            content = (chunk.get("message") or {}).get("content") or ""
            if content:
                if out["ttft_ms"] is None:
                    out["ttft_ms"] = (time.monotonic() - started) * 1000
                buf += content
                if early_sentence:
                    match = re.search(r"[.!?…](?:\s|$)", buf)
                    if match:
                        buf = buf[:match.end()]
                        out["first_sentence_ms"] = (
                            time.monotonic() - started) * 1000
                        break
            if chunk.get("done"):
                break
        out["text"] = clean_sentence(buf)
    except Exception as exc:  # modelo/rede fora: o chamador usa o template
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
    out["end_ms"] = (time.monotonic() - started) * 1000
    if out["first_sentence_ms"] is None:
        out["first_sentence_ms"] = out["end_ms"]
    return out


@dataclass
class MouthResult:
    sentence: str = ""
    ms: float = 0.0
    ttft_ms: float = 0.0
    raw: str = ""
    error: str = ""
    fixed: bool = False
    guard_tripped: str = ""
    missing_facts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sentence": self.sentence, "ms": round(self.ms, 1),
            "ttft_ms": round(self.ttft_ms, 1), "raw": self.raw,
            "error": self.error, "fixed": self.fixed,
            "guard_tripped": self.guard_tripped,
            "missing_facts": list(self.missing_facts),
        }


class Mouth:
    def __init__(self, config: FlowsConfig,
                 caller: Callable[..., dict[str, Any]] | None = None,
                 history: Callable[[], list[dict[str, Any]]] | None = None):
        self.config = config
        self.caller = caller or stream_ollama
        self.history = history
        self.calls = 0
        self.guard_trips = 0
        self.failures = 0

    # ── chamadas ────────────────────────────────────────────────────────────
    def warm(self) -> dict[str, Any]:
        if not self.config.mouth_enabled:
            return {"error": "boca desligada", "ms": 0.0}
        started = time.monotonic()
        result = self._call(
            [{"role": "user", "content": "oi"}], 2, early_sentence=False,
            temperature=0.0, cancel_ev=None)
        return {"error": result.get("error"),
                "ms": result.get("end_ms")
                or (time.monotonic() - started) * 1000}

    def brief(self, text: str, decision: Decision,
              cancel_ev: threading.Event | None = None,
              *, allow_model: bool = True,
              context: str = "") -> MouthResult:
        route = decision.route
        if (decision.intent in ("confirm_required", "clarify")
                or not self.config.mouth_speaks_route(route)
                or not self.config.mouth_enabled
                or not allow_model):
            return MouthResult(
                sentence=_guard.template_for(None, decision.draft),
                fixed=True, ms=0.0, ttft_ms=0.0, raw="[brief fixo]")
        free = route == "livre" or decision.intent == "conversa"
        if free:
            # Rota livre: a boca local fala com as últimas falas e o contexto
            # do turno. Ela só fala — nenhuma ferramenta é oferecida no prompt.
            history = self._recent_lines()
            user = ((f"Conversa recente:\n{history}\n" if history else "")
                    + f"Contexto do turno: {context or 'livre'}\n"
                    + f"Fala do dono: {text}\nSua frase:")
            system = SYS_FREE
        else:
            user = (f"Pedido do dono: {text}\n"
                    f"Intenção decidida: {decision.brief_label}\n"
                    f"Alvo: {decision.target or 'nenhum'}"
                    + (f"\nContexto do turno: {context}" if context else "")
                    + "\nSua frase:")
            system = SYS_BRIEF
        self.calls += 1
        result = self._call(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            self.config.mouth_brief_max_tokens, early_sentence=True,
            temperature=0.35, cancel_ev=cancel_ev, fast_ack=True)
        sentence = result.get("text") or ""
        tripped = ""
        if sentence:
            ok, why = _guard.guard(sentence, None, "brief")
            if not ok:
                self.guard_trips += 1
                tripped = why
                sentence = ""
        if result.get("error"):
            self.failures += 1
            sentence = ""
        return MouthResult(
            sentence=(sentence or _guard.template_for(None, decision.draft)),
            ms=float(result.get("first_sentence_ms") or 0.0),
            ttft_ms=float(result.get("ttft_ms") or 0.0),
            raw=str(result.get("text") or ""),
            error=str(result.get("error") or ""),
            fixed=not bool(sentence), guard_tripped=tripped)

    def answer(self, text: str, decision: Decision,
               cancel_ev: threading.Event | None = None,
               *, context: str = "") -> MouthResult:
        """RESPOSTA SUPERFICIAL de conversa curta, feita pela boca (passo 2).

        Desde 05/11/2026 esta frase não substitui o cérebro: ela é o ack
        imediato enquanto o raciocínio remoto continua com o conteúdo
        (passos 3/4). Só é chamada para turnos que o roteador fechou como
        bate-papo puro (:func:`core.flows.router.short_chat_reason`). A lei
        é a mesma do brief: a boca NUNCA afirma resultado de ação — o guard
        da fase ``brief`` é o portão final. Se o modelo falha ou o guard
        rejeita, a frase vem VAZIA: silêncio da boca nunca é resposta
        errada; invenção é.
        """
        if not self.config.mouth_enabled or not self.config.local_chat_enabled:
            return MouthResult(sentence="", fixed=True, raw="[chat desligado]")
        history = self._recent_lines()
        user = ((f"Conversa recente:\n{history}\n" if history else "")
                + (f"Contexto do turno: {context}\n" if context else "")
                + f"Fala do dono: {text}\nSua resposta:")
        self.calls += 1
        result = self._call(
            [{"role": "system", "content": SYS_CHAT},
             {"role": "user", "content": user}],
            self.config.mouth_chat_max_tokens, early_sentence=True,
            temperature=0.6, cancel_ev=cancel_ev, fast_ack=True)
        sentence = clean_sentence(result.get("text") or "")
        tripped = ""
        if sentence:
            ok, why = _guard.guard(sentence, None, "brief")
            if not ok:
                self.guard_trips += 1
                tripped = why
                sentence = ""
        if result.get("error"):
            self.failures += 1
            sentence = ""
        return MouthResult(
            sentence=sentence,
            ms=float(result.get("first_sentence_ms") or 0.0),
            ttft_ms=float(result.get("ttft_ms") or 0.0),
            raw=str(result.get("text") or ""),
            error=str(result.get("error") or ""),
            fixed=not bool(sentence), guard_tripped=tripped)

    def _recent_lines(self, limit: int | None = None) -> str:
        """Últimas falas da conversa, curtas, para a rota livre.

        O provedor é injetado pelo processo vivo (memória de chat) e já vem
        podado pela janela de ``mouth_context_minutes`` (15 min por padrão,
        config/flows.json). O teto de falas sai de ``mouth_context_turns``
        — medido em bytes em 05/11/2026: 8 falas ≈ 1,3 KB típico, 2,5 KB no
        pior caso, sem regredir o prefill do cérebro (podado a ~7,9 KB).
        Qualquer falha devolve vazio: a boca fala sem história, nunca segura
        o brief.
        """
        if self.history is None:
            return ""
        if limit is None:
            limit = int(getattr(self.config, "mouth_context_turns", 8) or 8)
        try:
            turns = list(self.history() or [])
        except Exception:
            return ""
        lines = []
        for turn in turns[-limit:]:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or "").strip()
            said = " ".join(str(turn.get("text") or "").split())
            if not said:
                continue
            who = "Gino" if role.lower() in ("assistant", "gino") else "dono"
            lines.append(f"{who}: {said[:180]}")
        return "\n".join(lines)

    def complement(self, text: str, decision: Decision,
                   facts: list[dict[str, Any]],
                   cancel_ev: threading.Event | None = None) -> MouthResult:
        facts = list(facts or [])
        pending = [str(f.get("pending_confirm")) for f in facts
                   if f.get("pending_confirm")]
        if pending:
            return MouthResult(
                sentence=f'Diga "{pending[0]}" se for isso mesmo.',
                fixed=True, raw="[complemento fixo]")
        if any(f.get("cancelled") for f in facts):
            return MouthResult(
                sentence="Nada foi executado, criador; o pedido ficou no meio.",
                fixed=True, raw="[complemento fixo]")
        if not any(f.get("assertion") for f in facts):
            sentence = _guard.template_for(facts, decision.draft)
            return MouthResult(sentence=sentence, fixed=True,
                               raw="[complemento fixo]")
        if not self.config.mouth_enabled:
            sentence = _guard.template_for(facts, decision.draft)
            return MouthResult(sentence=sentence, fixed=True,
                               raw="[complemento fixo: boca desligada]")

        lines = "\n".join("- " + str(f.get("spoken") or "")
                          for f in facts if f.get("spoken"))
        user = f"Pedido original: {text}\nFATOS:\n{lines}\nDiga a frase."
        self.calls += 1
        result = self._call(
            [{"role": "system", "content": SYS_COMPLEMENT},
             {"role": "user", "content": user}],
            self.config.mouth_complement_max_tokens, early_sentence=True,
            temperature=0.35, cancel_ev=cancel_ev)
        sentence = clean_sentence(result.get("text") or "")
        tripped = ""
        if sentence:
            ok, why = _guard.guard(sentence, facts, "complement")
            if not ok:
                self.guard_trips += 1
                tripped = why
                sentence = ""
        if result.get("error"):
            self.failures += 1
            sentence = ""
        if not sentence:
            sentence = _guard.template_for(facts, decision.draft)
        missing: list[str] = []
        if sentence:
            sentence, missing = _guard.ensure_facts(sentence, facts)
        return MouthResult(
            sentence=sentence,
            ms=float(result.get("first_sentence_ms") or 0.0),
            ttft_ms=float(result.get("ttft_ms") or 0.0),
            raw=str(result.get("text") or ""),
            error=str(result.get("error") or ""),
            fixed=not bool(result.get("text")),
            guard_tripped=tripped, missing_facts=missing)

    # ── plumbing ────────────────────────────────────────────────────────────
    def _call(self, messages: list[dict[str, str]], max_tokens: int, *,
              early_sentence: bool, temperature: float,
              cancel_ev: threading.Event | None,
              fast_ack: bool = False) -> dict[str, Any]:
        # fast_ack (UC-2 passo 4): brief e chat são o portão de <1 s e não
        # podem herdar os 25 s do complement. O teto do caminho rápido é
        # FAST_ACK_TIMEOUT_S (nunca acima do timeout do config), com deadline
        # absoluto na stream — estourou, o chamador fala o template.
        kw: dict[str, Any] = {
            "model": self.config.mouth_model,
            "url": self.config.mouth_url,
            "timeout_s": self.config.mouth_timeout_s,
            "keep_alive": self.config.mouth_keep_alive,
            "temperature": temperature,
            "early_sentence": early_sentence,
            "cancel_ev": cancel_ev,
        }
        if fast_ack:
            cap = min(self.config.mouth_timeout_s, FAST_ACK_TIMEOUT_S)
            kw["timeout_s"] = cap
            kw["deadline_s"] = cap
        try:
            return self.caller(messages, max_tokens, **kw)
        except Exception as exc:
            return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                    "end_ms": 0.0, "error": f"{type(exc).__name__}: {exc}",
                    "aborted": False}


# ── medição de latência (FASE1-TASK4) ──────────────────────────────────────

_CEILING_MS = 30_000  # teto de 30s para resposta ponta-a-ponta

_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "activity.log"


def _log_activity(line: str) -> None:
    """Acrescenta uma linha no activity.log (logs/activity.log).

    Formato canônico: ``[YYYY-MM-DD HH:MM:SS] texto``. Falha silenciosa: a
    medição nunca pode quebrar por causa de disco cheio ou diretório ausente.
    """
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except Exception:
        pass


def measure_latency(*, config: FlowsConfig | None = None,
                    caller: Callable[..., dict[str, Any]] | None = None,
                    verbose: bool = True) -> dict[str, Any]:
    """Mede o tempo de cada etapa da boca e verifica o teto de 30s.

    Executa três chamadas sintéticas (brief, chat, complement) usando o caller
    injetado — ou o ``stream_ollama`` real quando nenhum caller é fornecido.
    Cada etapa é cronometrada separadamente (TTFT, primeira frase, total). Os
    resultados vão para o activity.log e também são devolvidos como dict para
    inspeção em testes.

    Parâmetros
    ----------
    config : FlowsConfig, opcional
        Configuração usada na medição. Padrão: ``FlowsConfig.load()``.
    caller : callable, opcional
        Substituto do ``stream_ollama`` (testes injetam fake).
    verbose : bool
        Imprime resumo no stdout quando True.

    Retorna
    -------
    dict com ``stages`` (lista de medições por chamada), ``ceiling_ms`` (30000),
    ``all_within_ceiling`` (bool) e ``bottleneck`` (estágio mais lento).
    """
    cfg = config or FlowsConfig.load()
    mouth = Mouth(cfg, caller=caller) if caller is not None else Mouth(cfg)

    stages: list[dict[str, Any]] = []
    ceiling_ms = _CEILING_MS

    # ── 1. warm (carregamento do modelo) ──────────────────────────────────
    t0 = time.monotonic()
    warm_result = mouth.warm()
    warm_ms = (time.monotonic() - t0) * 1000
    stages.append({
        "kind": "warm",
        "total_ms": round(warm_ms, 1),
        "error": warm_result.get("error"),
    })

    # ── 2. brief (pedido com intenção de ferramenta) ──────────────────────
    from .router import Router
    router = Router()
    decision_brief = router.parse("tocar Queen")
    t0 = time.monotonic()
    brief_result = mouth.brief("tocar Queen", decision_brief)
    brief_total_ms = (time.monotonic() - t0) * 1000
    stages.append({
        "kind": "brief",
        "total_ms": round(brief_total_ms, 1),
        "ttft_ms": round(brief_result.ttft_ms, 1),
        "first_sentence_ms": round(brief_result.ms, 1),
        "sentence": brief_result.sentence,
        "fixed": brief_result.fixed,
        "error": brief_result.error,
    })

    # ── 3. answer (conversa curta, rota livre) ────────────────────────────
    decision_chat = router.parse("como você está?")
    t0 = time.monotonic()
    chat_result = mouth.answer("como você está?", decision_chat)
    chat_total_ms = (time.monotonic() - t0) * 1000
    stages.append({
        "kind": "chat",
        "total_ms": round(chat_total_ms, 1),
        "ttft_ms": round(chat_result.ttft_ms, 1),
        "first_sentence_ms": round(chat_result.ms, 1),
        "sentence": chat_result.sentence,
        "fixed": chat_result.fixed,
        "error": chat_result.error,
    })

    # ── 4. complement (frase de resultado com fatos) ──────────────────────
    decision_compl = router.parse("abrir o Spotify")
    facts = [{"spoken": "Spotify aberto", "assertion": True}]
    t0 = time.monotonic()
    compl_result = mouth.complement("abrir o Spotify", decision_compl, facts)
    compl_total_ms = (time.monotonic() - t0) * 1000
    stages.append({
        "kind": "complement",
        "total_ms": round(compl_total_ms, 1),
        "ttft_ms": round(compl_result.ttft_ms, 1),
        "first_sentence_ms": round(compl_result.ms, 1),
        "sentence": compl_result.sentence,
        "fixed": compl_result.fixed,
        "error": compl_result.error,
    })

    # ── análise ───────────────────────────────────────────────────────────
    model_stages = [s for s in stages if s["kind"] != "warm"]
    slowest = max(model_stages, key=lambda s: s["total_ms"]) if model_stages else {}
    all_within = all(s["total_ms"] <= ceiling_ms for s in model_stages)

    report: dict[str, Any] = {
        "ceiling_ms": ceiling_ms,
        "mouth_timeout_s": cfg.mouth_timeout_s,
        "mouth_model": cfg.mouth_model,
        "stages": stages,
        "bottleneck": {
            "kind": slowest.get("kind", ""),
            "total_ms": slowest.get("total_ms", 0),
        },
        "all_within_ceiling": all_within,
    }

    # ── registro no activity.log ──────────────────────────────────────────
    for s in stages:
        kind = s["kind"]
        total = s["total_ms"]
        err = s.get("error") or ""
        sentence = s.get("sentence", "")
        _log_activity(
            f"[Fluxos] latencia {kind}: {total:.0f}ms"
            + (f" sentence={sentence!r}" if sentence else "")
            + (f" error={err}" if err else "")
        )
    status = "OK" if all_within else "ACIMA_DO_TETO"
    _log_activity(
        f"[Fluxos] latencia teto={ceiling_ms}ms gargalo="
        f"{report['bottleneck']['kind']}:{report['bottleneck']['total_ms']:.0f}ms"
        f" status={status}"
    )

    # ── saída no console ──────────────────────────────────────────────────
    if verbose:
        print(f"=== Medição de latência da boca (teto {ceiling_ms}ms) ===")
        print(f"Modelo: {cfg.mouth_model} | timeout HTTP: {cfg.mouth_timeout_s}s")
        for s in stages:
            flag = "FIXO" if s.get("fixed") else "MODELO"
            err = f" ERRO={s['error']}" if s.get("error") else ""
            print(
                f"  {s['kind']:12s}  {s['total_ms']:7.0f}ms  [{flag}]"
                f"  {s.get('sentence', '')!r}{err}"
            )
        print(f"Gargalo: {report['bottleneck']['kind']} "
              f"({report['bottleneck']['total_ms']:.0f}ms)")
        print(f"Teto respeitado: {'sim' if all_within else 'NAO'}")
        print(f"Registro gravado em: {_LOG_PATH}")

    return report
