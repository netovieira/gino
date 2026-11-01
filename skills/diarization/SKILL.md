---
name: diarization
description: Documentação técnica da identificação de falantes e cadastro de voz. Não é carregada no prompt ao vivo.
---

# Diarization

- `core/speaker_id.py` mantém voiceprints em `memory/voices/` (nunca áudio bruto) e um `index.json` de centroides/amostras.
- Política: dono autorizado responde; convidado conhecido só responde se autorizado; desconhecido não recebe informação.
- O primeiro falante vira o dono; com apenas um cadastro, baixa confiança é tratada como microfone, não intruso.
- Cadastro guiado estilo "Ok Google": frases curtas, embeddings apenas, amostra recente (≤2 min).
- Endpoints da ponte: `voice_list`, `voice_authorize`.

## Testes

- `pytest tests/test_speaker_id.py tests/test_speaker_diarization.py tests/test_voice_training.py`
