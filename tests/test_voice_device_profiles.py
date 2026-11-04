"""Per-device voice profiles for the owner, with a deliberate phone window.

The owner's order (translated): "Update the enrollment per device, that way
you guarantee you know it's me regardless of which device it comes from."
These tests pin the two halves:

  (a) the index may hold more than one capture chain for the SAME speaker
      (``pc`` and ``celular``); any of the owner's profiles authorises the
      owner, and a third party's profile authorises only that third party;
  (b) the ``celular`` profile is only written from notes captured while a
      window explicitly armed by the owner's verified MICROPHONE voice is
      open, with coherence/duplicate/quality checks, timeout and cancel.
      Typed text and notes outside the window never enroll anything.

No model, no network, no real audio: embeddings are synthetic and the
WhatsApp decoder is a fake. The gates and the window are the real code.
"""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

import main
from core import speaker_id, voice_device_enroll

OWNER = "Anthero"
ACCOUNT = "trabalho"
OWNER_CHAT = "5517999990000"
THIRD_CHAT = "5511888888888"
VOICE_PCM = b"\xb8\x0b" * 32000       # 2 s at 16 kHz, rms ~0.09 (passes quality)

PC_A = [1.0, 0.0, 0.0, 0.0]
PC_B = [0.94, 0.34, 0.0, 0.0]
CEL_A = [0.0, 1.0, 0.0, 0.0]
CEL_B = [0.3, 1.0, 0.0, 0.0]
CEL_C = [-0.2, 1.0, 0.0, 0.0]
THIRD_A = [0.0, 0.0, 1.0, 0.0]
THIRD_B = [0.0, 0.2, 1.0, 0.0]


def _centroid(samples):
    arr = np.asarray(samples, dtype=np.float64)
    mean = arr.mean(axis=0)
    return (mean / np.linalg.norm(mean)).tolist()


class FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text):
        self.logs.append(str(text))


class FakeSpeaker:
    """Minimal index stand-in used by the main.py gates."""

    threshold = 0.45

    def __init__(self, name=OWNER, known=True, authorized=True,
                 score=0.9, device="pc", owner_name=OWNER, devices=None):
        self.owner_name = owner_name
        self._name = name
        self._known = known
        self._authorized = authorized
        self._score = score
        self._device = device
        self._devices = devices
        self.embeddings: list[list[float]] = []
        self.enrolled_devices: list[tuple] = []
        self.enroll_calls: list[tuple] = []
        self.identify_devices: list[str | None] = []

    def embedding(self, _pcm):
        if self.embeddings:
            return list(self.embeddings.pop(0))
        return list(CEL_A)

    def identify(self, _emb, device=None):
        self.identify_devices.append(device)
        return {
            "name": self._name if self._known else None,
            "score": self._score,
            "sample_score": self._score,
            "effective_score": self._score,
            "known": self._known,
            "authorized": self._authorized,
            "verdict": "known" if self._known else "unknown",
            "device": self._device,
        }

    def list(self):
        record = {"name": self.owner_name, "owner": True, "authorized": True}
        if self._devices is not None:
            record["devices"] = list(self._devices)
        return [record]

    def enroll_device(self, name, samples, device="pc", authorized=True,
                      append=False):
        self.enroll_calls.append((name, list(samples), device, authorized,
                                  bool(append)))
        self.enrolled_devices.append((name, device))
        return {
            "name": name,
            "device": device,
            "samples": len(samples),
            "coherence_mean": 0.97,
            "coherence_min": 0.95,
            "backup": "",
        }


