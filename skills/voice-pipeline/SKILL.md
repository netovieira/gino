---
name: voice-pipeline
description: Documentação técnica do caminho de voz (captura, VAD, endpointing, STT, barge-in). Não é carregada no prompt ao vivo.
---

# Voice Pipeline

1. Captura em blocos de 64 ms; o cancelador de playback remove o eco da própria voz.
2. Silero VAD detecta fala; Smart Turn v3.2 decide se a pausa encerra o turno ou se o próximo trecho deve ser fundido.
3. STT local (faster-whisper) transcreve; Groq é apenas plano B quando configurado.
4. Diarização por utterance rotula `[FALANTE: nome]` antes de entregar ao Qoder.
5. Barge-in: só uma interrupção clara corta a fala; backchannel e continuação não cortam.
6. O volume dos outros apps é abaixado durante a fala e devolvido ao fim, com watchdog.

## Testes

- `pytest tests/test_vad_wrappers.py tests/test_endpointing.py tests/test_speaker_diarization.py tests/test_duck_watchdog.py`
