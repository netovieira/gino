---
name: tv-ui
description: UI de TV do TheroTV — foco com controle remoto, d-pad, layout 10-foot e player em react-native-tvos. Use ao criar ou revisar tela para Android TV e Apple TV.
---

# UI de TV (10-foot, controle remoto e foco)

O TheroTV roda em TV pelo fork `react-native-tvos` sobre o app Expo. TV não é o mobile com fonte maior: o controle remoto só move foco, e a tela inteira depende disso.

## Base do projeto
- `react-native` aponta para `npm:react-native-tvos@<base>-stable`, na mesma base do React Native do SDK (no SDK 58, com RN 0.88, o fork vai na linha 0.88). Fork fora da versão quebra build e foco.
- Plugin `@react-native-tvos/config-tv` no `app.json`; prebuild de TV com `EXPO_TV=1 npx expo prebuild --clean`. Expo Go não serve: use development build.
- Em monorepo, todos os projetos que compartilham dependências usam o fork, mesmo os que não miram TV, para não conflitar.
- Bibliotecas confirmadas em TV: expo-video, expo-image, expo-font, Reanimated, FlashList, SQLite e Skia. O que não está na lista de suporte do Expo para TV não entra sem teste em aparelho.

## Foco e controle
- Todo elemento acionável é `Pressable` com `focusable`, `onFocus` e `onBlur` que desenham o estado de foco. `TouchableWithoutFeedback` e `TouchableNativeFeedback` não recebem foco e não servem para TV.
- `TVFocusGuideView` é a ferramenta principal: `autoFocus` para primeiro foco e memória do último item; `destinations` para sidebar ou menu que precisa receber foco mesmo fora de linha; `trapFocusUp/Down/Left/Right` só em modal ou overlay, porque fora disso prender o foco deixa o usuário sem saída.
- `nextFocusUp/Down/Left/Right` corrige geometria irregular. O comportamento difere entre tvOS e Android TV, então teste nos dois.
- `useTVEventHandler` para play/pause, seek e teclas de mídia; `TVEventControl.enableTVMenuKey` no Apple TV para o menu voltar de forma consistente; `BackHandler` já trata menu e botão voltar.
- `FlatList`/`VirtualizedList` já vêm com guia de foco e trap conforme a orientação. Evite lista aninhada: o foco se perde ao reentrar (problema conhecido do fork). Para rolar sem item focável, use `TVTextScrollView`; para carrossel com snap por item, `snapToAlignment="item"`.

## Layout 10-foot
- Desenhe em 1920x1080 e escale para 4K; margem segura de 5% (48 a 64 px) por causa de overscan.
- Texto de corpo a partir de 24 px, título de 32 a 48 px, peso médio ou maior; nada de cinza claro sobre preto.
- Card de pôster com proporção fixa (2:3 ou 16:9); foco com escala de 1.05 a 1.1, ring de 3 a 4 px e transição de 150 a 200 ms. Um único sinal de foco, igual no app inteiro.
- Navegação principal é sidebar ou rail focável; não conte com barra de abas nativa de celular em TV.
- Nada depende de hover nem de tooltip; ação secundária fica visível ou em menu focável.
- Ao voltar da tela de detalhe, o foco volta para o card de origem, não para o primeiro item.

## Player
- `expo-video` é suportado em tvOS e Android TV. Controle próprio precisa ser focável e grande; `nativeControls` pode ficar desligado quando o controle é da casa.
- Controles somem após 3 a 5 s sem interação e qualquer tecla os traz de volta. Voltar sai do player e devolve o foco ao card que abriu o vídeo.
- Menu de legenda e áudio é focável e fecha com voltar; nada de lista suspensa pequena.
- Ao trocar de episódio, reuse o player; criar player por zapping vaza memória e áudio.

## Desempenho
- Caixa Android TV é fraca: sem blur, sem sombra pesada, sem SVG animado grande; prefira gradiente estático e `transform`/`opacity`.
- Pôster no tamanho real de exibição (2x para 4K), com cache e placeholder; não carregue miniatura gigante.
- Linha de catálogo memoizada, uma imagem por card e prefetch dos metadados do item focado.
- Teste em aparelho de 1 GB, não só no emulador; cold start e troca de aba contam.

## Verificação
- Emulador Android TV (API 31+) navegando com d-pad; simulador Apple TV com tvOS SDK 17+.
- Roteiro por tela: primeiro foco, as quatro direções até a borda, select, voltar e reentrada. Nenhum beco sem saída e nenhum foco invisível.
- TalkBack no Android TV; 720p e 1080p com overscan; rede lenta e vídeo em HLS; abrir e fechar o player dez vezes sem vazar áudio.