def _obj(speaker=None):
    obj = object.__new__(main.JarvisLive)
    obj._voice_id_enabled = True
    obj._speaker = speaker if speaker is not None else FakeSpeaker()
    obj.ui = FakeUI()
    obj.said: list[str] = []
    obj._say_now = lambda text, **kw: obj.said.append(str(text))
    obj._log_heard = lambda text: obj.said.append(f"heard:{text}")
    obj._device_enroll = None
    obj._device_enroll_timer = None
    obj._device_enroll_lock = threading.RLock()
    obj._device_enroll_arm_timer = lambda seconds: None
    obj._cinema_active = False
    obj._cinema_active_now = lambda: False
    obj.replies: list[tuple] = []
    obj._send_whatsapp_answer = (
        lambda account, chat, text, media_type="text":
        obj.replies.append((account, chat, text)))
    return obj


class IndexPerDeviceTests(unittest.TestCase):
    def test_two_profiles_of_the_owner_both_authorise_and_third_party_does_not(self):
        with TemporaryDirectory() as tmp:
            sp = speaker_id.SpeakerId(store_dir=tmp, owner_name=OWNER)
            sp.enroll(OWNER, [PC_A, PC_B], authorized=True)
            sp.enroll_device(OWNER, [CEL_A, CEL_B],
                             device=speaker_id.DEVICE_CELLPHONE)
            sp.enroll("Ana", [THIRD_A, THIRD_B], authorized=True)

            pc = sp.identify(PC_A)
            cel = sp.identify(CEL_A)
            third = sp.identify(THIRD_A)

        self.assertTrue(pc["known"] and pc["authorized"])
        self.assertEqual(pc["name"], OWNER)
        self.assertEqual(pc["device"], "pc")
        self.assertTrue(cel["known"] and cel["authorized"])
        self.assertEqual(cel["name"], OWNER)
        self.assertEqual(cel["device"], "celular")
        # A third party matches their own profile, never the owner's.
        self.assertEqual(third["name"], "Ana")
        self.assertFalse(third["authorized"] and third["name"] == OWNER)

    def test_non_owner_can_never_receive_the_cellphone_profile(self):
        with TemporaryDirectory() as tmp:
            sp = speaker_id.SpeakerId(store_dir=tmp, owner_name=OWNER)
            sp.enroll("Ana", [THIRD_A, THIRD_B], authorized=True)
            with self.assertRaises(PermissionError):
                sp.enroll_device("Ana", [CEL_A], device="celular")

    def test_index_shape_keeps_legacy_top_level_and_adds_optional_profiles(self):
        with TemporaryDirectory() as tmp:
            sp = speaker_id.SpeakerId(store_dir=tmp, owner_name=OWNER)
            sp.enroll(OWNER, [PC_A, PC_B], authorized=True)
            sp.enroll_device(OWNER, [CEL_A, CEL_B], device="celular")
            data = json.loads((Path(tmp) / "index.json").read_text("utf-8"))

        rec = data["speakers"]["anthero"]
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["threshold"], 0.45)
        self.assertEqual(set(rec["profiles"]), {"pc", "celular"})
        # The old consumers keep seeing ``samples``/``centroid``; they mirror
        # the pc profile so nothing that ignores ``profiles`` changes.
        self.assertEqual(len(rec["samples"]), 2)
        self.assertEqual(rec["samples"], rec["profiles"]["pc"]["samples"])
        self.assertEqual(rec["centroid"], rec["profiles"]["pc"]["centroid"])
        self.assertEqual(len(rec["profiles"]["celular"]["samples"]), 2)

    def test_legacy_single_profile_index_still_loads_and_accepts_a_new_device(self):
        samples = [PC_A, PC_B]
        payload = {
            "version": 1,
            "threshold": 0.45,
            "owner": OWNER,
            "updated": 0,
            "speakers": {
                "anthero": {
                    "name": OWNER,
                    "authorized": True,
                    "samples": samples,
                    "centroid": _centroid(samples),
                    "auto_samples": [],
                    "auto_centroid": [],
                    "created": 0,
                    "updated": 0,
                },
            },
        }
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "index.json").write_text(
                json.dumps(payload), encoding="utf-8")
            sp = speaker_id.SpeakerId(store_dir=tmp, owner_name=OWNER)
            legacy = sp.identify(PC_A)
            listed = sp.list()[0]
            sp.enroll_device(OWNER, [CEL_A], device="celular")
            cel = sp.identify(CEL_A)

        self.assertEqual(legacy["name"], OWNER)
        self.assertTrue(legacy["known"] and legacy["authorized"])
        self.assertEqual(legacy["device"], "pc")
        self.assertEqual(listed["samples"], 2)
        self.assertIn("pc", listed["devices"])
        self.assertEqual(cel["device"], "celular")
        self.assertEqual(cel["name"], OWNER)


