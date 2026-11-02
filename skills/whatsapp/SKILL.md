---
name: whatsapp
description: Leis permanentes do WhatsApp do assistente — gatilho pelo nome configurado em todas as contas, chat direto do dono como turno da conversa, espelho de formato (texto/áudio) e proibições (grupo, eco).
---

# WhatsApp — leis permanentes

Estas regras valem em **todas as contas pareadas**, ao mesmo tempo, e não dependem de o dono estar na sala.

1. **Gatilho pelo nome configurado.** O nome que chama o assistente é o `assistant_name` do config (`get_assistant_name`), nunca uma palavra fixa no código. Mensagem nova em chat 1:1 que contenha esse nome — em qualquer caixa, com ou sem acento, no começo ou no meio da frase — recebe resposta. Conta nova pareada depois do boot também é monitorada; se uma conta não estiver respondendo, o defeito é para consertar, não para justificar silêncio.
2. **Chat direto do dono é turno da conversa.** Mensagem do dono no chat direto (o número dele no `owner_chat` da conta de saída, ou o chat do próprio número pareado da conta) é conversa comigo: respondo sem exigir o nome e mesmo quando o evento chega como `from_me`, porque o celular dele é um aparelho da mesma conta. O turno usa o mesmo cérebro e o mesmo contexto da conversa ao vivo (memória, sessão, índice) — e a resposta sai **no WhatsApp**, nunca pelo alto-falante da sala. Nada de aviso falado em cima do mesmo pedido.
3. **Espelho de formato.** Texto recebido → resposta em texto. Áudio recebido → transcrever, gerar a resposta, sintetizar **na voz do assistente** e enviar como áudio no **mesmo chat**. Se a síntese ou a fila de áudio falhar, a mesma resposta sai em texto, uma única vez — nunca os dois.
4. **Grupo nunca recebe resposta.** Grupo é leitura, não conversa: nem o nome configurado, nem o dono no grupo, nem OTP viram resposta ao grupo.
5. **Eco próprio não dispara.** Mensagem enviada por este processo não reabre a conversa (o id do envio é lembrado). Fora do chat direto, `from_me` é sempre resposta própria e é ignorado.
6. **Confirmação curta.** Ao responder autonomamente, a linha de log/atividade diz em uma frase o que foi enviado (para quem, texto ou áudio, e o estado de entrega). Não repita a resposta inteira nem anuncie isso em voz alta na sala.
