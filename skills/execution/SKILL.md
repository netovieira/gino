---
name: execution
description: Política de execução pesada. Use quando o dono pedir para verificar, corrigir, instalar, testar, investigar código ou mexer no repositório.
---

# Execução pesada

- Toda execução acontece em um **subagente em segundo plano**: chame `Agent` com `run_in_background=true` e `subagent_type="executor"`.
- A conversa nunca espera a execução. Depois de delegar, diga em **uma frase curta** o que está rodando e pare. Não peça para o dono esperar e não repita atualizações de andamento.
- O resultado chega nesta mesma sessão quando terminar. Aí comente em uma ou duas frases o que mudou e como foi verificado.
- Nunca edite, rode comando longo, instale ou investigue o repositório inline no chat ao vivo. O gancho de permissão bloqueia; tentar só atrasa.
- Antes de delegar, escreva a tarefa com o contexto necessário: o que investigar, quais arquivos, qual comportamento esperado. O subagente não vê a conversa; a descrição é o contrato dele.
- O executor deve rodar os testes relevantes e só afirmar sucesso com evidência. Se falhar, diga o erro real, sem inventar.
- `git push`, release e publicação externa só depois de o dono pedir explicitamente para subir/publicar. Um subagente pode fazer o push apenas nessa janela.
- Não criar planos em `.md` nem documentos intermediários sem o dono pedir.
