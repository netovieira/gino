---
name: api-design
description: REST API conventions for production backends — HTTP verb semantics, status code correctness, URL shape, versioning, cursor pagination, OpenAPI contract and the security baseline every route needs. Use when Arnaldo designs, changes or reviews an endpoint, an API contract or a paginated list.
---

# API Design — production REST

Design the contract before the handler. Once a client ships, the route is frozen:
Expo, Electron e web consomem a mesma API. Fonte: `agent-skills-standard`
(`common-api-design`, Apache/MIT, repo 582★), adaptada ao cofre.

## Verbs

- `GET` read-only e idempotente. Nunca muda estado.
- `POST` cria ou dispara; `PUT` substitui inteiro; `PATCH` altera parte; `DELETE` remove.
- Ação fora do CRUD vira sub-recurso: `POST /orders/:id/cancel`, não `POST /cancelOrder`.

## Status codes

| Code | Meaning | Note |
|------|---------|------|
| 200 | success | body presente |
| 201 | created | adicione `Location` |
| 204 | no body | updates/deletes sem payload |
| 400 | validation | devolva `details[]` campo a campo |
| 401 | unauthenticated | credencial ausente/inválida |
| 403 | unauthorized | autenticado, sem permissão |
| 404 | not found | recurso inexistente |
| 409 | conflict | estado/versão concorrente |
| 422 | business rule | sintaxe ok, regra de negócio violada |
| 429 | rate limit | adicione `Retry-After` |
| 500 | unhandled | logue com id de correlação, não vaze stack |

## URLs

- minúsculas, kebab-case: `/user-profiles`, não `/UserProfiles` nem `/user_profiles`.
- substantivo plural: `/orders`, `/products`.
- sem verbo no path, exceto sub-recurso de ação.
- aninhamento no máximo 2 níveis: `/users/:id/orders` sim; `/users/:id/orders/:orderId/items/:itemId` não.

## Versioning

- Versionamento por path: `/v1/users`. Header (`Api-Version: 2`) só interno.
- Uma versão por módulo de rota; nunca misture v1 e v2 no mesmo controller.
- Versão anterior vive no mínimo 6 meses depois da nova.
- Aposentadoria: headers `Deprecation: true` e `Sunset: <date>`.

## Pagination

- Cursor (`cursor` + `limit`) para volume grande ou dado vivo; offset só em conjunto pequeno e estático.
- default `limit: 20`, máximo `100`; acima do máximo, rejeite com 400.
- envelope estável: `{ "data": [], "pagination": { "nextCursor": null, "hasNextPage": false } }`.

## Contract (OpenAPI 3.1)

- Gerado a partir do código/DSL, não YAML escrito à mão depois.
- Cobre: schemas de request/response, formato de erro, requisitos de auth, exemplos.
- Spec revisada junto com o PR: breaking change exige bump de versão.

## Security baseline

- Auth obrigatório por padrão em toda rota; rota pública é opt-out explícito.
- Valide e sanitize query, path e body antes de usar (ver skill `typescript-security`).
- `Content-Type: application/json` explícito; rejeite tipos inesperados.
- Headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`.

## Anti-patterns

- `GET` que altera estado — CDN e prefetch executam.
- HTTP 200 com `{ "success": false }` — quebra monitoramento e cache.
- URL profundamente aninhada — impossível versionar e cachear.
- Remover/renomear campo no lugar — consumidor quebra em silêncio.

## Verificação antes de dizer "pronto"

- Uma chamada real por rota nova mostrando status e body de verdade.
- O spec/contrato contém a rota, o formato de erro e a exigência de auth.
- Lista paginada testada com o limite máximo e com cursor final.
