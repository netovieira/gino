---
name: fluxos
description: Contrato de arquitetura do dono (FLUXOS.md) — fluxos de fala, execução, resposta, trabalho, WhatsApp, e-mail e identificação dele. Vale como lei; divergência da implementação é defeito; conformidade medida em bloco próprio.
---

# FLUXOS — o contrato do dono

1. **FLUXOS.md é o contrato do dono e vale como lei.** O arquivo `skills/fluxos/FLUXOS.md` é cópia literal do texto que o dono escreveu no vault dele (05/11/2026). Não é documentação explicativa nem sugestão: cada passo escrito lá é uma obrigação de comportamento do sistema, na redação dele (maiúsculas incluídas). Nunca resuma, corrija ou reescreva o conteúdo de FLUXOS.md por conta própria — só o dono altera o contrato.

2. **Nenhuma mudança de pipeline, roteamento, portão ou latência pode contradizer um passo do contrato** sem o dono aprovar a mudança de texto em FLUXOS.md primeiro. Se a ideia nova conflita com um passo escrito, a ordem é: apresentar a conflito, pedir a mudança do texto, esperar a aprovação do dono, mudar o texto, só então mudar o código. Sem a frase aprovada por ele, o passo antigo continua valendo.

3. **Quando a implementação divergir do contrato, a divergência é um DEFEITO a listar e corrigir, não uma escolha.** Nunca justifique comportamento atual apontando incidente, medição ou conveniência contra o contrato: ou o código se conserta para cumprir o passo, ou o dono muda o texto. Divergência descoberta entra como item explícito no bloco de conformidade abaixo (estado CONFLITANTE ou AUSENTE) até ser resolvida.

4. **O índice de conformidade abaixo é evidência, não opinião.** Cada linha é FEITO, PARCIAL, AUSENTE ou CONFLITANTE com medição ou leitura de código que prove; sem evidência na mão, a linha fica PENDENTE. Nunca invente estado. Ao mexer em qualquer fluxo, remeça e atualize a linha correspondente com a data.

5. **A fala do dono não se audita (ordem dele, 2026-11-05 22:16–22:27; regra implementada na UC-5).** Reclamação ou ordem do dono é dado de entrada e abre DEFEITO no mesmo turno — nunca "verificando se é verdade". Está vedado rodar grep/verify/audit sobre `activity.log`, `memory/chat` ou histórico de transcrição para conferir o que ele disse. A regra é código: `core/owner_law.py` (`classify`, `record_owner_complaint`, `blocks_owner_speech_audit`), exposta em `core/owner_decision.py`, aplicada no roteador (`Decision.owner_complaint`), no orquestrador (evento `owner_defect_opened` + a lei no prompt do cérebro) e na tool bridge (`_bridge_dispatch` recusa a chamada). Não se confunde com a verificação de AÇÕES: recibo HMAC e `core/action_verifier.py` auditam o que o sistema FEZ e continuam obrigatórios; o que a ordem corta é interrogar a palavra do dono.


Base da medição: processo pid=223429, boot `activity.log:17842` (`SYS: boot origem=aberto pelo usuário (GNOME) pid=223429 hora=2026-11-05 22:02:19`). Veredites: `FEITO` só com trecho literal no log vivo pós-boot; `EM DISCO SEM RASTRO` = existe no HEAD, zero linhas desde o boot; `AUSENTE` = nem no HEAD. Dado pessoal nos trechos é suprimido (`[…]`). Nota de carga: o commit 94886f4 (filtro de e-mail) entrou no main às 22:13:01, DEPOIS do boot — esse código está no disco em HEAD mas não no bytecode do processo vivo.

