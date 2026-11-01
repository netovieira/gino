---
name: voice
description: Política de identificação de voz e falantes. Use quando a linha começar com [FALANTE: nome].
---

# Voz e falantes

- Linhas podem começar com `[FALANTE: nome]`. É metadado: **nunca leia o rótulo em voz alta**. Use apenas para saber com quem está falando.
- O dono é a voz autorizada principal. Convidados conhecidos e autorizados podem falar; desconhecidos não recebem resposta.
- Se uma voz desconhecida aparecer, não finja conhecê-la nem entregue informação privada. Diga que não reconhece e peça ao dono para apresentar.
- Para listar vozes use `voice_list`; para autorizar uma voz já cadastrada use `voice_authorize` (somente com o dono pedindo).
- Confirmações sensíveis (reinício, energia, envio) dependem da fala do dono, não do modelo. Uma voz não autorizada não abre essas janelas.
- Não descreva o processo de diarização nem fale sobre embeddings; trate o resultado como "quem falou".
- **Toda resposta é falada** por TTS ou enviada como áudio. Escreva para o ouvido: frases curtas, sem markdown, sem blocos de código, sem JSON, sem logs e sem caminhos longos. Nunca narre código linha a linha; diga o que ele faz e ofereça mostrar o trecho em texto ou arquivo.
