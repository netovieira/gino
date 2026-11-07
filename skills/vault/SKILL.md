---
name: vault
description: Padrão de nome de arquivo do cofre do Jarvis (~/Jarvis). Lei do dono de 07/11/2026: TIPO-AAAA-MM-DD-slug.md com tipo da lista fechada; vale ao criar qualquer .md no vault. Reference canônica e o portão de código que recusa nome fora do padrão.
---

# Nomes de arquivo no cofre — é lei, não convenção

1. **A regra (dono, 07/11/2026).** Todo arquivo Markdown criado no vault do
   Jarvis obedece a `TIPO-AAAA-MM-DD-slug.md`. A referência canônica é a nota
   `~/Jarvis/REGRA-2026-11-07-padro-de-nomes-de-arquivo.md`, que contém a
   regex, a tabela de tipos e a tabela de migração. O vault **não se move**
   (`~/Jarvis` continua sendo o cofre que `core/obsidian.py` usa); o que muda
   é só o nome por dentro.

2. **Vocabulário fechado — seis tipos, nada além.**
   `PLANO` (o que se planeja fazer), `REGRA` (o que obriga), `TAREFA` (trabalho
   em andamento e seu andamento), `AUDITORIA` (o que foi conferido contra o
   que deveria), `RELATORIO` (o resultado medido de uma rodada), `NOTA` (o que
   não é nenhum dos cinco anteriores, e por isso a última opção, nunca a
   primeira). Tipo inventado (`MINUTA`, `DOC`, `ESBOÇO`) não existe: se parece
   faltar um tipo, o buraco é discussão com o dono, não escolha de nome.

3. **Como criar.** Sempre pela API do cofre, nunca por `open()` solto:
   `core.obsidian.write_note(titulo, corpo, folder="Planos", tipo="PLANO")`
   (ou `open_note(..., tipo=...)`, que repassa). Passar `tipo=` é obrigatório
   para nota nova — é o que liga o nome ao padrão. O nome sai de `build_name`
   e é validado antes de encostar no disco: fora do padrão, levanta
   `ValueError` com o diagnóstico do que refazer. Não se contorna o erro
   trocando o tipo por um que passe; corrige-se o título ou pergunta-se ao dono.

4. **O portão está nos três caminhos de escrita** (`write_note`,
   `mirror_plan`, `open_path`). Nome já dentro do padrão passa direto. Nome
   legado fora do padrão passa pela política do env `JARVIS_VAULT_NAME_POLICY`:
   `registrar` (hoje: escreve como está e loga
   `event=vault_name_off_pattern` no activity.log), `normalizar` (renomeia para
   o padrão usando a data real do conteúdo) ou `rejeitar` (erro). O default é
   `registrar` enquanto o dono não autorizar a rodada de renomeio — normalizar
   às escondidas renomearia notas vivas e é exatamente o ato que ainda não foi
   liberado.

5. **Slug.** Minúsculo, ASCII sem acento, sem espaço, hífens simples (nunca
   `--`, nunca no começo nem no fim), no máximo 50 caracteres, derivado do H1
   e não do nome antigo. Data é `AAAA-MM-DD` de calendário real: a do conteúdo
   (a que o nome já declara, ou o `mtime`), não a do dia em que se renomeou.
   Use `obsidian.ascii_slug(texto)` para produzir.

6. **Proibido, sem exceção:** espaço no nome, maiúscula no slug, acento,
   tipo em minúscula, tipo fora da lista, data incompleta ou inventada
   (mês 13, 30/02), `.md` duplicado, caminho dentro do nome (a pasta vai no
   argumento `folder=`, não no nome), sufixo `.MD`/`.markdown`.

7. **Exceção travada em código:** `Indices/Teia.md` e `Indices/Grafo.json`
   fogem do padrão porque são reescritos pela aranha de memória
   (`obsidian.mirror_index`) com nome fixo que `core/memory_index.py` e
   `core/guide_panel.py` procuram. Renomeá-los é mudança de contrato, não de
   nomenclatura: só com ordem explícita do dono e patch de código + testes.

8. **Convenções que continuam valendo.** Link entre notas é `[[NomeDoArquivo]]`
   (sem caminho, sem `.md`) e, com rótulo, `[[NomeDoArquivo|rótulo]]`; âncora
   interna `[[#seção]]` não depende de nome. Ao renomear qualquer arquivo, a
   primeira consequência é a reescrita dos wikilinks que apontam para ele —
   nunca renomeie sem rodar a relinkagem.

9. **Frontmatter no espelho Nodum.** O cofre é o vault do Obsidian, que tem o
   painel de "Propriedades"; o espelho Nodum **não tem**. Então bloco YAML de
   metadados (`tags:`, `status:`, `tipo:`, `data:`) precisa virar **linha de
   tag visível** (`#tag`, `**status:** ...`) na conversão — senão a nota chega
   nua do outro lado. Nada de inventar um "frontmatter legível" no espelho.

10. **A tabela de migração é documento, não ação.** Nenhum arquivo do cofre é
    renomeado, movido ou apagado sem o dono autorizar a rodada. Ler, listar e
    planejar é livre; escrever nome novo em arquivo existente, não.
