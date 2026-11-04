---
name: web-ui
description: Interface web do monorepo TheroTV (site e desktop) — Next.js, tokens, responsivo, acessibilidade e modo TV. Use ao criar ou revisar página, componente ou design system no web.
---

# UI web (site e desktop do TheroTV)

Referência corrente: Next.js 16.4 no App Router, React 19.3, Turbopack e Tailwind CSS v4.3 em modo CSS-first; confirme a versão instalada no repo antes de usar recurso específico. No monorepo, o alvo é `site/` e `desktop/`.

## Escolhas padrão
- **Server Component por padrão**; `'use client'` só na folha que tem estado, evento ou API de browser. Não passe função, segredo nem objeto grande pela fronteira.
- **Cache explícito**: `'use cache'` com `cacheLife` para conteúdo compartilhado e `Suspense` para o que é por usuário. Não conte com cache implícito de versões antigas do App Router.
- **Tokens são a fonte da verdade**: `@theme` no CSS global expõe variáveis (`--color-*`, `--radius-*`, `--spacing-*`); o componente usa utilitário ou `var(--...)`, nunca hex solto. Um JSON/TS de tokens alimenta o CSS do site e o StyleSheet do app, para as duas pontas não divergirem.
- **Tema**: claro/escuro por classe com `@custom-variant dark` e `color-scheme`, contraste conferido nos dois e sem piscar no primeiro paint.
- **Componentes**: Base UI 1.x para primitivas novas (dialog, menu, popover, tabs), porque entrega foco e teclado prontos. Se o repo já usa Radix/shadcn, fique nele e não misture. Nunca reimplemente focus trap, `aria-expanded` ou navegação de menu à mão.
- **Layout**: mobile-first, breakpoint pelo conteúdo (não pelo aparelho), grid com `minmax` e container queries; tipografia com `clamp`; `next/image` com proporção definida e `next/font` para não deslocar layout.
- **Interação**: nada essencial depende de hover; todo clique tem equivalente de teclado; alvo mínimo de 24x24 px (WCAG 2.2) e 44 px no toque.
- **Vídeo e catálogo**: pôster com dimensão fixa, player carregado sob demanda (`next/dynamic`), controles de teclado (espaço, setas, M, F), legenda e faixa de áudio; nada de autoplay com som.
- **Desktop**: se o app empacota o site (Electron/Tauri), reusa o mesmo design system e os mesmos tokens; não crie tema paralelo. Cuide de tamanho mínimo de janela, atalhos e estado offline.

## Modo TV no web
- Ative por viewport grande mais distância de visualização, ou por rota/flag; nunca por user agent.
- Tipografia a partir de 24 px, alvos de 48 px, margem segura de 5%, foco grosso (3 a 4 px) e contrastado, sem hover.
- Navegação por setas com `roving tabindex` ou spatial navigation (`@noriginmedia/norigin-spatial-navigation` ou `react-tv-space-navigation`); o foco não pode sumir da tela nem ficar preso fora de modal.
- Em TV com navegador (Tizen, webOS, Android TV), teste 1280x720 e overscan.

## Acessibilidade (WCAG 2.2 AA)
- Um `h1` por página, landmarks (`header`, `nav`, `main`, `footer`), label em todo campo, `aria-live="polite"` para status e erro ligado ao campo.
- Foco não obscurecido por sticky ou banner de cookie; ordem de tabulação igual à visual; link para pular ao conteúdo.
- Alternativa para arrastar, autenticação que não exige memória nem captcha visual, sem pedir o mesmo dado duas vezes.
- `prefers-reduced-motion` desliga parallax, autoplay de carrossel e transição longa.
- Contraste de 4.5:1 em texto e 3:1 em ícone/controle; cor nunca é o único sinal.

## Armadilhas
- No Tailwind v4 os tokens vivem no CSS (`@theme`), não no `tailwind.config.js`. Classe montada por concatenação não é detectada: use mapa completo de classes.
- `100vh` no mobile corta; use `dvh`/`svh`. `z-index` sem escala vira guerra; combine com portal e camada.
- Modal sem portal, sem retorno de foco e sem `aria-modal` quebra leitor de tela.
- Suspense sem fallback do mesmo tamanho gera CLS; skeleton precisa da proporção final.
- Server Action sem validação e sem limite é entrada aberta; valide no servidor e devolva erro de campo.
- Fonte variável sem `font-display` e fallback métrico troca o texto durante o carregamento.

## Verificação
- `next build` (pega erro de fronteira server/client), `tsc --noEmit` e lint.
- Playwright com `@axe-core/playwright` nas rotas principais; Lighthouse para LCP/INP/CLS; orçamento de JS por rota no bundle analyzer.
- Passe só de teclado, zoom a 200%, leitor de tela (NVDA ou VoiceOver), tema escuro, reduced motion e viewports 1920x1080 e 1280x720.