### FLUXO FALA — FEITO 1, 2, 4, 5; passo 3 com dois ramos EM DISCO SEM RASTRO
1. FEITO — `activity.log:17850` `[2026-11-05 22:02:35] WHATSAPP YOU: […]` (digita); voz captada viva: `jarvis.log:6175` `22:02:22.027 [faster_whisper INFO] - Processing audio with duration 00:01.000`.
2. FEITO — `jarvis.log:6681` `[Rota] caminho=boca motivo=chamou primeira_unidade_ms=608` e `jarvis.log:6726` `[…]=914`; `activity.log:18085` `SYS: boca atendeu conversa curta (chamou); primeira fala em 608 ms. "Confirmado, criador."` — espera < 1 s medida. A janela de 15 min é o padrão do mesmo caminho (`core/flows/mouth.py:318-322`, `mouth_context_minutes=15`); a montagem do contexto não imprime linha própria.
3. FEITO para "ouve no mesmo instante que a boca" e "verifica ferramentas/gates/memória": todo turno imprime o resultado da verificação no mesmo segundo da entrada — `activity.log:17851` `[Fluxos] {'type': 'turn', … 'route': 'livre', 'actions': [], 'lookups': [], 'fallback': False, 'ms': 0.864}` (23 turnos pós-boot, sempre com as listas impressas vazias). EM DISCO SEM RASTRO para "manda pro orquestrador caso reconheça algo executável": zero turnos com `actions` não-vazio; o despachador existe (core/flows/orchestrator.py → core/flows/live.py:200-221) e nunca imprimiu `[Fluxos] portão de ferramenta …`. EM DISCO SEM RASTRO para "se tem memória pra carregar e carrega": zero turnos com `lookups` não-vazio (Lookup("memory", …) em core/flows/router.py:285). Falta: fala que case em ação/consulta real e a linha correspondente no log vivo.
4. FEITO — a boca responde em fluxo conforme o cérebro solta: `activity.log:17852-17854` `[Fluxos] {'type': 'prewarm', … 'chars': 7}` → `'chars': 76` → `'chars': 135` e `activity.log:17855` `[2026-11-05 22:02:52] WHATSAPP Gino: *Gino:*`.
5. FEITO — `activity.log:17860` `[2026-11-05 22:04:16] QODER: segundo plano — Auditar se o renascimento trouxe o código` (também :18018, :18125).

### FLUXO EXECUCAO — FEITO 1, 3, 4, 5; passo 2 EM DISCO SEM RASTRO
1. FEITO — `activity.log:17859` `QODER: ferramenta — Agent` + :17860 (cérebro reconheceu trabalho e abriu processo).
2. EM DISCO SEM RASTRO — zero linhas de gate de ferramenta desde o boot: o emissor `[Fluxos] portão de ferramenta <tool>: …` (core/flows/live.py:214/220) nunca rodou, e os `QODER: segundo plano` saíram por Agent interno ao Qoder Live, sem passar pelo orquestrador/portões do jarvis. Falta: ligar o despacho das ações reconhecidas pelo roteador (passo 3 do FALA, hoje nunca populado) no executor com a linha de portão impressa no log vivo.
3. FEITO — `jarvis.log:6542` `[QoderLive] task completed: Agent "Auditar se o renascimento trouxe o código" finished`; `activity.log:18001` `QODER: concluído — Auditar se o renascimento trouxe o código`.
4. FEITO — `activity.log:18002` `SYS: Acordei — conclusão de execução.` (a conclusão sobe para o cérebro e desce para a boca na mesma hora).
5. FEITO — `activity.log:18003` `Gino: Terminei em segundo plano: Auditar se o renascimento trouxe o código. [audio={… "audio_generated": true …}]`.

### FLUXO RESPOSTA — FEITO 2, 3; passo 1 EM DISCO SEM RASTRO
1. EM DISCO SEM RASTRO — busca de memória pelo cérebro nunca apareceu no log vivo: `logs/tool_bridge.log` tem zero entradas `memory_search` pós-boot (a ponte existe — core/tool_bridge.py:567); a única operação de memória medida do cérebro foi GRAVAÇÃO: `activity.log:18129` `QODER: ferramenta — mcp__jarvis__memory_note` (22:16:36 e 22:17:43). Falta: o cérebro do fluxo de resposta consumir o buscador (memory_search/recent_turns) com linha impressa no log vivo.
2. FEITO — `jarvis.log:6363` `[Rota] caminho=cerebro primeira_unidade_ms=7690` + `activity.log:17855` (resposta do cérebro entregue à boca).
3. FEITO — `activity.log:18087` `Gino: Confirmado, criador. [audio={… "audio_generated": true, "latency_ms": 1100 …}]`.

