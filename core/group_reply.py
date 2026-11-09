"""Resposta em grupo: o espelho da pergunta, sem tocar no turno do app.

Ordem do dono (09/11/2026), regras 2 e 3: ler grupos e responder a quem está
na allow-list, "como em chats diretos, espelho da pergunta" — áudio entra,
áudio sai; texto entra, texto sai.

Quem DECIDE é ``core/group_policy.group_verdict`` (allow-list, presença do
dono, chamada pelo nome/persona, contexto, privacidade). Este módulo só EXECUTA
o que o veredito liberou:

    texto  -> pergunta -> resposta -> ``core.whatsapp.send`` (assinada pelo
             ponto único da assinatura, ``send``)
    áudio  -> baixar/transcrever -> resposta -> ``core.voice_note.synthesize``
             -> ``core.whatsapp.send_media`` (PTT canônico)

Leis que este arquivo carrega:

* ESPELHO SEM DEGRADAÇÃO (dono, 07/11/2026): áudio que não sintetiza NÃO vira
  texto por omissão. Nada é enviado e a falha é dita no log. Trocar de formato
  sem liberação explícita é o defeito que o dono proibiu.
* NENHUMA FERRAMENTA: a resposta de grupo é gerada SEM o catálogo de tools. Um
  turno de grupo pode falar, não pode agir nem mexer na máquina do dono — é o
  desenho que faz a regra 6 (privacidade) valer mesmo se o modelo quiser.
* PRIVACIDADE: quando o veredito manda pedir permissão, o que sai é a pergunta
  ao dono NO MESMO GRUPO (ordem literal dele), sem repetir o conteúdo sensível.
* VOZ DESCONHECIDA: nota de voz de alguém sem cadastro vira memória da pessoa
  com permissão de visitante (``core/voice_visitors.py``); nada mais.

O texto da resposta vem do cérebro em cascata (``core/brain.chat_stream``) com
as linhas de lei no system. Injetáveis (``set_answerer``, ``set_sender``,
``set_transcriber``) existem para teste e para o app plugar o turno dele no
mesmo ponto sem haver um segundo caminho de decisão.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from typing import Any, Callable

from core import group_policy, voice_note, voice_visitors, whatsapp

_ANSWERER: Callable[..., str] | None = None
_SENDER: Callable[..., Any] | None = None
_MEDIA_SENDER: Callable[..., Any] | None = None
_TRANSCRIBER: Callable[[bytes], str] | None = None
_SYNTHESIZER: Callable[[str], tuple[str, str]] | None = None
_DECODER: Callable[[str], bytes] | None = None
_LOG: Callable[[str], None] = lambda _m: None
_LOCK = threading.Lock()

#: Máximo de caracteres da pergunta que vai ao cérebro. Nota de voz longa de
#: grupo é conversa de terceiros; o começo já diz do que se trata.
MAX_QUESTION_CHARS = 1200

#: O pedido de permissão ao dono nunca repete o conteúdo sensível.
_ASK_OWNER = ("Ei, antes de responder aqui: isso toca em {topic}. "
              "Posso responder ou prefere que eu fique calado?")

#: 16 kHz, mono, 16 bits, 0,6 s. Abaixo disto não existe voz reconhecível.
MIN_VOICEPRINT_BYTES = 16000 * 2 * 6 // 10

_MEDIA_ROLL_CALL = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")


def configure(*, answerer: Callable[..., str] | None = None,
              sender: Callable[..., Any] | None = None,
              media_sender: Callable[..., Any] | None = None,
              transcriber: Callable[[bytes], str] | None = None,
              synthesizer: Callable[[str], tuple[str, str]] | None = None,
              decoder: Callable[[str], bytes] | None = None,
              log: Callable[[str], None] | None = None) -> None:
    """Injeta respondedor/envio/transcrição/síntese/log (testes e wiring)."""
    global _ANSWERER, _SENDER, _MEDIA_SENDER, _TRANSCRIBER, _SYNTHESIZER
    global _DECODER, _LOG
    with _LOCK:
        _ANSWERER = answerer
        _SENDER = sender
        _MEDIA_SENDER = media_sender
        _TRANSCRIBER = transcriber
        _SYNTHESIZER = synthesizer
        _DECODER = decoder
        if log is not None:
            _LOG = log


def reset() -> None:
    """Volta aos mecanismos reais (depois de teste)."""
    configure(answerer=None, sender=None, media_sender=None, transcriber=None,
              synthesizer=None, decoder=None, log=None)


def _log(msg: str) -> None:
    try:
        _LOG(msg)
    except Exception:
        pass


# ── mecanismos reais ────────────────────────────────────────────────────────

def _send_text(group: str, text: str, account: str,
               speaker: str = "") -> tuple[bool, str]:
    if _SENDER is not None:
        return _SENDER(group, text, account, speaker)      # type: ignore[call-arg]
    return whatsapp.send(group, text, account=account,
                         speaker=speaker or None)


def _send_audio(group: str, path: str, account: str) -> tuple[bool, str]:
    if _MEDIA_SENDER is not None:
        return _MEDIA_SENDER(group, path, account)          # type: ignore[call-arg]
    return whatsapp.send_media(group, path, caption="", account=account)


def _decode_pcm(path: str) -> bytes:
    """Arquivo de mídia → PCM 16 kHz mono (o mesmo formato do microfone)."""
    from pathlib import Path
    if not path:
        return b""
    try:
        import time as _t
        deadline = _t.monotonic() + 12.0
        last = -1
        while _t.monotonic() < deadline:
            if Path(path).exists():
                size = Path(path).stat().st_size
                if size > 0 and size == last:
                    break
                last = size
            _t.sleep(0.2)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not Path(path).exists():
            return b""
        whatsapp.ffmpeg_ready()
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-i", path, "-f", "s16le",
             "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
            capture_output=True, timeout=120)
        return proc.stdout or b""
    except Exception as exc:
        _log(f"[Grupos] decodificacao do audio falhou: {exc}")
        return b""


def _transcribe(pcm: bytes) -> str:
    """PCM → texto. Groq quando tem chave; senão o STT local; senão ''."""
    if _TRANSCRIBER is not None:
        return str(_TRANSCRIBER(pcm) or "").strip()
    if not pcm:
        return ""
    try:
        from core import groq_stt
        from memory.config_manager import get_plugin_config
        st = groq_stt.settings(get_plugin_config)
        if st.get("api_key"):
            return str(groq_stt.transcribe(pcm, st["api_key"], st["model"],
                                           st.get("language", "pt"), 16000)
                        or "").strip()
    except Exception as exc:
        _log(f"[Grupos] STT remoto indisponivel: {exc}")
    _log("[Grupos] sem STT configurado (groq sem chave): a nota de grupo nao "
         "foi ouvida e nada sera enviado.")
    return ""


def _brain(question: str, *, sender_name: str, group: str,
           persona: str = "", extra_laws: tuple[str, ...] = ()) -> str:
    """A resposta, pelo cérebro em cascata e SEM ferramentas."""
    if _ANSWERER is not None:
        return str(_ANSWERER(question, sender_name=sender_name, group=group,
                             persona=persona) or "").strip()
    try:
        from core import brain as _b
        from memory.config_manager import get_assistant_name, get_plugin_config
        name = str(get_assistant_name() or "o assistente").strip()
        law = [group_policy.law_line(), voice_visitors.law_line(),
               *extra_laws]
        system = (
            f"Voce e {name}, um assistente de voz. Esta mensagem chegou num "
            f"GRUPO de WhatsApp{f' onde esta {sender_name}' if sender_name else ''}"
            f"{f', e a persona chamada e {persona}' if persona else ''}. "
            "Responda em uma ou duas frases, no idioma da pergunta, sem "
            "emenis, sem markdown. Voce nao tem nenhuma ferramenta aqui: "
            "nao afirma que fez, nem que vai fazer nada. " + " ".join(law)
        )
        st = _b.settings(get_plugin_config)
        sentences: list[str] = []
        text, _calls = _b.chat_stream(
            [{"role": "system", "content": system},
             {"role": "user", "content": question[:MAX_QUESTION_CHARS]}],
            None, st, on_sentence=sentences.append)
        return str(text or " ".join(sentences)).strip()
    except Exception as exc:
        _log(f"[Grupos] cerebro indisponivel: {exc}")
        return ""


# ── o fluxo ─────────────────────────────────────────────────────────────────

def _clean_question(text: Any) -> str:
    """O rótulo de mídia ([Áudio]) não é a pergunta; sobra o que foi dito."""
    body = str(text or "").strip()
    return _MEDIA_ROLL_CALL.sub("", body).strip()


def ask_owner(account: str, item: dict, topic: str) -> bool:
    """Pede permissão ao dono NO MESMO GRUPO, sem repetir o conteúdo."""
    group = str(item.get("chat") or "").strip()
    if not group:
        return False
    line = _ASK_OWNER.format(topic=topic or "assunto delicado")
    ok, phrase = _send_text(group, line, account, "")
    _log(f"[Grupos] permissao pedida ao dono no mesmo chat: "
         f"{'enviada' if ok else 'nao enviada'} ({phrase})")
    return bool(ok)


def respond(account: str, item: dict,
            verdict: group_policy.GroupVerdict | None = None) -> bool:
    """Executa o veredito de grupo. True quando algo foi realmente enviado."""
    account = str(account or "").strip()
    if not isinstance(item, dict):
        return False
    got = verdict or group_policy.group_verdict(item, account=account)
    _log(group_policy.describe(got))
    if not got.allowed:
        if got.ask_owner:
            return ask_owner(account, item, got.privacy.get("topic") or "")
        return False
    group = got.group or str(item.get("chat") or "").strip()
    if not group:
        return False
    question = _clean_question(item.get("text"))
    if got.reply_format == "audio":
        media = item.get("media") or {}
        path = str(media.get("path") or "") or whatsapp.media_dest(item, account)
        pcm = _DECODER(path) if _DECODER is not None else _decode_pcm(path)
        heard = _transcribe(pcm)
        if not heard:
            # Sem ouvir nada não há pergunta: silêncio total, nada é enviado.
            _log("[Grupos] audio nao transcrito — nada enviado.")
            return False
        _register_unknown_voice(pcm, item)
        question = heard
        # O nome, o contexto e a privacidade de uma nota são julgados no que
        # foi OUVIDO (a mesma lei do chat individual): sem o nome no
        # transcript, volta a ser silêncio — e um pedido de dado sensível
        # falado vira pedido de permissão ao dono, não resposta.
        if got.needs_transcript:
            recomputed = group_policy.group_verdict(item, account=account,
                                                    heard=heard,
                                                    owner_present=got.owner_present)
            _log(group_policy.describe(recomputed))
            if not recomputed.allowed:
                if recomputed.ask_owner:
                    return ask_owner(account, item,
                                     recomputed.privacy.get("topic") or "")
                return False
            got = recomputed
    if not question:
        _log("[Grupos] pergunta vazia — nada enviado.")
        return False

    privacy = got.privacy or {}
    if privacy.get("action") == "responder_superficial" and privacy.get("answer"):
        answer = str(privacy["answer"])
    else:
        answer = _brain(question, sender_name=got.sender_name, group=group,
                        persona=got.addressed if got.addressed not in ("", "assistente")
                        else "")
    if not answer:
        # Resposta vazia é silêncio honesto: sem inventar, sem "estou bem".
        _log("[Grupos] o cerebro nao respondeu — nada enviado.")
        return False

    speaker = "" if got.addressed in ("", "assistente") else got.addressed
    if got.reply_format == "audio":
        if _SYNTHESIZER is not None:
            ogg, spoken = _SYNTHESIZER(answer)
        else:
            ogg, spoken = voice_note.synthesize(answer)
        if not ogg:
            # A LEI DO DONO (07/11/2026): audio pedido que nao sai NAO vira
            # texto. Nada é enviado e a falha fica dita.
            _log(f"[Grupos] sintetizacao falhou — a resposta nao saiu em texto "
                 f"por falta de liberacao: nada enviado. ({spoken[:80]})")
            return False
        ok, phrase = _send_audio(group, ogg, account)
        _log(f"[Grupos] resposta em AUDIO para {got.sender_name or group}: "
             f"{'enviada' if ok else 'NAO enviada'} ({phrase}).")
        return bool(ok)
    ok, phrase = _send_text(group, answer, account, speaker)
    _log(f"[Grupos] resposta em TEXTO para {got.sender_name or group}: "
         f"{'enviada' if ok else 'NAO enviada'} ({phrase}).")
    return bool(ok)


def _register_unknown_voice(pcm: bytes, item: dict) -> None:
    """Regra 5 do dono: voz que ele não conhece vira visitante com memória.

    Só registra o ``verdict`` ``unknown`` — rejeição de verdade. Áudio curto
    demais (``indecisive_short``) ou microfone sem cadastro nenhum
    (``no_profile``) não permitem afirmar que a voz é nova, e inventar
    identidade de voz seria pior que não registrar.
    """
    if len(pcm or b"") < MIN_VOICEPRINT_BYTES:
        # Menos de ~0,6 s de fala em 16 kHz mono não é voiceprint: um estalo,
        # um "ah" ou um fragmento não podem virar identidade de ninguém.
        return
    try:
        from core.speaker_id import SpeakerId
        speaker = SpeakerId()
        vec = speaker.embedding(pcm)
        if not vec:
            return
        got = speaker.identify(vec, include_auto=True) or {}
        if str(got.get("verdict") or "") != "unknown":
            return
        result = voice_visitors.register(name=item.get("sender_name") or "",
                                         jid=item.get("sender") or "",
                                         embedding=vec,
                                         group=item.get("chat") or "",
                                         source="whatsapp-grupo")
        if not result.get("registered"):
            _log(f"[Grupos] voz de visitante nao registrada: "
                 f"{result.get('reason')}")
    except Exception as exc:
        _log(f"[Grupos] registro de visitante indisponivel: {exc}")
