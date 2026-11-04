---
name: a11y-wcag22
description: Audit an interface against WCAG 2.2 AA/AAA and ARIA patterns, with criterion-referenced findings, severity, measured contrast and a specific fix per finding. Use when Aline checks accessibility, contrast, keyboard or screen-reader behavior, target size, focus visibility or reduced motion.
---

# Accessibility Audit — WCAG 2.2

Acessibilidade não é trocável por estética. Toda afirmação de conformidade vem
de medição, nunca de olho. Fonte: `ux-ui-agent-skills` (`a11y-audit` + checklist
WCAG 2.2, repo 3077★); checklist completa em
`references/wcag-checklist.md`.

## Conjunto P0 por componente

1. Navegável por teclado, na ordem visual, sem armadilha de foco.
2. Foco visível com contraste ≥ 3:1 contra o fundo adjacente.
3. Nome, papel e estado expostos ao leitor de tela (nativo quando existe; ARIA só para completar).
4. Contraste: 4.5:1 em texto, 3:1 em UI e objeto gráfico.
5. Alvo de toque ≥ 24×24 CSS px (WCAG 2.5.8), com espaçamento que não invada vizinho.
6. Nenhuma informação só por cor — erro leva ícone e texto, não só vermelho.
7. Movimento respeita `prefers-reduced-motion`.

## Adições da 2.2 (as que quebram auditoria antiga)

- **2.4.11 Focus Not Obscured**: elemento focado nunca fica escondido atrás de header fixo, toast ou sticky footer.
- **2.5.8 Target Size (Minimum)**: 24×24 px, ou espaçamento equivalente.
- **2.5.7 Dragging Movements**: toda ação de arrastar tem alternativa de clique/teclado.
- **3.3.8 Accessible Authentication**: login sem teste de memória ou quebra-cabeça cognitivo; `autocomplete` e colar senha funcionando.

## Como auditar

1. Leia a checklist POUR e marque o que está em escopo (não audite o que não existe na tela).
2. Percorra o fluxo só com teclado: Tab, Shift+Tab, Enter, Espaço, Esc, setas.
3. Meça contraste de cada par texto/fundo e de cada estado interativo — padrão, hover e foco. Botão que passa parado pode falhar no hover.
4. Cheque 320px de largura sem scroll horizontal, zoom 200% sem perda, texto espaçado.
5. Simule daltonismo e leia em ordem linear (CSS desligado).
6. Em TV, some o d-pad: foco alcançável e previsível, sem depender de hover.

## Saída

Tabela: critério (ex. `1.4.3`) · severidade (P0/P1/P2) · o que falha · correção concreta.
Confirme explicitamente o que passa. "Não medido" é resposta válida; razão inventada não é.

## Anti-patterns

- `outline: none` sem substituto visível.
- `div` clicável sem papel, tabindex e teclado.
- Placeholder como único rótulo.
- `aria-label` repetindo texto visível divergente.
- Contraste "parece ok" sem cálculo.
