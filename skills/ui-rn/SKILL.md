---
name: ui-rn
description: Interface do app TheroTV em React Native/Expo — rotas, estilos, animação, listas de catálogo e player. Use ao criar, revisar ou diagnosticar tela no app mobile/web.
---

# UI React Native / Expo (TheroTV)

App Expo/React Native com expo-router e TypeScript, em `/projects/therotv`. Referência corrente: Expo SDK 58 (React Native 0.88, React 19.2, Nova Arquitetura); confirme a versão no `package.json` antes de aplicar algo específico de SDK. Alvos: Android, iOS e web; TV tem skill própria (`tv-ui`).

## Escolhas padrão
- **Siga o que o app já usa.** Não troque biblioteca de estilo, lista ou animação por preferência: proponha migração só com ganho medido, custo e aprovação do dono.
- **Rotas**: arquivos em `app/` e layouts em `_layout.tsx`. `NativeTabs` para abas nativas no iOS/Android; no web ele cai num fallback básico, então use `_layout.web.tsx` com abas headless de `expo-router/ui`. `Stack.Header` e `Stack.Toolbar` são o padrão de cabeçalho; `Stack.SearchBar` entra só quando a tela tem busca.
- **Não importe de `@react-navigation/*`**: desde o SDK 56 o expo-router não depende do React Navigation. `withLayoutContext` está deprecado; use `integrateWithRouter`.
- **Estilo**: um sistema só por app. Unistyles 3 (Nitro, tema sem re-render) e NativeWind v4 (Tailwind 3) são maduros; NativeWind v5 ainda é pré-release e não vai para produção; Uniwind só com o custo do Pro avaliado.
- **Tokens**: um arquivo único de cores, espaçamento, raio e tipografia, exportado para o app e para o web. Nenhum hex ou número mágico repetido em tela.
- **Animação**: Reanimated 4 com `react-native-worklets`. Mudança de estado (abrir menu, trocar tema, expandir card) usa `transitionProperty`/`transitionDuration`; gesto e scroll usam `useAnimatedStyle` com shared values. Anime `transform` e `opacity`; largura, altura e sombra em loop travam a UI.
- **Imagens**: sempre `expo-image`, com `recyclingKey={item.id}` em lista reciclada, `cachePolicy="memory-disk"` e placeholder thumbhash/blurhash dentro de caixa com `aspectRatio` fixo.
- **Listas de catálogo**: FlashList v2 (Nova Arquitetura, sem `estimatedItemSize`) para as prateleiras; Legend List 2 quando os itens têm altura dinâmica. Carrossel horizontal com altura fixa e skeleton na proporção do pôster para não pular layout.
- **Player**: `expo-video` com `useVideoPlayer` e `VideoView`; um player por tela, `nativeControls={false}` quando o controle é próprio, desmonte ao sair. Não sobreponha blur, sombra pesada ou gradiente animado ao vídeo.
- **Texto**: `expo-font` (variable fonts no SDK 58) e `allowFontScaling` ligado; `maxFontSizeMultiplier` só onde o layout realmente quebra. Título em altura fixa precisa de `numberOfLines`.
- **Web**: `react-native-web` compartilha o componente; hover só existe no web e foco de teclado não ganha anel automático, então desenhe o estado de foco. No SDK 58 o expo-router não fornece mais insets de safe area no web; use CSS `env(safe-area-inset-*)`.

## Armadilhas
- Reanimated 4 exige Nova Arquitetura e o plugin `react-native-worklets/plugin` por último no Babel. Depois de lib nativa, refaça o development build e limpe o cache do Metro.
- VirtualizedList dentro de ScrollView, lista aninhada na mesma direção e `getItemLayout` errado quebram rolagem e reciclagem. Não use.
- `renderItem` inline, linha recriada a cada render e `key` instável anulam a reciclagem; memoize a linha e passe o mínimo de dados.
- Não use `@expo/vector-icons` (deprecado no SDK 56): use `@react-native-vector-icons/*`. `ImageBackground` e `InteractionManager` saíram; use View com Image absoluta e `requestIdleCallback`.
- Ref no React Native 0.88 usa tipo de instância (`ViewInstance`, `TextInputInstance`), não `NativeMethods`.
- No iOS 27 o app pode ser redimensionado; não confie em trava de orientação para o layout.
- Texto sobre pôster sem scrim tem contraste baixo; use camada escura ou chip sólido.

## Verificação
- `npx expo-doctor`, `npx tsc --noEmit`, `npx expo lint`; Jest com Testing Library na tela e Maestro no fluxo.
- Abra no simulador, em aparelho e no navegador: rolagem do catálogo sem área branca, foco de teclado no web, fonte a 200%, reduce motion ligado, tema claro e escuro.
- Meça no React Native DevTools: nenhum frame perdido ao abrir detalhe e nenhuma linha visível remontando.
