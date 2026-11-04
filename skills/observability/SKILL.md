---
name: observability
description: Instrument a backend for production — structured JSON logs with request correlation, OpenTelemetry spans with route-safe names, RED metrics and SLO burn-rate alerts. Use when Arnaldo instruments a handler, debugs an incident, adds tracing/metrics or decides what an error log must carry.
---

# Observability — logs, traces, métricas

Sinal de produção é parte do código, não do painel. Fonte: `agent-skills-standard`
(`common-observability`, repo 582★), adaptada ao cofre.

## Logs e tracing

- Log estruturado em JSON, sempre. `console.log` solto não sobrevive a produção.
- Correlação: extraia `X-Request-Id` ou `traceparent` na borda e propague em todo contexto assíncrono (handler → repo → chamada externa).
- OpenTelemetry para spans; propague W3C `traceparent` entre serviços.
- Nome de span normalizado: `<MÉTODO> <rota>` → `GET /users/:id`. Nunca `GET /users/123`.

## Métricas

- Obrigatórias: taxa de requisição, taxa de erro, histograma de latência (p50/p95/p99), saturação.
- Labels com rota normalizada, não id — cardinalidade explode.
- Alerta em burn rate de SLO, não em pico bruto de uma métrica.

## Fluxo de instrumentação

1. Defina a operação e o nome normalizado do span antes de escrever código.
2. Propague contexto de request/trace em cada fronteira assíncrona.
3. Um evento estruturado por operação, com campos de correlação; redija segredo e body sensível.
4. Some RED (rate, errors, duration) com label segura, depois defina SLO e alerta de burn rate.
5. Exercite um caminho de sucesso e um de falha; confirme que os spans fecham e que o id liga log a trace.

## Anti-patterns

- PII/segredo no log: token, senha, body completo, header de auth.
- Nome de span dinâmico com id.
- Span sem `end` — vaza memória e quebra a árvore de trace.
- Log sem request id: impossível juntar requisição e erro em incidente.

## Verificação

- Dispare uma requisição real e cole a linha de log com `requestId` e o nome do span.
- Force um erro e mostre o log de erro com o mesmo id, sem payload sensível.
- Confirme que nenhuma métrica usa id de usuário/recurso como label.
