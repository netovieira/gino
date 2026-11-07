"""Fluxo completo de arquivos no WhatsApp: foto → visão, áudio → transcrição.

UV-6 (Prioridade 3). O código de processamento já existia (`core/whatsapp.py`
baixa, `core/vision.py` descreve, `actions/file_processor.py` processa), mas o
meio do caminho estava quebrado: o arquivo baixado não tinha caminho nenhum
devolvido a quem processa, e a imagem nunca chegava ao backend de visão. Uma
foto recebida virava o rótulo `[foto]` e o modelo respondia da legenda.

Estes testes percorrem o fluxo REAL com o anexo de mentira do neonize e o CLI
da visão mockado — nenhuma rede, nenhum WhatsApp de verdade:

    mensagem com mídia → `_download_media` → arquivo no disco + `media["path"]`
    → `recent()` devolve o caminho → `file_processor(action=describe)`
    → `core.vision.describe_file` → resposta baseada no CONTEÚDO

    nota de voz → `_download_media` → `media_path` → transcrição do app
    → turno com a transcrição e resposta em áudio

`tests/test_vision_qoder.py` cobre o `--attachment` em si; aqui é a emenda.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import vision, whatsapp  # noqa: E402


def png_bytes() -> bytes:
    """Um PNG de verdade: o `_prepare` da visão abre o arquivo."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (240, 120), (220, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes() -> bytes:
    """Um JPEG de verdade — é o que o WhatsApp traz numa foto."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (240, 120), (220, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def ogg_bytes() -> bytes:
    return b"OggS\x00\x02\x00\x00\x00\x00\x00\x00opus-fake"


class _FakeNeonize:
    """`download_any` do neonize: escreve o anexo no destino que foi dado."""

    def __init__(self, payload: bytes, *, fail: bool = False,
                 empty: bool = False):
        self.payload = b"" if empty else payload
        self.fail = fail
        self.calls: list[str] = []

    def download_any(self, message, dest: str) -> None:
        self.calls.append(dest)
        if self.fail:
            raise RuntimeError("link expirado")
        Path(dest).write_bytes(self.payload)


def media_item(kind: str = "foto", ext: str = ".jpg", *, name: str = "Anthero",
               group: bool = False, mid: str = "ABC123") -> dict:
    audio = ext in (".ogg", ".mp3", ".m4a")
    return {
        "ts": int(time.time()),
        "chat": "5511999999999@s.whatsapp.net",
        "name": name,
        "from_me": False,
        "group": group,
        "text": f"[{kind}] descreva essa imagem",
        "id": mid,
        "media": {"kind": kind,
                  "mime": "audio/ogg" if audio else "image/jpeg",
                  "name": "", "seconds": 4, "size": 2048, "ext": ext},
    }


@pytest.fixture
def account(tmp_path, monkeypatch):
    """Uma conta isolada em disco temporário (nada de memory/ de verdade)."""
    original = dict(whatsapp._accounts)
    acct = whatsapp._Account("principal", "uuid-principal", default=True)
    acct.dir = tmp_path / "principal"
    acct.inbox_file = tmp_path / "principal.jsonl"
    acct.media_dir = tmp_path / "principal-media"
    monkeypatch.setattr(whatsapp, "_ensure_accounts", lambda: None)
    monkeypatch.setattr(whatsapp, "download_wanted", lambda: True)
    logs: list[str] = []
    monkeypatch.setattr(whatsapp, "_logger", logs.append)
    whatsapp._accounts.clear()
    whatsapp._accounts["uuid-principal"] = acct
    yield acct, logs
    whatsapp._accounts.clear()
    whatsapp._accounts.update(original)


class DownloadCarriesTheFileTest:
    """O download precisa devolver o arquivo, não só existir."""

    def test_photo_lands_on_disk_and_the_item_keeps_its_path(self, account):
        acct, _logs = account
        payload = png_bytes()
        item = media_item()
        client = _FakeNeonize(payload)

        whatsapp._download_media(client, object(), item, acct)

        path = item["media"]["path"]
        assert path == str(acct.media_dir / whatsapp._media_filename(item))
        assert Path(path).read_bytes() == payload
        assert client.calls == [path]

    def test_the_saved_file_is_anounced_without_a_number_or_a_path(self, account):
        acct, logs = account
        whatsapp._download_media(_FakeNeonize(png_bytes()), object(),
                                 media_item(), acct)
        line = " ".join(logs)
        assert "foto salva para processamento" in line
        # O log pode ser lido em voz alta: nem o número nem o caminho vão para lá.
        assert "5511" not in line
        assert str(acct.media_dir) not in line

    def test_a_file_already_on_disk_is_reused_and_still_exposed(self, account):
        acct, logs = account
        item = media_item()
        dest = acct.media_dir / whatsapp._media_filename(item)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png_bytes())
        client = _FakeNeonize(b"")

        whatsapp._download_media(client, object(), item, acct)

        assert item["media"]["path"] == str(dest)
        assert client.calls == []              # nada foi baixado de novo
        assert "já estava baixada" in " ".join(logs)

    def test_a_failed_download_leaves_no_path_and_says_so(self, account):
        acct, logs = account
        item = media_item()
        whatsapp._download_media(_FakeNeonize(b"", fail=True), object(),
                                 item, acct)
        assert "path" not in item["media"]
        assert "não consegui baixar" in " ".join(logs)

    def test_an_empty_file_is_not_a_saved_media(self, account):
        """Zero bytes no disco não é mídia baixada: nada de caminho, nada de evento."""
        acct, logs = account
        item = media_item()
        whatsapp._download_media(_FakeNeonize(b"", empty=True), object(),
                                 item, acct)
        assert "path" not in item["media"]
        assert not item["media"].get("path")
        assert "salva para processamento" not in " ".join(logs)


class InboxExposesTheLocalFileTest:
    """`recent()`/`search()` são a única janela do modelo para o que chegou."""

    def _push(self, acct, item, payload: bytes) -> dict:
        whatsapp._download_media(_FakeNeonize(payload), object(), item, acct)
        whatsapp._append_inbox(item, acct)
        return item

    def test_recent_returns_the_saved_photo_path(self, account):
        acct, _logs = account
        item = self._push(acct, media_item(), png_bytes())
        found = whatsapp.recent(limit=5, account="principal")
        assert found[0]["media"]["path"] == item["media"]["path"]
        assert Path(found[0]["media"]["path"]).is_file()

    def test_search_returns_it_too(self, account):
        acct, _logs = account
        item = self._push(acct, media_item(name="Carol"), png_bytes())
        hits = whatsapp.search("Carol", account="principal")
        assert hits and hits[0]["media"]["path"] == item["media"]["path"]

    def test_a_media_that_was_never_downloaded_exposes_no_path(self, account):
        acct, _logs = account
        item = media_item(mid="NAO-BAIXADA")
        whatsapp._append_inbox(item, acct)
        found = whatsapp.recent(limit=5, account="principal")
        assert found[0]["media"]["kind"] == "foto"
        assert "path" not in found[0]["media"]

    def test_a_group_never_exposes_a_local_path(self, account):
        acct, _logs = account
        item = self._push(acct, media_item(name="Grupo", group=True,
                                           mid="GRUPO1"), png_bytes())
        out = whatsapp._with_media_path(item, acct)
        assert "path" not in out["media"]

    def test_text_without_media_is_untouched(self, account):
        acct, _logs = account
        item = {"ts": int(time.time()), "chat": "x", "name": "Carol",
                "text": "oi", "id": "T1", "media": None}
        whatsapp._append_inbox(item, acct)
        found = whatsapp.recent(limit=5, account="principal")
        assert found[0]["text"] == "oi"


class _CliMock:
    """O `qoder -p --attachment` de mentira: anota o anexo, devolve a resposta."""

    def __init__(self, answer: str):
        self.seen: dict = {}
        self.answer = answer

    def run(self, cmd, **kwargs):
        import json as _json
        import subprocess
        self.seen["cmd"] = cmd
        self.seen["kwargs"] = kwargs
        path = cmd[cmd.index("--attachment") + 1]
        self.seen["attachment"] = path
        self.seen["bytes"] = open(path, "rb").read()
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stderr="",
            stdout=_json.dumps({"type": "result", "subtype": "success",
                                "is_error": False, "result": self.answer}))

    def patches(self):
        return [mock.patch("core.qoder.find_cli", return_value="/usr/bin/qoder"),
                mock.patch("core.qoder._find_cli", return_value="/usr/bin/qoder"),
                mock.patch("core.qoder.subprocess.run", side_effect=self.run)]


class _cli:
    """`with _cli(answer) as fake:` — o CLI mockado, sem rede, sem login."""

    def __init__(self, answer: str):
        self.fake = _CliMock(answer)
        self._started = []

    def __enter__(self) -> _CliMock:
        for p in self.fake.patches():
            self._started.append(p.start())
        return self.fake

    def __exit__(self, *_exc):
        for p in self._started:
            p.stop()
        self._started.clear()
        return False


class DescribeFileTest(unittest.TestCase):
    """A foto no disco vira descrição pelo backend de visão, sem rede."""

    def test_real_image_is_attached_byte_for_byte(self):
        payload = jpeg_bytes()
        fd, path = tempfile.mkstemp(prefix="foto-", suffix=".jpg")
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with _cli("um retângulo vermelho") as fake:
            text = vision.describe_file(path, "O que aparece?")
        self.assertEqual(text, "um retângulo vermelho")
        self.assertEqual(fake.seen["bytes"], payload)
        self.assertTrue(fake.seen["attachment"].endswith(".jpg"))
        # O arquivo temporário da visão não sobrevive ao turno.
        self.assertFalse(os.path.exists(fake.seen["attachment"]))

    def test_the_requested_timeout_reaches_the_cli_and_only_eyes_are_given(self):
        fd, path = tempfile.mkstemp(prefix="foto-", suffix=".png")
        with os.fdopen(fd, "wb") as fh:
            fh.write(png_bytes())
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with _cli("uma tela") as fake:
            vision.describe_file(path, "descreva",
                                 model=vision.DEFAULT_MODEL, timeout=33.0)
        cmd = fake.seen["cmd"]
        self.assertEqual(fake.seen["kwargs"]["timeout"], 33.0)
        # Ver não pode agir: nenhum tool neste turno.
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        # O modelo pedido é o configurado; se ele deixar de ser gratuito o
        # próprio Qoder troca por outro gratuito (é o que a visão aceita usar).
        sent = cmd[cmd.index("--model") + 1]
        self.assertIn(sent, (vision.DEFAULT_MODEL, "Efficient"))

    def test_missing_file_is_an_honest_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            vision.describe_file("/nao/existe/foto.jpg", "descreva")
        self.assertIn("não encontrei", str(ctx.exception))

    def test_a_non_image_is_refused_before_any_call(self):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"%PDF-1.4")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with mock.patch("core.qoder.find_cli",
                        return_value="/usr/bin/qoder") as find:
            with self.assertRaises(RuntimeError) as ctx:
                vision.describe_file(path, "descreva")
            find.assert_not_called()
        self.assertIn("não é uma imagem", str(ctx.exception))

    def test_an_empty_path_is_refused(self):
        with self.assertRaises(RuntimeError):
            vision.describe_file("", "descreva")

    def test_a_huge_image_is_refused_before_the_cli(self):
        payload = png_bytes()
        fd, path = tempfile.mkstemp(prefix="foto-", suffix=".jpg")
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with mock.patch.object(vision, "MAX_FILE_BYTES", len(payload) - 1), \
             _cli("não deveria chegar aqui"):
            with self.assertRaises(RuntimeError) as ctx:
                vision.describe_file(path, "descreva")
        self.assertIn("grande demais", str(ctx.exception))


class FileProcessorVisionRouteTest(unittest.TestCase):
    """`file_processor` lê a imagem pelo backend configurado, não pela legenda."""

    def setUp(self):
        from actions import file_processor as fp
        self.fp = fp
        fd, self.png = tempfile.mkstemp(prefix="zap-foto-", suffix=".jpg")
        with os.fdopen(fd, "wb") as fh:
            fh.write(png_bytes())
        self.addCleanup(lambda: os.path.exists(self.png) and os.unlink(self.png))

    def _brain(self, provider: str = "qoder"):
        """settings() do cérebro com a visão do jeito que o dono deixou."""
        return mock.patch("core.brain.settings",
                          return_value=vision.settings({"vision_provider":
                                                        provider}))

    def test_describe_goes_to_vision_and_never_reads_the_caption(self):
        gemini = mock.Mock()
        with self._brain(), \
             mock.patch.object(self.fp, "_gemini_client", gemini), \
             mock.patch("core.vision.describe_file",
                        return_value="um retângulo vermelho com JARVIS") as df:
            out = self.fp.file_processor({"file_path": self.png,
                                          "action": "describe"})
        self.assertIn("retângulo vermelho", out)
        df.assert_called_once()
        self.assertEqual(df.call_args.args[0], self.png)
        gemini.assert_not_called()

    def test_ocr_and_a_free_instruction_reach_the_same_door(self):
        with self._brain(), \
             mock.patch("core.vision.describe_file", return_value="NOTA FISCAL") as df:
            out = self.fp.file_processor({"file_path": self.png, "action": "ocr",
                                          "instruction": "leia o valor"})
        self.assertEqual(out, "NOTA FISCAL")
        self.assertIn("leia o valor", df.call_args.args[1])

    def test_a_whatsapp_path_is_the_same_door_as_an_upload(self):
        """A emenda do fluxo: o `path` do inbox entra aqui e sai conteúdo."""
        acct = whatsapp._Account("principal", "uuid-flow", default=True)
        box = Path(tempfile.mkdtemp(prefix="jarvis-uv6-"))
        acct.dir = box / "p"
        acct.inbox_file = acct.dir / "inbox.jsonl"
        acct.media_dir = box / "media"
        item = media_item()
        with mock.patch.object(whatsapp, "_ensure_accounts", lambda: None), \
             mock.patch.dict(whatsapp._accounts, {"uuid-flow": acct}, clear=True), \
             mock.patch.object(whatsapp, "_logger", None), self._brain(), \
             mock.patch("core.vision.describe_file",
                        return_value="um cachorro na praia"):
            whatsapp._download_media(_FakeNeonize(png_bytes()), object(),
                                     item, acct)
            whatsapp._append_inbox(item, acct)
            saved = whatsapp.recent(limit=3, account="principal")[0]["media"]["path"]
            out = self.fp.file_processor({"file_path": saved,
                                          "action": "describe"})
        self.assertIn("cachorro na praia", out)
        self.assertNotIn("descreva essa imagem", out)

    def test_vision_off_falls_back_to_the_gemini_ladder(self):
        client = mock.Mock()
        client.generate_content.return_value = types.SimpleNamespace(
            text="descrição pelo Gemini")
        with self._brain("off"), \
             mock.patch.object(self.fp, "_gemini_client", return_value=client):
            out = self.fp.file_processor({"file_path": self.png,
                                          "action": "describe"})
        self.assertIn("pelo Gemini", out)
        self.assertNotIn("indisponível", out)
        client.generate_content.assert_called_once()

    def test_a_vision_failure_falls_back_and_says_so(self):
        client = mock.Mock()
        client.generate_content.return_value = types.SimpleNamespace(
            text="descrição pelo Gemini")
        with self._brain(), \
             mock.patch("core.vision.describe_file",
                        side_effect=RuntimeError("qoder CLI não encontrado")), \
             mock.patch.object(self.fp, "_gemini_client", return_value=client):
            out = self.fp.file_processor({"file_path": self.png,
                                          "action": "describe"})
        self.assertIn("pelo Gemini", out)
        self.assertIn("visão estava indisponível", out)

    def test_both_doors_broken_is_a_refusal_not_a_fabrication(self):
        with self._brain(), \
             mock.patch("core.vision.describe_file",
                        side_effect=RuntimeError("tempo esgotado")), \
             mock.patch.object(self.fp, "_gemini_client",
                               side_effect=RuntimeError("sem chave")):
            out = self.fp.file_processor({"file_path": self.png,
                                          "action": "describe"})
        self.assertIn("Não consegui ver esta imagem", out)

    def test_the_run_is_logged_for_the_activity_log(self):
        """Passo 6 da tarefa: o evento fica registrado, no player do app."""
        seen: list[str] = []
        player = types.SimpleNamespace(write_log=seen.append)
        with self._brain(), \
             mock.patch("core.vision.describe_file", return_value="um retângulo"):
            self.fp.file_processor({"file_path": self.png, "action": "describe"},
                                   player=player)
        self.assertTrue(any("FileProcessor" in line and "IMAGE" in line
                            for line in seen))


class AudioNoteBecomesATurnTest(unittest.TestCase):
    """Áudio → arquivo no disco → transcrição → turno (a outra perna do UV-6)."""

    def setUp(self):
        import main
        self.main = main
        box = Path(tempfile.mkdtemp(prefix="jarvis-uv6-audio-"))
        self.acct = whatsapp._Account("principal", "uuid-audio", default=True)
        self.acct.dir = box / "p"
        self.acct.inbox_file = self.acct.dir / "inbox.jsonl"
        self.acct.media_dir = box / "media"

    def _obj(self):
        obj = object.__new__(self.main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_live_enabled = True
        obj._qoder_live_ready = lambda: True
        obj._asst_name = "Gino"
        obj.ui = types.SimpleNamespace(write_log=lambda m: None, muted=False)
        obj._chat_memory = None
        obj._session_log = []
        obj._last_user_speech = 0.0
        obj._speaker = None
        obj._qoder_turn = mock.Mock()
        obj._qoder_hold = mock.Mock()
        obj._begin_user_epoch = mock.Mock(return_value={"new_subject": False})
        obj._memory_record_user = mock.Mock()
        obj._index_event = mock.Mock()
        obj._next_turn_ref = mock.Mock()
        obj._addressed_to_me = mock.Mock(return_value=False)
        obj._device_enroll_note = mock.Mock(return_value=False)
        obj._owner_text_gate = mock.Mock(return_value=False)
        obj._voice_bootstrap_gate = mock.Mock(return_value=False)
        obj._voice_grant_gate = mock.Mock(return_value=False)
        obj._whatsapp_owner_voice = mock.Mock(return_value=(None, ""))
        obj._whatsapp_voice_anomaly = mock.Mock()
        obj._restart_confirmation_pcm = mock.Mock(return_value=None)
        obj._wa_stt_lock = threading.Lock()
        obj._groq = types.SimpleNamespace(
            transcribe_pcm=lambda pcm: "conta uma piada para mim")
        return obj

    def test_voice_note_is_transcribed_from_the_downloaded_file(self):
        item = media_item(kind="mensagem de voz", ext=".ogg", mid="VOZ1")
        client = _FakeNeonize(ogg_bytes())
        obj = self._obj()
        decoded: list[str] = []

        obj._decode_whatsapp_pcm = lambda path: (decoded.append(path)
                                                 or b"pcm-cru")
        with mock.patch.object(whatsapp, "_ensure_accounts", lambda: None), \
             mock.patch.dict(whatsapp._accounts, {"uuid-audio": self.acct},
                             clear=True), \
             mock.patch.object(whatsapp, "_logger", None), \
             mock.patch("core.whatsapp_watch.is_direct", return_value=True):
            whatsapp._download_media(client, object(), item, self.acct)
            saved = item["media"]["path"]
            self.assertTrue(saved and Path(saved).is_file())
            self.assertEqual(client.calls, [saved])

            with mock.patch.object(whatsapp, "media_path",
                                   return_value=saved):
                self.assertTrue(obj._whatsapp_autoreply(
                    "principal", dict(item, text="[mensagem de voz de 4s]")))

        # O áudio transcrito é o ARQUIVO que chegou, não um caminho perdido.
        self.assertEqual(decoded, [saved])
        obj._qoder_turn.assert_called_once()
        instruction = obj._qoder_turn.call_args.args[0]
        self.assertIn("conta uma piada para mim", instruction)
        self.assertEqual(obj._qoder_turn.call_args.kwargs["media_type"], "audio")
        self.assertEqual(obj._qoder_turn.call_args.kwargs["reply_channel"],
                         "whatsapp")

    def test_an_audio_that_never_landed_still_waits_for_the_expected_file(self):
        """02/11/2026: o watcher rodou antes da thread de download terminar."""
        item = media_item(kind="mensagem de voz", ext=".ogg", mid="VOZ2")
        obj = self._obj()
        obj._decode_whatsapp_pcm = lambda path: b""
        obj._transcribe_whatsapp_audio = mock.Mock(return_value="")
        with mock.patch.object(whatsapp, "_ensure_accounts", lambda: None), \
             mock.patch.dict(whatsapp._accounts, {"uuid-audio": self.acct},
                             clear=True), \
             mock.patch.object(whatsapp, "_logger", None), \
             mock.patch("core.whatsapp_watch.is_direct", return_value=True), \
             mock.patch.object(whatsapp, "media_path", return_value=""), \
             mock.patch.object(whatsapp, "download_wanted", return_value=True), \
             mock.patch.object(whatsapp, "media_dest",
                               return_value=str(self.acct.media_dir / "x.ogg")):
            obj._whatsapp_autoreply("principal", dict(item,
                                                      text="[mensagem de voz]"))
        obj._transcribe_whatsapp_audio.assert_called_once_with(
            str(self.acct.media_dir / "x.ogg"))


if __name__ == "__main__":
    unittest.main()
