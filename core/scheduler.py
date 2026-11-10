"""AGENDADOR — mecanismo A do plano ``PLANO-2026-11-09-fluxo-agendamento-e-avisos-por-evento``.

O contrato é dele, à letra (áudio das 19:00 de 09/11/2026):

    "eu quero que você use o agendador para agendar as coisas que vão ser
    fixas, tipo que tem horário fixo pra acontecer ... você vai criar um
    agendamento e definir o gatilho que esse agendamento vai executar ...
    todo scheduler tem que ter um script ali que vai ser executado no caso
    ou uma função de algum arquivo seu aí em Python que vai executar."

As seis regras que este módulo aplica, cada uma com a resposta dele atrás:

1. **Só horário fixo.** O que não tem instante marcado não entra aqui — entra
   no ouvinte de execução (mecanismo B, seção 4 do plano) ou não entra.
2. **Todo registro carrega um gatilho resolvível na criação.** Sem gatilho, ou
   com gatilho que não resolve, a resposta é **recusa com o nome do que falta**
   (lei C4); nunca existe "aviso padrão" nem fallback silencioso.
3. **A fonte da verdade é nota no cofre** (decisão 2, "opcao c, com cache na
   memoria"). Cada agendamento vira uma nota ``TAREFA-AAAA-MM-DD-...md``
   nomeada por :func:`core.obsidian.build_name` — nunca nome na mão. O processo
   lê um **cache em memória** e só varre o disco quando a assinatura dos
   arquivos muda: o laço de 15 s não martela o cofre a cada segundo.
4. **Sem teto de simultaneidade** (decisão 5, "opcao c"). Nada neste módulo
   limita quantos agendamentos estão abertos ao mesmo tempo.
5. **Destino default são os dois** (decisão 7, "opcao a"): voz **e** WhatsApp
   quando o registro não nomeia destino.
6. **Cancelar** (decisão 8, "opcao b com opcao d"): um candidato resolve, mais
   de um pergunta em vez de sortear, e **nada** é cancelado sem confirmação.

Acordar com vencidos (decisão 6): "Avise que tem N lembretes, me de um resumo
e pergunte se quero ouvir um a um".

Personas (decisão 3): não registram, não pospõem e não cadastram gatilho por
conta própria — o pedido vira **pedido de aprovação** e **nada é gravado**.
A barreira (:func:`_barreira_da_persona`) é chamada antes de qualquer escrita
no cofre: recusar depois de escrever deixaria nota que ele nunca autorizou.

Nenhum laço de tempo aqui: este módulo não abre thread, não importa Qt, não
importa ``main.py`` (mesmo padrão de ``core/task_queue.py:33-35``). Quem chama
:func:`tick` é o laço de hora que já existe (``main.py:_task_watch``) — somar
um quarto laço é o risco 1 do plano.

PONTOS DE EXTENSAO NOMEADOS (decisões que ainda pedem contexto ao dono — nada
aqui escolhe por ele):
  * ``PONTO-EXTENSAO-laco-vivo`` — **regra 1/5 do contrato, o laço**: nada
    aqui abre thread, então ``tick``/``acordar`` só falam quando o laço de
    hora vivo (``main.py:_task_watch_step``) chamar, e ``ponte()`` só tem boca
    depois que ``main.py`` ligar voz + WhatsApp. Fiação em ``main.py`` é rota
    sensível e pede o reinício único dele — pendência declarada, não
    esquecimento. A action, essa, entra sozinha: ``core/action_loader.py``
    descobre ``actions/schedule.py`` pelo ``TOOL``.
  * ``PONTO-EXTENSAO-portao-fluxo`` — **item 1 do adendo aprovado** (gate
    ``require_schedule_or_watcher``): a checagem declarativa "tem horário
    fixo? tem estado apontado?" mora no cérebro do fluxo de fala/trabalho
    (``core/flow_ops/rules.py`` + ``config/flows_rules.json``), não aqui, e
    depende do mecanismo B — ainda cego.
  * ``PONTO-EXTENSAO-backend-ouvinte`` — o detector de eventos do mecanismo B
    (polling / gancho no emissor / inotify / waitpid) decisão 1; o GATILHO
    abaixo já é a forma compartilhada, falta só ``core/event_watcher.py``.
  * ``PONTO-EXTENSAO-pos-reboot`` — além da regra ditada da decisão 6
    (:func:`acordar`), a política completa pós-reboot (decisão 4) não está
    implementada; :func:`tick` apenas não dispara o que venceu longe demais.
  * ``PONTO-EXTENSAO-unificacao`` — ``actions/reminder.py`` e
    ``plugins/organizer.py`` continuam onde estão (decisão 9 sem resposta);
    este módulo **não** os toca e não cria segundo disparo para o que eles já
    anunciam (``due`` deles vive em ``core/task_queue.py``).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core import actiongate, obsidian, task_queue

# ── vocabulários fechados (a trava é o enum, nunca a descrição) ─────────────

#: O que roda. "texto livre" e string de shell não são tipos de gatilho.
GATILHO_TIPOS = ("funcao", "script")

#: Para onde o disparo vai (decisão 7 dele virou enum, seção 3.1 do plano).
GATILHO_DESTINOS = ("voz", "whatsapp", "arquivo-baixar", "arquivo-subir",
                    "rede-social")

#: Destino quando o registro não nomeia nenhum — "opcao a": os dois.
DESTINO_PADRAO = ("voz", "whatsapp")

#: Tipos de argumento aceitos no gatilho, validados na criação.
ARGUMENTO_TIPOS = ("str", "int", "float", "bool", "iso8601")

#: Prefixos de módulo onde um gatilho ``funcao`` pode morar. Lista fechada
#: de propósito: sem isto, "função" viraria caminho para ``os.system``.
MODULOS_PERMITIDOS = ("core.", "actions.", "plugins.", "memory.")

#: Destinos que só existem se a casa tiver a rota; nomear o que falta é a
#: recusa exigida pela lei C4 (seção 3.2: "baixar arquivo tem de ser escrito
#: antes de poder ser agendado").
DESTINO_EXIGENCIA = {
    "arquivo-baixar": "não existe download genérico na casa "
                      "(só core/music.py:download_audio, que é música)",
    "arquivo-subir": "upload_file",
    "rede-social": "linkedin_control",
}

#: Destinos que passam pelo tier da ferramenta no momento do disparo
#: (seção 3.5: o agendador não afrouxa tier nenhum).
DESTINO_FERRAMENTA = {
    "arquivo-subir": "upload_file",
    "rede-social": "linkedin_control",
}

#: Nome/caminho dos dados deste módulo.
PASTA_NOTAS = "Memoria"
SUFIXO_NOTA = "agendamento"
REGISTRO_SCRIPTS = Path(__file__).resolve().parent.parent / "config" / \
    "schedule_scripts.json"

#: Até quando um vencido ainda vale falar na hora (mesma graça de
#: ``actions/reminder.py:68`` e de ``plugins/organizer.py:232``).
LATE_GRACE_SECONDS = 180.0
#: Timeout do script de um gatilho (argv vetorial, nunca shell).
SCRIPT_TIMEOUT_SECONDS = 120

_LOCK = threading.RLock()

#: Cache em memória da fonte da verdade (notas do cofre).
_cache: dict = {"assinatura": None, "linhas": {}}
#: Cache em memória do cadastro fechado de scripts.
_scripts_cache: dict = {"assinatura": None, "lista": []}
#: O resumo de vencidos já foi falado neste processo? (decisão 6, uma vez só)
_ja_acordei = False
#: Avisos de "pendente de autorização" já ditados neste processo (anti-rajada).
_pendente_aviso: set = set()
#: Pedidos de aprovação vindos de personas (decisão 3) — em memória, nada é
#: gravado como agendamento enquanto ele não responder.
_pedidos_aprovacao: list = []


# ── tempo ───────────────────────────────────────────────────────────────────

def agora() -> datetime:
    return datetime.now().astimezone()


def interpretar_quando(valor, *, base: datetime | None = None):
    """Hora **falada** -> ISO com fuso, ou recusa nomeando o que não bateu.

    Não é um parser novo: este normaliza as formas que a fala dele produz
    ("hoje 14:30", "amanhã às 7", "às 7 da manhã", "daqui a duas horas", ISO
    puro) e entrega o resto ao parser canônico da casa,
    :func:`core.task_queue._parse_dt`. Instante não parsesável é **recusa com
    frase própria**, nunca "agora" como fallback (risco 2 e lei C4).

    Hora seca ("às 22:00" dito à 23:34) vira a **próxima** ocorrência do
    relógio, nunca a passada — é a única leitura que não agenda o passado.
    """
    base = base or agora()
    texto = str(valor or "").strip().lower()
    if not texto:
        return None, "sem instante: diga a hora (ex.: 'amanhã 14:30')"

    # "às"/"à"/"a" só quando abrem a frase: comer "as " solto destruiria
    # "daqui a duas horas" (o "as" de "duas").
    texto = re.sub(r"^(?:às|as|à|a)\s+", "", texto).strip()
    periodo = ""
    m = re.search(r"\bd(?:a|e)\s+(manha|manhã|tarde|noite)\b", texto)
    if m:
        periodo = m.group(1).replace("ã", "a")
        texto = (texto[:m.start()] + texto[m.end():]).strip()
    texto = re.sub(r"\s+", " ", texto).strip()

    m = re.match(r"^(hoje|amanha|amanhã)\b\s*(.*)$", texto)
    palavra = m.group(1) if m else ""
    resto = (m.group(2) if m else texto).strip()
    # "amanhã **às** 14:30": o 'às' aparece depois da palavra de dia, então
    # o corte inicial acima não o alcança.
    if palavra:
        resto = re.sub(r"^(?:às|as|à|a)\s+", "", resto).strip()

    m = re.match(r"^daqui a (\d+|uma|duas|tres|três)\s*"
                 r"(minuto|minutos|hora|horas|dia|dias)\b", resto)
    if m:
        numero = {"uma": 1, "duas": 2, "tres": 3, "três": 3}.get(m.group(1))
        if numero is None:
            numero = int(m.group(1))
        unidade = m.group(2)
        if unidade.startswith("minuto"):
            delta = timedelta(minutes=numero)
        elif unidade.startswith("dia"):
            delta = timedelta(days=numero)
        else:
            delta = timedelta(hours=numero)
        return (base + delta).isoformat(), ""

    m = re.match(r"^(\d{1,2})(?:\s*[:h]\s*(\d{2}))?\s*(?:hrs?|horas)?$", resto)
    if m:
        hora = int(m.group(1))
        minuto = int(m.group(2) or 0)
        if periodo in ("tarde", "noite") and 1 <= hora <= 11:
            hora += 12
        elif periodo == "manha" and hora == 12:
            hora = 0
        if hora > 23 or minuto > 59:
            return None, f"hora fora do relógio em {str(valor)!r}"
        if palavra in ("amanha", "amanhã"):
            dia = base.date() + timedelta(days=1)
            alvo = base.replace(year=dia.year, month=dia.month, day=dia.day,
                                hour=hora, minute=minuto, second=0,
                                microsecond=0)
            return alvo.isoformat(), ""
        alvo = base.replace(hour=hora, minute=minuto, second=0, microsecond=0)
        if not palavra and alvo <= base:
            alvo += timedelta(days=1)
        return alvo.isoformat(), ""

    if palavra in ("hoje", "amanha", "amanhã"):
        # o dono deu o dia, não a hora — chutar meia-noite seria pior que
        # perguntar (C4: a recusa nomeia o que faltou).
        return None, (f"falta a hora em {str(valor)!r}: "
                      f"'{palavra}' é dia, não instante")

    dt = task_queue._parse_dt(task_queue._iso(resto))
    if dt is not None:
        return dt.isoformat(), ""
    return None, f"não entendi a hora em {str(valor)!r}"


# ── cadastro fechado de scripts (recarga sem reiniciar) ─────────────────────

def _assinatura_scripts() -> tuple:
    try:
        st = REGISTRO_SCRIPTS.stat()
        return (str(REGISTRO_SCRIPTS), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (str(REGISTRO_SCRIPTS), 0, 0)


def scripts_cadastrados() -> list[dict]:
    """A lista fechada de scripts que podem ser gatilho (``config/
    schedule_scripts.json``), lida **por chamada** — padrão
    ``core/flow_ops/rules.py:325``, muda no disco e vale sem reiniciar."""
    with _LOCK:
        assinatura = _assinatura_scripts()
        if _scripts_cache["assinatura"] != assinatura:
            lista: list[dict] = []
            try:
                dados = json.loads(REGISTRO_SCRIPTS.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - sem cadastro, cadastro vazio
                dados = []
            if isinstance(dados, dict):
                dados = dados.get("scripts", [])
            for item in dados if isinstance(dados, list) else []:
                if not isinstance(item, dict):
                    continue
                caminho = str(item.get("caminho") or "").strip()
                if caminho:
                    lista.append({"caminho": caminho,
                                  "descricao": str(item.get("descricao") or "")})
            _scripts_cache["assinatura"] = assinatura
            _scripts_cache["lista"] = lista
        return [dict(item) for item in _scripts_cache["lista"]]


def cadastrar_script(caminho, descricao = "", *, origem = "",
                     persona = "") -> dict:
    """Cadastra um script como gatilho possível. Só o dono cadastra."""
    negado = _quem_pode(origem, persona, acao="cadastrar script")
    if negado:
        return {"ok": False, "recusa": negado}
    # Regra 7 antes da escrita: o cadastro fechado é uma capacidade, e
    # persona nenhuma abre capacidade por conta própria.
    barreira = _barreira_da_persona(origem, persona,
                                    acao="cadastrar um script de gatilho",
                                    alvo={"titulo": str(caminho or "")})
    if barreira:
        return barreira
    alvo = Path(str(caminho or "")).expanduser()
    if not str(caminho or "").strip() or not alvo.is_absolute():
        return {"ok": False, "recusa": "o script precisa de caminho absoluto"}
    if not alvo.is_file():
        return {"ok": False, "recusa": f"o script {alvo} não existe"}
    lista = scripts_cadastrados()
    if not any(item["caminho"] == str(alvo) for item in lista):
        lista.append({"caminho": str(alvo), "descricao": str(descricao or "")})
    dados = {"scripts": lista}
    REGISTRO_SCRIPTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRO_SCRIPTS.with_name(f".{REGISTRO_SCRIPTS.name}."
                                     f"{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(dados, ensure_ascii=False, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, REGISTRO_SCRIPTS)
    with _LOCK:
        _scripts_cache["assinatura"] = None
    return {"ok": True, "caminho": str(alvo), "scripts": len(lista)}


# ── forma do gatilho ────────────────────────────────────────────────────────

def _validar_argumentos(argumentos):
    """Lista tipada -> (argv/kwargs normalizados, erros).

    Cada argumento é ``{"nome": ..., "tipo": ..., "valor": ...}`` com tipo em
    :data:`ARGUMENTO_TIPOS`, ou um valor nu (que vira ``str``). Tipo errado é
    recusa **na criação**, não exceção no disparo.
    """
    if argumentos in (None, "", [], {}):
        return [], []
    if isinstance(argumentos, dict):
        argumentos = [dict(valor=v, nome=k) for k, v in argumentos.items()]
    if not isinstance(argumentos, (list, tuple)):
        return [], ["argumentos precisa ser lista ou objeto"]
    sapecados, erros = [], []
    for i, item in enumerate(argumentos):
        if isinstance(item, dict):
            nome = str(item.get("nome") or "").strip()
            tipo = str(item.get("tipo") or "").strip().lower()
            valor = item.get("valor")
        else:
            nome, tipo, valor = "", "str", item
        if not tipo:
            tipo = "str"
        if tipo not in ARGUMENTO_TIPOS:
            erros.append(f"argumento {nome or i}: tipo {tipo!r} fora da lista "
                         f"{'/'.join(ARGUMENTO_TIPOS)}")
            continue
        if tipo == "int":
            try:
                valor = int(valor)
            except (TypeError, ValueError):
                erros.append(f"argumento {nome or i}: {valor!r} não é int")
                continue
        elif tipo == "float":
            try:
                valor = float(valor)
            except (TypeError, ValueError):
                erros.append(f"argumento {nome or i}: {valor!r} não é float")
                continue
        elif tipo == "bool":
            if isinstance(valor, str):
                valor = valor.strip().lower() in ("1", "true", "sim", "yes",
                                                  "on")
            else:
                valor = bool(valor)
        elif tipo == "iso8601":
            if task_queue._parse_dt(valor) is None:
                erros.append(f"argumento {nome or i}: {valor!r} não é ISO-8601")
                continue
            valor = task_queue._iso(valor)
        else:
            valor = str(valor if valor is not None else "")
        sapecados.append({"nome": nome, "tipo": tipo, "valor": valor})
    return sapecados, erros


def _plugin_ligado(nome_plugin: str) -> bool:
    try:
        from memory import config_manager
        return bool(config_manager.get_plugin_enabled(nome_plugin))
    except Exception:  # noqa: BLE001 - sem config, o portão fica fechado
        return False


def _tier_da_ferramenta(nome: str) -> str:
    try:
        from core import tool_bridge
        return str(tool_bridge.ACTION_TIERS.get(nome, "low"))
    except Exception:  # noqa: BLE001 - ambiente enxuto: tier baixo
        return "low"


def validar_gatilho(gatilho):
    """(gatilho normalizado, erros). Resolve **na criação**: import real,
    caminho cadastrado, argumentos tipados, destino do enum."""
    erros: list[str] = []
    if not isinstance(gatilho, dict):
        return None, ["o agendamento precisa de um gatilho: tipo funcao "
                      "(modulo + funcao) ou script cadastrado"]
    tipo = str(gatilho.get("tipo") or "").strip().lower()
    if tipo not in GATILHO_TIPOS:
        return None, [f"tipo de gatilho {gatilho.get('tipo')!r} fora da lista "
                      f"{'/'.join(GATILHO_TIPOS)} (texto livre e shell não "
                      "são gatilho)"]
    argumentos, erros_arg = _validar_argumentos(gatilho.get("argumentos"))
    erros.extend(erros_arg)

    normalizado: dict = {"tipo": tipo, "argumentos": argumentos}

    if tipo == "funcao":
        modulo = str(gatilho.get("modulo") or "").strip()
        funcao = str(gatilho.get("funcao") or "").strip()
        if not modulo or not funcao:
            erros.append("gatilho funcao precisa de modulo e funcao")
        elif not modulo.startswith(MODULOS_PERMITIDOS):
            erros.append(f"modulo {modulo!r} fora da lista de onde um gatilho "
                         f"pode morar ({', '.join(MODULOS_PERMITIDOS)})")
        elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", funcao):
            erros.append(f"nome de funcao {funcao!r} não é um símbolo Python")
        else:
            import importlib
            try:
                modulo_obj = importlib.import_module(modulo)
                alvo = getattr(modulo_obj, funcao, None)
            except Exception as exc:  # noqa: BLE001 - recusa, não exceção
                alvo = None
                erros.append(f"não consegui importar {modulo}: {exc}")
            if not callable(alvo) and not erros:
                erros.append(f"{modulo}.{funcao} não existe ou não é chamável")
            if not erros:
                normalizado["modulo"] = modulo
                normalizado["funcao"] = funcao
    else:
        caminho = str(gatilho.get("script") or "").strip()
        if not caminho:
            erros.append("gatilho script precisa de caminho absoluto cadastrado")
        elif not os.path.isabs(caminho):
            erros.append(f"script {caminho!r} precisa de caminho absoluto")
        elif caminho not in [item["caminho"] for item in scripts_cadastrados()]:
            erros.append(f"script {caminho!r} não está no cadastro fechado "
                         "(config/schedule_scripts.json) — cadastre antes")
        elif not os.path.isfile(caminho):
            erros.append(f"o script {caminho} não existe no disco")
        else:
            normalizado["script"] = caminho

    destinos = gatilho.get("destino") or gatilho.get("destinos")
    if destinos in (None, "", []):
        destinos = list(DESTINO_PADRAO)
    if isinstance(destinos, str):
        destinos = [destinos]
    if not isinstance(destinos, (list, tuple)):
        destinos = [str(destinos)]
    destinos = [str(d or "").strip().lower() for d in destinos if str(d or "")]
    for destino in destinos:
        if destino not in GATILHO_DESTINOS:
            erros.append(f"destino {destino!r} fora da lista "
                         f"{'/'.join(GATILHO_DESTINOS)}")
            continue
        exigencia = DESTINO_EXIGENCIA.get(destino)
        if destino == "arquivo-baixar":
            erros.append(f"destino {destino!r} recusado: {exigencia}")
        elif destino == "rede-social" and not _plugin_ligado(exigencia):
            erros.append(f"destino {destino!r} recusado enquanto o plugin "
                         f"{exigencia} está desligado (ordem dele de "
                         "07/11/2026); ligar plugin não é atributo de "
                         "agendamento")
    normalizado["destino"] = sorted(set(destinos)) or list(DESTINO_PADRAO)
    return (None if erros else normalizado), erros


# ── a fonte da verdade: nota no cofre + cache em memória ─────────────────────

def _pasta_notas() -> Path:
    return obsidian.ensure_vault() / PASTA_NOTAS


def pasta_das_notas() -> Path:
    """Onde vivem as notas-agendamento (a fonte da verdade)."""
    return _pasta_notas()


def nome_da_nota(linha: dict) -> str:
    """Nome pelo padrão do cofre — build_name + check_name, nunca mão."""
    dia = str(linha.get("criado_em") or "")[:10] or None
    return obsidian.build_name("TAREFA", f"{SUFIXO_NOTA}-{linha['id']}",
                               day=dia)


def caminho_da_nota(linha: dict) -> Path:
    return _pasta_notas() / nome_da_nota(linha)


def corpo_da_nota(linha: dict) -> str:
    """Markdown legível + o bloco JSON que é o registro."""
    gatilho = linha.get("gatilho") or {}
    alvo = (f"{gatilho.get('modulo')}.{gatilho.get('funcao')}"
            if gatilho.get("tipo") == "funcao" else gatilho.get("script", ""))
    linhas = [
        f"# Agendamento {linha.get('id')}",
        "",
        f"- titulo: {linha.get('titulo', '')}",
        f"- quando: {linha.get('quando', '')}",
        f"- gatilho: {gatilho.get('tipo', '')} -> {alvo}",
        f"- destino: {', '.join(gatilho.get('destino') or [])}",
        f"- origem: {linha.get('origem', '')}"
        + (f" (persona {linha['persona']})" if linha.get("persona") else ""),
        f"- criado_em: {linha.get('criado_em', '')}",
        f"- disparou_em: {linha.get('disparou_em') or '-'}",
        f"- cancelado_em: {linha.get('cancelado_em') or '-'}",
        "",
        "```json",
        json.dumps(linha, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(linhas)


def _caminho_conhecido(linha: dict) -> Path:
    """A nota DESTE agendamento, não uma nova com a data de hoje.

    Atualizar um registro criado ontem tem de reescrever a nota de ontem:
    se cada escrita usasse ``write_note`` (que nomeia pelo dia de hoje), o
    cofre ganharia duas notas para o mesmo id — a duplicação que o dono
    apontou no risco 9 (fonte de verdade dupla).
    """
    nota = str(linha.get("nota") or "").strip()
    if nota and Path(nota).is_file():
        return Path(nota)
    achadas = sorted(_pasta_notas().glob(
        f"TAREFA-*-{SUFIXO_NOTA}-{linha['id']}.md"))
    if achadas:
        return achadas[-1]
    return caminho_da_nota(linha)


def _gravar(linha: dict) -> Path:
    corpo = corpo_da_nota(linha)
    destino = _caminho_conhecido(linha)
    if destino.exists():
        # reescrita atômica na mesma nota (padrão tmp+fsync+os.replace)
        obsidian._atomic_write(destino, corpo)
    else:
        destino = obsidian.write_note(f"{SUFIXO_NOTA}-{linha['id']}", corpo,
                                      folder=PASTA_NOTAS, tipo="TAREFA")
    linha["nota"] = str(destino)
    return destino


def _extrair_json(texto: str):
    m = re.search(r"```json\s*(\{.*?\})\s*```", texto or "", re.S)
    if not m:
        return None
    try:
        dados = json.loads(m.group(1))
    except Exception:  # noqa: BLE001 - nota malformada é relatada, não calada
        return None
    return dados if isinstance(dados, dict) else None


def _assinatura_notas(pasta: Path) -> tuple:
    """Carimbo dos arquivos do cofre: while ele não muda, o cache responde."""
    marcador = []
    try:
        for path in sorted(pasta.glob(f"TAREFA-*-{SUFIXO_NOTA}-*.md")):
            try:
                st = path.stat()
                marcador.append((path.name, int(st.st_mtime_ns),
                                 int(st.st_size)))
            except OSError:
                marcador.append((path.name, 0, 0))
    except OSError:
        return (str(pasta), "inacessivel")
    return (str(pasta), tuple(marcador))


def _dados_da_nota(path: Path) -> tuple[dict | None, str]:
    """Leitura de UMA nota do cofre: (dados, erro). Único lugar que encosta
    no disco — é por isto que o teste do cache conta chamadas aqui."""
    try:
        texto = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{path.name}: {exc}"
    dados = _extrair_json(texto)
    if not isinstance(dados, dict) or not dados.get("id"):
        return None, f"{path.name}: sem bloco json legível"
    dados["nota"] = str(path)
    return dados, ""


def carregar(*, forcar: bool = False) -> dict:
    """As linhas do agendamento, do **cache em memória** quando possível.

    A varredura do cofre só acontece quando a assinatura dos arquivos mudou —
    é isto que separa "fonte da verdade é a nota" de "ler disco a cada
    segundo". Decisão 2 dele: "opcao c, com cache na memoria".
    """
    with _LOCK:
        pasta = _pasta_notas()
        assinatura = _assinatura_notas(pasta)
        if not forcar and _cache["assinatura"] == assinatura:
            return {k: dict(v) for k, v in _cache["linhas"].items()}
        linhas: dict[str, dict] = {}
        erros: list[str] = []
        vistos: dict[str, list[str]] = {}
        for path in sorted(pasta.glob(f"TAREFA-*-{SUFIXO_NOTA}-*.md")):
            dados, erro = _dados_da_nota(path)
            if erro:
                erros.append(erro)
            if dados:
                registro = str(dados["id"])
                vistos.setdefault(registro, []).append(path.name)
                linhas[registro] = dados
        for registro, nomes in vistos.items():
            if len(nomes) > 1:
                erros.append(f"id {registro} tem {len(nomes)} notas "
                             f"({', '.join(nomes)}): fonte de verdade dupla")
        _cache["assinatura"] = assinatura
        _cache["linhas"] = linhas
        _cache["erros"] = erros
        return {k: dict(v) for k, v in linhas.items()}


def erro_de_leitura() -> list[str]:
    carregar()
    with _LOCK:
        return list(_cache.get("erros") or [])


# ── quem pode pedir o quê ───────────────────────────────────────────────────

def _quem_pode(origem, persona: str = "", *, acao: str = "agendar") -> str:
    """Recusa quando a origem não pode; "" quando pode.

    Tabela 5.3 do plano: visitante, filtro de e-mail e origem não declarada
    não registram nem cancelam. A regra das personas (decisão 3) não mora
    aqui: ela devolve **pedido de aprovação**, não recusa seca — e por isso
    está em :func:`_barreira_da_persona`, chamada por :func:`registrar`,
    :func:`pospor` e :func:`cadastrar_script` sempre **antes** da escrita.
    """
    rotulo = actiongate.normalize_origin(origem)
    if rotulo == actiongate.ORIGIN_VISITOR_SPEECH:
        return (f"não vou {acao}: mensagem de terceiro não autoriza registro "
                "(terceiro nunca abriu portão privilegiado)")
    if rotulo == actiongate.ORIGIN_EMAIL_FILTER:
        return f"não vou {acao}: corpo de e-mail é conteúdo, não ordem"
    if rotulo == actiongate.ORIGIN_UNDECLARED:
        return (f"não vou {acao} sem origem declarada — diga quem pediu "
                "(lei C4: recusa nomeia o que faltou)")
    return ""


def pedidos_aprovacao() -> list[dict]:
    """Pedidos de persona que ainda esperam a resposta do dono."""
    with _LOCK:
        return [dict(item) for item in _pedidos_aprovacao]


def _pedido_de_aprovacao(persona: str, acao: str, alvo: dict) -> dict:
    """A trilha da decisão 3: vira **pergunta**, e nada é gravado.

    Só é chamada de um lado: **antes** de qualquer escrita. Gravar a nota e
    depois recusar deixaria no cofre um agendamento que ele nunca autorizou.
    """
    pedido = {"id": str(alvo.get("id") or ""), "persona": persona,
              "acao": acao, "titulo": alvo.get("titulo", ""),
              "quando": alvo.get("quando", ""),
              "gatilho": alvo.get("gatilho") or {},
              "pedido_em": agora().isoformat()}
    with _LOCK:
        _pedidos_aprovacao.append(pedido)
    return {
        "ok": False,
        "recusa": (f"{persona or 'a persona'} não pode {acao} por conta "
                   f"própria: {alvo.get('titulo', '')!r} ficou como pedido de "
                   "aprovação. Autorize e faça você mesmo, ou peça por você."),
        "pedido": pedido,
    }


def _barreira_da_persona(origem, persona, *, acao: str, alvo: dict) -> dict:
    """Decisão 3 (regra 7): persona escreve **somente** sob solicitação dele.

    Devolve ``{}`` quando pode seguir (não é persona, ou a origem é a fala do
    dono); devolve a recusa com trilha quando não pode. Quem chama usa antes
    de encostar no cofre — é a ordem que a regra exige.
    """
    dono = str(persona or "").strip()
    if not dono:
        return {}
    if actiongate.normalize_origin(origem) == actiongate.ORIGIN_OWNER_SPEECH:
        return {}
    return _pedido_de_aprovacao(dono, acao, alvo)


# ── registro, cancelamento, soneca ──────────────────────────────────────────

def registrar(quando, titulo, gatilho, *, origem = "", persona = "",
              texto = "", criado_em = None, permitir_passado = False) -> dict:
    """Cria o agendamento. Três recusas na porta: hora, instante passado, gatilho."""
    negado = _quem_pode(origem, persona, acao="agendar")
    if negado:
        return {"ok": False, "recusa": negado}
    iso, erro = interpretar_quando(quando)
    if erro:
        return {"ok": False, "recusa": erro}
    quando_dt = task_queue._parse_dt(iso)
    if (not permitir_passado and quando_dt is not None
            and quando_dt <= agora()):
        return {"ok": False,
                "recusa": (f"a hora em {str(quando)!r} já passou "
                           f"({iso[:16]}): agendar passado seria criar um "
                           "vencido na hora — diga uma hora futura")}
    normalizado, erros = validar_gatilho(gatilho)
    if erros:
        return {"ok": False, "recusa": "; ".join(erros)}
    titulo = str(titulo or "").strip()
    if not titulo:
        return {"ok": False, "recusa": "sem título: eu preciso de um nome "
                                       "para dizer o que venci"}
    linha = {
        "id": uuid.uuid4().hex[:12],
        "titulo": titulo,
        "texto": str(texto or "").strip() or titulo,
        "quando": iso,
        "criado_em": str(criado_em or agora().isoformat()),
        "origem": actiongate.normalize_origin(origem),
        "persona": str(persona or "").strip(),
        "gatilho": normalizado,
        "disparou_em": "",
        "aviso_atrasado_em": "",
        "cancelado_em": "",
        "boot_id": task_queue._boot_id(),
    }
    # Decisão 3 dele, à letra: "apenas sob minha solicitacao ou caso
    # necessitem, que peçam aprovacao". Persona registrando por conta própria
    # **não grava nada** -- fail-closed: o pedido vira pergunta e a nota do
    # cofre continua sem esta linha. A barreira vem antes do ``_gravar``.
    barreira = _barreira_da_persona(origem, linha["persona"],
                                    acao="registrar agendamento", alvo=linha)
    if barreira:
        return barreira
    try:
        _gravar(linha)
    except Exception as exc:  # noqa: BLE001 - cofre fora do ar não é sucesso
        return {"ok": False, "recusa": f"não consegui escrever a nota do "
                                       f"agendamento no cofre: {exc}"}
    carregar(forcar=True)
    return {"ok": True, "linha": linha, "nota": linha["nota"],
            "recibo": _recibo("add", linha)}


def listar(*, abertos_only: bool = True) -> list[dict]:
    linhas = sorted(carregar().values(),
                    key=lambda item: (str(item.get("quando") or ""),
                                      str(item.get("criado_em") or "")))
    if abertos_only:
        linhas = [l for l in linhas
                  if not l.get("disparou_em") and not l.get("cancelado_em")]
    return linhas


def _candidatos(texto: str = "", id: str = "", *, origem = "",
               persona = "") -> list[dict]:
    """O que "esquece aquilo" poderia ser. Nunca sorteio: quem chama é que
    recebe a lista e decide entre perguntar ou confirmar."""
    rotulo = actiongate.normalize_origin(origem)
    linhas = listar()
    if id:
        return [l for l in linhas if str(l.get("id")) == str(id)]
    if rotulo != actiongate.ORIGIN_OWNER_SPEECH or persona:
        dono_do_registro = str(persona or "").strip()
        linhas = [l for l in linhas
                  if str(l.get("persona") or "") == dono_do_registro
                  and (rotulo == actiongate.ORIGIN_OWNER_SPEECH
                       or str(l.get("origem") or "") == rotulo)]
    busca = str(texto or "").strip().lower()
    if busca:
        linhas = [l for l in linhas
                  if busca in str(l.get("titulo") or "").lower()]
    return linhas


def cancelar(*, origem = "", persona = "", id = "", texto = "",
             confirmacao = False) -> dict:
    """Decisão 8 dele, "opcao b com opcao d".

    Nenhum candidato -> diz que não achou. Mais de um -> pergunta, nunca sorna.
    Um -> ainda assim precisa de confirmação: **nada** é cancelado sem ela.
    """
    negado = _quem_pode(origem, persona, acao="cancelar")
    if negado:
        return {"ok": False, "recusa": negado}
    candidatos = _candidatos(texto, id, origem=origem, persona=persona)
    if not candidatos:
        return {"ok": False,
                "recusa": "não achei agendamento aberto com esse nome"}
    if len(candidatos) > 1:
        nomes = "; ".join(f"{c['id']} ({c.get('titulo', '')} em "
                          f"{c.get('quando', '')})" for c in candidatos)
        return {"ok": False,
                "pergunta": f"tem {len(candidatos)} agendamentos abertos: "
                            f"{nomes}. Qual deles eu esqueço?",
                "candidatos": [c["id"] for c in candidatos]}
    linha = candidatos[0]
    if not confirmacao:
        return {"ok": False,
                "pergunta": (f"Vou cancelar {linha.get('titulo', '')!r} "
                             f"(id {linha['id']}, {linha.get('quando', '')}). "
                             "Confirma?"),
                "candidato": linha["id"]}
    linha["cancelado_em"] = agora().isoformat()
    linha["cancelado_por"] = actiongate.normalize_origin(origem)
    _gravar(linha)
    carregar(forcar=True)
    return {"ok": True, "cancelado": linha, "recibo": _recibo("cancel", linha),
            "linha": f"Esqueci: {linha.get('titulo', '')}."}


def pospor(id_ou_texto, quando, *, origem = "", persona = "") -> dict:
    """Soneca: um novo instante para o mesmo gatilho."""
    negado = _quem_pode(origem, persona, acao="pospor um agendamento")
    if negado:
        return {"ok": False, "recusa": negado}
    alvo = str(id_ou_texto or "").strip()
    achados = [l for l in listar()
               if str(l.get("id")) == alvo
               or alvo.lower() in str(l.get("titulo") or "").lower()]
    if not achados:
        return {"ok": False, "recusa": f"não achei o agendamento {alvo!r}"}
    if len(achados) > 1:
        return {"ok": False,
                "pergunta": "mais de um agendamento bate com esse nome; diga "
                            "o id: " + ", ".join(l["id"] for l in achados)}
    linha = achados[0]
    # Regra 7 na mesma ordem da criação: a persona é barrada **antes** de
    # qualquer reescrita da nota. Pospor é escrever no cofre.
    barreira = _barreira_da_persona(origem, persona, acao="pospor um "
                                                        "agendamento",
                                    alvo=dict(linha, quando=str(quando or "")))
    if barreira:
        return barreira
    if linha.get("disparou_em"):
        return {"ok": False, "recusa": (f"{linha.get('titulo', '')} já "
                                        "disparou; para de novo, agende de novo")}
    iso, erro = interpretar_quando(quando)
    if erro:
        return {"ok": False, "recusa": erro}
    novo_dt = task_queue._parse_dt(iso)
    if novo_dt is not None and novo_dt <= agora():
        return {"ok": False, "recusa": (f"a nova hora em {str(quando)!r} já "
                                        "passou; para repetir, agende de novo")}
    linha["quando"] = iso
    linha["soneca_em"] = agora().isoformat()
    _gravar(linha)
    carregar(forcar=True)
    return {"ok": True, "linha": linha, "recibo": _recibo("snooze", linha)}


# ── o disparo: UMA única função roda gatilho ────────────────────────────────

def _recibo(acao: str, linha: dict) -> dict:
    """Recibo HMAC do registro (critério 10 do plano). Sem chave, diz que não
    assinou em vez de fingir um recibo."""
    args = {"acao": acao, "id": linha.get("id", ""),
            "quando": linha.get("quando", ""),
            "gatilho": linha.get("gatilho") or {}}
    try:
        from core import tool_receipts
        return tool_receipts.make_receipt("schedule", args, {"ok": True})
    except Exception as exc:  # noqa: BLE001 - recibo não derruba o registro
        return {"tool": "schedule", "hmac": "",
                "motivo": f"sem chave de recibo: {exc}"}


#: Saídas ligadas por ``main.py`` (boca do turno, WhatsApp do dono, upload).
_PORTAS: dict = {}


def ponte(voz=None, whatsapp=None, upload=None) -> dict:
    """Liga as saídas que existem ``main.py`` (boca, WhatsApp, upload).

    O agendador **não tem boca própria**: pede ao mesmo caminho que o turno
    usa (seção 3.2). Sem ponte, :func:`executar_gatilho` recusa dizendo o que
    falta — nunca sucesso mudo.
    """
    if callable(voz):
        _PORTAS["voz"] = voz
    if callable(whatsapp):
        _PORTAS["whatsapp"] = whatsapp
    if callable(upload):
        _PORTAS["upload"] = upload
    return {"voz": callable(_PORTAS.get("voz")),
            "whatsapp": callable(_PORTAS.get("whatsapp"))}


def executar_gatilho(gatilho, *, linha = None, boca = None, whatsapp = None,
                     agora_dt = None) -> dict:
    """A **única** função que roda um gatilho — voz, texto e o ``tick`` passam
    todos por aqui (lei C1: se um porto pode, o outro pode).

    Nunca consome ``job_grant``, nunca aprova plano, nunca abre janela de
    confirmação, nunca sobe tier: se o destino exige autorização
    (``arquivo-subir``), o disparo **pede a autorização na hora marcada** e,
    sem fator do dono aberto, devolve ``pendente`` e não executa mudo (3.5).
    """
    gatilho = gatilho or {}
    normalizado, erros = validar_gatilho(gatilho)
    if erros:
        return {"ok": False, "recusa": "; ".join(erros)}
    gatilho = normalizado
    boca = boca or _PORTAS.get("voz")
    whatsapp = whatsapp or _PORTAS.get("whatsapp")
    texto = str((linha or {}).get("texto") or (linha or {}).get("titulo")
                or "")
    resultado = {"ok": True, "feito": [], "destinos": gatilho["destino"],
                 "valor": "", "pendente": "", "recusa": ""}

    # 1) o trabalho do gatilho roda primeiro (é isto que o dono chamou de
    #    "o script ou a função que vai executar").
    trabalho, erro = _rodar_gatilho(gatilho, linha=linha, resultado=resultado)
    if erro:
        return {"ok": False, "recusa": erro}
    if isinstance(trabalho, dict) and trabalho.get("recusa"):
        return {"ok": False, "recusa": str(trabalho["recusa"])}
    if trabalho is not None:
        resultado["valor"] = trabalho if isinstance(trabalho, str) else ""
    fala = str(resultado["valor"] or "").strip() or texto

    # 2) para onde vai o resultado.
    for destino in gatilho["destino"]:
        if destino in DESTINO_FERRAMENTA:
            ferramenta = DESTINO_FERRAMENTA[destino]
            if _tier_da_ferramenta(ferramenta) != "low" and \
                    not actiongate.authorized():
                resultado["pendente"] = (
                    f"falta autorização do dono para {ferramenta} "
                    f"(tier {_tier_da_ferramenta(ferramenta)}); o registro "
                    "fica pendente e diz isso — não executa mudo")
                continue
        if destino == "voz":
            if not callable(boca):
                resultado["recusa"] = ("nenhuma boca ligada ao agendador "
                                       "(ponte(voz=...)); nada foi falado")
                resultado["ok"] = False
                continue
            boca(fala)
            resultado["feito"].append("voz")
        elif destino == "whatsapp":
            if not callable(whatsapp):
                resultado["recusa"] = ("nenhum canal de WhatsApp ligado ao "
                                       "agendador (ponte(whatsapp=...)); nada "
                                       "foi enviado")
                resultado["ok"] = False
                continue
            whatsapp(fala)
            resultado["feito"].append("whatsapp")
        else:
            resultado["feito"].append(destino)
    if resultado["pendente"] and not resultado["feito"]:
        resultado["ok"] = False
    return resultado


def _rodar_gatilho(gatilho, *, linha=None, resultado=None):
    """Chama a função cadastrada ou o script cadastrado. Nada de shell."""
    resultado = resultado if isinstance(resultado, dict) else {}
    argumentos = gatilho.get("argumentos") or []
    contexto = {"linha": linha or {}, "resultado": resultado}
    if gatilho["tipo"] == "funcao":
        import importlib
        modulo = importlib.import_module(gatilho["modulo"])
        alvo = getattr(modulo, gatilho["funcao"])
        kwargs, args = {}, []
        for item in argumentos:
            if item.get("nome"):
                kwargs[item["nome"]] = item["valor"]
            else:
                args.append(item["valor"])
        try:
            return alvo(*args, **kwargs), ""
        except TypeError as exc:
            # assinatura incompatível foi **validada na criação** e mesmo
            # assim estourou: recusa nomeada, nunca traceback no turno.
            return None, (f"{gatilho['modulo']}.{gatilho['funcao']} não aceitou "
                          f"os argumentos do registro: {exc}")
        except Exception as exc:  # noqa: BLE001 - o disparo não derruba o turno
            return None, (f"{gatilho['modulo']}.{gatilho['funcao']} falhou no "
                          f"disparo: {exc}")
    argv = [str(item["valor"]) for item in argumentos]
    cmd = [gatilho["script"], *argv]
    resultado["argv"] = list(cmd)
    try:
        feito = subprocess.run(cmd, shell=False, capture_output=True,
                               text=True, timeout=SCRIPT_TIMEOUT_SECONDS)
    except OSError as exc:
        return None, f"não consegui rodar o script {gatilho['script']}: {exc}"
    except subprocess.TimeoutExpired:
        return None, (f"o script {gatilho['script']} não terminou em "
                      f"{SCRIPT_TIMEOUT_SECONDS}s")
    if feito.returncode != 0:
        return None, (f"o script {gatilho['script']} saiu com código "
                      f"{feito.returncode}: {str(feito.stderr or '')[:200]}")
    return str(feito.stdout or "").strip(), ""


# ── o laço: uma passada, sem thread, sem duplicar ────────────────────────────

def vencidos(*, base: datetime | None = None,
             grace_seconds: float = LATE_GRACE_SECONDS) -> list[dict]:
    """Abertos, já passados da hora + graça e ainda sem resumo falado."""
    base = base or agora()
    saida = []
    for linha in listar():
        quando = task_queue._parse_dt(linha.get("quando"))
        if quando is None:
            continue
        if linha.get("aviso_atrasado_em"):
            continue
        if (base - quando).total_seconds() > float(grace_seconds):
            saida.append(linha)
    return saida


def resumo_dos_vencidos(linhas: list[dict]) -> str:
    cabeca = "; ".join(f"{l.get('titulo', '')} (era {str(l.get('quando', ''))[:16]})"
                       for l in linhas[:3])
    resto = f"; e mais {len(linhas) - 3}" if len(linhas) > 3 else ""
    return f"{cabeca}{resto}"


def acordar(*, base = None, boca = None, whatsapp = None,
            grace_seconds: float = LATE_GRACE_SECONDS) -> dict:
    """Regra ditada dele (decisão 6): contagem, resumo e a pergunta.

    "Avise que tem N lembretes, me de um resumo e pergunte se quero ouvir um a
    um." Uma frase só por passada, marcada no registro (``aviso_atrasado_em``)
    **antes** de falar — o precedente é ``core/wake_announce.py``: a marca vem
    primeiro, a fala depois; por isso o resumo não se repete nem depois de
    reiniciar.
    """
    global _ja_acordei
    base = base or agora()
    _ja_acordei = True
    linhas = vencidos(base=base, grace_seconds=grace_seconds)
    if not linhas:
        return {"ok": True, "quantidade": 0, "texto": ""}
    n = len(linhas)
    texto = (f"Tem {n} lembrete{n > 1 and 's' or ''} vencido"
             f"{n > 1 and 's' or ''}: {resumo_dos_vencidos(linhas)}. "
             "Quer ouvir um a um?")
    for linha in linhas:
        linha["aviso_atrasado_em"] = base.isoformat()
        _gravar(linha)
    carregar(forcar=True)
    boca = boca or _PORTAS.get("voz")
    whatsapp = whatsapp or _PORTAS.get("whatsapp")
    feito = []
    if callable(boca):
        boca(texto)
        feito.append("voz")
    if callable(whatsapp):
        whatsapp(texto)
        feito.append("whatsapp")
    return {"ok": True, "quantidade": n, "texto": texto, "feito": feito,
            "ids": [l["id"] for l in linhas]}


def um_a_um(*, base = None, boca = None, whatsapp = None) -> dict:
    """O "sim" à pergunta da decisão 6: fala cada vencido, marca como disparado."""
    base = base or agora()
    boca = boca or _PORTAS.get("voz")
    whatsapp = whatsapp or _PORTAS.get("whatsapp")
    falados = []
    for linha in vencidos(base=base):
        resultado = executar_gatilho(linha.get("gatilho") or {}, linha=linha,
                                     boca=boca, whatsapp=whatsapp,
                                     agora_dt=base)
        linha["disparou_em"] = base.isoformat()
        linha["disparo"] = {"feito": resultado.get("feito", []),
                            "recusa": resultado.get("recusa", ""),
                            "pendente": resultado.get("pendente", "")}
        _gravar(linha)
        falados.append(linha["id"])
    if falados:
        carregar(forcar=True)
    return {"ok": True, "falados": falados}


def tick(*, agora_dt = None, boca = None, whatsapp = None,
         grace_seconds: float = LATE_GRACE_SECONDS) -> list[dict]:
    """Uma passada do laço que já existe. Devolve o que aconteceu (nunca levanta).

    Um único ponto de disparo por vencimento: a marca ``disparou_em`` é escrita
    **antes** da fala (padrão ``core/wake_announce.py:71-72``), então o segundo
    ``tick`` do mesmo id não repete o recado (risco 1 do plano).
    """
    eventos: list[dict] = []
    base = agora_dt or agora()
    global _ja_acordei
    if not _ja_acordei:
        eventos.append({"tipo": "acordar", **acordar(
            base=base, boca=boca, whatsapp=whatsapp,
            grace_seconds=grace_seconds)})
    for linha in listar():
        quando = task_queue._parse_dt(linha.get("quando"))
        if quando is None:
            eventos.append({"tipo": "erro", "id": linha.get("id"),
                            "motivo": "quando ilegível na nota (a criação "
                                      "valida; alguém editou a mão)"})
            continue
        if quando > base:
            continue
        if (base - quando).total_seconds() > float(grace_seconds):
            # vencido longe demais: não fala na hora errada, entra no resumo
            # da regra 6 na próxima acordada (PONTO-EXTENSAO-pos-reboot)
            if linha.get("disparou_em"):
                continue
            with _LOCK:
                _ja_acordei = True
            continue
        if linha.get("disparou_em") or linha.get("cancelado_em"):
            continue
        linha["disparou_em"] = base.isoformat()
        _gravar(linha)
        resultado = executar_gatilho(linha.get("gatilho") or {}, linha=linha,
                                     boca=boca, whatsapp=whatsapp,
                                     agora_dt=base)
        linha["disparo"] = {"feito": resultado.get("feito", []),
                            "recusa": resultado.get("recusa", ""),
                            "pendente": resultado.get("pendente", "")}
        if resultado.get("pendente") and not resultado.get("feito"):
            # não virou sucesso mudo; volta a tentar na próxima passada, mas
            # só avisa uma vez por processo (anti-rajada)
            linha["disparou_em"] = ""
            linha["pendente_desde"] = base.isoformat()
        _gravar(linha)
        carregar(forcar=True)
        eventos.append({"tipo": "disparo", "id": linha["id"],
                        "titulo": linha.get("titulo", ""), **resultado})
    return eventos


# ── leitura para o turno (o que o dono pergunta) ────────────────────────────

def linha_de(id_ou_texto) -> dict | None:
    alvo = str(id_ou_texto or "").strip()
    for linha in carregar().values():
        if str(linha.get("id")) == alvo or \
                alvo.lower() in str(linha.get("titulo") or "").lower():
            return linha
    return None


def agenda_de_hoje(*, base = None) -> list[dict]:
    base = base or agora()
    dia = base.date().isoformat()
    return [l for l in listar(abertos_only=False)
            if str(l.get("quando") or "").startswith(dia)]


def linha_texto(linha: dict) -> str:
    gatilho = linha.get("gatilho") or {}
    alvo = (f"{gatilho.get('modulo')}.{gatilho.get('funcao')}"
            if gatilho.get("tipo") == "funcao" else gatilho.get("script", ""))
    estado = ("cancelado" if linha.get("cancelado_em") else
              "disparou" if linha.get("disparou_em") else "aberto")
    return (f"{linha.get('id')} | {str(linha.get('quando') or '')[:16]} | "
            f"{linha.get('titulo', '')} | {gatilho.get('tipo', '')} "
            f"{alvo} | {'+'.join(gatilho.get('destino') or [])} | {estado}")


__all__ = [
    "GATILHO_TIPOS", "GATILHO_DESTINOS", "DESTINO_PADRAO", "ARGUMENTO_TIPOS",
    "MODULOS_PERMITIDOS", "LATE_GRACE_SECONDS",
    "agora", "interpretar_quando", "validar_gatilho", "executar_gatilho",
    "registrar", "listar", "cancelar", "pospor", "tick", "acordar", "um_a_um",
    "vencidos", "carregar", "erro_de_leitura", "ponte", "cadastrar_script",
    "scripts_cadastrados", "nome_da_nota", "caminho_da_nota", "corpo_da_nota",
    "linha_de", "agenda_de_hoje", "linha_texto", "pedidos_aprovacao",
]
