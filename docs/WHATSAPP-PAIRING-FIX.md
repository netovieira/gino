# Correcao: pareamento do WhatsApp (neonize x protobuf)

- Data: 2026-11-07
- Branch: `main` (commit anterior a esta mudanca: `993b56a`)
- Escopo: **dependencias apenas**. Nenhum codigo de `core/whatsapp.py`,
  `actions/whatsapp*.py` ou `plugins/whatsapp_control.py` foi tocado.
- Nada em `config/whatsapp_web/` (sessao, `paired`, `whatsapp_qr.png`) foi
  apagado ou reescrito.

---

## Causa

O `neonize==0.5.2` traz o gencode protobuf gerado com **7.34.1**. O venv tinha
**protobuf 6.33.6**. O runtime do protobuf recusa carregar gencode mais novo
que ele:

```
google.protobuf.runtime_version.VersionError: Detected incompatible Protobuf
Gencode/Runtime versions when loading waE2E/WAWebProtobufsE2E.proto:
gencode 7.34.1 runtime 6.33.6. Runtime version cannot be older than the linked
gencode version.
```

Ou seja: `import neonize` morria na primeira importacao do proto. O
pareamento nunca chegava a gerar QR.

Por que o app dizia que estava tudo bem: `whatsapp.available()` usa
`importlib.util.find_spec("neonize")`, que so olha se o pacote esta instalado
nao se ele importa. Por isso `available()` era `True` e "nem manualmente ia":
o status mentia e o `_run()` nunca era chamado com um import valido.

## Fix aplicado (rota A)

Upgrade do runtime do protobuf, retro-compativel com gencode antigo:

```bash
.venv/bin/pip install "protobuf==7.34.1"     # 6.33.6 -> 7.34.1
```

O venv ficou identico ao snapshot anterior em todo o resto, menos protobuf
(`diff /tmp/pip-freeze-before-whatsapp-fix.txt` depois do fix so aponta a linha
do protobuf).

### Versoes

| Pacote            | Antes   | Depois  |
|-------------------|---------|---------|
| protobuf          | 6.33.6  | 7.34.1  |
| neonize           | 0.5.2   | 0.5.2   |
| onnxruntime       | 1.31.1  | 1.31.1  |
| pipecat-ai        | 1.15.0  | 1.15.0  |
| google-api-core   | 2.43.1  | 2.43.1  |
| proto-plus        | 1.29.0  | 1.29.0  |

## O que foi verificado

- `import neonize` OK; `whatsapp.status()` devolve `available: true`,
  `error: ""` (antes a conta morria em `falha na conex: VersionError`).
- APIs do neonize usadas pelo codigo seguem todas la: `client.event.qr`,
  `ConnectedEv`, `DisconnectedEv`, `LoggedOutEv`, `MessageEv`, `ReceiptEv`,
  `StreamReplacedEv`, `utils.build_jid`, `utils.extract_text`.
- Construir o cliente exatamente como `_build_client()` faz (banco descartavel
  em `/tmp`, **sem** `connect()`) passou, com os handlers registrados.
- Consumidores compartilhados do protobuf seguem importando: `onnxruntime`
  (wake word / ECAPA), `pipecat`, `google.api_core`, `proto` (proto-plus).
- Testes:
  - `tests/test_whatsapp*.py`: **271 coletados, 0 falhas, 0 erros**. No
    baseline (protobuf 6.33.6) a coleta quebrava com `VersionError`
    (`ERROR tests/test_whatsapp_capture.py`).
  - Suites que dependem de protobuf (`test_email.py`, `test_youtube*`,
    `test_speaker_id*`, `test_voice_pipeline*`, `test_noise_filter*`,
    `test_silero*`, `test_qwen3_tts.py`): so falham os 4 testes de
    `test_qwen3_tts.py`, todos por `torch.OutOfMemoryError` na GPU (7.66 GiB,
    ocupada pelo `main.py` vivo e pelo `ollama`). Nada de protobuf.
  - Suite completa: 3256 testes, 9 falhas, 0 erros. As 5 falhas logicas
    (`test_held_segment`, `test_preroll`, `test_stt_segment_cap`,
    `test_stt_transient_gate`, `test_vision_qoder`) foram reproduzidas
    **identicas** com protobuf 6.33.6, entao sao pre-existentes. As outras 4
    sao o OOM do Qwen3-TTS. Zero mencao a protobuf/`VersionError` na saida.

## Consequencia conhecida (documentada, nao resolvida)

`pipecat-ai 1.15.0` declara `protobuf<7,>=5.29.6`. O pip avisa do conflito na
instalacao, mas o pipecat importa e roda normal com 7.34.1 nos testes acima.
**Nao** foi adicionado pino de protobuf no `requirements.txt`: um pino `>=7.34`
la dentro faria o resolutor do pip falhar num clone novo, por causa desse
conflito com o pipecat. Se um dia for preciso fechar isso no manifesto, e
decisao do dono (soltar o teto do pipecat ou trocar o pipecat).

## Como validar o pareamento no telefone

1. Reiniciar o JARVIS (o processo vivo ainda carrega o protobuf antigo em
   memoria; sem reiniciar o fix nao vale para ele).
2. Abrir o WhatsApp do celular > **Configuracoes > Aparelhos conectados >
   Conectar um aparelho**.
3. Escanear o QR que aparece na tela do JARVIS (copia em PNG em
   `config/whatsapp_web/<conta>/whatsapp_qr.png`). Uma tela de QR nova aparece
   so com a sessao ausente/invalida; se o celular ja estiver pareado, o app
   reconecta direto.

## Como reverter

```bash
.venv/bin/pip install "protobuf==6.33.6"
.venv/bin/pip install "neonize==0.5.2"     # reinstala se o pip tiver mexido nela
```

Depois reiniciar o JARVIS. A volta ao 6.33.6 reativa o erro do neonize
(ou seja: o WhatsApp para de parear de novo), mas devolve o estado exato de
antes para onnxruntime, pipecat e google-api-core. O snapshot com todas as
versoes de antes esta em `/tmp/pip-freeze-before-whatsapp-fix.txt`.

## Alternativa nao usada (rota B)

Downgrade do neonize para uma versao cujo gencode seja `<=6.33.6`, mantendo o
protobuf em 6.33.6. Nao foi necessaria: a rota A nao quebrou nada fora do zap.
Se um dia for preciso, lembrar de conferir `event.qr`, `ReceiptEv`,
`StreamReplacedEv`, `build_jid` e `extract_text` na versao escolhida.

## Detalhe pre-existente observado (fora do escopo desta mudanca)

- `from neonize import gocode` (em `_stop_go()`, `core/whatsapp.py:2421`) nao
  existe no neonize 0.5.2: o pacote traz `_binder.py`, `client.py`, `events.py`,
  `proto/`, `aioze/` e o `.so`, mas nenhum modulo `gocode`. O chamada ja esta
  dentro de `try/except: pass`, entao nao gera erro visivel - so significa que
  o "pare todos os clientes whatsmeow" no desligamento nunca acontece de
  verdade. Nao tem relacao com o protobuf e nao foi mexido aqui (fora de escopo).
