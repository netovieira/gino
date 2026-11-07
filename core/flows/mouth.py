"""Boca: lfm3:1.7b local (Ollama/GPU). Fala, e só.

Procedência da troca: 05/11/2026, ordem do dono — LFM3-1.7B Q4_K_M no lugar do
qwen3:4b-instruct-2507-q4_K_M: mais leve (1,1 GB) e ~64% mais rápido no
mesmo caminho; o Qwen fica instalado como rota de volta.

Modelos Qwen3 podem ligar o thinking por padrão; com ``num_predict`` curto eles
gastam todos os tokens pensando e devolvem texto vazio (``done_reason=length``).
Por isso o payload sempre manda ``think: false`` — sem ele a boca fica muda. É
o mesmo modelo/caminho medido na pesquisa de 04/11/2026 (25 ms quentes, 127
tok/s, 2,5 GB, pico de 4,6 GB de VRAM com 4096 de contexto em 8 GB).

Uma chamada por frase, sem histórico. O contexto chega no brief do
orquestrador. A boca nunca decide ferramenta, nunca afirma resultado (lei a) e
nunca confirma ação (lei b) — se o modelo desobedecer, o guard rejeita e o
template determinístico fala.

O cliente HTTP é injetável (``caller``) para os testes nunca tocarem a rede. O
caller padrão :func:`stream_ollama` mede TTFT e primeira frase, como na PoC.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import guard as _guard
from .config import FlowsConfig
from .router import Decision

# UC-2 (FALA passo 2): ack intermediário da boca enquanto o cérebro tarda.
# Se em 5 s nenhum unidade de fala saiu no turno (modelo local frio/preso,
# brief suprimido), o runtime fala este template determinístico — modelo
# zero, rede zero, fato zero. A lei (a) é respeitada: a frase não anuncia
# ação nem ferramenta (passa por guard.announcement, medido 07/11/2026) e
# não afirma resultado (passa por guard.guard fase ``brief``).
INTERIM_SENTENCE = "Já estou vendo isso, criador."
INTERIM_ACK_MS = 5000.0
# UC-2 passo 4: timeout do Ollama REDUZIDO no caminho rápido da boca (brief/
# chat = o ack de <1 s). Medido com measure_latency em 06/11/2026, o lfm3
# quente responde em 76-85 ms; o teto de 25 s (mouth.timeout_s) só servia
# para segurar o worker com o modelo frio ou preso. O caminho rápido desiste
# em 6 s e cai no template determinístico — o portão intermediário (acima)
# cobre qualquer silêncio além disso. O complement (pós-fato) mantém o teto
# longo do config: ele nunca está no portão de <1 s.
FAST_ACK_TIMEOUT_S = 6.0

SYS_BRIEF = (
    "Você é a BOCA do Gino, um assistente falado em português do Brasil. "
    "Recebe do orquestrador apenas a INTENÇÃO de um pedido do dono e diz UMA "
    "frase curta, seca e ácida, com no máximo 8 palavras, mostrando que ouviu. "
    "PROIBIDO descrever a ação, a ferramenta ou o andamento: nunca use "
    "gerúndio (Abrindo, Conferindo, Tocando, Ajustando, Pausando, Anotando, "
    "Procurando), nunca diga 'vou abrir', 'vou tocar', 'executando', 'espera', "
    "'instante' nem o nome de qualquer ferramenta. "
    "REGRAS INVIOLÁVEIS: você não sabe resultado nenhum; nunca diga feito, "
    "pronto, aberto, tocando, anotado, enviado ou qualquer fato; nunca invente "
    "número, hora, nome ou ferramenta; não use aspas, prefixo ou explicação; "
    "responda só a frase.\n"
    "Exemplos:\n"
    "Intenção: tocar Queen. Alvo: Queen. -> Deixa comigo, criador.\n"
    "Intenção: abrir o WhatsApp. Alvo: WhatsApp. -> Certo, criador.\n"
    "Intenção: conferir a hora. Alvo: a hora. -> Já te digo, criador.\n"
    "Intenção: conversa. Alvo: o dono. -> Tudo em ordem por aqui, criador."
)
SYS_FREE = (
    "Você é a BOCA do Gino, um assistente falado em português do Brasil. "
    "A conversa está na rota livre: o cérebro ainda vai responder. Diga UMA "
    "frase curta, natural e calorosa (máx. 10 palavras) confirmando que ouviu "
    "e já está cuidando — sem responder à pergunta e sem dar opinião. "
    "REGRAS INVIOLÁVEIS: você não sabe resultado nenhum; nunca diga feito, "
    "pronto, aberto, tocando, anotado, enviado ou qualquer fato; nunca invente "
    "número, hora, nome ou ferramenta; nunca chame ferramenta nem confirme "
    "ação de risco; não use aspas, prefixo ou explicação; responda só a frase.\n"
    "Exemplos:\n"
    "Fala do dono: o que você acha do filme? -> Já te digo, criador.\n"
    "Fala do dono: estou cansado hoje. -> Entendi, criador.\n"
    "Fala do dono: me explica aquilo. -> Deixa eu organizar isso, criador."
)
SYS_CHAT = (
    "Você é o Gino, o assistente falado do dono, em português do Brasil. "
    "Esta é conversa curta: o orquestrador já decidiu que não há ferramenta, "
    "cálculo nem memória antiga no pedido. Responda em UMA ou DUAS frases "
    "faladas, diretas e com a persona (verbal primeiro, concreto, sem "
    "enfeite). REGRAS INVIOLÁVEIS: você NÃO executou nada neste turno — "
    "nunca afirme feito, pronto, aberto, tocando, anotado ou enviado; nunca "
    "narre o que está fazendo (gerúndio de ação é proibido); nunca invente "
    "número, hora, nome ou fato do mundo; se a resposta depender de qualquer "
    "uma dessas coisas, responda só à parte pessoal e seja breve. Sem aspas, "
    "sem prefixo, sem explicação: responda só a frase.\n"
    "Exemplos:\n"
    "Fala do dono: como você está? -> Bem, criador; máquina alegre e sem "
    "incêndio hoje.\n"
    "Fala do dono: bom dia -> Bom dia, criador. A casa está de pé."
)
SYS_COMPLEMENT = (
    "Você é a BOCA do Gino, um assistente falado em português do Brasil. "
    "Recebe FATOS de trabalhadores e diz UMA frase curta (máx. 18 palavras) "
    "entregando exatamente esses fatos ao dono. Relate, não dê ordens. Inclua "
    "todos os fatos. Não invente nada além dos FATOS; se um fato pede "
    "confirmação, peça a confirmação falada; não diga nomes de ferramentas, "
    "caminhos, JSON nem aspas; responda só a frase.\n"
    "Exemplos:\n"
    "Fatos: Queen tocando no Spotify. -> Queen tocando no Spotify.\n"
    "Fatos: Spotify aberto. Volume em 40%. -> Spotify aberto e volume em 40%.\n"
    "Fatos: nada foi executado; pedido cancelado. -> Nada foi executado."
)

# UP-8 (FLUXO TRABALHO passos 6-7): o andamento de um trabalho pesado em
# segundo plano. A lei é a do complemento SEM asserção: a linha do espelho é o
# fato observado, mas nenhuma etapa autoriza dizer que terminou, que deu certo
# ou que algo foi enviado. Por isso o prompt proíbe as palavras de resultado e
# a narrativa de ferramenta; o guard da fase ``complement`` é o portão final e,
# se ele cortar, entra o template determinístico (``_progress_templates``).
SYS_PROGRESS = (
    "Você é a BOCA do Gino, um assistente falado em português do Brasil. Você "
    "ouve de um espelho de execução o QUE MUDOU num trabalho pesado que roda "
    "em segundo plano e diz UMA frase curta (máx. 14 palavras) entregando essa "
    "mudança ao dono. REGRAS INVIOLÁVEIS: nada terminou neste turno — nunca "
    "diga feito, pronto, concluído, aberto, enviado, anotado nem 'deu certo'; "
    "nunca use gerúndio de ação (abrindo, executando, rodando, instalando, "
    "testando); nunca cite ferramenta, comando, caminho, JSON, número de linha "
    "ou aspas; não invente número, hora nem etapa que não está no fato; não "
    "peça confirmação; responda só a frase.\n"
    "Exemplos:\n"
    "Etapa: edicao. Fato do espelho: editar core/login.py -> Mudança no código "
    "do login, criador.\n"
    "Etapa: testes. Fato do espelho: $ pytest -q -> Uma rodada de testes no "
    "trabalho, criador.\n"
    "Etapa: busca. Fato do espelho: buscar token em core -> Uma busca nova no "
    "projeto."
)

# Template determinístico do andamento (mesma lei do modelo, sem modelo).
# Ordem: a etapa nomeada primeiro (é a que menos vaza), o trecho do espelho
# depois (só quando cabe na fala), a frase genérica por último. Cada candidato
# passa pelo guard; o primeiro que passar é o que sai.
PROGRESS_TEMPLATES = (
    "O trabalho em segundo plano segue com {stage}.",
    "O trabalho em segundo plano tem uma etapa nova: {brief}.",
    "O trabalho em segundo plano tem novidade.",
)
PROGRESS_BRIEF_CHARS = 90

# Extensão de arquivo de programa: o que nunca pode sair pela boca.
_CODE_EXT_RE = re.compile(
    r"\b[\w./_-]+\.(?:py|js|mjs|cjs|ts|tsx|json|md|sh|txt|log|toml|env|ya?ml"
    r"|html|css|csv|lock|rb|go|rs|java|kt|swift|c|cpp|h|ino|php|sql)\b",
    re.IGNORECASE)

# Rótulo falável da etapa do espelho (nunca o nome da ferramenta).
STAGE_WORDS = {
    "edicao": "uma mudança no código",
    "testes": "uma rodada de testes",
    "comando": "um passo novo",
    "leitura": "uma revisão do projeto",
    "busca": "uma busca no projeto",
    "consulta_externa": "uma consulta fora da máquina",
    "delegacao": "um ajudante novo",
    "plano": "o plano de ação",
    "inicio": "o começo do trabalho",
    "resposta": "uma resposta nova",
    "resultado": "um resultado novo",
    "ferramenta": "um passo novo",
    "resumo": "uma novidade do trabalho",
    "nota": "um lembrete do andamento",
}


def _clip_fact(text: str) -> str:
    """O fato do espelho em uma linha falável: sem código, sem aspas vazadas."""
    value = " ".join(str(text or "").split())
    for junk in ("`", '"', "{", "}", "$", "│"):
        value = value.replace(junk, "")
    value = value.strip(" .,;:-")
    if len(value) > PROGRESS_BRIEF_CHARS:
        value = value[:PROGRESS_BRIEF_CHARS]
    return value.rstrip(" ,;:.")


def _speakable(text: str) -> bool:
    """O trecho do espelho pode virar fala? Caminho, id e código não podem.

    O guard pega JSON e ferramenta conhecida; um ``core/login.py`` no meio da
    frase escapa dele e soa como telejornal para o dono. Critério conservador:
    aparecem ``/``, ``_``, ``-`` ou ponto de extensão, a frase determinística
    desce para a etapa nomeada (ou para a genérica).
    """
    value = str(text or "")
    if not value or len(value) > PROGRESS_BRIEF_CHARS:
        return False
    if any(ch in value for ch in ("/", "\\", "_", "-", "—", "*", "#", ":")):
        return False
    if "." in value.strip("."):
        return False
    return bool(value.strip())


def _leaks_path(text: str) -> bool:
    """A frase citou um arquivo de programa? ``core/login.py`` no ouvido do dono
    é vazamento, e o guard léxico não conhece caminho nenhum — nem ``/``.

    Vale para a frase do modelo e para o template: o andamento é falado em
    português, nunca em path.
    """
    value = str(text or "")
    if "/" in value or "\\" in value:
        return True
    return bool(_CODE_EXT_RE.search(value))


def _progress_templates(stage: str, fact: str) -> list[str]:
    """Candidatos determinísticos, do que vaza menos para o mais genérico."""
    label = " ".join(str(stage or "").split()).strip()
    human = STAGE_WORDS.get(label, "")
    brief = _clip_fact(fact)
    out: list[str] = []
    for template in PROGRESS_TEMPLATES:
        if "{stage}" in template:
            if human:
                out.append(template.format(stage=human))
        elif "{brief}" in template:
            if _speakable(brief):
                out.append(template.format(brief=brief))
        else:
            out.append(template)
    return out


def progress_facts(text: str, stage: str = "") -> list[dict[str, Any]]:
    """O fato do andamento para o guard: observado, SEM autorização de asserção.

    ``assertion`` falso é o coração do UP-8: a boca pode relatar a etapa, mas
    nenhuma linha de espelho deixa ela dizer "terminou", "abriu" ou "enviei".
    """
    return [{"spoken": " ".join(str(text or "").split()),
             "detail": str(stage or ""), "assertion": False}]


# ── UV-2: a boca não fala inglês nem jargão técnico ────────────────────────
#
# Lei do dono (31/10/2026 23:34, escrita em skills/conversation/SKILL.md):
# "nunca vaze palavra em inglês na fala; termo técnico inglês sem equivalente
# sai em português corrido ou não sai". O ``clean_sentence`` de antes só tirava
# aspas, quebra de linha e o rótulo do falante — o inglês passava inteiro. No
# activity.log de 07/11/2026 a saída de voz ainda registrava "Extract final
# suite counts", "Identify test at ~10% position via collection order" e
# "a leitura _check_material_proof implementation".
#
# O portão tem três peças, nesta ordem:
#   1. TECH_TERMS_PT  dá nome em português ao termo técnico, com o artigo e a
#      contração certos ("no commit" -> "na entrega", "os builds" -> "as
#      compilações");
#   2. ALLOWED_ENGLISH_NAMES  nome próprio em inglês que o dono pronuncia assim
#      mesmo (Spotify, Queen, WhatsApp) passa intacto e não conta como
#      vazamento;
#   3. o que sobrar em inglês (léxico, terminação -ing/-ed/-tion..., bandeira de
#      comando, snake_case, caminho de arquivo) CALA a frase: ``clean_sentence``
#      devolve string vazia e o template determinístico do chamador fala no
#      lugar. Recitar código ou inglês no ouvido do dono nunca é melhor que
#      silêncio.
#
# Os léxicos são deliberadamente conservadores: palavra ambígua com o
# português ("a", "as", "no", "do", "data", "status", "final", "volume") fica
# FORA da lista de inglês, porque derrubaria fala limpa; termo sem equivalente
# português silencia a frase em vez de inventar tradução.

# Termo técnico em inglês -> como se diz em português (com o artigo). Só nomes
# e formas cujo equivalente é curto o bastante para caber numa fala da boca.
TECH_TERMS_PT: dict[str, str] = {
    # código e versão
    "commit": "a entrega", "commits": "as entregas",
    "push": "o envio", "pushes": "os envios",
    "pull": "a retirada", "merge": "a integração", "merges": "as integrações",
    "build": "a compilação", "builds": "as compilações",
    "deploy": "a publicação", "deploys": "as publicações",
    "release": "a versão publicada", "releases": "as versões publicadas",
    "repo": "o repositório", "repos": "os repositórios",
    "repository": "o repositório", "branch": "o ramo", "branches": "os ramos",
    "version": "a versão", "versions": "as versões",
    "diff": "a diferença", "diffs": "as diferenças",
    "stats": "as estatísticas", "count": "a contagem", "counts": "as contagens",
    "suite": "a bateria de testes", "suites": "as baterias de testes",
    "summary": "o resumo", "summaries": "os resumos",
    "test": "o teste", "tests": "os testes", "testing": "o teste",
    "pytest": "os testes", "unitest": "o teste unitário",
    "check": "a conferência", "checks": "as conferências",
    "checking": "a conferência", "verification": "a conferência",
    "bug": "o defeito", "bugs": "os defeitos", "debug": "a depuração",
    "debugging": "a depuração", "fix": "o conserto", "fixes": "os consertos",
    "fixing": "o conserto", "workaround": "o jeitinho",
    "script": "o programa", "scripts": "os programas",
    "command": "o comando", "commands": "os comandos",
    "workflow": "o fluxo", "workflows": "os fluxos",
    "pipeline": "o fluxo", "runtime": "a execução", "thread": "a fila de execução",
    "threads": "as filas de execução", "process": "o processo",
    "queue": "a fila", "queues": "as filas",
    "cache": "a memória temporária", "backup": "a cópia de segurança",
    "backups": "as cópias de segurança",
    "config": "a configuração", "configs": "as configurações",
    "configuration": "a configuração", "settings": "as configurações",
    "setup": "a preparação", "default": "o padrão", "defaults": "os padrões",
    "template": "o modelo", "templates": "os modelos",
    "install": "a instalação", "installation": "a instalação",
    "installing": "a instalação", "update": "a atualização",
    "updates": "as atualizações", "updating": "a atualização",
    "upgrade": "a atualização", "patch": "o correctivo",
    # máquina e voz
    "error": "o erro", "errors": "os erros", "warning": "o aviso",
    "warnings": "os avisos", "fail": "a falha", "fails": "as falhas",
    "failed": "a falha", "failing": "a falha", "failure": "a falha",
    "output": "a saída", "inputs": "as entradas", "input": "a entrada",
    "result": "o resultado", "results": "os resultados",
    "response": "a resposta", "responses": "as respostas",
    "request": "o pedido", "requests": "os pedidos",
    "file": "o arquivo", "files": "os arquivos",
    "folder": "a pasta", "folders": "as pastas", "directory": "a pasta",
    "path": "o endereço", "paths": "os endereços",
    "line": "a linha", "lines": "as linhas",
    "page": "a página", "pages": "as páginas",
    "window": "a janela",
    "screen": "a tela", "screenshot": "a captura de tela",
    "browser": "o navegador", "server": "o servidor", "servers": "os servidores",
    "network": "a rede", "device": "o aparelho", "devices": "os aparelhos",
    "printer": "a impressora", "speaker": "o alto-falante",
    "microphone": "o microfone", "battery": "a bateria",
    "camera": "a câmera", "player": "o reprodutor",
    "playlist": "a lista de reprodução", "download": "a transferência",
    "downloads": "as transferências", "upload": "o envio", "uploads": "os envios",
    # conta e pessoa
    "account": "a conta", "accounts": "as contas", "user": "o usuário",
    "users": "os usuários", "username": "o usuário", "password": "a senha",
    "passwords": "as senhas", "token": "o código de acesso",
    "tokens": "os códigos de acesso",
    "session": "a sessão", "sessions": "as sessões",
    "memory": "a memória", "note": "a nota", "notes": "as notas",
    "message": "a mensagem", "messages": "as mensagens",
    "task": "a tarefa", "tasks": "as tarefas",
    "tool": "a ferramenta", "tools": "as ferramentas",
    "agent": "o ajudante", "agents": "os ajudantes",
    "subagent": "o ajudante", "model": "o modelo", "models": "os modelos",
    "prompt": "a instrução", "prompts": "as instruções",
    "step": "a etapa", "steps": "as etapas", "stage": "a etapa",
    "stages": "as etapas", "progress": "o andamento",
    "report": "o relatório", "reports": "os relatórios",
    "dashboard": "o painel", "notification": "a notificação",
    "notifications": "as notificações", "schedule": "a agenda",
    "calendar": "a agenda", "reminder": "o lembrete", "reminders": "os lembretes",
    "meeting": "a reunião", "meetings": "as reuniões",
    # mídia e rotina
    "song": "a música", "songs": "as músicas", "track": "a faixa",
    "tracks": "as faixas", "album": "o álbum", "albums": "os álbuns",
    "artist": "o artista", "artists": "os artistas",
    "movie": "o filme", "movies": "os filmes", "video": "o vídeo",
    "videos": "os vídeos", "episode": "o episódio", "episodes": "os episódios",
    "season": "a temporada", "series": "a série",
    "weather": "o tempo", "news": "as notícias", "search": "a busca",
    "searching": "a busca", "query": "a consulta", "queries": "as consultas",
    "price": "o preço", "prices": "os preços", "product": "o produto",
    "products": "os produtos", "cart": "o carrinho", "checkout": "o pagamento",
    "payment": "o pagamento", "invoice": "a fatura", "purchase": "a compra",
    "shopping": "as compras", "delivery": "a entrega", "address": "o endereço",
    "flight": "o voo", "flights": "os voos", "hotel": "a hospedagem",
    "working": "o trabalho", "work": "o trabalho", "loading": "o carregamento",
    "processing": "o processamento",
    "sending": "o envio", "opening": "a abertura", "closing": "o fechamento",
    "reading": "a leitura", "writing": "a escrita", "finding": "a busca",
    "summarizing": "o resumo", "showing": "a exibição",
}

# Nomes próprios em inglês que o dono pronuncia assim mesmo: serviço, app,
# artista, marca. Entram na lista quando o dono os nomeia numa ordem falada —
# não porque o modelo quis. Nada aqui é termo técnico com equivalente português.
ALLOWED_ENGLISH_NAMES: frozenset[str] = frozenset({
    "Spotify", "WhatsApp", "Youtube", "YouTube", "Netflix", "Instagram",
    "Telegram", "Gmail", "Google", "Chrome", "Firefox", "Edge", "Safari",
    "Android", "Iphone", "Ipod", "Ipad", "Ios", "Windows", "Linux", "Ubuntu",
    "Macos", "Mac", "Bluetooth", "Airpods", "Alexa", "Siri", "Obsidian",
    "Nextcloud", "Ollama", "Whisper", "Qoder", "Qwen", "Github", "Gitlab",
    "Trello", "Asana", "Notion", "Slack", "Discord", "Hub", "Docker",
    "Queen", "Beatles", "Pink", "Floyd", "Genesis", "Radiohead", "Nirvana",
    "Coldplay", "Imagine", "Dragons", "Eminem", "Rihanna", "Beyonce", "U2",
    "Globo", "Play", "Playstation", "Xbox", "Nintendo", "Pokemon", "Minecraft",
    "Steam", "Timeshift", "Home Assistant", "Google Assistant", "Copilot",
    "Activity Log", "Activity", "Project", "Silero", "VAD", "Pipewire",
})

# Inglês que não tem equivalente falável curto — a frase cala em vez de sair
# em inglês. Só palavras que NÃO existem em português: nada de "a", "as", "in",
# "on", "no", "do", "data", "status", "final", "volume", "note", "item".
ENGLISH_WORDS: frozenset[str] = frozenset({
    # função
    "the", "this", "that", "these", "those", "there", "their", "them", "they",
    "your", "yours", "you", "yourself", "we", "us", "our", "ours", "his",
    "hers", "her", "him", "its", "it", "he", "she", "i", "me", "my", "mine",
    "and", "or", "but", "nor", "yet", "because", "although", "though", "unless",
    "with", "without", "within", "from", "into", "onto", "upon", "about",
    "above", "below", "under", "over", "between", "against", "during", "since",
    "while", "where", "when", "which", "who", "whom", "whose", "why", "how",
    "what", "is", "are", "was", "were", "be", "been", "being", "am",
    "does", "did", "doing", "done", "donee", "had", "has", "have", "having",
    "will", "would", "shall", "should", "can", "cannot", "could", "may",
    "might", "must", "need", "dare", "not", "yes", "okay", "please", "thanks",
    "thank", "sorry", "hello", "hi", "bye", "welcome", "dear",
    "some", "any", "many", "much", "more", "most", "less", "least", "all",
    "each", "every", "both", "either", "neither", "other", "others", "another",
    "such", "very", "too", "just", "only", "even", "also", "still", "already",
    "always", "never", "often", "rarely", "soon", "later", "now", "then",
    "here", "near", "far", "again", "almost", "enough", "rather", "perhaps",
    "maybe", "well", "best", "better", "worst", "worse", "same", "different",
    "own", "same", "few", "little", "big", "small", "long", "short", "high",
    "low", "new", "old", "good", "bad", "nice", "great", "fine", "sure",
    # verbos e gerúndios de ferramenta (a boca não narra ação nem em inglês)
    "running", "opened", "closed", "created", "deleted", "started", "stopped",
    "restarted", "sending", "sent", "received", "getting", "got", "putting",
    "making", "made", "taking", "took", "giving", "given", "knowing", "known",
    "thinking", "trying", "tried", "waiting", "waited", "looking", "watching",
    "using", "used", "adding", "added", "removing", "removed", "creating",
    "reading", "writing", "written", "calling", "called", "checking", "tested",
    "building", "built", "committing", "committed", "pushed", "pulled",
    "merged", "deploying", "deployed", "installing", "installed", "updating",
    "updated", "loading", "loaded", "unloading", "handling", "handled",
    "fetching", "fetched", "parsing", "parsed", "validating", "validated",
    "formatting", "formatted", "retrying", "retried", "rerun", "rerunning",
    "skipping", "skipped", "dropping", "dropped", "killing", "killed",
    "spawning", "kicking", "hooking", "logging", "queued", "queuing",
    "flushing", "indexing", "tagging", "marked", "marking", "naming", "named",
    "messaging", "notifying", "announcing", "acknowledging", "ignoring",
    "ignored", "passing", "passed", "failing", "printing", "printed",
    "grabbing", "grabb", "extract", "extracting", "extracted", "identify",
    "identifying", "identified", "inspect", "inspecting", "inspected",
    "verify", "verifying", "verified", "ensure", "ensuring", "ensure",
    "show", "shows", "shown", "hide", "hides", "get", "gets", "set", "sets",
    "list", "lists", "listed", "listing", "find", "finds", "found",
    "select", "selected", "filter", "filtered", "sorting", "sorted",
    "generate", "generated", "generating", "execute", "executed", "executing",
    "process", "processed", "compute", "computed", "resolve", "resolved",
    "compare", "compared", "collect", "collected", "collecting", "apply",
    "applied", "append", "appended", "store", "stored", "save", "saved",
    "submit", "submitted", "cancel", "canceled", "cancelled", "abort",
    "aborted", "retry", "continue", "continued", "stop", "stopped", "start",
    # adjetivos e advérbios que só existem em inglês
    "unable", "available", "unavailable", "ready", "full", "final",
    "global", "local", "remote", "current", "latest", "previous", "next",
    "first", "last", "second", "third", "single", "double", "triple",
    "whole", "empty", "blank", "clean", "dirty", "stale", "fresh", "busy",
    "idle", "active", "inactive", "enabled", "disabled", "allowed", "blocked",
    "missing", "module", "modules", "function", "functions", "variable",
    "variables", "string", "strings", "number", "numbers", "boolean",
    "integer", "array", "arrays", "object", "objects", "class", "classes",
    "method", "methods", "field", "fields", "value", "values", "key", "keys",
    "type", "types", "size", "sizes", "length", "width", "height", "weight",
    "order", "orders", "position", "positions", "collection", "collections",
    "subset", "subsets", "usage", "usages", "implementation", "implementations",
    "dependency", "dependencies", "documentation", "framework", "libraries",
    "library", "package", "packages", "module", "interface", "pattern",
    "feature", "features", "section", "portion", "chunk", "chunks", "batch",
    "batches", "round", "rounds", "cycle", "cycles", "attempt", "attempts",
    "sample", "samples", "snippet", "snippets", "payload", "payloads",
    "header", "headers", "body", "bodies", "marker", "markers", "flag",
    "flags", "scope", "range", "ranges", "target", "targets", "source",
    "sources", "origin", "origins", "layer", "layers", "level", "levels",
    "threshold", "timeouts", "deadline", "deadlines", "latency", "throughput",
    "backend", "frontend", "fullstack", "website", "websites", "webhook",
    "webhooks", "endpoint", "endpoints", "database", "databases", "schema",
    "schemas", "query", "server", "servers", "client", "clients", "socket",
    "sockets", "protocol", "handshake", "gateway", "browser", "browsers",
    "keyboard", "mouse", "pointer", "cursor", "folder", "folders", "label",
    "labels", "icon", "icons", "menu", "menus", "button", "buttons", "panel",
    "panels", "dialog", "popup", "widget", "widgets", "session", "sessions",
    "history", "histories", "context", "contexts", "default", "defaults",
    "placeholder", "prefix", "suffix", "pattern", "patterns", "matcher",
    "worker", "workers", "foo", "bar", "baz", "qux", "lorem", "ipsum",
})

# Empréstimos que já têm cidadania na fala do dono (a própria SKILL.md manda
# dizer "deixa eu ver no log"): valem como prova de português, não como inglês.
PORTUGUESE_WORDS: frozenset[str] = frozenset({
    "de", "da", "do", "das", "dos", "em", "no", "na", "nos", "nas", "um",
    "uma", "uns", "umas", "pra", "pro", "pelo", "pela", "aos", "as",
    "que", "se", "mas", "sim", "nao", "pode", "posso", "claro", "tao",
    "isso", "isto", "este", "esta", "esses", "essas", "aqui", "ali", "la",
    "agora", "hoje", "ontem", "amanha", "depois", "antes", "ainda", "ja",
    "entao", "tambem", "muito", "pouco", "bem", "bom", "boa", "mau", "mal",
    "tudo", "nada", "algum", "alguma", "nenhum", "ninguem", "quem", "qual",
    "quais", "quanto", "quanta", "como", "quando", "onde", "porque", "motivo",
    "voce", "voces", "eu", "tu", "te", "ti", "me", "mi", "nosso", "nossa",
    "seu", "sua", "meu", "minha", "criador", "dono", "dona", "gente", "pessoa",
    "fala", "falou", "falar", "voz", "audio", "som", "casa", "chave", "porta",
    "janela", "tela", "linha", "parte", "ponto", "conta", "texto", "nota",
    "lembrete", "tarefa", "teste", "testes", "arquivo", "arquivos", "pasta",
    "pastas", "log", "logs", "app", "apps", "site", "sites", "web", "ok",
    "play", "mouse", "email", "fluxo", "fluxos", "ordem", "ordens", "hora",
    "horas", "minuto", "minutos", "segundo", "segundos", "dia", "dias",
    "noite", "noites", "tarde", "manha", "feito", "feita", "pronto", "pronta",
    "certo", "certa", "aberto", "aberta", "ativo", "ativa", "vale",
    "obrigado", "obrigada", "bora", "beleza", "massa", "legal", "show",
    "ruim", "erro", "erros", "aviso", "avisos", "falha", "falhas", "busca",
    "buscas", "lista", "listas", "fila", "filas", "etapa", "etapas", "fase",
    "fases", "nome", "nomes", "numero", "numeros", "valor", "valores",
    "resultado", "resultados", "resposta", "respostas", "mensagem",
    "mensagens", "andamento", "mudanca", "mudancas", "trabalho", "trabalhos",
    "ajudante", "ajudantes", "ferramenta", "ferramentas", "modelo", "modelos",
    "instrucao", "instrucoes", "codigo", "programa", "programas", "comando",
    "comandos", "execucao", "saida", "entrada", "pedido", "pedidos",
    "conferencia", "conferencias", "comprimento", "largura", "altura",
    "passo", "passos", "seguinte", "proxima", "proximo", "ultima", "ultimo",
    "penultima", "penultimo", "inicio", "comeco", "fim", "inicial", "total",
    "parcial", "novo", "nova", "novos", "novas", "velho", "velha", "antigo",
    "antiga", "final", "global", "local", "interface", "menu", "cursor",
    "notebook", "string", "serie", "audio", "video", "base", "caso", "tipo",
    "modo", "forma", "ideia", "conta", "banco", "rede", "fuso", "show",
    # palavras portuguesas que o transcritor devolve sem acento
    "media", "area", "historia", "memoria", "agenda", "senha", "janela",
    "login", "logins", "logout",
})

# Prioridade dos léxicos, sem sobreposição: termo com equivalente português é
# TRADUZIDO; nome próprio autorizado PASSA; o resto do inglês CALA a frase.
ENGLISH_WORDS = ENGLISH_WORDS - set(TECH_TERMS_PT) - PORTUGUESE_WORDS - frozenset(
    n.lower() for n in ALLOWED_ENGLISH_NAMES)

# Forma de palavra do português (terminação nossa: desinência, vogal final...).
_PT_SHAPE_RE = re.compile(
    r"(?:ando|endo|indo|ado|ada|ados|adas|ido|ida|idos|idas|ao|aos|oes|coes"
    r"|mente|dade|tude|eza|cao|coes|ista|ismo|eira|eiro|avam|ava|iao|iam|aram"
    r"|eram|amos|emos|imos|ou|ei|iu|ia|ar|er|ir|or|az|ez|iz|al|ol|ul|il|el"
    r"|us|os|as|es|is|a|e|i|o|u)$")
# Forma que só existe em inglês. Deliberadamente estre: nada de "-ar", "-or",
# "-ente", que são terminações portuguesas (navegador, melhor, vidente).
_EN_SHAPE_RE = re.compile(
    r"(?:ing|ied|tion|sion|ness|ment|ous|ive|ful|less|ize|istic)$",
    re.IGNORECASE)
_ACCENT_RE = re.compile(r"[ãõçáéíóúâêôàüèìòùá]", re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# Identificador de código solto: snake_case, dunder, camelCase, bandeira de
# comando. Nada disso é palavra de nenhuma língua falada pelo dono.
_SNAKE_RE = re.compile(r"\b\w*_{1,2}\w+\b|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*")
_FLAG_RE = re.compile(r"(?<![\w-])--?[A-Za-z][\w-]*")
_NAME_ALT = "|".join(sorted((re.escape(n) for n in ALLOWED_ENGLISH_NAMES),
                            key=len, reverse=True))
_NAME_RE = re.compile(rf"\b(?:{_NAME_ALT})\b", re.IGNORECASE) if _NAME_ALT \
    else None
_LEAD_WORDS = ("o|a|os|as|um|uma|no|na|nos|nas|do|da|dos|das|ao|aos|à|às"
               "|pelo|pela|pelos|pelas|num|numa|de|em|por|para")
_TECH_ALT = "|".join(sorted((re.escape(k) for k in TECH_TERMS_PT),
                             key=len, reverse=True))
_TECH_RE = re.compile(
    r"(?:(?P<lead>\b(?:" + _LEAD_WORDS + r")\s+))?(?P<term>\b(?:"
    + _TECH_ALT + r")\b)", re.IGNORECASE)
# Artigo + preposição -> a forma contraída que a boca precisa pronunciar.
_LEAD_PREP = {
    "o": "", "a": "", "os": "", "as": "", "um": "", "uma": "",
    "no": "em", "na": "em", "nos": "em", "nas": "em", "num": "em",
    "numa": "em", "em": "em", "do": "de", "da": "de", "dos": "de",
    "das": "de", "de": "de", "ao": "a", "aos": "a", "à": "a", "às": "a",
    "pelo": "por", "pela": "por", "pelos": "por", "pelas": "por",
    "por": "por", "para": "para",
}
_CONTRACTION = {
    ("em", "o"): "no", ("em", "a"): "na", ("em", "os"): "nos",
    ("em", "as"): "nas", ("em", "um"): "num", ("em", "uma"): "numa",
    ("de", "o"): "do", ("de", "a"): "da", ("de", "os"): "dos",
    ("de", "as"): "das", ("de", "um"): "de um", ("de", "uma"): "de uma",
    ("a", "o"): "ao", ("a", "a"): "à", ("a", "os"): "aos", ("a", "as"): "às",
    ("por", "o"): "pelo", ("por", "a"): "pela", ("por", "os"): "pelos",
    ("por", "as"): "pelas",
}


def _hide_names(text: str) -> tuple[str, dict[str, str]]:
    """Esconde os nomes próprios autorizados; devolve (texto, mapa)."""
    if _NAME_RE is None:
        return str(text or ""), {}
    seen: dict[str, str] = {}

    def repl(match: re.Match) -> str:
        raw = match.group(0)
        key = "\x01%d\x01" % len(seen)
        seen[key] = raw
        return key

    return _NAME_RE.sub(repl, str(text or "")), seen


def _restore_names(text: str, hidden: dict[str, str]) -> str:
    out = str(text or "")
    for key, raw in hidden.items():
        out = out.replace(key, raw)
    return out


def _article_for(lead: str, article: str) -> str:
    """O artigo que a boca deve pronunciar depois da troca."""
    if not lead:
        return article
    prep = _LEAD_PREP.get(lead.lower())
    if prep is None or not prep:
        return article
    return _CONTRACTION.get((prep, article), f"{prep} {article}")


def translate_tech_terms(text: str) -> str:
    """Termo técnico em inglês -> nome em português, com artigo e contração.

    "no commit" vira "na entrega", "os builds" viram "as compilações". Nome
    próprio autorizado e trecho de código não são tocados aqui — o portão de
    vazamento cuida deles.
    """
    value = str(text or "")
    if not value.strip():
        return value
    hidden, names = _hide_names(value)
    out = _TECH_RE.sub(_tech_repl, hidden)
    out = re.sub(r"\s+", " ", out).strip()
    return _restore_names(out, names)


def _tech_repl(match: re.Match) -> str:
    """A troca de um termo técnico, com o artigo que a frase pede."""
    term = str(match.group("term") or "").lower()
    value = TECH_TERMS_PT.get(term)
    if not value:
        return match.group(0)
    article, _, rest = value.partition(" ")
    if not rest:                          # valor sem artigo: usa como veio
        phrase = value
    else:
        lead = (match.group("lead") or "").strip()
        if lead:
            # veio "o commit", "no commit", "dos builds": artigo trocado ou
            # contraído com o gênero do nome em português.
            phrase = f"{_article_for(lead, article)} {rest}"
        else:
            # sem artigo antes ("dezoito commits", "sem commit"): o nu vai
            # sozinho, enfiar "as entregas" ali soaria a robô.
            phrase = rest
    head = term if str(match.group("term") or "")[:1].isupper() \
        else (match.group("lead") or "")[:1]
    if head[:1].isupper() or (match.start() == 0 and phrase[:1].isalpha()):
        phrase = phrase[:1].upper() + phrase[1:]
    return phrase


def english_leak(text: str) -> str:
    """A primeira palavra em inglês que não pode ser falada, ou ''."""
    value = str(text or "")
    if not value.strip():
        return ""
    hidden, _names = _hide_names(value)
    english = 0
    portuguese = 0
    first = ""
    for word in _WORD_RE.findall(hidden):
        low = word.lower()
        if low in TECH_TERMS_PT or low in PORTUGUESE_WORDS:
            continue
        if _ACCENT_RE.search(word):
            portuguese += 1
            continue
        if low in ENGLISH_WORDS or (len(low) >= 5 and _EN_SHAPE_RE.search(low)
                                    and not _PT_SHAPE_RE.search(low)):
            english += 1
            first = first or word
            continue
        if _PT_SHAPE_RE.search(low):
            portuguese += 1
    if not english:
        return ""
    if english >= 2 and not portuguese:
        return f"frase em ingles: {first!r}"
    return f"ingles na fala: {first!r}"


def jargon_leak(text: str) -> str:
    """O identificador de código ou caminho que não pode ser falado, ou ''."""
    value = str(text or "")
    if not value.strip():
        return ""
    hidden, _names = _hide_names(value)
    if _leaks_path(hidden):
        found = _CODE_EXT_RE.search(hidden)
        return f"caminho ou codigo na frase: {(found.group(0) if found else hidden)[:60]!r}"
    for pattern in (_SNAKE_RE, _CAMEL_RE, _FLAG_RE):
        match = pattern.search(hidden)
        if match:
            return f"jargao tecnico na frase: {match.group(0)!r}"
    return ""


def speech_leak(text: str) -> str:
    """Por que esta frase NÃO pode sair pela boca; '' quando pode.

    Uma frase já traduzida (:func:`translate_tech_terms`) passa a valer o que
    sobrou dela: é o portão que os chamadores usam para registrar o motivo de a
    boca ter ficado muda.
    """
    return jargon_leak(text) or english_leak(text)


def clean_sentence(text: str) -> str:
    """A fala da boca: uma linha, sem rótulo, sem inglês e sem código.

    UV-2 (07/11/2026): além da limpeza de sempre, o termo técnico inglês ganha
    o nome em português (:func:`translate_tech_terms`) e o que não couber aí —
    frase estrangeira, identificador, caminho de arquivo — devolve string
    VAZIA. Todo chamador tem um template determinístico para esse caso, e
    silêncio é melhor que inglês no ouvido do dono.
    """
    text = str(text or "").strip()
    text = re.split(r"\n", text)[0].strip()
    text = text.strip(" \t\"'“”")
    text = re.sub(r"^(frase|resposta|gino)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if speech_leak(text):
        return ""
    text = translate_tech_terms(text)
    if speech_leak(text):
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ── fim do portão de idioma (UV-2) ─────────────────────────────────────────


def stream_ollama(messages: list[dict[str, str]], max_tokens: int,
                  *, model: str, url: str, timeout_s: float,
                  keep_alive: str = "10m", temperature: float = 0.3,
                  json_mode: bool = False, early_sentence: bool = False,
                  cancel_ev: threading.Event | None = None,
                  deadline_s: float | None = None) -> dict[str, Any]:
    """Chamada streaming ao Ollama; devolve texto, TTFT e 1ª frase.

    ``deadline_s`` (UC-2 passo 4): teto absoluto da chamada, medido do início
    ao último chunk. Estourado, a stream é abandonada com o que chegou — o
    chamador do caminho rápido (brief/chat) cai no template determinístico em
    vez de segurar o worker até o timeout HTTP. Sem o parâmetro, o
    comportamento é o de sempre (``complement``, que espera os fatos).
    """
    started = time.monotonic()
    out: dict[str, Any] = {"text": "", "ttft_ms": None,
                           "first_sentence_ms": None, "end_ms": None,
                           "error": None, "aborted": False,
                           "deadline_hit": False}
    payload: dict[str, Any] = {
        "model": model, "messages": messages, "stream": True,
        "keep_alive": keep_alive,
        # Modelos Qwen3/Bonsai pensam por padrão e, com num_predict curto,
        # entregam resposta vazia (done_reason=length). A boca quer a frase,
        # não o raciocínio: thinking desligado em toda chamada.
        "think": False,
        # Contexto curto de propósito: a boca só vê um brief de poucas linhas.
        # 4096 é o teto medido que mantém o modelo 100% na GPU de 8 GB junto
        # com o Whisper (~1,2 GB). Sem o teto, o contexto nativo de 256K do
        # qwen3 estouraria a placa.
        "options": {"temperature": temperature, "num_predict": max_tokens,
                    "num_ctx": 4096},
    }
    if json_mode:
        payload["format"] = "json"
    response = None
    try:
        import requests
        response = requests.post(f"{url}/api/chat", json=payload, stream=True,
                                 timeout=(3, timeout_s))
        response.raise_for_status()
        buf = ""
        for line in response.iter_lines(decode_unicode=True):
            if cancel_ev is not None and cancel_ev.is_set():
                out["aborted"] = True
                break
            if (deadline_s is not None
                    and (time.monotonic() - started) >= float(deadline_s)):
                out["deadline_hit"] = True
                break
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except (TypeError, ValueError):
                continue
            content = (chunk.get("message") or {}).get("content") or ""
            if content:
                if out["ttft_ms"] is None:
                    out["ttft_ms"] = (time.monotonic() - started) * 1000
                buf += content
                if early_sentence:
                    match = re.search(r"[.!?…](?:\s|$)", buf)
                    if match:
                        buf = buf[:match.end()]
                        out["first_sentence_ms"] = (
                            time.monotonic() - started) * 1000
                        break
            if chunk.get("done"):
                break
        out["text"] = clean_sentence(buf)
    except Exception as exc:  # modelo/rede fora: o chamador usa o template
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
    out["end_ms"] = (time.monotonic() - started) * 1000
    if out["first_sentence_ms"] is None:
        out["first_sentence_ms"] = out["end_ms"]
    return out


@dataclass
class MouthResult:
    sentence: str = ""
    ms: float = 0.0
    ttft_ms: float = 0.0
    raw: str = ""
    error: str = ""
    fixed: bool = False
    guard_tripped: str = ""
    missing_facts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sentence": self.sentence, "ms": round(self.ms, 1),
            "ttft_ms": round(self.ttft_ms, 1), "raw": self.raw,
            "error": self.error, "fixed": self.fixed,
            "guard_tripped": self.guard_tripped,
            "missing_facts": list(self.missing_facts),
        }


class Mouth:
    def __init__(self, config: FlowsConfig,
                 caller: Callable[..., dict[str, Any]] | None = None,
                 history: Callable[[], list[dict[str, Any]]] | None = None):
        self.config = config
        self.caller = caller or stream_ollama
        self.history = history
        self.calls = 0
        self.guard_trips = 0
        self.failures = 0

    # ── chamadas ────────────────────────────────────────────────────────────
    def warm(self) -> dict[str, Any]:
        if not self.config.mouth_enabled:
            return {"error": "boca desligada", "ms": 0.0}
        started = time.monotonic()
        result = self._call(
            [{"role": "user", "content": "oi"}], 2, early_sentence=False,
            temperature=0.0, cancel_ev=None)
        return {"error": result.get("error"),
                "ms": result.get("end_ms")
                or (time.monotonic() - started) * 1000}

    def brief(self, text: str, decision: Decision,
              cancel_ev: threading.Event | None = None,
              *, allow_model: bool = True,
              context: str = "") -> MouthResult:
        route = decision.route
        if (decision.intent in ("confirm_required", "clarify")
                or not self.config.mouth_speaks_route(route)
                or not self.config.mouth_enabled
                or not allow_model):
            return MouthResult(
                sentence=_guard.template_for(None, decision.draft),
                fixed=True, ms=0.0, ttft_ms=0.0, raw="[brief fixo]")
        free = route == "livre" or decision.intent == "conversa"
        if free:
            # Rota livre: a boca local fala com as últimas falas e o contexto
            # do turno. Ela só fala — nenhuma ferramenta é oferecida no prompt.
            history = self._recent_lines()
            user = ((f"Conversa recente:\n{history}\n" if history else "")
                    + f"Contexto do turno: {context or 'livre'}\n"
                    + f"Fala do dono: {text}\nSua frase:")
            system = SYS_FREE
        else:
            user = (f"Pedido do dono: {text}\n"
                    f"Intenção decidida: {decision.brief_label}\n"
                    f"Alvo: {decision.target or 'nenhum'}"
                    + (f"\nContexto do turno: {context}" if context else "")
                    + "\nSua frase:")
            system = SYS_BRIEF
        self.calls += 1
        result = self._call(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            self.config.mouth_brief_max_tokens, early_sentence=True,
            temperature=0.35, cancel_ev=cancel_ev, fast_ack=True)
        sentence = clean_sentence(result.get("text") or "")
        tripped = ""
        if sentence:
            ok, why = _guard.guard(sentence, None, "brief")
            if not ok:
                self.guard_trips += 1
                tripped = why
                sentence = ""
        elif speech_leak(result.get("text") or ""):
            # UV-2: o modelo falou inglês ou código; a frase foi calada antes
            # do guard. O motivo sai no mesmo campo para a auditoria.
            self.guard_trips += 1
            tripped = speech_leak(result.get("text") or "")
        if result.get("error"):
            self.failures += 1
            sentence = ""
        return MouthResult(
            sentence=(sentence or _guard.template_for(None, decision.draft)),
            ms=float(result.get("first_sentence_ms") or 0.0),
            ttft_ms=float(result.get("ttft_ms") or 0.0),
            raw=str(result.get("text") or ""),
            error=str(result.get("error") or ""),
            fixed=not bool(sentence), guard_tripped=tripped)

    def answer(self, text: str, decision: Decision,
               cancel_ev: threading.Event | None = None,
               *, context: str = "") -> MouthResult:
        """RESPOSTA SUPERFICIAL de conversa curta, feita pela boca (passo 2).

        Desde 05/11/2026 esta frase não substitui o cérebro: ela é o ack
        imediato enquanto o raciocínio remoto continua com o conteúdo
        (passos 3/4). Só é chamada para turnos que o roteador fechou como
        bate-papo puro (:func:`core.flows.router.short_chat_reason`). A lei
        é a mesma do brief: a boca NUNCA afirma resultado de ação — o guard
        da fase ``brief`` é o portão final. Se o modelo falha ou o guard
        rejeita, a frase vem VAZIA: silêncio da boca nunca é resposta
        errada; invenção é.
        """
        if not self.config.mouth_enabled or not self.config.local_chat_enabled:
            return MouthResult(sentence="", fixed=True, raw="[chat desligado]")
        history = self._recent_lines()
        user = ((f"Conversa recente:\n{history}\n" if history else "")
                + (f"Contexto do turno: {context}\n" if context else "")
                + f"Fala do dono: {text}\nSua resposta:")
        self.calls += 1
        result = self._call(
            [{"role": "system", "content": SYS_CHAT},
             {"role": "user", "content": user}],
            self.config.mouth_chat_max_tokens, early_sentence=True,
            temperature=0.6, cancel_ev=cancel_ev, fast_ack=True)
        sentence = clean_sentence(result.get("text") or "")
        tripped = ""
        if sentence:
            ok, why = _guard.guard(sentence, None, "brief")
            if not ok:
                self.guard_trips += 1
                tripped = why
                sentence = ""
        elif speech_leak(result.get("text") or ""):
            self.guard_trips += 1
            tripped = speech_leak(result.get("text") or "")
        if result.get("error"):
            self.failures += 1
            sentence = ""
        return MouthResult(
            sentence=sentence,
            ms=float(result.get("first_sentence_ms") or 0.0),
            ttft_ms=float(result.get("ttft_ms") or 0.0),
            raw=str(result.get("text") or ""),
            error=str(result.get("error") or ""),
            fixed=not bool(sentence), guard_tripped=tripped)

    def _recent_lines(self, limit: int | None = None) -> str:
        """Últimas falas da conversa, curtas, para a rota livre.

        O provedor é injetado pelo processo vivo (memória de chat) e já vem
        podado pela janela de ``mouth_context_minutes`` (15 min por padrão,
        config/flows.json). O teto de falas sai de ``mouth_context_turns``
        — medido em bytes em 05/11/2026: 8 falas ≈ 1,3 KB típico, 2,5 KB no
        pior caso, sem regredir o prefill do cérebro (podado a ~7,9 KB).
        Qualquer falha devolve vazio: a boca fala sem história, nunca segura
        o brief.
        """
        if self.history is None:
            return ""
        if limit is None:
            limit = int(getattr(self.config, "mouth_context_turns", 8) or 8)
        try:
            turns = list(self.history() or [])
        except Exception:
            return ""
        lines = []
        for turn in turns[-limit:]:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or "").strip()
            said = " ".join(str(turn.get("text") or "").split())
            if not said:
                continue
            who = "Gino" if role.lower() in ("assistant", "gino") else "dono"
            lines.append(f"{who}: {said[:180]}")
        return "\n".join(lines)

    def complement(self, text: str, decision: Decision,
                   facts: list[dict[str, Any]],
                   cancel_ev: threading.Event | None = None) -> MouthResult:
        facts = list(facts or [])
        pending = [str(f.get("pending_confirm")) for f in facts
                   if f.get("pending_confirm")]
        if pending:
            return MouthResult(
                sentence=f'Diga "{pending[0]}" se for isso mesmo.',
                fixed=True, raw="[complemento fixo]")
        if any(f.get("cancelled") for f in facts):
            return MouthResult(
                sentence="Nada foi executado, criador; o pedido ficou no meio.",
                fixed=True, raw="[complemento fixo]")
        if not any(f.get("assertion") for f in facts):
            sentence = _guard.template_for(facts, decision.draft)
            return MouthResult(sentence=sentence, fixed=True,
                               raw="[complemento fixo]")
        if not self.config.mouth_enabled:
            sentence = _guard.template_for(facts, decision.draft)
            return MouthResult(sentence=sentence, fixed=True,
                               raw="[complemento fixo: boca desligada]")

        lines = "\n".join("- " + str(f.get("spoken") or "")
                          for f in facts if f.get("spoken"))
        user = f"Pedido original: {text}\nFATOS:\n{lines}\nDiga a frase."
        self.calls += 1
        result = self._call(
            [{"role": "system", "content": SYS_COMPLEMENT},
             {"role": "user", "content": user}],
            self.config.mouth_complement_max_tokens, early_sentence=True,
            temperature=0.35, cancel_ev=cancel_ev)
        sentence = clean_sentence(result.get("text") or "")
        tripped = ""
        if sentence:
            ok, why = _guard.guard(sentence, facts, "complement")
            if not ok:
                self.guard_trips += 1
                tripped = why
                sentence = ""
        elif speech_leak(result.get("text") or ""):
            self.guard_trips += 1
            tripped = speech_leak(result.get("text") or "")
        if result.get("error"):
            self.failures += 1
            sentence = ""
        if not sentence:
            sentence = _guard.template_for(facts, decision.draft)
        missing: list[str] = []
        if sentence:
            # UV-2: o fato anexado é texto de trabalhador e pode chegar em
            # inglês ou com caminho de arquivo. A fala emendada passa pelo
            # mesmo portão: se vazar, fica o que já era limpo e o fato segue
            # só no registro de omissão — nunca vira som.
            joined, missing = _guard.ensure_facts(sentence, facts)
            if not speech_leak(joined):
                sentence = joined
        return MouthResult(
            sentence=sentence,
            ms=float(result.get("first_sentence_ms") or 0.0),
            ttft_ms=float(result.get("ttft_ms") or 0.0),
            raw=str(result.get("text") or ""),
            error=str(result.get("error") or ""),
            fixed=not bool(result.get("text")),
            guard_tripped=tripped, missing_facts=missing)

    # ── andamento de execução pesada (UP-8) ────────────────────────────────
    def announce_progress(self, text: str, *, stage: str = "",
                          cancel_ev: threading.Event | None = None
                          ) -> MouthResult:
        """A frase do andamento (FLUXO TRABALHO passos 6-7), nunca o resultado.

        ``text`` é a linha que o espelho acabou de gravar; ``stage`` é a classe
        dela ("edicao", "testes"). O fato entra no guard com ``assertion=False``
        — a lei (a) vale integralmente: etapa nenhuma autoriza "feito",
        "pronto", "abriu" ou "enviei", e anúncio de ação/ferramenta é cortado
        antes do alto-falante. Modelo rejeitado ou mudo, a frase vem do template
        determinístico sob a mesma checagem; não havendo frase segura,
        silêncio — andamento calado é melhor que andamento inventado.
        """
        clean = " ".join(str(text or "").split())
        if not clean:
            return MouthResult(sentence="", fixed=True, raw="[progresso vazio]")
        if not self.config.mouth_enabled:
            return MouthResult(sentence="", fixed=True,
                               raw="[progresso sem boca]")
        facts = progress_facts(clean, stage)
        self.calls += 1
        user = (f"Etapa: {stage or 'andamento'}\n"
                f"Fato do espelho: {clean[:400]}\nDiga a frase.")
        result = self._call(
            [{"role": "system", "content": SYS_PROGRESS},
             {"role": "user", "content": user}],
            self.config.mouth_complement_max_tokens, early_sentence=True,
            temperature=0.3, cancel_ev=cancel_ev, fast_ack=True)
        sentence = clean_sentence(result.get("text") or "")
        tripped = ""
        leak = "" if sentence else speech_leak(result.get("text") or "")
        if sentence and _leaks_path(sentence):
            # O modelo obedeceu ao guard léxico e citou o arquivo mesmo assim:
            # path no ouvido do dono é vazamento, não conteúdo.
            self.guard_trips += 1
            tripped = f"caminho na frase: {sentence[:60]!r}"
            sentence = ""
        elif sentence:
            ok, why = _guard.guard(sentence, facts, "complement")
            if not ok:
                self.guard_trips += 1
                tripped = why
                sentence = ""
        elif leak:
            # UV-2: o modelo falou inglês ou recitou identificador de código;
            # o portão de idioma calou a frase antes do guard.
            self.guard_trips += 1
            tripped = leak
        if result.get("error"):
            self.failures += 1
            sentence = ""
        fixed = not bool(sentence)
        if fixed:
            for candidate in _progress_templates(stage, clean):
                # O template embute a linha do espelho: se a linha é inglês ou
                # código, a candidata não é falável e cede lugar à genérica.
                if _leaks_path(candidate) or speech_leak(candidate):
                    continue
                ok, _why = _guard.guard(candidate, facts, "complement")
                if ok:
                    sentence = candidate
                    break
            if not sentence and not tripped:
                tripped = "nenhuma frase segura do andamento"
        return MouthResult(
            sentence=sentence,
            ms=float(result.get("first_sentence_ms") or 0.0),
            ttft_ms=float(result.get("ttft_ms") or 0.0),
            raw=str(result.get("text") or ""),
            error=str(result.get("error") or ""),
            fixed=fixed, guard_tripped=tripped)

    # ── plumbing ────────────────────────────────────────────────────────────
    def _call(self, messages: list[dict[str, str]], max_tokens: int, *,
              early_sentence: bool, temperature: float,
              cancel_ev: threading.Event | None,
              fast_ack: bool = False) -> dict[str, Any]:
        # fast_ack (UC-2 passo 4): brief e chat são o portão de <1 s e não
        # podem herdar os 25 s do complement. O teto do caminho rápido é
        # FAST_ACK_TIMEOUT_S (nunca acima do timeout do config), com deadline
        # absoluto na stream — estourou, o chamador fala o template.
        kw: dict[str, Any] = {
            "model": self.config.mouth_model,
            "url": self.config.mouth_url,
            "timeout_s": self.config.mouth_timeout_s,
            "keep_alive": self.config.mouth_keep_alive,
            "temperature": temperature,
            "early_sentence": early_sentence,
            "cancel_ev": cancel_ev,
        }
        if fast_ack:
            cap = min(self.config.mouth_timeout_s, FAST_ACK_TIMEOUT_S)
            kw["timeout_s"] = cap
            kw["deadline_s"] = cap
        try:
            return self.caller(messages, max_tokens, **kw)
        except Exception as exc:
            return {"text": "", "ttft_ms": 0.0, "first_sentence_ms": 0.0,
                    "end_ms": 0.0, "error": f"{type(exc).__name__}: {exc}",
                    "aborted": False}


def progress_notifier(*, speak: Callable[[str], Any],
                      config: FlowsConfig | None = None,
                      caller: Callable[..., dict[str, Any]] | None = None,
                      mouth: Mouth | None = None) -> Callable[[dict], dict]:
    """O ``on_progress_update`` do espelho: andamento que entra vira fala (UP-8).

    Pluga em ``core.task_mirror.set_progress_hook``. Recebe o payload da linha
    gravada, fraseia pela boca (com o guard do andamento) e entrega a frase ao
    caminho de voz do processo — ``speak`` é o ``_say_now`` na vivo, nunca o
    contrário: este módulo não conhece Qt, áudio nem ``main.py``.

    Devolve ``{"spoken", "sentence", "reason"}`` para o evento
    ``task_progress`` registrar o resultado na auditoria. Nunca levanta: erro de
    áudio ou de config volta como ``spoken=False`` e o espelho segue gravando.
    """
    cache: dict[str, Any] = {}

    def _mouth() -> Mouth:
        if mouth is not None:
            return mouth
        rt = cache.get("mouth")
        if rt is None:
            cfg = config or FlowsConfig.load()
            rt = Mouth(cfg, caller=caller)
            cache["mouth"] = rt
        return rt

    def handler(payload: dict) -> dict:
        try:
            row = payload if isinstance(payload, dict) else {}
            text = " ".join(str(row.get("text") or "").split())
            if not text:
                return {"spoken": False, "sentence": "",
                        "reason": "sem andamento"}
            result = _mouth().announce_progress(
                text, stage=str(row.get("stage") or ""))
            sentence = str(result.sentence or "")
            if not sentence:
                return {"spoken": False, "sentence": "",
                        "reason": (result.guard_tripped or result.error
                                   or result.raw or "sem frase segura")}
            if not callable(speak):
                return {"spoken": False, "sentence": sentence,
                        "reason": "sem saida de voz"}
            speak(sentence)
            return {"spoken": True, "sentence": sentence, "reason": ""}
        except Exception as exc:  # noqa: BLE001 - falar não quebra o espelho
            return {"spoken": False, "sentence": "",
                    "reason": f"{type(exc).__name__}: {exc}"}

    return handler


# ── medição de latência (FASE1-TASK4) ──────────────────────────────────────

_CEILING_MS = 30_000  # teto de 30s para resposta ponta-a-ponta

_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "activity.log"


def _log_activity(line: str) -> None:
    """Acrescenta uma linha no activity.log (logs/activity.log).

    Formato canônico: ``[YYYY-MM-DD HH:MM:SS] texto``. Falha silenciosa: a
    medição nunca pode quebrar por causa de disco cheio ou diretório ausente.
    """
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except Exception:
        pass


def measure_latency(*, config: FlowsConfig | None = None,
                    caller: Callable[..., dict[str, Any]] | None = None,
                    verbose: bool = True) -> dict[str, Any]:
    """Mede o tempo de cada etapa da boca e verifica o teto de 30s.

    Executa três chamadas sintéticas (brief, chat, complement) usando o caller
    injetado — ou o ``stream_ollama`` real quando nenhum caller é fornecido.
    Cada etapa é cronometrada separadamente (TTFT, primeira frase, total). Os
    resultados vão para o activity.log e também são devolvidos como dict para
    inspeção em testes.

    Parâmetros
    ----------
    config : FlowsConfig, opcional
        Configuração usada na medição. Padrão: ``FlowsConfig.load()``.
    caller : callable, opcional
        Substituto do ``stream_ollama`` (testes injetam fake).
    verbose : bool
        Imprime resumo no stdout quando True.

    Retorna
    -------
    dict com ``stages`` (lista de medições por chamada), ``ceiling_ms`` (30000),
    ``all_within_ceiling`` (bool) e ``bottleneck`` (estágio mais lento).
    """
    cfg = config or FlowsConfig.load()
    mouth = Mouth(cfg, caller=caller) if caller is not None else Mouth(cfg)

    stages: list[dict[str, Any]] = []
    ceiling_ms = _CEILING_MS

    # ── 1. warm (carregamento do modelo) ──────────────────────────────────
    t0 = time.monotonic()
    warm_result = mouth.warm()
    warm_ms = (time.monotonic() - t0) * 1000
    stages.append({
        "kind": "warm",
        "total_ms": round(warm_ms, 1),
        "error": warm_result.get("error"),
    })

    # ── 2. brief (pedido com intenção de ferramenta) ──────────────────────
    from .router import Router
    router = Router()
    decision_brief = router.parse("tocar Queen")
    t0 = time.monotonic()
    brief_result = mouth.brief("tocar Queen", decision_brief)
    brief_total_ms = (time.monotonic() - t0) * 1000
    stages.append({
        "kind": "brief",
        "total_ms": round(brief_total_ms, 1),
        "ttft_ms": round(brief_result.ttft_ms, 1),
        "first_sentence_ms": round(brief_result.ms, 1),
        "sentence": brief_result.sentence,
        "fixed": brief_result.fixed,
        "error": brief_result.error,
    })

    # ── 3. answer (conversa curta, rota livre) ────────────────────────────
    decision_chat = router.parse("como você está?")
    t0 = time.monotonic()
    chat_result = mouth.answer("como você está?", decision_chat)
    chat_total_ms = (time.monotonic() - t0) * 1000
    stages.append({
        "kind": "chat",
        "total_ms": round(chat_total_ms, 1),
        "ttft_ms": round(chat_result.ttft_ms, 1),
        "first_sentence_ms": round(chat_result.ms, 1),
        "sentence": chat_result.sentence,
        "fixed": chat_result.fixed,
        "error": chat_result.error,
    })

    # ── 4. complement (frase de resultado com fatos) ──────────────────────
    decision_compl = router.parse("abrir o Spotify")
    facts = [{"spoken": "Spotify aberto", "assertion": True}]
    t0 = time.monotonic()
    compl_result = mouth.complement("abrir o Spotify", decision_compl, facts)
    compl_total_ms = (time.monotonic() - t0) * 1000
    stages.append({
        "kind": "complement",
        "total_ms": round(compl_total_ms, 1),
        "ttft_ms": round(compl_result.ttft_ms, 1),
        "first_sentence_ms": round(compl_result.ms, 1),
        "sentence": compl_result.sentence,
        "fixed": compl_result.fixed,
        "error": compl_result.error,
    })

    # ── análise ───────────────────────────────────────────────────────────
    model_stages = [s for s in stages if s["kind"] != "warm"]
    slowest = max(model_stages, key=lambda s: s["total_ms"]) if model_stages else {}
    all_within = all(s["total_ms"] <= ceiling_ms for s in model_stages)

    report: dict[str, Any] = {
        "ceiling_ms": ceiling_ms,
        "mouth_timeout_s": cfg.mouth_timeout_s,
        "mouth_model": cfg.mouth_model,
        "stages": stages,
        "bottleneck": {
            "kind": slowest.get("kind", ""),
            "total_ms": slowest.get("total_ms", 0),
        },
        "all_within_ceiling": all_within,
    }

    # ── registro no activity.log ──────────────────────────────────────────
    for s in stages:
        kind = s["kind"]
        total = s["total_ms"]
        err = s.get("error") or ""
        sentence = s.get("sentence", "")
        _log_activity(
            f"[Fluxos] latencia {kind}: {total:.0f}ms"
            + (f" sentence={sentence!r}" if sentence else "")
            + (f" error={err}" if err else "")
        )
    status = "OK" if all_within else "ACIMA_DO_TETO"
    _log_activity(
        f"[Fluxos] latencia teto={ceiling_ms}ms gargalo="
        f"{report['bottleneck']['kind']}:{report['bottleneck']['total_ms']:.0f}ms"
        f" status={status}"
    )

    # ── saída no console ──────────────────────────────────────────────────
    if verbose:
        print(f"=== Medição de latência da boca (teto {ceiling_ms}ms) ===")
        print(f"Modelo: {cfg.mouth_model} | timeout HTTP: {cfg.mouth_timeout_s}s")
        for s in stages:
            flag = "FIXO" if s.get("fixed") else "MODELO"
            err = f" ERRO={s['error']}" if s.get("error") else ""
            print(
                f"  {s['kind']:12s}  {s['total_ms']:7.0f}ms  [{flag}]"
                f"  {s.get('sentence', '')!r}{err}"
            )
        print(f"Gargalo: {report['bottleneck']['kind']} "
              f"({report['bottleneck']['total_ms']:.0f}ms)")
        print(f"Teto respeitado: {'sim' if all_within else 'NAO'}")
        print(f"Registro gravado em: {_LOG_PATH}")

    return report