class PhoneWindowTests(unittest.TestCase):
    def test_typed_text_never_arms_the_window(self):
        obj = _obj()
        armed = main.JarvisLive._device_enroll_gate(
            obj, "cadastra minha voz do celular", None)
        self.assertFalse(armed)
        self.assertIsNone(obj._device_enroll)
        self.assertTrue(any("texto digitado nunca arma" in line
                            for line in obj.ui.logs))

    def test_non_owner_microphone_voice_never_arms(self):
        obj = _obj(FakeSpeaker(name="Ana", authorized=True, score=0.95))
        armed = main.JarvisLive._device_enroll_gate(
            obj, "cadastra minha voz do celular", VOICE_PCM)
        self.assertTrue(armed, "a frase é consumida com uma recusa explícita")
        self.assertFalse(obj._device_enroll.active())
        self.assertTrue(any("não confirmada como dono" in line
                            for line in obj.ui.logs))
        self.assertTrue(any("Só o dono" in line for line in obj.said))

    def test_owner_microphone_voice_arms_and_the_prompt_asks_for_notes(self):
        obj = _obj(FakeSpeaker(device="pc"))
        armed = main.JarvisLive._device_enroll_gate(
            obj, "cadastra minha voz do celular", VOICE_PCM)
        self.assertTrue(armed)
        self.assertIsNotNone(obj._device_enroll)
        self.assertTrue(obj._device_enroll.active())
        self.assertTrue(any("3 mensagens de voz curtas" in line
                            for line in obj.said))
        self.assertTrue(any("perfil=pc" in line for line in obj.ui.logs))
        obj._device_enroll.cancel()

    def test_typed_cancel_closes_the_window(self):
        obj = _obj()
        window = obj._device_enroll_window()
        window.arm(by="teste")
        cancelled = main.JarvisLive._device_enroll_cancel_gate(
            obj, "cancela o cadastro do celular", None)
        self.assertTrue(cancelled)
        self.assertFalse(window.active())

    def test_note_outside_the_window_is_never_decoded_nor_enrolled(self):
        obj = _obj()
        obj._device_enroll = voice_device_enroll.DeviceEnrollment()
        obj._decode_whatsapp_pcm = mock.Mock(return_value=VOICE_PCM)
        item = {"chat": OWNER_CHAT, "media": {"kind": "mensagem de voz"}}
        handled = main.JarvisLive._device_enroll_note(
            obj, ACCOUNT, item, "/tmp/nota.ogg")
        self.assertFalse(handled)
        obj._decode_whatsapp_pcm.assert_not_called()
        self.assertEqual(obj._speaker.enroll_calls, [])

    def test_three_coherent_notes_save_the_cellphone_profile(self):
        speaker = FakeSpeaker(device="celular")
        speaker.embeddings = [CEL_A, CEL_B, CEL_C]
        obj = _obj(speaker)
        obj._decode_whatsapp_pcm = mock.Mock(return_value=VOICE_PCM)
        window = obj._device_enroll_window()
        window.arm(by="voz do dono no microfone")
        item = {"chat": OWNER_CHAT, "media": {"kind": "mensagem de voz"}}

        for path in ("/tmp/1.ogg", "/tmp/2.ogg", "/tmp/3.ogg"):
            self.assertTrue(main.JarvisLive._device_enroll_note(
                obj, ACCOUNT, item, path))

        self.assertEqual(len(speaker.enroll_calls), 1)
        name, samples, device, authorized, append = speaker.enroll_calls[0]
        self.assertEqual(name, OWNER)
        self.assertEqual(len(samples), 3)
        self.assertEqual(device, "celular")
        self.assertTrue(authorized)
        self.assertFalse(append)
        self.assertFalse(window.active())
        self.assertTrue(any("Pronto: perfil do celular gravado" in text
                            for _, _, text in obj.replies))
        self.assertTrue(any("limiar global NÃO" in line
                            for line in obj.ui.logs))

    def test_off_cluster_note_is_rejected_and_does_not_complete(self):
        now = [100.0]
        window = voice_device_enroll.DeviceEnrollment(clock=lambda: now[0])
        window.arm(by="teste", now=now[0])
        one = window.consider(CEL_A, quality={"ok": True, "seconds": 3.0},
                              source="acc|chat", now=now[0])
        two = window.consider(CEL_B, quality={"ok": True, "seconds": 3.0},
                              source="acc|chat", now=now[0])
        off = window.consider([0.0, 0.0, 1.0, 0.0],
                              quality={"ok": True, "seconds": 3.0},
                              source="acc|chat", now=now[0])

        self.assertTrue(one["accepted"])
        self.assertTrue(two["accepted"])
        self.assertFalse(off["accepted"])
        self.assertIn("fora do agrupamento", off["reason"])
        self.assertEqual(off["count"], 2)
        self.assertFalse(off["complete"])
        self.assertEqual(len(window.status(now=now[0])["samples"]), 2)

    def test_duplicate_note_is_rejected(self):
        now = [0.0]
        window = voice_device_enroll.DeviceEnrollment(clock=lambda: now[0])
        window.arm(now=0.0)
        window.consider(CEL_A, source="acc|chat", now=now[0])
        dup = window.consider(CEL_A, source="acc|chat", now=now[0])
        self.assertFalse(dup["accepted"])
        self.assertIn("repetida", dup["reason"])
        self.assertEqual(dup["count"], 1)

    def test_window_expires_without_saving(self):
        now = [0.0]
        window = voice_device_enroll.DeviceEnrollment(
            timeout_s=10.0, clock=lambda: now[0])
        window.arm(now=0.0)
        window.consider(CEL_A, source="acc|chat", now=0.0)
        now[0] = 11.0
        self.assertFalse(window.active(now=now[0]))
        late = window.consider(CEL_B, source="acc|chat", now=now[0])
        self.assertFalse(late["accepted"])
        self.assertEqual(late["count"], 1)
        self.assertEqual(window.status(now=now[0])["samples"], [])


