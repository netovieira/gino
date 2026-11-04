---
name: design-tokens
description: Author and audit design tokens in DTCG format with the three-tier hierarchy (primitive, semantic, component), OKLCH palettes, one shared theme for light and dark, and token-by-intent naming. Use when Aline defines a palette, type scale, spacing, radius, elevation or motion, themes a product, or reviews code for hardcoded values.
---

# Design Tokens — DTCG, três camadas

Token é a única forma de a interface ter uma fonte. Fonte: `ux-ui-agent-skills`
(`design-tokens` + regras de token/cor e tipografia, repo 3077★), adaptada ao
cofre e sem os scripts do kit.

## Hierarquia obrigatória

```
componente  button-bg-primary → {semantic.action.primary}
semântico   action.primary    → {primitive.blue.600}
primitivo   blue.600          → #2563EB
```

- **Primitivo**: valor bruto. Nunca usado direto em componente.
- **Semântico**: alias por propósito. É aqui que o tema troca.
- **Componente**: escopo do componente, usado na implementação.

Formato DTCG: cada token com `$type` e `$value`; preserve `$description`.
Referencie, nunca repita valor.

## Nomenclatura

`{categoria}.{propriedade}.{variante}-{estado}` — exemplos:
`semantic.text.primary`, `component.button.primary-bg-hover`,
`semantic.feedback.error-text`.

## Dark mode

Primitivos não mudam. O escuro troca no **semântico**, via `[data-theme="dark"]`
ou `prefers-color-scheme` sobre CSS custom properties. Teste todo estado de
componente nos dois modos; um modo só é meio sistema.

## Cor

Contraste mínimo (WCAG 2.2):

| Elemento | Mínimo |
|----------|--------|
| texto normal (< 24px) | 4.5:1 |
| texto grande (≥ 24px, ou ≥ 18.66px bold) | 3:1 |
| componente/objeto gráfico, borda essencial | 3:1 |
| indicador de foco | 3:1 |

Regras:

1. Cor nunca é o único indicador — sempre ícone, texto ou padrão junto.
2. Paleta gerada em **OKLCH**: hue da marca, 11 tons de L=97% (50) a L=15% (950) com chroma coerente.
3. Verifique: tom 500 ≥ 4.5:1 sobre branco para texto; tom 600 ≥ 3:1 sobre branco para UI.
4. Uma primária, uma destrutiva, neutros. Acento com parcimônia.
5. **Token por intenção**: destrutivo usa `action.destructive` em todo lugar (botão e modal de confirmação), nunca a primária. Secundário é neutro (outline/transparente), nunca preenchimento colorido.

## Tipografia e espaço

- Escala Major Third (1.25): 12 14 16 18 20 24 30 36 48 60 72.
- Uma família para UI, uma serifada para editorial, mono para código e dado.
- `h1` uma vez por página; nível de heading nunca pula.
- Corpo 45–75 caracteres por linha (`max-width: 65ch`); leading 1.25 em heading, 1.5 em corpo.
- Espaço em múltiplos de 4: `0 2 4 8 12 16 20 24 32 40 48 64`.
- Espaço externo maior que interno; itens relacionados mais próximos.

## Um tema só

Uma camada de variáveis (`:root` + `[data-theme="dark"]`), importada uma vez na
raiz. Nenhuma página redefine cor. Zero hex/px/timing hardcoded em componente —
exceção só no adaptador que mapeia tokens para API de terceiro.

## Verificação

- Todo arquivo de token é JSON válido e todo alias resolve (rode o parser do projeto ou `python3 -m json.tool`).
- Contraste medido, não estimado: calcule a razão com a fórmula de luminância relativa antes de afirmar qualquer número.
- Busque hex hardcoded fora dos tokens (`grep -rnE '#[0-9a-fA-F]{3,6}'`); o que aparecer é drift.
- Nenhum emoji como ícone, bullet ou marcador de status: use conjunto de ícones (lucide) em SVG com `currentColor`.