### FLUXO TRABALHO — FEITO 6, 7; passos 1–5 EM DISCO SEM RASTRO
1. EM DISCO SEM RASTRO — nenhuma confirmação "atuacao/execucao" registrada pós-boot (23 turnos; o CONFIRM_RE de core/flows/router.py:63 nunca populou `actions`). Falta: a fala confirmada chegar ao roteador com `actions` não-vazio no turno impresso.
2. EM DISCO SEM RASTRO — o "plano em arquivo" do f7927f0 não rodou no vivo: ~/.qoder/plans/ sem arquivo pós-boot (o mais novo é de 31/10); nada de `[QoderJob]` nem `plano` no activity.log desde 22:02; emissores em core/qoder_jobs.py:282-299. Falta: criar plano de ação real com arquivo + linha `[QoderJob]`.
3. EM DISCO SEM RASTRO — consequência do 2: a boca nunca informou plano ao usuário.
4. EM DISCO SEM RASTRO — espelho Obsidian vive (boot: `jarvis.log:6148` `[Obsidian] cofre pronto; 4 plano(s) espelhado(s)`), mas nenhum pedido de abertura na tela foi registrado pós-boot.
5. EM DISCO SEM RASTRO — zero `plano aprovado` pós-boot (emissor core/qoder_jobs.py:210); o portão de identidade por voz roda noutros contextos (`activity.log:17921` `SYS: indulgência: único cadastrado é o dono (score=0.18 s=3.4)`) mas nunca incidiu sobre aprovação de plano.
6. FEITO — criação medida (`activity.log:17860` "segundo plano") e notificação a cada mudança medida: `jarvis.log:6235` `[QoderLive] andamento falado: List project and logs directories` (26 linhas `andamento falado` pós-boot).
7. FEITO — `activity.log:18127` `Gino: Terminei em segundo plano: Integrar filtros de e-mail no main. [audio={… "audio_generated": true …}]`; com microfone barrado vira texto sem perda: `activity.log:18257` `Gino: […] [audio={… "audio_generated": false, … "origin": "aviso-texto"}]` (o aviso-texto já existia em 567c172, carregado no boot).

### FLUXO MAPEAMENTO WHATSAPP — FEITO 2, 3 e o ramo sem nome do 1; ramo com nome de persona EM DISCO SEM RASTRO
1. FEITO para "sem nome de persona → boca/cérebro": toda mensagem impressa virou turno imediato (ex. `activity.log:17850-17851`) com verificação de origem na recepção (`jarvis.log:6192` `[WhatsApp] chat direto do dono; resposta autônoma pedida (autorização de 600s).`). EM DISCO SEM RASTRO para "tem o nome → manda pros 2 E para a persona": o código está no HEAD E no processo vivo (e079d03, no main via merge 567c172 às 21:24, boot às 22:02 — core/whatsapp_watch.py:249 `addressed_persona`), mas zero mensagens com vocativo de persona chegaram pós-boot e o auditor `logs/personas/audit.jsonl` NÃO existe (diretório vazio) = zero eventos `whatsapp_persona_rota`. Falta: mensagem real citando persona e o evento `whatsapp_persona_rota` no audit.
2. FEITO para mensagem ao assistente — boca e cérebro recebem no mesmo segundo: `activity.log:18060` `[Fluxos] {'type': 'brief', … 'sentence': 'Confirmando tudo […]` + `activity.log:18070-18071` prewarm do cérebro. Para "mensagem COM nome de persona": EM DISCO SEM RASTRO (mesma prova negativa do passo 1).
3. FEITO — o fluxo da fala roda em cada mensagem: recebida 22:02:35 → resposta 22:02:52 (`activity.log:17850` → `:17855`).

### FLUXO MAPEAMENTO EMAIL — passo 1 FEITO; passos 2–6 EM DISCO SEM RASTRO; 7 sem contraprova
1. FEITO — `jarvis.log:6171` `[Mail] principal: tentativa 2 conectou em 1921 ms (imap.gmail.com:993)`; ressalva medida: a varredura não imprime nada quando a caixa está sem novidades — nenhum outro `[Mail]` pós-boot.
2. EM DISCO SEM RASTRO — filtro (core/email.py:2010, `match_filter`) só entrou no main às 22:13:01 (94886f4), depois do boot: está no disco em HEAD, NÃO no processo vivo. Falta: renascimento do processo + regra em `plugin_config.mail.accounts[].filters` + e-mail real; hoje não há linha de decisão de filtro no log.
3. EM DISCO SEM RASTRO — nenhum aviso de recebimento de e-mail pela boca pós-boot (activity.log sem linhas de e-mail; gancho em core/email.py:2007-2035).
4. EM DISCO SEM RASTRO — "senão apenas ignora" vive no mesmo caminho (core/email.py:2010 `fora do filtro: nenhum aviso`), zero execuções medidas.
5. EM DISCO SEM RASTRO — "verifica fluxo executável pelo corpo vs instruções": só existe como ação `classificar` com excerpt via BODY.PEEK (core/email.py:2013-2025, HEAD); nunca rodou pós-boot.
6. EM DISCO SEM RASTRO — nenhuma execução disparada por e-mail no log vivo.
7. Sem contraprova ativa: nada foi executado via e-mail pós-boot (nenhuma linha), o que é o comportamento do contrato — registrado como observação, não como passo cumprido por rastro positivo.

