"""Boca: qwen3:4b-instruct-2507-q4_K_M local (Ollama/GPU). Fala, e só.

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
from typing import Any, Callable

from . import guard as _guard
from .config import FlowsConfig
from .router import Decision

SYS_BRIEF = (
    "Você é a BOCA do Gino, um assistente falado em português do Brasil. "
    "Recebe do orquestrador apenas a INTENÇÃO de um pedido do dono e diz UMA "
    "frase curta, seca e ácida, de no máximo 12 palavras, anunciando o que vai "
    "fazer. Comece com gerúndio (Conferindo, Abrindo, Pondo, Anotando, "
    "Procurando) e NUNCA repita a pergunta do dono. "
    "REGRAS INVIOLÁVEIS: você não sabe resultado nenhum; nunca diga feito, "
    "pronto, aberto, tocando, anotado, enviado ou qualquer fato; nunca invente "
    "número, hora, nome ou ferramenta; não use aspas, prefixo ou explicação; "
    "responda só a frase.\n"
    "Exemplos:\n"
    "Intenção: tocar Queen. Alvo: Queen. -> Pondo Queen no Spotify, criador.\n"
    "Intenção: abrir o WhatsApp. Alvo: WhatsApp. -> Abrindo o WhatsApp.\n"
    "Intenção: conferir a hora. Alvo: a hora. -> Conferindo a hora.\n"
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
                  cancel_ev: threading.Event | None = None) -> dict[str, Any]:
    """Chamada streaming ao Ollama; devolve texto, TTFT e 1ª frase."""
    started = time.monotonic()
    out: dict[str, Any] = {"text": "", "ttft_ms": None,
                           "first_sentence_ms": None, "end_ms": None,
                           "error": None, "aborted": False}
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
            return MouthResult(sentence=decision.draft, fixed=True,
                               ms=0.0, ttft_ms=0.0, raw="[brief fixo]")
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
            temperature=0.35, cancel_ev=cancel_ev)
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
            sentence=(sentence or decision.draft),
            ms=float(result.get("first_sentence_ms") or 0.0),
            ttft_ms=float(result.get("ttft_ms") or 0.0),
            raw=str(result.get("text") or ""),
            error=str(result.get("error") or ""),
            fixed=not bool(sentence), guard_tripped=tripped)

    def _recent_lines(self, limit: int = 6) -> str:
        """Últimas falas da conversa, curtas, para a rota livre.

        O provedor é injetado pelo processo vivo (memória de chat). Qualquer
        falha devolve vazio: a boca fala sem história, nunca segura o brief.
        """
        if self.history is None:
            return ""
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
              cancel_ev: threading.Event | None) -> dict[str, Any]:
        try:
            return self.caller(
                messages, max_tokens,
                model=self.config.mouth_model,
                url=self.config.mouth_url,
                timeout_s=self.config.mouth_timeout_s,
                keep_alive=self.config.mouth_keep_alive,
                temperature=temperature,
                early_sentence=early_sentence,
                cancel_ev=cancel_ev)
        except Exception as exc:
            return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                    "end_ms": 0.0, "error": f"{type(exc).__name__}: {exc}",
                    "aborted": False}
