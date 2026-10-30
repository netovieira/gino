"""Diagnóstico do modo economy: o cérebro responde? a voz sintetiza?

Roda as duas metades do caminho separadamente — a que pensa (chat_stream, com a
cadeia qoder -> groq) e a que fala (synth_pcm24k, Fish Audio ou Edge) — para
saber exatamente onde o turno morre quando o JARVIS ouve e não responde.
"""
import sys
import time

sys.path.insert(0, ".")

from core import brain
from memory.config_manager import get_plugin_config

st = brain.settings(get_plugin_config)
print("cadeia:", " → ".join(brain.label(r) for r in st["chain"]))
print("voz:", brain.voice_label(st))
print("modelo Fish:", st["fish_model"], "| free:", brain.fish_model_is_free(st))


def conta_saldo():
    bal = brain.fish_balance(st["fish_api_key"])
    print("saldo Fish:", "ilegível" if bal is None else bal)


conta_saldo()

print("\n=== 1. cérebro (chat_stream) ===")
frases = []
t0 = time.monotonic()
primeira = [None]


def _on_sentence(s):
    frases.append(s)
    if primeira[0] is None:
        primeira[0] = time.monotonic() - t0
        print(f"  primeira frase em {primeira[0]:.1f}s: {s[:70]!r}")
    else:
        print(f"  + {time.monotonic() - t0:5.1f}s: {s[:70]!r}")


try:
    content, calls = brain.chat_stream(
        [{"role": "system", "content": "Responda em uma frase curta, em português."},
         {"role": "user", "content": "Diga apenas: teste de voz ok."}],
        None, st, _on_sentence, lambda: False)
    print(f"  ok: {len(content)} chars, {len(frases)} frases, {len(calls)} tool calls, "
          f"{time.monotonic() - t0:.1f}s no total")
    print(f"  texto: {content[:200]!r}")
except Exception as e:
    print(f"  FALHOU: {e.__class__.__name__}: {e}")

print("\n=== 2. voz (synth_pcm24k) ===")
t0 = time.monotonic()
try:
    pcm = brain.synth_pcm24k("Teste de voz do Jarvis.", st)
    dur = len(pcm) / 48000.0
    print(f"  ok: {len(pcm)} bytes = {dur:.2f}s de áudio em {time.monotonic() - t0:.1f}s")
    if pcm:
        amostras = memoryview(pcm).cast("h")
        pico = max(abs(min(amostras)), abs(max(amostras)))
        print(f"  pico do sinal: {pico} (0 = silêncio absoluto)")
except Exception as e:
    print(f"  FALHOU: {e.__class__.__name__}: {e}")

print("\n=== 3. voz de reserva (Edge) ===")
t0 = time.monotonic()
try:
    pcm = brain.tts_pcm24k("Teste de voz do Jarvis.", st["tts_voice"])
    print(f"  ok: {len(pcm)} bytes = {len(pcm) / 48000.0:.2f}s em {time.monotonic() - t0:.1f}s")
except Exception as e:
    print(f"  FALHOU: {e.__class__.__name__}: {e}")