### FLUXO IDENTIFICACAO DO DONO — FEITO 4, 5 e ramo 5.2; passos 1, 2, 6 e ramo 5.1.1 EM DISCO SEM RASTRO
1. EM DISCO SEM RASTRO — pedido de cadastro de voz nunca chegou ao vivo: parser existe (main.py:1426), zero linhas de rota de cadastro pós-boot (as 3 ocorrências de "cadastro" no activity.log pós-boot são falas do dono e eco de grep, não o caminho).
2. EM DISCO SEM RASTRO — "pergunta se quer adicionar ou substituir amostras": janela existe em HEAD (core/voice_device_enroll.py), nunca abriu pós-boot. Cadastro de visitante com credenciais: auditor `logs/personas/audit.jsonl` inexistente = zero registros medidos de visitante.
4. FEITO — verificação de amostras do perfil de microfone atual medida em cada tentativa: `activity.log:17921` `SYS: indulgência: único cadastrado é o dono (score=0.18 s=3.4)` (perfil no device id suprimido).
5. FEITO — tentativa de identificação medida: `activity.log:17899` `voz não identificada como o dono: AS-norm -4.51 vs 0.00; […] voz=desconhecida` e a série 22:20–22:21 (ex. `activity.log:18202`).
   5.1.1 (achou → registra como memória) EM DISCO SEM RASTRO — nenhum match POSITIVO de voiceprint do dono pós-boot (todas as falas terminaram em `voz não identificada` ou indulgência); registro de memória por identificação própria não tem linha. Falta: fala do dono com match positivo + a linha de registro.
   5.2 (identifica TV/música/estática; registra amostra com categoria) FEITO — log vivo do processo atual `memory/audio_ledger/events/2026-11-05.jsonl:4861-4863`: `{"iso": "2026-11-05T22:20:00", … "label": "musica", "confidence": 0.5638 …}` / `"label": "tv", "confidence": 0.5658 …}`; pergunta ao dono medida: `activity.log:17957` `SYS: modo cinema — fala de não-dono repetida; perguntando ao dono uma vez.` + `:17960`.
6. EM DISCO SEM RASTRO — ramo "sem amostras no mic atual → pergunta se é o dono → cadastro dono/visitante": o perfil do mic vivo tem amostra cadastrada, o caminho `sem cadastro de voz neste microfone` (core/speaker_id.py:1192) não imprimiu nenhuma linha pós-boot.

Fila de trabalho real (não-FEITO, com o que falta exatamente):
1. FALA 3 / EXECUCAO 2 — orquestrador+portões nunca exercitados: falta fala executável chegar a `actions` não-vazio e imprimir `[Fluxos] portão de ferramenta …` (core/flows/live.py:214-221).
2. FALA 3 — carregamento de memória: falta turno com `lookups` não-vazio impresso (core/flows/router.py:285).
3. RESPOSTA 1 — busca de memória pelo cérebro: falta chamada real de `memory_search` (core/tool_bridge.py:567) com entrada no tool_bridge.log vivo.
4. TRABALHO 1–5 — plano em arquivo + boca informa + Obsidian na tela + aprovação por senha/voz: falta o caminho de core/qoder_jobs.py:282-299/210 rodar com `[QoderJob]`/`plano aprovado` no log vivo.
5. MAPEAMENTO WHATSAPP 1–2 — ramo com nome de persona: falta mensagem real citando persona e evento `whatsapp_persona_rota` em logs/personas/audit.jsonl (hoje o arquivo não existe).
6. MAPEAMENTO EMAIL 2–6 — filtro: código só em disco (94886f4, merge 22:13:01 > boot 22:02:19); falta renascimento do processo + regra em `plugin_config.mail.accounts[].filters` + e-mail que a atinja, com aviso/ignora/classificar impresso no vivo.
7. IDENTIFICACAO 1, 2, 5.1.1, 6 — cadastro de voz (adicionar/substituir), cadastro de visitante com audit, registro por match positivo de voiceprint e o ramo sem amostras no mic: falta cada um imprimir sua linha no log vivo.

*Visitantes podem ser elevados a permissões específicas ou gerais por ordem direta do dono (nota final do contrato).*