class GateProfileTests(unittest.TestCase):
    def test_owner_gate_accepts_the_cellphone_profile_and_logs_which_one(self):
        obj = _obj(FakeSpeaker(device="celular"))
        ok, detail = main.JarvisLive._owner_voice(obj, VOICE_PCM)
        self.assertTrue(ok)
        self.assertIn("perfil=celular", detail)

        obj._only_owner_enrolled = lambda: True
        self.assertTrue(main.JarvisLive._owner_voice_ok(obj, VOICE_PCM))
        self.assertTrue(any("perfil=celular" in line for line in obj.ui.logs))

    def test_third_party_is_refused_by_the_same_gate(self):
        obj = _obj(FakeSpeaker(name="Ana", authorized=True, score=0.95,
                               device="pc"))
        ok, detail = main.JarvisLive._owner_voice(obj, VOICE_PCM)
        self.assertFalse(ok)
        self.assertIn("voz=Ana", detail)
        self.assertFalse(main.JarvisLive._speaker_order_identity(
            obj, VOICE_PCM)[0])

    def test_privileged_order_reports_the_profile_that_matched(self):
        obj = _obj(FakeSpeaker(device="celular"))
        is_owner, known, detail = main.JarvisLive._speaker_order_identity(
            obj, VOICE_PCM)
        self.assertTrue(is_owner)
        self.assertTrue(known)
        self.assertIn("perfil=celular", detail)
        self.assertTrue(any("portão privilegiado" in line
                            for line in obj.ui.logs))


