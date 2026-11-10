---
name: voice-pipeline
description: Documentação técnica do caminho de voz (captura, VAD, endpointing, STT, barge-in). Não é carregada no prompt ao vivo.
---

# Voice Pipeline

1. Captura em blocos de 64 ms, **fonte única = microfone configurado** (dono, 2026-11-10: "aborto esse negócio de usar vários microfones, deixa quieto isso daí"). Não há segunda captura: o monitor de playback (`alsa_output.*.monitor`) e o `PlaybackCanceller` (`core/playback_cancel.py`, parec filho) saíram do pipeline — o cancelamento deles foi apagado com o módulo. O eco do próprio TTS segue tratado a jusante, sem mudar: echo tail medido do dispositivo, `EchoTextGuard`, wake word, ghost_filter e ducking.
2. Silero VAD detecta fala; Smart Turn v3.2 decide se a pausa encerra o turno ou se o próximo trecho deve ser fundido.
3. STT local (faster-whisper) transcreve; Groq é apenas plano B quando configurado.
4. Diarização por utterance rotula `[FALANTE: nome]` antes de entregar ao Qoder.
5. Barge-in: só uma interrupção clara corta a fala; backchannel e continuação não cortam.
6. O volume dos outros apps é abaixado durante a fala e devolvido ao fim, com watchdog.

## Testes

- `pytest tests/test_vad_wrappers.py tests/test_endpointing.py tests/test_speaker_diarization.py tests/test_duck_watchdog.py`
