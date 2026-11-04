---
name: backend-testing
description: Quality-first testing for backend and mobile code — pick the smallest honest layer, prove a real RED before implementing, keep one test per contract, and refuse tests that only echo the implementation. Use when Arnaldo writes, fixes or reviews a test, adds coverage or decides what "tested" means before shipping.
---

# Backend Testing — RED antes de GREEN

Teste que passa não basta: ele precisa provar um comportamento do dono do código
e uma falha plausível distinta. Fonte: `agent-skills-standard` (`common-tdd`,
repo 582★), destilada para o cofre.

## Escolha o modo

- **Comportamento novo:** RED → GREEN → REFACTOR, estrito. Nenhuma linha de produção antes do RED esperado.
- **Bug ou legado:** caracterize só o necessário, depois reproduza a correção como regressão falhando (RED). Se a implementação veio antes, registre o RED original como `NOT RUN` — não fabrique RED apagando guarda.
- Antes de escrever: compare com a suíte existente. Se o cenário já está coberto numa camada honesta, não duplique.

## Antes do teste, decida o contrato

Uma linha por comportamento/risco:

- `contract`: resultado observável ou efeito externo.
- `fault`: a falha plausível que este teste pega. Sem isso, é teste de decoração.
- `layer`: a menor camada honesta — `unit`, `integration`, `contract` ou `e2e`.
- `command`: o comando único e focado que roda só isso.

## Loop limitado

1. Rode lint/typecheck e derive o menor comando.
2. **RED:** adicione um grupo de intenção e rode em foreground, uma vez.
3. Classifique: `expected_red`, `frozen_snapshot_red`, `not_run`, `invalid_red`, `unexpected_green`.
   Erro de setup (import, sintaxe, fixture) é `invalid_red`, não RED comportamental.
   `unexpected_green` → inspecione cobertura existente e remova caso fraco antes de implementar.
4. **GREEN:** implemente só o suficiente; rode o mesmo comando.
5. **REFACTOR:** limpe sem mudar comportamento; rode o mesmo comando.
6. Escale só com evidência: alvo unitário relacionado → integração/contrato → suíte completa.

## Forma do teste

- Um contrato lógico por teste; várias asserções são aceitáveis se verificam aspectos do mesmo contrato.
- Asserte saída observável e efeito externo, não coreografia de mock.
- Mock só em fronteira externa que exige isolamento; domínio puro roda de verdade.
- Nome descreve o comportamento, sem id de ticket nem `TODO`.

## Red flags

- "Depois eu escrevo o teste", "é pequeno demais", "passou de primeira", "roda a suíte toda de novo".
- Mock de todo colaborador, oráculo copiado da implementação, teste só para subir percentual de cobertura.

## Verificação

- O RED aparece citado com a saída real do runner (não inventado).
- O GREEN roda o mesmo comando e o contador de testes muda de forma explicável.
- Nenhum teste novo é duplicata silenciosa de um existente.
