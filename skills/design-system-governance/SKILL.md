---
name: design-system-governance
description: Govern how a design system evolves — SemVer level for token and component changes, promotion from product to candidate to core, deprecation with replacement and removal version, migration maps and change communication. Use when Aline adds, promotes, versions or deprecates a token or component, or when the system is fragmenting.
---

# Design System Governance

Sistema sem regra de evolução vira três sistemas. Fonte: `ux-ui-agent-skills`
(`governance`, repo 3077★), adaptada ao cofre.

## Classifique a mudança (SemVer)

| Nível | Exemplos |
|-------|----------|
| **major** | token/prop renomeado ou removido, anatomia ou default alterado, contraste que muda a leitura |
| **minor** | token/componente/variante/prop opcional novo, aditivo |
| **patch** | correção de contraste, bug, doc, valor dentro da tolerância |

Sem nível declarado não existe mudança — existe drift.

## Promoção (produto → candidato → core)

1. O item serve necessidade real e repetida: aparece em **≥ 2 lugares**. Uso único é caso local, não componente de sistema.
2. Nasce em produto, vira candidato quando outro time reusa, vira core quando passa a barra de qualidade: 8 estados, mapeamento de token, a11y medida, spec escrita.
3. Promover inclui spec: anatomia, tokens usados, estados, teclado, conteúdo, limites de uso.

## Depreciação

- Marque com motivo, substituto e versão de remoção.
- Continua funcionando por **≥ 1 ciclo minor**.
- Entregue mapa de migração (de → para), no estilo de um crosswalk de design system.
- Remoção só em major.

## Consistência e comunicação

- Nova página/componente que não usa o tema é bug, não exceção.
- Toda mudança carrega changelog com o nível SemVer e o motivo.
- Um item de sistema = uma variante em todo lugar: a mesma ação tem o mesmo visual em todas as telas.
- Remoção/renomeação quebra consumidor em silêncio: anuncie antes, com data e substituto.

## Definition of done

- Nível SemVer declarado e entrada de changelog escrita.
- Remoção com janela de depreciação, substituto e tabela de migração.
- Spec do item novo cobre os 8 estados, a11y medida e mapeamento de token.
- Contraste revalidado quando a cor mudou; tokens válidos e aliases resolvendo.