class PoolAndAttestationTests(unittest.TestCase):
    """The owner's final contract: one pool per PERSON, device only as audit.

    A sample from a device never enrolled does not authorise on its own; it
    waits in an expiring request until the owner confirms on a known device.
    """

    def _speaker(self, tmp):
        sp = speaker_id.SpeakerId(store_dir=tmp, owner_name=OWNER)
        sp.enroll(OWNER, [PC_A, PC_B], authorized=True, device="webcam")
        return sp

    def test_pool_is_one_set_per_person_with_device_labels(self):
        with TemporaryDirectory() as tmp:
            sp = self._speaker(tmp)
            sp.enroll_device(OWNER, [CEL_A, CEL_B], device="celular",
                             append=True)
            sp.enroll_device(OWNER, [CEL_C], device="celular", append=True)
            pool = sp.pool(OWNER)
            labels = {row["device"] for row in pool}

        self.assertEqual(labels, {"webcam", "celular"})
        self.assertEqual(len(pool), 5)
        self.assertEqual(pool[0]["name"], OWNER)
        self.assertTrue(pool[0]["owner"])
        self.assertEqual(sp.known_devices(OWNER), ["celular", "webcam"])

    def test_device_label_never_restricts_the_match(self):
        with TemporaryDirectory() as tmp:
            sp = self._speaker(tmp)
            sp.enroll_device(OWNER, [CEL_A, CEL_B], device="celular",
                             append=True)
            # A new chain is labelled only for audit; any sample matches.
            sp.enroll_device(OWNER, [PC_B], device="epson", append=True)
            phone = sp.identify(CEL_A)
            mic = sp.identify(PC_A)
            new_chain = sp.identify(PC_B)

        self.assertEqual(phone["name"], OWNER)
        self.assertEqual(phone["device"], "celular")
        self.assertEqual(mic["name"], OWNER)
        self.assertEqual(new_chain["name"], OWNER)
        self.assertIn(new_chain["device"], {"webcam", "epson"})
        self.assertTrue(phone["authorized"] and new_chain["authorized"])

    def test_unknown_device_notes_never_authorise_and_wait_in_a_request(self):
        speaker = FakeSpeaker(known=False, devices=["pc"])
        obj = _obj(speaker)
        obj._decode_whatsapp_pcm = mock.Mock(return_value=VOICE_PCM)
        item = {"chat": OWNER_CHAT, "media": {"kind": "mensagem de voz"}}

        pcm, detail = main.JarvisLive._whatsapp_owner_voice(
            obj, ACCOUNT, item, "/tmp/nota.ogg", direct=True)

        self.assertIsNone(pcm)
        self.assertIn("não atestado", detail)
        pending = obj._device_attest_window().pending()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["device"], "celular")
        self.assertEqual(pending["count"], 1)
        self.assertEqual(speaker.enroll_calls, [],
                         "pedido pendente não grava nada no pool")
        self.assertTrue(any("aparelho novo aguardando atestação" in line
                            for line in obj.ui.logs))
        self.assertTrue(any("confirme no microfone" in text
                            for _, _, text in obj.replies))

    def test_known_device_voice_confirms_and_enters_the_pool_labelled(self):
        speaker = FakeSpeaker(known=True, authorized=True, devices=["pc"])
        obj = _obj(speaker)
        obj._device_attest_begin("celular", [CEL_A, CEL_B])

        handled = main.JarvisLive._device_attest_gate(
            obj, "confirma que sou eu e registra a voz do celular",
            VOICE_PCM)

        self.assertTrue(handled)
        self.assertEqual(len(speaker.enroll_calls), 1)
        name, samples, device, authorized, append = speaker.enroll_calls[0]
        self.assertEqual(name, OWNER)
        self.assertEqual(len(samples), 2)
        self.assertEqual(device, "celular")
        self.assertTrue(authorized)
        self.assertTrue(append, "o pool é somado, não substituído")
        self.assertIsNone(obj._device_attest_window().pending())
        self.assertTrue(any("aparelho novo atestado" in line
                            for line in obj.ui.logs))

    def test_attestation_request_expires_and_drops_the_samples(self):
        now = [0.0]
        att = voice_device_enroll.DeviceAttestation(
            timeout_s=30.0, clock=lambda: now[0])
        att.begin("celular", [CEL_A], source="acc|chat", now=0.0)
        self.assertIsNotNone(att.pending(now=0.0))
        now[0] = 31.0
        self.assertIsNone(att.pending(now=now[0]))
        late = att.confirm("pc", True, now=now[0])
        self.assertFalse(late["confirmed"])
        self.assertEqual(late["samples"], [])

        now[0] = 0.0
        att.begin("celular", [CEL_A], now=0.0)
        self.assertTrue(att.expire(now=31.0))
        self.assertIsNone(att.pending(now=31.0))

    def test_text_never_confirms_an_unknown_device(self):
        speaker = FakeSpeaker(known=True, authorized=True, devices=["pc"])
        obj = _obj(speaker)
        obj._device_attest_begin("celular", [CEL_A])

        handled = main.JarvisLive._device_attest_gate(
            obj, "confirma que sou eu", None)

        self.assertTrue(handled, "a frase é consumida com uma recusa")
        self.assertEqual(speaker.enroll_calls, [])
        self.assertIsNotNone(obj._device_attest_window().pending())
        self.assertTrue(any("texto digitado nunca confirma" in line
                            for line in obj.ui.logs))
        self.assertTrue(any("Texto não confirma" in text for text in obj.said))


