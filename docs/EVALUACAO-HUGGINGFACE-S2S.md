# Avaliacao: HuggingFace S2S para Privacidade Total

- Data: 2026-11-06
- Tarefa: FASE4-TASK4
- Status: **Avaliacao concluida — POC NAO implementada**

---

## Resumo Executivo

**Recomendacao: NAO implementar S2S agora.**

O HuggingFace Speech-to-Speech (S2S) oferece privacidade total (zero chamadas a nuvem), mas os custos de hardware e complexidade nao se justificam porque:

1. **Hardware insuficiente**: GPU atual (RTX 3070, 8 GB VRAM) atende apenas 1/3 do minimo exigido (24 GB VRAM)
2. **Privacidade total NAO e exigida pelo dono**: a arquitetura atual ja usa APIs gratuitas (Qoder, DeepSeek, Groq) com custo zero por chamada
3. **Qualidade inferior**: LLM local (Qwen3.5-4B, 4B parametros) e significativamente menos capaz que os modelos cloud atuais
4. **Custo de upgrade**: GPU com 24 GB VRAM (RTX 4090 / RTX 5090) custa R$ 8.000–15.000

**Condicao para reavaliacao**: se o dono exigir privacidade total no futuro, implementar POC com GPU adequada.

---

## 1. O que e HuggingFace S2S

**Repositorio**: https://github.com/huggingface/speech-to-speech  
**Licenca**: Apache 2.0  
**Stars**: 9.1k (novembro 2026)  
**Pipeline**: VAD → STT → LLM → TTS (tudo local, zero chamadas a nuvem)

### Componentes padrao

| Estagio | Modelo padrao | Tamanho |
|---------|---------------|---------|
| VAD | Silero VAD v5 | ~2 MB |
| STT | Parakeet TDT 0.6B v3 | 600 MB |
| LLM | Qwen3.5-4B | 4B parametros (~8 GB FP16) |
| TTS | Qwen3-TTS 1.7B | 1.7B parametros (~3.4 GB FP16) |

### Protocolo

- API WebSocket compativel com OpenAI Realtime (`/v1/realtime`)
- Clientes OpenAI podem conectar sem modificacao (apenas mudar `base_url`)
- Suporta interrupcao de fala (barge-in) nativamente
- Producao: backend de conversa para milhares de robos Reachy Mini

### Idiomas suportados

- **STT (Parakeet TDT)**: 25 idiomas europeus (inclui portugues)
- **TTS (Qwen3-TTS)**: multilingue (portugues suportado)
- **LLM (Qwen3.5-4B)**: treinado em 100+ idiomas (portugues incluido)

---

## 2. Requisitos de Hardware

### Configuracoes suportadas

| Plataforma | LLM | VRAM/Memoria | Performance |
|------------|-----|--------------|-------------|
| **Linux/Windows + NVIDIA** | Qwen3.5-4B (Transformers) | **24 GB VRAM** | Respostas rapidas |
| **Apple Silicon Mac** | Qwen3.5-4B 4-bit (MLX) | 16 GB memoria unificada | Respostas rapidas |
| **CPU (sem GPU)** | Qwen3.5-4B (Transformers) | 32+ GB RAM | "Slow replies" |

### Hardware atual do MARK XL

```
GPU: NVIDIA GeForce RTX 3070
VRAM: 8 GB (4.9 GB livre no momento)
RAM: 32 GB
```

### Gap de hardware

- **Exigido**: 24 GB VRAM (Linux/Windows)
- **Disponivel**: 8 GB VRAM
- **Deficit**: 16 GB VRAM (3x insuficiente)

**Opcoes para atender**:

1. **Upgrade de GPU**: RTX 4090 (24 GB) — R$ 10.000–12.000
2. **Upgrade de GPU**: RTX 5090 (32 GB) — R$ 15.000–18.000
3. **Mac com Apple Silicon**: M2/M3 Max com 32+ GB — R$ 20.000+
4. **CPU-only**: possivel, mas latencia inaceitavel para conversa interativa

