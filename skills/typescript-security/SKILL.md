---
name: typescript-security
description: Secure server-side TypeScript — validate untrusted input at the boundary (Zod), stop injection and XSS, store secrets correctly, configure auth tokens and CORS, and avoid command/SSRF footguns. Use when Arnaldo writes or reviews API handlers, auth, env/config or anything that touches user input.
---

# TypeScript Security

Prioridade P0: entrada de usuário é hostil até prova em contrário. Fonte:
`agent-skills-standard` (`typescript-security`, repo 582★), adaptada ao cofre;
pontos do TheroTV (apps/api em Vercel ESM, Firestore/RTDB, LiveKit) são os
exemplos.

## Validate at the boundary

- Um schema no limite da API (`Zod`; `Joi`/`class-validator` se o repo já usa), nunca espalhado pela camada de domínio.
- `parse` quando erro deve lançar; `safeParse` quando você devolve erro estruturado. Falha → `400` com `details[]`.
- Valide query, path params, body e headers que você consome. Não confie no tipo do TypeScript em runtime: `req.body as X` é mentira, não validação.

## Injection and XSS

- SQL: query parametrizada (`pool.query('... WHERE id = $1', [id])`) ou ORM tipado (`Prisma.sql` para raw). Concatenação de string é proibida.
- HTML: sanitize com `DOMPurify` antes de injetar; nunca `dangerouslySetInnerHTML` com dado do usuário.
- Path/OS: sanitize antes de usar entrada em caminho de arquivo ou comando.

## Auth and secrets

- Hash de senha com `Argon2id` (ou o hasher já aprovado no repo). Nunca MD5/SHA1.
- JWT via `jose`/`jsonwebtoken`, cookie `HttpOnly` + `Secure` + `SameSite`; `RS256` para par de chaves; refresh com rotação.
- Segredo em env/secret manager. Nunca em código, em skill, em log ou em ficha de persona.
- CORS com allowlist estrita de origem; `origin: '*'` só em endpoint realmente público e sem credencial.
- Cripto com `crypto` do Node ou Web Crypto; nada caseiro.

## Footguns

- Sem execução dinâmica: `eval`, `new Function`, string em timer.
- Sem interpolação de shell: `execFileSync('git', ['arg1'])`, nunca `` execSync(`git ${input}`) `` nem env/config colado em comando.
- Sem SSRF: URL vinda de env/config ou de usuário é validada contra allowlist de origem antes de `fetch`/`axios`.
- Sem segredo em git. Sem confiar no cliente: toda regra é revalidada no servidor.

## Verificação

- `tsc --noEmit` (ou o typecheck do repo) limpo depois do schema — narrowing correto, sem `as` novo para calar erro.
- Um teste ou chamada real com payload inválido devolvendo 400 estruturado.
- Busca rápida no diff por `eval`, `exec`, `dangerouslySetInnerHTML`, `origin: '*'`, `process.env` impresso em log.
