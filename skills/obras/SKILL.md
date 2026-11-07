---
name: obras
description: Registro de obras na vault exclusiva do assistente (~/Gino-Obras, visível ao dono). Use ao começar QUALQUER implementação no repositório: abre pasta do projeto, pasta da implementação e mantém status.md, prompt.md, historico.md e testes.md; na raiz, status/progresso/historico/testes gerais. Concluir exige commit e teste como evidência.
---

# Obras — toda implementação deixa dossiê na vault do registro

Ordem do dono, literal, 07/11/2026: "cria um vault (visível pra mim) mas de
uso exclusivo seu, nesse vault, você cria uma pasta pra cada projeto que você
for trabalhar, e nessa pasta do projeto, uma pasta com o nome/título da
implementação, e dentro dela, um md de status... um arquivo md com o prompt que
o cérebro criou pra executar aquilo, um histórico também em md, que registrar
os commits, e um testes em md... E um status, um progresso, um histórico e um
de testes geral na raiz do vault." Esta skill é a lei que faz isso acontecer no
próximo trabalho, não uma explicação do código.

1. **Nenhuma implementação nasce sem pasta.** Antes de mexer no repositório,
   `core.obras.registrar_implementacao(projeto, título)` cria
   `<vault>/<projeto>/<implementação>/` com os QUATRO arquivos: `status.md`,
   `prompt.md`, `historico.md`, `testes.md`. Os nomes são os do dono, por
   extenso — não se traduz, não se padroniza, não se acrescenta arquivo
   (`skills/escopo-do-pedido/SKILL.md`). A REGRA `TIPO-AAAA-MM-DD-slug.md` do
   cofre do Obsidian (`~/Jarvis`, `skills/vault/SKILL.md`) NÃO vale aqui.

2. **O prompt vai para o arquivo, não para a minha memória.** `gravar_prompt`
   grava o texto real que gerou a execução (a tarefa que eu escrevi para o
   subagente, ou a ordem do dono). Se o texto integral não está ao meu alcance,
   grava-se o resumo fiel com a marca `reconstruído` — nunca uma redação
   inventada que parece original.

3. **Concluída só com prova material.** `definir_status(implementação,
   "concluída")` levanta `ValueError` se `historico.md` não tiver commit ou
   `testes.md` não tiver teste. Hash de commit entra como veio (`git log`);
   nada de hash de memória. É o portão do dono de 07/11/2026 para "feito" que
   não era feito.

4. **Validação ao vivo tem marcador próprio.** O que não foi conferido no
   processo vivo (serviço não reiniciado, caminho não exercido) fica como
   `pendente de validacao ao vivo` no status, com a conclusão em aberto — não
   se fecha o que não foi visto acontecer.

5. **A raiz é gerada, nunca editada.** `status.md`, `progresso.md`,
   `historico.md` e `testes.md` da raiz saem de `atualizar_raiz()`, que lê o
   que existe por baixo. Quem editar a raiz na mão perde o texto na próxima
   escrita. Toda implementação que mexe em arquivo da vault chama
   `atualizar_raiz()` (o módulo já faz isso nas escritas).

6. **Segredo não entra na vault.** Registro é escrito por máquina a partir de
   prompt real e commit real, então todo texto livre passa por `redigir()`:
   senha, token, chave de API, credencial de e-mail, bloco PEM, blob de base64
   e caminho de arquivo de segredo viram `[retido]`. Hash de commit é a única
   string longa que passa, porque é público e é a evidência.

7. **A vault não é git.** Fica fora do repositório (chave única `OBRAS_DIR` em
   `core/obras.py`, hoje `~/Gino-Obras`), o módulo recusa caminho dentro do
   repo, e nada dela é commitado. O commit é do código; o dossiê é da vault.

8. **Onde começa e onde termina.** Começa em `registrar_implementacao` (antes
   do primeiro diff). Durante: cada teste novo em `registrar_testes`, cada
   commit em `registrar_commits`, cada mudança de estado em `definir_status`.
   Termina com a raiz regenerada e o status no valor fechado que a evidência
   sustenta.