---

## 3. Comparacao: Arquitetura Atual vs S2S

### Arquitetura atual (MARK XL)

```
Microfone
  ↓
Silero VAD (local)
  ↓
Whisper STT (local, faster-whisper)
  ↓
LLM cloud (Qoder gratuito / DeepSeek / Groq / Gemini)
  ↓
Qwen3-TTS (local) ou Kokoro (local) ou EdgeTTS (cloud)
  ↓
Alto-falantes
```

**Caracteristicas**:

- STT: 100% local (Whisper)
- LLM: 100% cloud (APIs gratuitas)
- TTS: 100% local (Qwen3-TTS/Kokoro) ou cloud (EdgeTTS)
- **Privacidade parcial**: STT e TTS sao locais, mas LLM envia texto para nuvem
- **Custo financeiro**: ZERO (todas as APIs usadas sao gratuitas)
- **Qualidade LLM**: alta (modelos grandes: DeepSeek-V3, GPT-OSS-120B, Gemini-2.5-Flash)
- **Latencia**: 2-5s (depende da API cloud)

### Arquitetura S2S (proposta)

```
Microfone
  ↓
Silero VAD (local)
  ↓
Parakeet TDT (local, STT)
  ↓
Qwen3.5-4B (local, LLM)
  ↓
Qwen3-TTS (local, TTS)
  ↓
Alto-falantes
```

**Caracteristicas**:

- Tudo 100% local (zero chamadas a nuvem)
- **Privacidade total**: nenhum dado sai da maquina
- **Custo financeiro**: ZERO (apos compra do hardware)
- **Qualidade LLM**: media (Qwen3.5-4B e muito menor que modelos cloud)
- **Latencia**: 1-3s (sem round-trips de rede)
- **Requisito de hardware**: 24 GB VRAM (GPU de R$ 10.000+)

### Tabela comparativa

| Criterio | Atual (cloud) | S2S (local) | Vencedor |
|----------|---------------|-------------|----------|
| **Privacidade** | Parcial (LLM na nuvem) | Total (tudo local) | S2S |
| **Custo financeiro** | Zero (APIs gratuitas) | Zero (apos hardware) | Empate |
| **Custo de hardware** | Nenhum | R$ 10.000–15.000 | Atual |
| **Qualidade do LLM** | Alta (modelos grandes) | Media (4B parametros) | Atual |
| **Latencia** | 2-5s (rede) | 1-3s (local) | S2S |
| **Confiabilidade** | Depende de internet | 100% offline | S2S |
| **Complexidade** | Baixa (APIs prontas) | Alta (gerenciar modelos) | Atual |
| **Manutencao** | Baixa (provedores atualizam) | Alta (voce atualiza) | Atual |

---

## 4. Analise de Custo-Beneficio

### Beneficios do S2S

1. **Privacidade total**: nenhum dado de conversa sai da maquina
2. **Latencia menor**: elimina round-trips de rede (2-5s → 1-3s)
3. **Offline total**: funciona sem internet
4. **Zero custo por chamada**: nao depende de limites de API gratuita

### Custos do S2S

1. **Hardware**: R$ 10.000–15.000 (GPU com 24+ GB VRAM)
2. **Complexidade**: gerenciar 4 modelos locais (VAD, STT, LLM, TTS)
3. **Qualidade inferior**: Qwen3.5-4B (4B params) vs modelos cloud (100B+ params)
4. **Manutencao**: atualizacoes de modelos, debugging local, monitoramento de VRAM

### Lei atual do MARK XL

> "Modelo gratuito, sem excecao"

Esta lei cobre **custo financeiro**, nao privacidade. A arquitetura atual atende:

- Qoder: gratuito (runway diario)
- DeepSeek: gratuito (API free tier)
- Groq: gratuito (free tier)
- Gemini: gratuito (free tier)
- Fish Audio: gratuito (free tier)
- EdgeTTS: gratuito (sem API key)

