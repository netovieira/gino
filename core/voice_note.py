"""Nota de voz PTT do WhatsApp — o mecanismo de voz do app, uma vez só.

Ordem do dono (WhatsApp, 07/11/2026 20:4x, literal): "os fluxos tem que usar os
mesmos mecanismos que você, se um gate pode você pode [...] certifique-se que
todas as funções são acessíveis por você em qualquer momento por voz ou texto".
O app já respondia em áudio (``main.JarvisLive._send_whatsapp_answer``); a
superfície de ferramenta não tinha como pedir isso. Este módulo é a correção:
a síntese que estava colada no turno de resposta vira UMA função e as duas
partes a chamam — o turno (``main.py``) e o ``whatsapp_control``
(``action=send_voice``). Não existe segunda implementação de TTS, de Ogg ou de
guarda de formato: quem precisa de nota de voz passa por aqui.

O formato é o que foi medido, não o que parece equivalente:

  PCM 24 kHz mono → WAV → Opus 48 kHz mono em contêiner Ogg, e nenhum silêncio
  interno maior que 100 ms (``smooth_pcm_silence``). Foi a emenda de
  05/11/2026: respostas de ~28 s saíam com 24 buracos acima de 120 ms. Só esse
  arquivo o celular aceita como PTT — um ``.ogg`` renomeado é descartado sem
  erro nenhum, por isso ``is_ptt`` confere por ffprobe e a rota cai para nada
  em vez de mandar silêncio.

A DURAÇÃO não é calculada aqui: quem monta a ``AudioMessage`` é o neonize
(``build_audio_message``, ``.venv/.../neonize/client.py:1706``), que já lê o
container com FFmpeg e põe ``seconds=duration``. É por isso que o envio continua
sendo só ``core.whatsapp.send_media`` — estimar duração por conta própria
criaria a segunda verdade que o dono mandou acabar.

Segredo: nada aqui fala com o WhatsApp de verdade nem com a rede; quem chama
decide o destino.
"""
from __future__ import annotations

import subprocess
import shutil
import uuid
import wave
from pathlib import Path
from typing import Any, Iterable, Sequence

BASE_DIR = Path(__file__).resolve().parent.parent

#: O mesmo lugar que o turno de resposta sempre usou (patchável em teste).
OUT_DIRNAME = ("memory", "whatsapp_media")

#: Acima disto a nota de voz deixa de ser resposta e vira palestra; o limite é
#: o mesmo que o turno de resposta aplicava antes de existir este módulo.
MAX_VOICE_CHARS = 1200

#: 24 kHz é a taxa que os motores de TTS devolvem; o Ogg final sai a 48 kHz.
SAMPLE_RATE = 24000

#: Continua medida (05/11/2026): nenhum silêncio interno passa disto.
MAX_INTERNAL_SILENCE_MS = 100

_OPUS_BITRATE = "48k"
_PTT_SAMPLE_RATE = "48000"


def out_dir() -> Path:
    """O diretório das notas geradas (``memory/whatsapp_media``)."""
    return BASE_DIR.joinpath(*OUT_DIRNAME)


def speaker_names(extra: Iterable[Any] = ()) -> tuple[str, ...]:
    """Nomes que assinam mensagem ESCRITA — e que nunca devem ser falados.

    É o conjunto que o turno de resposta usava: o alto-falante atual do config
    mais as personas cadastradas. O app tem o seu próprio ``_speaker_names``
    (com o ``_asst_name`` da sessão, que pode divergir do config); quem tem
    sessão passa a lista própria — este aqui é o default para chamadas sem
    turno, como a ferramenta.
    """
    from core import whatsapp as _wa
    names = [str(_wa.current_speaker() or "").strip()]
    names.extend(str(n or "").strip() for n in extra)
    try:
        from core import personas as _personas
        names.extend(str(info.get("name") or "").strip()
                     for info in _personas.list_personas())
    except Exception:
        pass
    return tuple(dict.fromkeys(n for n in names if n))


def is_ptt(path: Any) -> bool:
    """O arquivo é Opus mono em contêiner Ogg (o PTT que o WhatsApp aceita)?

    Usa ffprobe quando existe. Sem ffprobe (ambiente mínimo), aceita o
    arquivo: a checagem é uma proteção extra, não um pré-requisito. O ffprobe
    sempre reporta 48 kHz na decodificação de Opus, então a taxa de entrada do
    OpusHead não é verificável por aqui — ela é garantida na geração
    (``-ar 48000``).
    """
    try:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return True
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries",
             "stream=codec_name,sample_rate,channels:format=format_name",
             "-of", "default=noprint_wrappers=1", str(path)],
            capture_output=True, timeout=30, text=True)
        if proc.returncode != 0:
            return False
        info = {}
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                info[key.strip()] = value.strip()
        formats = (info.get("format_name") or "").split(",")
        return (info.get("codec_name") == "opus"
                and info.get("sample_rate") == _PTT_SAMPLE_RATE
                and info.get("channels") == "1"
                and "ogg" in formats)
    except Exception:
        return True


