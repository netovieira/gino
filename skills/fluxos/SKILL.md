---
name: fluxos
description: Contrato de arquitetura do dono (FLUXOS.md) — fluxos de fala, execução, resposta, trabalho, WhatsApp, e-mail e identificação dele. Vale como lei; divergência da implementação é defeito; conformidade medida em bloco próprio.
---

# FLUXOS — o contrato do dono

1. **FLUXOS.md é o contrato do dono e vale como lei.** O arquivo `skills/fluxos/FLUXOS.md` é cópia literal do texto que o dono escreveu no vault dele (05/11/2026). Não é documentação explicativa nem sugestão: cada passo escrito lá é uma obrigação de comportamento do sistema, na redação dele (maiúsculas incluídas). Nunca resuma, corrija ou reescreva o conteúdo de FLUXOS.md por conta própria — só o dono altera o contrato.

2. **Nenhuma mudança de pipeline, roteamento, portão ou latência pode contradizer um passo do contrato** sem o dono aprovar a mudança de texto em FLUXOS.md primeiro. Se a ideia nova conflita com um passo escrito, a ordem é: apresentar a conflito, pedir a mudança do texto, esperar a aprovação do dono, mudar o texto, só então mudar o código. Sem a frase aprovada por ele, o passo antigo continua valendo.

3. **Quando a implementação divergir do contrato, a divergência é um DEFEITO a listar e corrigir, não uma escolha.** Nunca justifique comportamento atual apontando incidente, medição ou conveniência contra o contrato: ou o código se conserta para cumprir o passo, ou o dono muda o texto. Divergência descoberta entra como item explícito no bloco de conformidade abaixo (estado CONFLITANTE ou AUSENTE) até ser resolvida.

4. **O índice de conformidade abaixo é evidência, não opinião.** Cada linha é FEITO, PARCIAL, AUSENTE ou CONFLITANTE com medição ou leitura de código que prove; sem evidência na mão, a linha fica PENDENTE. Nunca invente estado. Ao mexer em qualquer fluxo, remeça e atualize a linha correspondente com a data.

## CONFORMIDADE (medido 05/11/2026)

- FLUXO FALA — PENDENTE
- FLUXO EXECUCAO — PENDENTE
- FLUXO RESPOSTA — PENDENTE
- FLUXO TRABALHO — PENDENTE
- FLUXO MAPEAMENTO WHATSAPP — PENDENTE
- FLUXO MAPEAMENTO EMAIL — PENDENTE
- FLUXO IDENTIFICACAO DO DONO — PENDENTE

*Visitantes podem ser elevados a permissões específicas ou gerais por ordem direta do dono (nota final do contrato).*