class RealIndexMigrationTests(unittest.TestCase):
    """The REAL memory/voices/index.json must survive the new format."""

    def test_real_index_keeps_every_sample_and_accepts_a_labelled_device(self):
        real = speaker_id.DEFAULT_STORE_DIR / "index.json"
        if not real.is_file():
            self.skipTest("index.json real ausente nesta máquina")
        payload = json.loads(real.read_text(encoding="utf-8"))
        raw = (payload.get("speakers") or {}).get("anthero") or {}
        raw_samples = list(raw.get("samples") or [])
        raw_auto = list(raw.get("auto_samples") or [])
        if not raw_samples:
            self.skipTest("o índice real não tem amostras do dono")

        with TemporaryDirectory() as tmp:
            (Path(tmp) / "index.json").write_text(
                real.read_text(encoding="utf-8"), encoding="utf-8")
            sp = speaker_id.SpeakerId(store_dir=tmp, owner_name=OWNER)
            listed = sp.list()[0]
            match = sp.identify(raw_samples[0])
            # A new capture chain, same person: labelled, pooled.
            new_sample = (np.asarray(raw_samples[0], dtype=np.float64)
                          * 0.999 + np.asarray(raw_samples[-1]) * 0.001)
            sp.enroll_device(OWNER, [new_sample.tolist()],
                             device="epson", append=True)
            after = sp.list()[0]
            known = sp.known_devices(OWNER)

        self.assertEqual(listed["samples"], len(raw_samples),
                         "a migração não pode perder amostras já matriculadas")
        self.assertEqual(listed["auto_samples"], len(raw_auto))
        self.assertTrue(match["known"])
        self.assertEqual(match["name"], OWNER)
        self.assertEqual(after["samples"], len(raw_samples),
                         "o perfil pc/legado continua íntegro")
        self.assertIn("epson", known)
        self.assertIn("pc", known)


if __name__ == "__main__":
    unittest.main()