def prepare_text(text: str, names: Sequence[Any] | None = None) -> str:
    """O texto que realmente vai ao motor: sem assinatura e sem vazamento.

    Áudio não assina — a voz já diz quem fala (regra 3 do WhatsApp). O guard
    roda antes do numwords/TTS de propósito: número de telefone e caminho de
    arquivo não podem chegar ao motor nem disfarçados de fala.
    """
    from core import activity as _activity
    from core import speech_guard as _guard
    body = _guard.strip_speaker(str(text or "").strip(),
                                tuple(names) if names is not None
                                else speaker_names())
    body = (_guard.sanitize(body, lang=(_activity.lang_of(body) or "pt")).text
            or body)
    return str(body or "").strip()


def synthesize(text: str, *, names: Sequence[Any] | None = None,
               folder: Path | None = None) -> tuple[str, str]:
    """Texto → nota de voz. Devolve ``(caminho_do_ogg, texto_falado)``.

    ``caminho`` vazio quando não há áudio (motor sem PCM, sem ffmpeg, texto
    grande demais, ou Ogg que não é PTT canônico) — e nesse caso NADA foi
    enviado: quem chama devolve erro, nunca finge sucesso nem troca de
    formato por conta própria. ``texto_falado`` é exatamente o que foi entregue
    ao motor, para a Activity Log registrar a divergência quando o dono auditar.

    Nada aqui toca o WhatsApp: ``core.whatsapp.send_media`` é quem envia, e é
    ele que escolhe o tipo pelo arquivo e monta o AudioMessage PTT com
    mimetype/waveform canônicos (e a duração, que o neonize calcula).
    """
    spoken = ""
    body = prepare_text(text, names)
    if not body or len(body) > MAX_VOICE_CHARS:
        return "", ""
    wav: Path | None = None
    ogg: Path | None = None
    try:
        from core import brain as _b
        from core import numwords as _nw
        from core import whatsapp as _wa
        from memory.config_manager import get_plugin_config
        _wa.ffmpeg_ready()
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return "", ""
        st = _b.settings(get_plugin_config)
        try:
            spoken = _nw.speakable(body, style="hybrid"
                                   if st.get("tts_engine") == "fish"
                                   else "words")
        except Exception:
            spoken = body
        pcm = _b.synth_pcm24k(spoken, st)
        if not pcm:
            return "", spoken
        pcm = _b.smooth_pcm_silence(pcm, SAMPLE_RATE,
                                    max_internal_ms=MAX_INTERNAL_SILENCE_MS)
        destination = Path(folder) if folder is not None else out_dir()
        destination.mkdir(parents=True, exist_ok=True)
        stem = f"reply_{uuid.uuid4().hex}"
        wav = destination / f"{stem}.wav"
        ogg = destination / f"{stem}.ogg"
        with wave.open(str(wav), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(pcm)
        proc = subprocess.run(
            # `-ar 48000 -ac 1` é o que o próprio WhatsApp grava: sem isso o
            # OpusHead sai com input rate 24000 (a taxa do WAV), um desvio do
            # PTT canônico que o celular pode recusar.
            [ffmpeg, "-y", "-v", "error", "-i", str(wav),
             "-c:a", "libopus", "-b:a", _OPUS_BITRATE,
             "-ar", _PTT_SAMPLE_RATE, "-ac", "1", str(ogg)],
            capture_output=True, timeout=90)
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass
        wav = None
        if proc.returncode != 0 or not ogg.exists():
            try:
                ogg.unlink(missing_ok=True)
            except Exception:
                pass
            ogg = None
            return "", spoken
        # O arquivo tem que ser exatamente o que o WhatsApp aceita como PTT
        # (opus, 48 kHz, mono). Um .ogg renomeado/errado seria descartado pelo
        # celular sem erro nenhum — melhor não mandar do que mandar um áudio
        # que ninguém ouve.
        if not is_ptt(ogg):
            try:
                ogg.unlink(missing_ok=True)
            except Exception:
                pass
            ogg = None
            print("[WhatsApp] OGG gerado não é Opus 48 kHz mono — "
                  "a nota de voz não saiu")
            return "", spoken
        result = str(ogg)
        ogg = None
        return result, spoken
    except Exception as e:
        print(f"[WhatsApp] TTS→áudio falhou: {e}")
        return "", spoken
    finally:
        for leftover in (wav, ogg):
            if leftover is not None:
                try:
                    leftover.unlink(missing_ok=True)
                except Exception:
                    pass
