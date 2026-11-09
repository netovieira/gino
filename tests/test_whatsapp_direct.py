"""WhatsApp direct chat: text→text, audio→audio, no room TTS, exact chat."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import main
from core import whatsapp_watch

# Transcrição longa no tamanho real do áudio de 236 s do dono (~4 mil
# caracteres). O fim é uma instrução única: se o caminho cortar em 1500, o
# teste falha porque a cauda desaparece do turno entregue ao modelo.
_LONG_TRANSCRIPT = " ".join(
    f"Item {i}: revisar o andamento do projeto, conferir os números e "
    f"registrar as decisões combinadas com a equipe, sem deixar nenhum "
    f"detalhe de fora."
    for i in range(1, 28)
) + (" ÚLTIMA INSTRUÇÃO: envie o relatório completo para o meu e-mail e "
     "confirme quando terminar.")


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)


class _Memory:
    def __init__(self):
        self.turns = []

    def append_turn(self, role, text):
        self.turns.append((role, text))


class _Loop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class DirectChatTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_live_enabled = True
        obj._qoder_live_ready = lambda: True
        obj._asst_name = "Gino"
        obj.ui = _UI()
        obj._chat_memory = _Memory()
        obj._last_user_speech = 0.0
        obj._qoder_turn = mock.Mock()
        obj._qoder_hold = mock.Mock()
        obj._begin_user_epoch = mock.Mock(return_value={"new_subject": False})
        obj._memory_record_user = mock.Mock()
        obj._transcribe_whatsapp_audio = mock.Mock(return_value="transcrição do áudio")
        obj._wa_stt_lock = threading.Lock()
        return obj

    def test_direct_text_goes_to_qoder_as_whatsapp_and_text_reply(self):
        obj = self._obj()
        item = {"chat": "5511999999999", "name": "Anthero", "text": "oi Gino"}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=True):
            self.assertTrue(obj._whatsapp_autoreply("trabalho", item))
        obj._qoder_turn.assert_called_once()
        args, kwargs = obj._qoder_turn.call_args
        self.assertIn("CHAT DIRETO DO DONO", args[0])
        self.assertEqual(kwargs["source"], "whatsapp")
        self.assertFalse(kwargs["speak"])
        self.assertEqual(kwargs["media_type"], "text")
        self.assertEqual(kwargs["wa_account"], "trabalho")
        self.assertEqual(kwargs["wa_chat"], "5511999999999")
        obj._memory_record_user.assert_called_once_with("oi Gino")
        self.assertTrue(any(line.startswith("WHATSAPP YOU:")
                            for line in obj.ui.logs))

    def test_direct_audio_is_transcribed_and_replies_audio(self):
        obj = self._obj()
        item = {"chat": "5511999999999", "name": "Anthero", "text": "",
                "media": {"kind": "áudio", "ext": ".ogg"}}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=True), \
                mock.patch("core.whatsapp.media_path", return_value="/tmp/x.ogg"):
            self.assertTrue(obj._whatsapp_autoreply("trabalho", item))
        obj._transcribe_whatsapp_audio.assert_called_once_with("/tmp/x.ogg")
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["media_type"], "audio")
        self.assertFalse(kwargs["speak"])
        self.assertIn("transcrição do áudio", obj._qoder_turn.call_args[0][0])

    def test_audio_download_not_finished_yet_still_waits_for_the_path(self):
        """O caso de 02/11: o watcher rodou antes da thread de download.

        ``media_path`` só responde quando o arquivo existe; o destino esperado
        é estável e é o que faz a transcrição esperar em vez de desistir.
        """
        obj = self._obj()
        item = {"chat": "5511999999999", "name": "Anthero", "text": "",
                "media": {"kind": "mensagem de voz", "ext": ".ogg"}}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=True), \
                mock.patch("core.whatsapp.media_path", return_value=""), \
                mock.patch("core.whatsapp.download_wanted", return_value=True), \
                mock.patch("core.whatsapp.media_dest",
                           return_value="/tmp/esperado.ogg"):
            self.assertTrue(obj._whatsapp_autoreply("trabalho", item))
        obj._transcribe_whatsapp_audio.assert_called_once_with("/tmp/esperado.ogg")
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["media_type"], "audio")

    def test_audio_with_downloads_disabled_does_not_wait(self):
        obj = self._obj()
        item = {"chat": "5511999999999", "name": "Anthero", "text": "",
                "media": {"kind": "mensagem de voz", "ext": ".ogg"}}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=True), \
                mock.patch("core.whatsapp.media_path", return_value=""), \
                mock.patch("core.whatsapp.download_wanted", return_value=False), \
                mock.patch("core.whatsapp.media_dest") as dest:
            self.assertTrue(obj._whatsapp_autoreply("trabalho", item))
        dest.assert_not_called()
        obj._transcribe_whatsapp_audio.assert_not_called()
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["media_type"], "audio")

    def test_direct_audio_long_transcript_reaches_the_brain_whole(self):
        """O áudio de 236 s do dono não pode perder a cauda em 1500.

        O bug real de 02/11: a transcrição inteira existia, mas o turno
        entregue ao modelo parava em 1500 caracteres e as últimas
        instruções do dono sumiam em silêncio.
        """
        self.assertGreater(len(_LONG_TRANSCRIPT), 3900)
        obj = self._obj()
        obj._transcribe_whatsapp_audio = mock.Mock(
            return_value=_LONG_TRANSCRIPT)
        item = {"chat": "5511999999999", "name": "Anthero", "text": "",
                "media": {"kind": "mensagem de voz", "ext": ".ogg"}}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=True), \
                mock.patch("core.whatsapp.media_path",
                           return_value="/tmp/x.ogg"):
            self.assertTrue(obj._whatsapp_autoreply("trabalho", item))
        prompt = obj._qoder_turn.call_args[0][0]
        self.assertIn("CHAT DIRETO DO DONO", prompt)
        # A transcrição completa, não uma fatia: cauda e instrução final.
        self.assertIn(f'"{_LONG_TRANSCRIPT}"', prompt)
        self.assertIn("ÚLTIMA INSTRUÇÃO: envie o relatório completo", prompt)
        self.assertNotIn("texto abreviado", prompt)

    def test_third_party_long_message_is_shortened_but_announced(self):
        """Teto de terceiro pode ficar; o corte tem que ser DITO, nunca mudo."""
        obj = self._obj()
        long_text = "a" * 3000 + " FIM QUE PRECISA SER ANUNCIADO"
        item = {"chat": "5511888888888", "name": "Vida", "text": long_text}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=False):
            self.assertTrue(obj._whatsapp_autoreply("principal", item))
        prompt = obj._qoder_turn.call_args[0][0]
        self.assertIn("MENSAGEM DE TERCEIRO", prompt)
        self.assertIn("[texto abreviado:", prompt)
        self.assertIn("caracteres omitidos]", prompt)
        self.assertNotIn("FIM QUE PRECISA SER ANUNCIADO", prompt)

    def test_third_party_is_external_not_owner(self):
        obj = self._obj()
        item = {"chat": "5511888888888", "name": "Vida",
                "text": "Gino, tudo bem?"}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=False):
            self.assertTrue(obj._whatsapp_autoreply("principal", item))
        obj._memory_record_user.assert_not_called()
        self.assertIn(("system", "[WhatsApp de Vida] Gino, tudo bem?"),
                      obj._chat_memory.turns)
        _, kwargs = obj._qoder_turn.call_args
        self.assertEqual(kwargs["media_type"], "text")
        self.assertIn("MENSAGEM DE TERCEIRO", obj._qoder_turn.call_args[0][0])

    def test_group_is_judged_by_the_verdict_never_a_blind_refusal(self):
        """LEI NOVA (ordem do dono, 09/11/2026 16:40): "AGORA VOCE VAI PODER
        LER GRUPOS, MAS RESPONDER APENAS PESSOAS AUTORIZADAS" — decisão 11 do
        plano do vault, que revogou a regra 4 dele próprio de 01/11 e
        02/11/2026 ("grupo nunca é lido nem respondido").

        O teste anterior ``test_group_is_never_answered_not_even_with_the_name``
        codificava a regra morta: exigia ``ui.logs == []``, ou seja, recusa
        CEGA antes de qualquer julgamento. Sob a lei nova o grupo nomeado
        AINDA NÃO vira turno — o veredito de ``core/group_policy`` recusa
        (aqui, sem allow-list nem dono provado) — mas a recusa agora é dita
        com motivo numa linha de rastro ``event=grupo_turno``, e é isso que
        se prova: nada de ``_qoder_turn``, nada de memória, E o motivo
        registrado. O caso positivo (grupo autorizado que vira turno) mora em
        ``tests/test_main_group_turn.py``.
        """
        obj = self._obj()
        item = {"chat": "120363000000000000@g.us",
                "name": "grupo 120363000000000000",
                "text": "Gino, olha isso", "group": True}
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=False):
            self.assertFalse(obj._whatsapp_autoreply("trabalho", item))
        obj._qoder_turn.assert_not_called()
        obj._qoder_hold.assert_not_called()
        obj._memory_record_user.assert_not_called()
        rastros = [ln for ln in obj.ui.logs if "event=grupo_turno" in ln]
        self.assertEqual(len(rastros), 1,
                         f"sem linha de rastro do veredito: {obj.ui.logs}")
        self.assertIn("decisao=recusado", rastros[0])
        self.assertIn("motivo=", rastros[0])

    def test_owner_echo_naming_the_assistant_is_an_order_in_that_chat(self):
        """Regra 5 do SKILL (04/11/2026): eco do dono COM o nome no chat de
        um terceiro é ordem e é respondida no MESMO chat. Incidente
        05/11/2026 22:59: o ramo antigo de ``main._whatsapp_autoreply``
        engolia todo eco de terceiro, com ou sem nome — comportamento que
        o teste anterior registrava e que a lei do dono cancelou."""
        obj = self._obj()
        item = {"chat": "5511888888888", "name": "Carol",
                "text": "Gino, fala pra Carol como você se sente",
                "from_me": True}
        prev = whatsapp_watch.trigger_name()
        self.addCleanup(whatsapp_watch.set_assistant_name, prev)
        whatsapp_watch.set_assistant_name("Gino")
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=False):
            self.assertTrue(obj._whatsapp_autoreply("trabalho", item))
        obj._qoder_turn.assert_called_once()
        instruction = obj._qoder_turn.call_args[0][0]
        self.assertIn("ORDEM DO DONO NO CHAT DE UM TERCEIRO", instruction)
        kwargs = obj._qoder_turn.call_args[1]
        self.assertEqual(kwargs["wa_chat"], "5511888888888")  # o MESMO chat
        self.assertEqual(kwargs["media_type"], "text")
        self.assertFalse(kwargs["speak"])

    def test_owner_echo_without_the_name_is_still_silence(self):
        """Sem o nome, eco continua eco: nada de turno, nada de envio."""
        obj = self._obj()
        item = {"chat": "5511888888888", "name": "Carol",
                "text": "boa noite, já chega por hoje",
                "from_me": True}
        prev = whatsapp_watch.trigger_name()
        self.addCleanup(whatsapp_watch.set_assistant_name, prev)
        whatsapp_watch.set_assistant_name("Gino")
        with mock.patch.object(whatsapp_watch, "is_direct", return_value=False):
            self.assertFalse(obj._whatsapp_autoreply("trabalho", item))
        obj._qoder_turn.assert_not_called()
        obj._qoder_hold.assert_not_called()
        self.assertEqual(obj._chat_memory.turns, [])


class TranscribeWaitTest(unittest.TestCase):
    """A transcrição espera o arquivo do download ficar completo."""

    class _Listener:
        def __init__(self, text="conta uma piada para mim"):
            self.text = text
            self.calls = 0

        def transcribe_pcm(self, pcm):
            self.calls += 1
            return self.text

    def _obj(self, listener):
        obj = object.__new__(main.JarvisLive)
        obj._groq = listener
        obj._wa_stt_lock = threading.Lock()
        return obj

    def test_waits_for_the_file_that_the_download_thread_creates(self):
        listener = self._Listener()
        obj = self._obj(listener)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "voz.ogg")

            def slow_download():
                time.sleep(0.35)
                Path(path).write_bytes(b"OggS" + b"\x00" * 64)

            thread = threading.Thread(target=slow_download)
            thread.start()
            fake = mock.Mock(returncode=0, stdout=b"\x00\x00" * 1600)
            with mock.patch("core.whatsapp.ffmpeg_ready"), \
                    mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                    mock.patch("subprocess.run", return_value=fake):
                started = time.monotonic()
                text = obj._transcribe_whatsapp_audio(path)
            thread.join(timeout=2)
        self.assertEqual(text, "conta uma piada para mim")
        self.assertEqual(listener.calls, 1)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)


class SendAnswerTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._synthesize_whatsapp_audio = mock.Mock(return_value="")
        return obj

    def test_text_answer_uses_send(self):
        obj = self._obj()
        with mock.patch("core.whatsapp.send", return_value=(True, "ok")) as send:
            self.assertTrue(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "text"))
        send.assert_called_once_with("5511999999999", "resposta",
                                     account="trabalho", speaker="Gino")

    def test_audio_answer_uses_send_media_and_never_duplicates_text(self):
        obj = self._obj()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as handle:
            path = handle.name
        obj._synthesize_whatsapp_audio = mock.Mock(return_value=path)
        with mock.patch("core.whatsapp.send_media",
                        return_value=(True, "ok")) as send_media, \
                mock.patch("core.whatsapp.send") as send:
            self.assertTrue(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "audio"))
        send_media.assert_called_once()
        # Nota de voz: sem legenda e sem `*Gino:*` — áudio não assina.
        _args, _kwargs = send_media.call_args
        self.assertEqual(_args[2], "")
        self.assertEqual(_kwargs, {"account": "trabalho"})
        send.assert_not_called()
        self.assertFalse(Path(path).exists())

    def test_audio_that_does_not_synthesize_sends_nothing_by_default(self):
        # Lei do dono (07/11/2026): áudio pedido que não sai NÃO vira texto
        # por omissão. Sem a flag, nada é enviado e a falha volta como False.
        obj = self._obj()
        obj._synthesize_whatsapp_audio = mock.Mock(return_value="")
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch("core.whatsapp.send_media") as send_media, \
                mock.patch("core.whatsapp.send") as send:
            with contextlib.redirect_stdout(buf):
                self.assertFalse(obj._send_whatsapp_answer(
                    "trabalho", "5511999999999", "resposta", "audio"))
        send_media.assert_not_called()
        send.assert_not_called()
        self.assertFalse(obj._wa_audio_sent)
        logged = buf.getvalue()
        self.assertIn("não foi liberada", logged)
        self.assertIn("nada foi enviado", logged)

    def test_tts_that_raises_sends_nothing_by_default(self):
        # Motor de voz fora do ar: o texto não entra no lugar por acidente.
        obj = self._obj()
        obj._synthesize_whatsapp_audio = mock.Mock(
            side_effect=RuntimeError("motor de voz fora"))
        with mock.patch("core.whatsapp.send_media") as send_media, \
                mock.patch("core.whatsapp.send") as send:
            self.assertFalse(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "audio"))
        send_media.assert_not_called()
        send.assert_not_called()
        self.assertFalse(obj._wa_audio_sent)

    def test_audio_that_fails_to_send_sends_nothing_by_default(self):
        obj = self._obj()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as handle:
            path = handle.name
        obj._synthesize_whatsapp_audio = mock.Mock(return_value=path)
        with mock.patch("core.whatsapp.send_media",
                        return_value=(False, "formato recusado")) as send_media, \
                mock.patch("core.whatsapp.send") as send:
            self.assertFalse(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "audio"))
        send_media.assert_called_once()
        send.assert_not_called()
        self.assertFalse(obj._wa_audio_sent)
        self.assertFalse(Path(path).exists())   # falhou e mesmo assim limpou

    def test_audio_failure_with_explicit_flag_falls_back_to_text_once(self):
        # O caso legítimo continua existindo — mas só para quem PASSA A FLAG
        # na chamada (espelho do turno assumido). A MESMA resposta sai em
        # texto uma única vez, assinada, e nada de áudio repetido.
        obj = self._obj()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as handle:
            path = handle.name
        obj._synthesize_whatsapp_audio = mock.Mock(return_value=path)
        with mock.patch("core.whatsapp.send_media",
                        return_value=(False, "formato recusado")) as send_media, \
                mock.patch("core.whatsapp.send",
                           return_value=(True, "ok")) as send:
            self.assertTrue(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "audio",
                audio_text_fallback=True))
        send_media.assert_called_once()
        send.assert_called_once_with("5511999999999", "resposta",
                                     account="trabalho", speaker="Gino")
        self.assertFalse(obj._wa_audio_sent)
        self.assertFalse(Path(path).exists())

    def test_synthesis_failure_with_explicit_flag_falls_back_to_text_once(self):
        obj = self._obj()
        obj._synthesize_whatsapp_audio = mock.Mock(return_value="")
        with mock.patch("core.whatsapp.send_media") as send_media, \
                mock.patch("core.whatsapp.send",
                           return_value=(True, "ok")) as send:
            self.assertTrue(obj._send_whatsapp_answer(
                "trabalho", "5511999999999", "resposta", "audio",
                audio_text_fallback=True))
        send_media.assert_not_called()
        send.assert_called_once()
        self.assertFalse(obj._wa_audio_sent)


class SynthesizeAnswerTest(unittest.TestCase):
    def _obj(self):
        return object.__new__(main.JarvisLive)

    def _pcm(self, rate=24000, seconds=0.25):
        import math
        import struct
        return b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(rate * seconds)))

    def test_phone_and_path_never_reach_the_whatsapp_tts(self):
        """Fixtures reais da tarde: número e /projects/therotv ficam fora do áudio.

        O guard roda antes do numwords/TTS; o motor recebe só a fala humana.
        """
        seen = []
        obj = self._obj()
        with mock.patch("core.whatsapp.ffmpeg_ready"), \
                mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style="words": text), \
                mock.patch("core.brain.synth_pcm24k",
                           side_effect=lambda text, st: seen.append(text) or b""):
            self.assertEqual(obj._synthesize_whatsapp_audio(
                "deixa comigo: a mensagem chega em +55 17 9****-**15 já"), "")
            phone_tts = obj._wa_tts_text
            self.assertEqual(obj._synthesize_whatsapp_audio(
                "Vou ler /projects/therotv"), "")
            path_tts = obj._wa_tts_text
        self.assertEqual(len(seen), 2)
        for spoken in (seen[0], phone_tts):
            self.assertFalse(any(ch.isdigit() for ch in spoken), spoken)
            self.assertNotIn("+55", spoken)
            self.assertNotIn("*", spoken)
            self.assertIn("destinatário", spoken)
        for spoken in (seen[1], path_tts):
            self.assertNotIn("/projects", spoken)
            self.assertNotIn("therotv", spoken)

    def test_tts_ogg_is_canonical_opus_48k_mono(self):
        import shutil
        import subprocess

        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            self.skipTest("ffmpeg/ffprobe indisponíveis")
        obj = self._obj()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=self._pcm()):
            path = obj._synthesize_whatsapp_audio("uma piada curta")
        self.assertTrue(path)
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name,sample_rate,channels",
                 "-of", "default=noprint_wrappers=1", path],
                capture_output=True, text=True, timeout=30)
            info = dict(line.split("=", 1) for line in
                        probe.stdout.splitlines() if "=" in line)
            self.assertEqual(info.get("codec_name"), "opus")
            self.assertEqual(info.get("sample_rate"), "48000")
            self.assertEqual(info.get("channels"), "1")
            self.assertTrue(obj._whatsapp_audio_is_ptt(path))
        finally:
            os.unlink(path)

    def test_wav_renamed_ogg_is_rejected_by_the_guard(self):
        import shutil
        import wave

        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            self.skipTest("ffmpeg/ffprobe indisponíveis")
        obj = self._obj()
        fake_ogg = tempfile.mktemp(suffix=".ogg")
        try:
            with wave.open(fake_ogg, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(48000)
                handle.writeframes(self._pcm(rate=48000))
            self.assertFalse(obj._whatsapp_audio_is_ptt(fake_ogg))
        finally:
            if os.path.exists(fake_ogg):
                os.unlink(fake_ogg)


class NoRoomSpeechTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_ignore = False
        obj._qoder_current_id = "t1"
        obj._qoder_turns = {"t1": "pedido"}
        obj._qoder_text_buf = {"t1": ""}
        obj._qoder_spoken = {"t1": []}
        obj._qoder_meta = {"t1": {"source": "whatsapp", "speak": False}}
        obj._qoder_say = mock.Mock()
        obj._qoder_finish = mock.Mock()
        obj._send_whatsapp_answer = mock.Mock()
        obj._memory_librarian = None
        obj._chat_memory = _Memory()
        obj._session_log = []
        obj._asst_name = "Gino"
        obj.ui = _UI()
        return obj

    def test_whatsapp_text_is_buffered_not_spoken(self):
        obj = self._obj()
        main.JarvisLive._qoder_on_text(obj, "t1", "resposta no zap", False)
        obj._qoder_say.assert_not_called()
        self.assertIn("resposta no zap", obj._qoder_text_buf["t1"])

    def test_whatsapp_done_sends_and_does_not_speak(self):
        obj = self._obj()
        obj._qoder_text_buf["t1"] = "resposta no zap"
        main.JarvisLive._qoder_on_done(obj, "t1", "resposta no zap")
        obj._qoder_say.assert_not_called()
        obj._send_whatsapp_answer.assert_called_once()
        self.assertTrue(any(line.startswith("WHATSAPP Gino:")
                            for line in obj.ui.logs))

    def test_whatsapp_audio_turn_is_delivered_as_audio(self):
        obj = self._obj()
        obj._qoder_meta["t1"]["media_type"] = "audio"
        obj._qoder_text_buf["t1"] = "resposta falada"
        main.JarvisLive._qoder_on_done(obj, "t1", "resposta falada")
        obj._qoder_say.assert_not_called()
        args, _kwargs = obj._send_whatsapp_answer.call_args
        self.assertEqual(args[3], "audio")

    def test_failed_send_is_flagged_in_the_activity_meta(self):
        """A linha do Activity Log não pode registrar sucesso quando não houve."""
        from core import activity
        obj = self._obj()
        obj._send_whatsapp_answer = mock.Mock(return_value=False)
        obj._qoder_text_buf["t1"] = "resposta no zap"
        main.JarvisLive._qoder_on_done(obj, "t1", "resposta no zap")
        meta = activity.parse(obj.ui.logs[-1])
        self.assertIsNotNone(meta)
        self.assertEqual(meta["origin"], "whatsapp não enviado")

    def test_ignored_turn_still_answers_whatsapp(self):
        # `_qoder_ignore` silencia a fala na sala (barge-in/confirmação), mas
        # a resposta devida no WhatsApp não pode ser engolida por ele.
        obj = self._obj()
        obj._qoder_ignore = True
        obj._qoder_text_buf["t1"] = "resposta no zap"
        main.JarvisLive._qoder_on_done(obj, "t1", "resposta no zap")
        obj._qoder_finish.assert_called_once_with("t1")
        obj._send_whatsapp_answer.assert_called_once()
        self.assertTrue(any(line.startswith("WHATSAPP Gino:")
                            for line in obj.ui.logs))

    def test_preempted_whatsapp_turn_still_delivers_what_was_produced(self):
        # A voz do dono na sala substituiu o turno do WhatsApp: o sidecar
        # aborta e o on_done nunca chega para esse id. A resposta que já
        # existia tem que sair mesmo assim (antes morria em silêncio).
        obj = self._obj()
        obj._qoder_meta["t1"].update({"media_type": "audio",
                                      "wa_account": "trabalho",
                                      "wa_chat": "5511999999999"})
        obj._qoder_text_buf["t1"] = "piada pela metade"
        obj._deliver_whatsapp_answer = mock.Mock()
        main.JarvisLive._qoder_preempt(obj, "t1")
        for _ in range(100):
            if obj._deliver_whatsapp_answer.called:
                break
            time.sleep(0.01)
        obj._deliver_whatsapp_answer.assert_called_once()
        meta, text = obj._deliver_whatsapp_answer.call_args[0]
        self.assertEqual(text, "piada pela metade")
        self.assertEqual(meta.get("media_type"), "audio")
        self.assertEqual(meta.get("wa_chat"), "5511999999999")

    def test_late_done_after_preempt_delivers_the_full_text(self):
        # Sem texto no buffer no instante do preempt, o destino do turno fica
        # guardado e um on_done atrasado entrega a resposta completa.
        obj = self._obj()
        obj._qoder_meta["t1"].update({"wa_account": "trabalho",
                                      "wa_chat": "5511999999999"})
        obj._qoder_finish = mock.Mock(
            side_effect=lambda turn_id, flush=True:
                obj._qoder_turns.pop(turn_id, None))
        obj._deliver_whatsapp_answer = mock.Mock()
        main.JarvisLive._qoder_preempt(obj, "t1")
        self.assertIn("t1", obj._wa_orphan_turns)
        main.JarvisLive._qoder_on_done(obj, "t1", "resposta completa")
        obj._deliver_whatsapp_answer.assert_called_once()
        _meta, text = obj._deliver_whatsapp_answer.call_args[0]
        self.assertEqual(text, "resposta completa")
        self.assertNotIn("t1", obj._wa_orphan_turns)

    def test_owner_voice_note_answers_in_whatsapp_not_the_room(self):
        # Bug do dono (duas vezes): nota de voz confirmada pelo voiceprint tem
        # source="owner" (portões) mas veio do WhatsApp. A resposta volta para
        # o chat de origem, em áudio, e nunca para o alto-falante.
        obj = self._obj()
        obj._qoder_meta["t1"].update({
            "source": "owner", "reply_channel": "whatsapp", "speak": False,
            "media_type": "audio", "wa_account": "trabalho",
            "wa_chat": "5511999999999", "owner_voice": True})
        obj._qoder_text_buf["t1"] = "piada contada"
        main.JarvisLive._qoder_on_done(obj, "t1", "piada contada")
        obj._qoder_say.assert_not_called()
        obj._send_whatsapp_answer.assert_called_once()
        args, _kwargs = obj._send_whatsapp_answer.call_args
        self.assertEqual(args[0], "trabalho")
        self.assertEqual(args[1], "5511999999999")
        self.assertEqual(args[3], "audio")
        self.assertTrue(any(line.startswith("WHATSAPP Gino:")
                            for line in obj.ui.logs))

    def test_owner_voice_note_delivered_after_preempt_stays_on_whatsapp(self):
        obj = self._obj()
        obj._qoder_meta["t1"].update({
            "source": "owner", "reply_channel": "whatsapp", "speak": False,
            "media_type": "audio", "wa_account": "trabalho",
            "wa_chat": "5511999999999", "owner_voice": True})
        obj._qoder_text_buf["t1"] = "resposta pela metade"
        obj._deliver_whatsapp_answer = mock.Mock()
        obj._carryover_question = mock.Mock()
        main.JarvisLive._qoder_preempt(obj, "t1")
        for _ in range(100):
            if obj._deliver_whatsapp_answer.called:
                break
            time.sleep(0.01)
        obj._deliver_whatsapp_answer.assert_called_once()
        obj._carryover_question.assert_not_called()

    def test_local_voice_turn_speaks_in_the_room_and_never_sends(self):
        # Turno falado ao vivo não tem canal de origem: só TTS na sala, zero
        # envio. O mesmo source="owner" que entrega no WhatsApp não pode
        # arrastar a fala do microfone para o WhatsApp.
        obj = self._obj()
        obj._voice_id_enabled = False
        obj._qoder_meta["t1"].update({"source": "owner",
                                      "reply_channel": "voice",
                                      "speak": True})
        obj._qoder_text_buf["t1"] = "resposta na sala"
        main.JarvisLive._qoder_on_done(obj, "t1", "resposta na sala")
        obj._qoder_say.assert_called_once_with("t1", "resposta na sala")
        obj._send_whatsapp_answer.assert_not_called()

    def test_owner_voice_audio_failure_sends_nothing_and_flags_the_log(self):
        # Lei do dono (07/11/2026) no caminho de produção: o espelho do turno
        # é voz do assistente ao dono no zap e NÃO liga a flag. Áudio que não
        # sai não vira texto: nada vai ao WhatsApp, nada cai no alto-falante,
        # e o Activity Log registra a falha ("whatsapp não enviado").
        from core import activity
        obj = self._obj()
        obj._speech_route = lambda *a, **k: {}
        obj._synthesize_whatsapp_audio = mock.Mock(return_value="")
        obj._send_whatsapp_answer = (
            lambda *a, **k: main.JarvisLive._send_whatsapp_answer(obj, *a, **k))
        obj._qoder_meta["t1"].update({
            "source": "owner", "reply_channel": "whatsapp", "speak": False,
            "media_type": "audio", "wa_account": "trabalho",
            "wa_chat": "5511999999999", "owner_voice": True})
        obj._qoder_text_buf["t1"] = "resposta falada"
        with mock.patch("core.whatsapp.send_media") as send_media, \
                mock.patch("core.whatsapp.send") as send:
            main.JarvisLive._qoder_on_done(obj, "t1", "resposta falada")
        obj._qoder_say.assert_not_called()
        send_media.assert_not_called()
        send.assert_not_called()
        self.assertFalse(obj._wa_audio_sent)
        line = next(l for l in obj.ui.logs
                    if l.startswith("WHATSAPP Gino:"))
        meta = activity.parse(line)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["origin"], "whatsapp não enviado")
        self.assertFalse(meta.get("generated", False))


class OutboundAccountTest(unittest.TestCase):
    def test_selected_outbound_account_removes_the_question(self):
        from plugins import whatsapp_control as wc
        accounts = [{"name": "principal"}, {"name": "trabalho"}]
        with mock.patch.object(wc, "_accounts", return_value=accounts), \
                mock.patch("plugins.whatsapp_control.whatsapp.default_account_name",
                           return_value="trabalho"):
            self.assertIsNone(wc._ambiguous(""))

    def test_without_a_selection_it_still_asks(self):
        from plugins import whatsapp_control as wc
        accounts = [{"name": "principal"}, {"name": "trabalho"}]
        with mock.patch.object(wc, "_accounts", return_value=accounts), \
                mock.patch("plugins.whatsapp_control.whatsapp.default_account_name",
                           return_value=""):
            self.assertTrue(wc._ambiguous(""))


class OwnerLabelTest(unittest.TestCase):
    """'manda pra mim'/'pra conta principal' resolve pelo owner_chat."""

    def _send(self, to, owner, accounts=None, text="oi"):
        from plugins import whatsapp_control as wc
        accounts = accounts or [{"name": "principal", "uuid": "jarvis",
                                 "number": owner},
                                {"name": "trabalho", "uuid": "jarvis-trabalho",
                                 "number": "5517992101133"}]
        with mock.patch.object(wc, "get_plugin_setting", return_value=owner), \
                mock.patch.object(wc, "_accounts", return_value=accounts), \
                mock.patch.object(wc, "_ambiguous", return_value=None), \
                mock.patch.object(wc.whatsapp, "send",
                                  return_value=(True, "ok")) as send:
            spoken, body = wc._do_send(to, text)
        return send, spoken, body

    def test_owner_labels_resolve_to_owner_chat(self):
        for label in ("mim", "para mim", "eu", "principal",
                      "conta principal", "minha conta principal",
                      "meu número", "conta do dono"):
            with self.subTest(label=label):
                send, spoken, body = self._send(label, "5517981792464")
                send.assert_called_once_with(
                    "5517981792464", "oi", account=None, signed=True)
                self.assertEqual(spoken, "ok")
                self.assertEqual(body, "ok")

    def test_account_label_resolves_to_that_account_number(self):
        send, _spoken, _body = self._send("conta trabalho", "5517981792464")
        send.assert_called_once_with("5517992101133", "oi", account=None,
                                     signed=True)

    def test_unknown_contact_is_not_guessed(self):
        send, _spoken, _body = self._send("João", "5517981792464")
        send.assert_called_once_with("João", "oi", account=None, signed=True)

    def test_missing_owner_chat_is_an_explicit_error(self):
        send, spoken, _body = self._send("mim", "")
        send.assert_not_called()
        self.assertIn("owner_chat", spoken)
        self.assertIn("registrad", spoken)
        self.assertNotIn("para quem?", spoken)

    def test_empty_destination_mentions_the_unregistered_number(self):
        from plugins import whatsapp_control as wc
        with mock.patch.object(wc, "get_plugin_setting", return_value=""):
            spoken, _body = wc._do_send("", "oi")
        self.assertIn("owner_chat", spoken)
        self.assertIn("registrad", spoken)


class _SayQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class SpeakerNameTest(unittest.TestCase):
    """Nome do falante: assina o TEXTO, nunca a VOZ (regra do dono)."""

    def _say_obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._speech_epoch = 0
        obj._asst_name = "Gino"
        obj._session_log = []
        obj._turn_done_event = None
        obj._turn_ref = 3
        obj._turn_input_chan = "voz"
        obj._speech_audit = {}
        obj._speech_audit_lock = threading.RLock()
        obj.audio_in_queue = _SayQueue()
        obj._loop = _Loop()
        obj.ui = _UI()
        obj._wait_for_user_quiet = lambda: True
        return obj

    def test_voice_note_tts_has_no_speaker_prefix(self):
        obj = object.__new__(main.JarvisLive)
        obj._asst_name = "Gino"
        seen = []
        with mock.patch("core.whatsapp.ffmpeg_ready"), \
                mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style="words": text), \
                mock.patch("core.brain.synth_pcm24k",
                           side_effect=lambda text, st:
                               seen.append(text) or b""):
            self.assertEqual(obj._synthesize_whatsapp_audio(
                "*Gino:* Bom dia, chefe."), "")
        self.assertEqual(seen, ["Bom dia, chefe."])
        self.assertNotIn("Gino", obj._wa_tts_text)
        self.assertNotIn(":", obj._wa_tts_text)

    def test_text_reply_signs_with_the_actual_speaker(self):
        for speaker in ("Gino", "Aline", "Arnaldo"):
            obj = object.__new__(main.JarvisLive)
            obj._asst_name = "Gino"
            obj._synthesize_whatsapp_audio = mock.Mock(return_value="")
            with mock.patch("core.whatsapp.send",
                            return_value=(True, "ok")) as send:
                self.assertTrue(obj._send_whatsapp_answer(
                    "trabalho", "5511999999999", "tudo certo", "text",
                    speaker=speaker))
            # Quem assina é o `send()` do core: a resposta leva o falante
            # ATUAL (persona inclusive), não um nome fixo no call site.
            send.assert_called_once_with(
                "5511999999999", "tudo certo",
                account="trabalho", speaker=speaker)

    def test_text_reply_does_not_double_the_signature(self):
        from core import whatsapp as _wa
        body = "*Aline:*\npronto"
        self.assertEqual(_wa.signed_text(body, "Aline"), body)
        self.assertEqual(_wa.signed_text("pronto", "Aline"),
                         "*Aline:*\npronto")

    def test_persona_voice_reply_has_no_spoken_prefix(self):
        obj = self._say_obj()
        seen = []
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style="words": text), \
                mock.patch("core.brain.synth_pcm24k",
                           side_effect=lambda text, st, **_kw:
                               seen.append(text) or b"\x00" * 2400):
            obj._say_blocking("*Aline:* já revisei o cadastro",
                              label="Aline", voice_id="v-aline",
                              chan="persona", origin="persona")
        self.assertEqual(seen, ["já revisei o cadastro"])
        self.assertNotIn("Aline", seen[0])
        self.assertNotIn(":", seen[0])

    def test_log_keeps_the_speaker_name_while_the_voice_drops_it(self):
        from core import activity
        obj = self._say_obj()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style="words": text), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=b"\x00" * 2400):
            obj._say_blocking("Arnaldo: já terminei a revisão",
                              label="Arnaldo", voice_id="v-arnaldo",
                              chan="persona", origin="persona")
        line = obj.ui.logs[-1]
        # O nome continua na linha do registro; só a voz não o diz.
        self.assertIn("Arnaldo:", activity.strip(line))
        data = activity.parse(line)
        self.assertEqual(data["tts"], "já terminei a revisão")
        self.assertIn("Arnaldo", obj._session_log[-1])


if __name__ == "__main__":
    unittest.main()