**Conclusao**: a lei atual NAO exige privacidade total, apenas custo zero.

---

## 5. Cenarios de Uso

### Cenario 1: Uso pessoal do dono (atual)

- **Privacidade exigida**: NAO (dono confia nas APIs gratuitas)
- **Custo aceitavel**: ZERO (lei atual)
- **Qualidade exigida**: ALTA (dono usa ferramentas complexas)
- **Recomendacao**: **Manter arquitetura atual**

### Cenario 2: Uso em empresa com dados sensiveis

- **Privacidade exigida**: SIM (LGPD, segredo industrial)
- **Custo aceitavel**: ALTO (hardware dedicado)
- **Qualidade exigida**: MEDIA (modelos menores sao aceitaveis)
- **Recomendacao**: **Implementar S2S com GPU adequada**

### Cenario 3: Uso em dispositivo embarcado (robo)

- **Privacidade exigida**: SIM (dados locais)
- **Custo aceitavel**: MEDIO (hardware limitado)
- **Qualidade exigida**: BAIXA (respostas simples)
- **Recomendacao**: **S2S com modelo menor (Qwen3.5-1.5B) ou Mac Mini**

---

## 6. Recomendacao Final

### NAO implementar S2S agora

**Justificativa**:

1. **Privacidade total NAO e exigida**: o dono nao mencionou preocupacao com privacidade em conversas
2. **Hardware insuficiente**: RTX 3070 (8 GB) nao atende minimo de 24 GB
3. **Custo injustificado**: R$ 10.000–15.000 em GPU para ganho marginal (privacidade nao exigida)
4. **Qualidade inferior**: Qwen3.5-4B e significativamente menos capaz que modelos cloud atuais
5. **Arquitetura atual funciona**: STT local + LLM cloud gratuito + TTS local atende todos os requisitos

### Condicoes para reavaliar

Implementar POC de S2S **SE**:

1. **Dono exigir privacidade total**: "quero que nenhuma conversa saia da maquina"
2. **Hardware adequado disponivel**: GPU com 24+ GB VRAM (RTX 4090/5090) ou Mac com 32+ GB
3. **Custo aceitavel**: dono aprovar gasto de R$ 10.000+ em hardware

### Passos futuros (se condicoes forem atendidas)

1. Comprar GPU com 24+ GB VRAM (RTX 4090 ~R$ 10.000)
2. Instalar `speech-to-speech` via pip
3. Criar POC em `/home/anthero/jarvis/hf_s2s_poc.py`
4. Medir latencia e qualidade em cenarios reais
5. Comparar com arquitetura atual
6. Se aprovado, migrar pipeline de voz para S2S

---

## 7. Fontes

- **Repositorio oficial**: https://github.com/huggingface/speech-to-speech
- **Blog HuggingFace**: https://huggingface.co/blog/s2s_endpoint
- **Guia explainx.ai**: https://explainx.ai/blog/huggingface-speech-to-speech-voice-agent-guide-2026
- **Modelos usados**:
  - Silero VAD: https://github.com/snakers4/silero-vad
  - Parakeet TDT: https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3
  - Qwen3.5-4B: https://huggingface.co/Qwen/Qwen3.5-4B
  - Qwen3-TTS: https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice

---

## 8. Conclusao

O HuggingFace S2S e uma solucao tecnica excelente para privacidade total, mas **nao e a escolha certa para o MARK XL neste momento**. A arquitetura atual (STT local + LLM cloud gratuito + TTS local) atende todos os requisitos do dono com custo zero e qualidade alta.

**Recomendacao**: manter arquitetura atual. Reavaliar S2S **somente se** o dono exigir privacidade total **E** houver hardware adequado (24+ GB VRAM).

---

**Avaliado por**: Gino (executor)  
**Data**: 2026-11-06  
**Status**: Concluido — POC nao implementada (privacidade total nao exigida)
