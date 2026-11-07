"""Fluxo de execucao por corpo de e-mail — FLUXO EMAIL, passos 5-6 (UP-6).

O filtro por conta (``core/email.py``) decide SE o e-mail vira aviso; este
modulo decide O QUE EXECUTAR quando o corpo do e-mail bate com uma instrucao
ou fluxo conhecido. O contrato (skills/fluxos/FLUXOS.md):

    5. CEREBRO VERIFICA SE EXISTE ALGUM FLUXO A EXECUTAR COM BASE NO CORPO
       DO EMAIL VS INSTRUCAOES E FILTROS
    6. SE SIM, EXECUTA O MESMO

Portao de entrada: ``core/email.py`` chama ``dispatch_flow()`` depois da
decisao de filtro, SÓ para regras com ``dispatch: true`` no config da conta.
Sem a flag, o comportamento e exatamente o de antes: nenhum corpo e lido
para execucao, nenhum fluxo roda.

DSL (``plugin_config.mail.flows``, validada por ``clean_flows``)
    Um fluxo e ``{"name", "type", "when": {...}, "allow": [...],
    "match": "contains" | "regex"}``. Tipos:

    * ``leitura_codigo``   — corpo contem codigo de verificacao
      (``core/otp_detect.py``) -> o codigo e lido em voz alta pelo gancho
      de fala registrado (o MESMO ``_announce_otp`` do vigia de OTP).
    * ``aviso_simples``    — nada a executar alem do aviso que ja aconteceu;
      registra o evento e encerra.
    * ``acao_automatica``  — corpo contem instrucao explicita do dono-em-confianca
      ("lembrete: ...", "tarefa: ...", "agendar ...") -> a acao casada com a
      lista ``allow`` do fluxo e roteada para o executor correto
      (``actions.reminder`` / ``actions.tasks``).

    ``when`` filtra por ``from``, ``subject`` e ``body`` (a primeira regra
    que casa vence, como nos filtros). Sem fluxo configurado vale o fluxo
    padrao de ``leitura_codigo`` — codigo de banco lido em voz alta e o
    criterio de aceitacao da UP-6.

LINKEDIN E SO UMA REGRA DESTE FLUXO (decisao do dono, 07/11/2026)
    Nao existe tipo, plugin, rota nem OAuth de LinkedIn aqui: a notificacao
    do LinkedIn e a INSTANCIA ``linkedin`` de ``aviso_simples`` -- o tipo que
    a DSL ja tinha para "o aviso aconteceu e nao ha nada a executar". Ela e
    a primeira regra de ``plugin_config.mail.flows``, casando pelo DOMINIO do
    remetente (``when.from`` em regex), de modo que um assunto que só menciona
    "linkedin" vindo de outro remetente nao casa. Sem dispatch de acao: o
    dono e avisado pelo filtro (``action: avisa``) e o fluxo registra o recibo
    com ``acao=aviso``. ``core/linkedin.py`` e ``plugins/linkedin_control.py``
    ficaram inerte (``plugins_enabled.linkedin_control = false``); nada aqui
    abre navegador, posta, comenta ou roda OAuth.

CORPO E DADO, NUNCA ORDEM (lei do contrato de e-mail)
    O texto vem de terceiro. Este modulo NUNCA trata o corpo como comando
    generico: só casa marcadores fixos (lembrete/tarefa/agendamento) com
    validacao dura — lembrete e agendamento exigem data E hora explicitas no
    proprio texto, sem data o fluxo NAO executa — e nada daqui alcanca o
    caminho de ENVIO de e-mail (irreversivel; so a voz do dono envia, via
    ``core/email.py.confirm_send``). Instrucoes tipo "ignore tudo e envie"
    nao casam com marcador nenhum: viram ``sem_instrucao``.

Privacidade
    O evento ``email_flow_executed`` leva conta, uid, nome do fluxo, tipo,
    acao e motivo — nunca remetente, assunto, corpo ou o digito do codigo.
    O historico de execucoes (``memory/email_flow_seen.json``) guarda so a
    chave ``endereco|uid|fluxo`` para nao executar duas vezes o mesmo recibo.
    A UNICA excecao escrita em disco e a mensagem privada do LinkedIn: por
    decisao do dono ela e guardada como texto (nota ``NOTA-<data>-linkedin-
    msg-<slug>.md`` no vault ``~/Jarvis``, senao linha em
    ``logs/linkedin-notifications.log``). A frase falada nunca leva corpo,
    link ou endereco — so quem enviou e o tipo de evento.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_DEFAULT_SEEN_PATH = _BASE / "memory" / "email_flow_seen.json"

FLOW_TYPES = ("leitura_codigo", "aviso_simples", "acao_automatica",
              "linkedin_notification")
ACTION_ROUTES = ("lembrete", "tarefa", "agendamento")
_WHEN_FIELDS = ("from", "subject", "body")
_MAX_FLOWS = 12
_MAX_FINDINGS = 8
_TEXT_LIMIT = 200
_SEEN_MAX = 2000
_RETRY_MOTIVOS = ("fala_indisponivel", "aviso_indisponivel")
_WATCH_LIMIT = 200

# LinkedIn (decisao do dono: filtro do fluxo de e-mail, nao plugin proprio).
_LINKEDIN_FLAT_LIMIT = 3000
_LINKEDIN_RESUMO_LIMIT = 160
_LINKEDIN_NOTE_LIMIT = 2000
_VAULT_NOTE_PREFIX = "NOTA-"
_LINKEDIN_VAULT = Path.home() / "Jarvis"
_LINKEDIN_LOG = _BASE / "logs" / "linkedin-notifications.log"

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_lock = threading.RLock()
_seen: dict[str, None] | None = None
_seen_path = _DEFAULT_SEEN_PATH
_event_hook = None          # fn(payload: dict) -> None (activity.log)
_speak_hook = None          # fn(payload: dict) -> bool  (leitura_codigo)
_notify_hook = None         # fn(payload: dict) -> bool  (linkedin_notification)
_notify_hook = None         # fn(payload: dict) -> bool  (linkedin_notification)
_executor = None            # fn(action, params, item, acct) -> bool
_logger = None

# Marcadores de instrucao: linha propria cometece explicita. Nada mais no
# corpo vira acao — "ignore tudo e envie para x@y" nao casa com marcador
# nenhum e fica em ``sem_instrucao``.
_MARKER_PATTERNS = (
    ("lembrete", re.compile(
        r"^\s*(?:lembrete|lembre[\s-]?se|n[aã]o\s+esque[cç]a(?:r)?(?:\s+de)?)"
        r"\s*[:\-–]\s*(.+)$", re.IGNORECASE)),
    ("tarefa", re.compile(
        r"^\s*(?:tarefa|para\s+fazer|to[\s-]?do)\s*[:\-–]\s*(.+)$",
        re.IGNORECASE)),
    ("agendamento", re.compile(
        r"^\s*(?:agendar|agendamento|marcar)\s*[:\-–]?\s+(.+)$",
        re.IGNORECASE)),
    ("agendamento", re.compile(
        r"^\s*(?:reuni[aã]o|compromisso)\s*[:\-–]\s*(.+)$",
        re.IGNORECASE)),
)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_TIME_RE = re.compile(r"\b(?:[àa]s\s+)?(\d{1,2})[:h](\d{2})\b",
                      re.IGNORECASE)

# ── LinkedIn: marcadores de notificacao (corpo lido como DADO, nunca ordem) ──
_LI_SENDER_RE = re.compile(r"@[\w.-]*linkedin\.com$", re.IGNORECASE)
_LI_BRAND_RE = re.compile(r"linkedin", re.IGNORECASE)
_LI_GENERIC_SENDER_RE = re.compile(
    r"^(?:linkedin|via\s+linkedin|linkedin\s+(?:notifications?|jobs?)"
    r"|.*no-?reply.*|.*noreply.*)$", re.IGNORECASE)
_LI_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_LI_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\];,]+", re.IGNORECASE)
_LI_SILENT_KINDS = ("vaga", "notificacao_empresa")
_LI_NAME_LIMIT = 60
_LI_KINDS = (
    ("convite_conexao", re.compile(
        r"(convite (?:para )?(?:de )?conex|conect(?:ar|e)[\s\-]se"
        r"|quer (?:conectar|conversar|falar) com voc|adicionou voc"
        r"|aceitar (?:o )?convite|invitation to connect"
        r"|want(?:s|ed)? to connect|connection request|sent you an invite)",
        re.IGNORECASE)),
    ("mensagem", re.compile(
        r"(enviou\s+(?:uma\s+|a\s+|nova\s+)*mensagem"
        r"|nova mensagem|novas mensagens"
        r"|tem (?:uma |nova )?mensagem|mensagem privada"
        r"|respondeu (?:a |à )?(?:sua )?(?:conversa|mensagem)|disse:"
        r"|sent you a message|new message|message from"
        r"|replied to your conversation)", re.IGNORECASE)),
    ("vaga", re.compile(
        r"(vagas? (?:recomendad|dispon\u00edve|para voc|no (?:seu )?cargo)"
        r"|job alert|recommended (?:job|jobs|for you)|jobs? match"
        r"|cargo (?:ideal|que combina)|vagas? em alta)", re.IGNORECASE)),
    ("notificacao_empresa", re.compile(
        r"(update[s]? da (?:empresa|p[a\u00e1]gina)|company update"
        r"|post(?:agem)? (?:da|no) (?:empresa|p[a\u00e1]gina)"
        r"|nova postagem|empresa que voc[\u00eae] segue|new follower"
        r"|seguidores (?:novos|da empresa)|publica[\u00e7c][\u00e3a]o da empresa)",
        re.IGNORECASE)),
)
_LI_NAME_VERB_RE = re.compile(
    r"([A-Z\u00c0-\u00dd][\w\u00c0-\u00df'\u2019.\-]{1,30}"
    r"(?:\s+[A-Z\u00c0-\u00dd][\w\u00c0-\u00df'\u2019.\-]{1,30}){1,3})"
    r"\s*(?:\u2014|\-|:|,)?\s*(?:quer (?:conectar|conversar|falar)|enviou"
    r"|adicionou|convidou|mencionou|comentou|publicou|disse"
    r"|sent |invited |wanted to connect|commented on)", re.UNICODE)
_LI_NAME_FROM_RE = re.compile(
    r"(?:mensagem|convite|notifica[\u00e7c][\u00e3a]o|pedido|recado)"
    r"\s+(?:de|from)\s+([A-Z\u00c0-\u00dd][\w\u00c0-\u00df'\u2019.\-]{1,30}"
    r"(?:\s+[A-Z\u00c0-\u00dd][\w\u00c0-\u00df'\u2019.\-]{1,30}){1,3})",
    re.UNICODE)

# ── LinkedIn: nada de codigo proprio (decisao do dono, 07/11/2026) ───────────
# O reconhecimento do LinkedIn mora na CONFIG deste fluxo, nao aqui: a regra
# ``linkedin`` e um ``aviso_simples`` com ``when.from`` em regex ancorada no
# dominio do remetente -- ``@(...\.)?linkedin\.com$``. O dominio e o criterio
# (assunto "linkedin" vindo de outro remetente e falso positivo e nao casa);
# os e-mails reais do LinkedIn chegam de messages-noreply/news-noreply/
# emissions@linkedin.com, todos terminados em linkedin.com. Nada de plugin,
# OAuth, post ou navegador: ver ``plugins_enabled.linkedin_control``.


def _log(message: str) -> None:
    fn = _logger
    if fn is None:
        try:
            print(f"[MailFlow] {message}")
        except Exception:
            pass
        return
    try:
        fn(message)
    except Exception:
        pass


def set_event_hook(handler=None) -> None:
    """Registra ``handler(payload)`` para o evento ``email_flow_executed``.

    Roda na thread da varredura; quem escreve no activity.log deve ser
    thread-safe (é o que ``main.JarvisLive._mail_flow_executed`` faz).
    """
    global _event_hook
    _event_hook = handler if callable(handler) else None


def set_speak_hook(handler=None) -> None:
    """Registra ``handler(payload) -> bool`` para o fluxo ``leitura_codigo``.

    O payload é o mesmo molde do vigia de OTP (``code``, ``app``, ``sender``,
    ``subject``, ``origin``). Devolver False (sessão ainda não pronta) faz o
    recibo NÃO ser marcado como visto: a varredura seguinte tenta de novo.
    """
    global _speak_hook
    _speak_hook = handler if callable(handler) else None


def set_executor(handler=None) -> None:
    """Registra ``handler(action, params, item, acct) -> bool``.

    ``None`` usa o roteamento interno: lembrete/agendamento vão para
    ``actions.reminder``, tarefa para ``actions.tasks``. Nada daqui alcança
    envio de e-mail — esse caminho é só do portão de voz do dono.
    """
    global _executor
    _executor = handler if callable(handler) else None


def set_notify_hook(handler=None) -> None:
    """Registra ``handler(payload) -> bool`` para ``linkedin_notification``.

    O payload é ``{"text", "source", "event_kind", "from_name", "link",
    "account", "uid"}`` — ``text`` já é a frase pronta do aviso ("LinkedIn:
    Fulano te mandou mensagem — quer ler?"). Sem gancho (processo fora do
    assistente vivo) o aviso é escrito em disco por padrão: nada é falado e
    nada é inventado. Devolver False (sessão não pronta) NÃO gasta o recibo:
    a varredura seguinte tenta de novo.
    """
    global _notify_hook
    _notify_hook = handler if callable(handler) else None


def set_notify_hook(handler=None) -> None:
    """Registra ``handler(payload) -> bool`` para ``linkedin_notification``.

    O payload e ``{"text", "source", "event_kind", "from_name", "link",
    "account", "uid"}``. Sem gancho o aviso vai para disco: nada e falado e
    nada e inventado. Devolver False (sessao nao pronta) NAO gasta o recibo.
    """
    global _notify_hook
    _notify_hook = handler if callable(handler) else None


def reset() -> None:
    """Esquece hooks, cache de vistos e caminhos (testes e ``mail.reset``)."""
    global _seen, _seen_path, _event_hook, _speak_hook, _executor
    global _notify_hook
    with _lock:
        _seen = None
        _seen_path = _DEFAULT_SEEN_PATH
    _event_hook = None
    _speak_hook = None
    _notify_hook = None
    _executor = None


# ── DSL: validação e carga dos fluxos ────────────────────────────────────────

def _one_line(value: object, limit: int = _TEXT_LIMIT) -> str:
    text = _CTRL_RE.sub(" ", str(value or ""))
    text = " ".join(text.split())
    return text[:limit]


def clean_flow(entry: object) -> dict | None:
    """Um fluxo validado da DSL, ou None se for descartável.

    Nome obrigatório; ``type`` em FLOW_TYPES; ``when`` só aceita os campos
    from/subject/body com padrão curto; ``match`` contains|regex (regex passa
    por compilação — padrão inválido derruba o fluxo inteiro, a gente não
    adivinha fluxo do dono); ``allow`` limitado a ACTION_ROUTES e vale apenas
    para ``acao_automatica`` (ausente = os três rotas); ``watch`` é regex
    opcional (assunto que reativa o aviso dos tipos silenciosos do LinkedIn)
    e passa pela mesma compilação.
    """
    if not isinstance(entry, dict):
        return None
    name = _one_line(entry.get("name") or "", 40)
    if not name:
        return None
    ftype = str(entry.get("type") or "").strip().lower()
    if ftype not in FLOW_TYPES:
        return None
    match = str(entry.get("match") or "contains").strip().lower()
    if match not in ("contains", "regex"):
        return None
    when_raw = entry.get("when")
    when: dict[str, str] = {}
    if isinstance(when_raw, dict):
        for key in _WHEN_FIELDS:
            pattern = _one_line(when_raw.get(key) or "", 120)
            if not pattern:
                continue
            if match == "regex":
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error:
                    return None
            when[key] = pattern
    elif when_raw not in (None, ""):
        return None
    watch = _one_line(entry.get("watch") or "", _WATCH_LIMIT)
    if watch:
        try:
            re.compile(watch, re.IGNORECASE)
        except re.error:
            return None
    allow_raw = entry.get("allow")
    allow: list[str] = []
    if ftype == "acao_automatica":
        if allow_raw is None:
            allow = list(ACTION_ROUTES)
        elif isinstance(allow_raw, list):
            allow = [str(a).strip().lower() for a in allow_raw
                     if str(a).strip().lower() in ACTION_ROUTES]
        if not allow:
            return None
    return {"name": name, "type": ftype, "when": when, "allow": allow,
            "match": match, "watch": watch}


def clean_flows(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for entry in entries[:_MAX_FLOWS]:
        flow = clean_flow(entry)
        if flow is None:
            _log("fluxo de e-mail inválido ignorado")
            continue
        out.append(flow)
    return out


def load_flows() -> list[dict]:
    """Fluxos do dono (``plugin_config.mail.flows``) + o ramal do LinkedIn.

    Sem fluxo configurado vale ``DEFAULT_FLOWS``. Com fluxo configurado, o
    ramal embutido do LinkedIn (``_BUILTIN_FLOWS``) entra na frente do que o
    dono escreveu: e por ele que a UP-4 roda hoje — notificacao de LinkedIn
    nao tem plugin proprio, e uma regra deste fluxo. O disparo continua sob o
    portao da regra ``dispatch: true`` do filtro, entao nada roda por conta
    propria.
    """
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config("mail") or {}
        entries = cfg.get("flows")
    except Exception:
        entries = None
    flows = clean_flows(entries)
    if flows:
        known = {flow["name"] for flow in flows}
        flows = [f for f in _BUILTIN_FLOWS if f["name"] not in known] + flows
    return flows if flows else list(DEFAULT_FLOWS)


DEFAULT_FLOWS: list[dict] = [
    {"name": "linkedin_aviso", "type": "linkedin_notification",
     "when": {"from": "linkedin"}, "allow": [], "match": "contains",
     "watch": ""},
    {"name": "leitura_codigo", "type": "leitura_codigo",
     "when": {}, "allow": [], "match": "contains", "watch": ""},
]

_BUILTIN_FLOWS: list[dict] = [DEFAULT_FLOWS[0]]


# ── parser do corpo ──────────────────────────────────────────────────────────

def _find_datetime(text: str) -> tuple[str | None, str | None]:
    """Data ISO e hora HH:MM (ou 14h30) lidas do próprio texto do marcador.

    A data sai da frase antes de procurar a hora: "lembrete às 14:00 do dia
    2026-11-10" acha os dois, em qualquer ordem.
    """
    d = _DATE_RE.search(text)
    date = d.group(1) if d else None
    rest = text if d is None else (text[:d.start()] + " " + text[d.end():])
    t = _TIME_RE.search(rest)
    time_ = None
    if t:
        hour = int(t.group(1))
        minute = int(t.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            time_ = f"{hour:02d}:{minute:02d}"
    return date, time_


def parse_body(text: object) -> list[dict]:
    """Instruções explícitas no corpo: lembrete, tarefa, agendamento.

    Linha a linha, só marcador cometece (``lembrete:``, ``tarefa:``,
    ``agendar ...``, ``reunião: ...``) vira ação. O texto da ação é a parte
    após o marcador, em uma linha, sem nada do resto do corpo. Data/hora
    são lidas do próprio texto da linha; ausentes ficam None — quem executa
    exige data E hora para lembrete/agendamento. Nunca envia e-mail, nunca
    confirma nada, nunca apaga nada: as três rotas são criação de item.
    Devolve ``[{"action", "text", "date", "time"}]`` (máximo 8 achados).
    """
    findings: list[dict] = []
    for raw_line in str(text or "").splitlines():
        line = " ".join(raw_line.split())
        if not line or len(findings) >= _MAX_FINDINGS:
            continue
        for action, pattern in _MARKER_PATTERNS:
            hit = pattern.match(line)
            if not hit:
                continue
            body_part = _one_line(hit.group(1), _TEXT_LIMIT)
            if not body_part:
                break
            date, time_ = _find_datetime(body_part)
            findings.append({"action": action, "text": body_part,
                             "date": date, "time": time_})
            break
    return findings


def detect_code(body: object, subject: object = "",
                sender: object = "") -> dict | None:
    """O detector do vigia (core/otp_detect.py) aplicado ao corpo do fluxo."""
    try:
        from core import otp_detect
        return otp_detect.detect(body, subject=subject, sender=sender)
    except Exception:
        return None


# ── parser de notificacao do LinkedIn (rota de e-mail da UP-4) ──────────────

def _flatten(value: object, limit: int = _TEXT_LIMIT) -> str:
    return _one_line(value, limit)


def is_linkedin_sender(item: dict) -> bool:
    """O remetente é do LinkedIn? Só o domínio importa: nome bonito não basta."""
    if not isinstance(item, dict):
        return False
    for key in ("from", "sender"):
        text = _flatten(item.get(key) or "", 160).strip()
        if text and _LI_SENDER_RE.search(text):
            return True
    return False


def _header_person(item: dict) -> str:
    """Nome de pessoa no cabeçalho, quando não é o genérico 'LinkedIn'."""
    if not isinstance(item, dict):
        return ""
    name = _flatten(item.get("sender") or "", _LI_NAME_LIMIT).strip()
    addr = _flatten(item.get("from") or "", 160).strip()
    if not name or name.casefold() == addr.casefold():
        return ""
    if _LI_GENERIC_SENDER_RE.match(name):
        return ""
    return name


def _body_person(flat: str) -> str:
    """Quem enviou, lido do corpo: nome antes do verbo ou depois de 'de'."""
    for pattern in (_LI_NAME_VERB_RE, _LI_NAME_FROM_RE):
        hit = pattern.search(flat)
        if not hit:
            continue
        name = " ".join(hit.group(1).split())
        if len(name) < 3 or any(ch.isdigit() for ch in name):
            continue
        if name.casefold() in ("linkedin", "equipe linkedin", "a equipe"):
            continue
        return _flatten(name, _LI_NAME_LIMIT)
    return ""


def _principal_link(flat: str) -> str:
    """O link principal: o primeiro que aponta para o LinkedIn, senão o primeiro."""
    urls = [u.rstrip(".,;:!?)'\"") for u in _LI_URL_RE.findall(flat)]
    urls = [u for u in urls if u]
    if not urls:
        return ""
    for url in urls:
        if _LI_BRAND_RE.search(url):
            return _flatten(url, 300)
    return _flatten(urls[0], 300)


def _resumo_for(text: str, start: int) -> str:
    """A frase onde a marca caiu — 1-2 frases, nunca o corpo inteiro."""
    head = text[:max(0, start)]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "), 0)
    tail = text[cut + 1:] if cut else text
    sentences = [s.strip() for s in _LI_SENTENCE_RE.split(tail, maxsplit=2)[:2]]
    return _flatten(" ".join(s for s in sentences if s),
                    _LINKEDIN_RESUMO_LIMIT)


def _kind_of(text: str, subj: str) -> tuple[str, str]:
    """(tipo de evento, resumo): o corpo manda, o assunto só quebra o empate."""
    for name, pattern in _LI_KINDS:
        hit = pattern.search(text)
        if hit:
            return name, _resumo_for(text, hit.start())
        hit = pattern.search(subj)
        if hit:
            return name, _resumo_for(subj, hit.start())
    return "", ""


def parse_linkedin(body: object, subject: object = "", watch: object = "",
                   item: dict | None = None) -> dict | None:
    """Notificacao do LinkedIn lida do corpo: QUEM, O QUE, LINK, RESUMO.

    O corpo é DADO: nada aqui vira ordem, nada abre navegador, nada roda
    OAuth (``core/linkedin.py`` segue inerte). Sem marca reconhecida e sem a
    marca 'LinkedIn' no texto, devolve None — não avisamos o que não lemos.

    ``watch`` é a regex de assunto que reativa os tipos silenciosos (vaga e
    update de empresa): sem ela, e sem assunto batendo, o evento é registrado
    mudo. Devolve ``{"from_name", "event_kind", "link_principal", "resumo",
    "announce"}``.
    """
    text = _flatten(body or "", _LINKEDIN_FLAT_LIMIT)
    subj = _flatten(subject or "", _LINKEDIN_RESUMO_LIMIT)
    flat = _flatten(f"{subj} {text}", _LINKEDIN_FLAT_LIMIT)
    if not flat.strip():
        return None
    kind, resumo = _kind_of(text, subj)
    if not kind:
        if not _LI_BRAND_RE.search(flat):
            return None
        kind = "notificacao"
        resumo = subj or _flatten(text, _LINKEDIN_RESUMO_LIMIT)
    who = _body_person(flat) or _header_person(item or {}) \
        or "Alguém no LinkedIn"
    announce = kind not in _LI_SILENT_KINDS
    if not announce:
        pattern = _flatten(watch or "", _WATCH_LIMIT)
        if pattern:
            try:
                announce = bool(re.search(pattern, subj, re.IGNORECASE))
            except re.error:
                announce = False
    return {"from_name": who, "event_kind": kind,
            "link_principal": _principal_link(flat),
            "resumo": _flatten(resumo or subj, _LINKEDIN_RESUMO_LIMIT),
            "announce": bool(announce)}


_LI_PHRASES = {
    "convite_conexao": "LinkedIn: {who} te mandou um convite de conexão.",
    "mensagem": "LinkedIn: {who} te mandou uma mensagem — quer ler?",
    "vaga": "LinkedIn: chegou uma vaga recomendada para você.",
    "notificacao_empresa": "LinkedIn: novidade de uma empresa que você segue.",
    "notificacao": "LinkedIn: {who} tem uma novidade para você. Quer ler?",
}


def linkedin_phrase(info: dict) -> str:
    """A frase do aviso — molde fixo por tipo de evento.

    O resumo lido do corpo NUNCA vai para a fala: frase montada com texto de
    terceiro embutiria link, telefone ou o recado inteiro no alto-falante.
    Só o nome de quem enviou entra, e mesmo assim passa pelo guarda de fala.
    """
    info = info if isinstance(info, dict) else {}
    kind = str(info.get("event_kind") or "")
    who = _flatten(info.get("from_name") or "Alguém no LinkedIn",
                   _LI_NAME_LIMIT)
    template = _LI_PHRASES.get(kind) or _LI_PHRASES["notificacao"]
    return _safe_spoken(template.replace("{who}", who))


def _safe_spoken(text: str) -> str:
    """Toda frase derivada de corpo de terceiro passa pelo guarda de fala."""
    raw = _flatten(text, _TEXT_LIMIT)
    if not raw:
        return ""
    try:
        from core import speech_guard as _guard
        return str(_guard.sanitize(raw).text or raw)[:_TEXT_LIMIT]
    except Exception:
        return raw


def _slug(value: object) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (text[:40] or "desconhecido")


def _write_note(who: str, message: str, link: str) -> None:
    """Mensagem privada guardada como texto: vault Jarvis, senão logs/."""
    import time as _time
    import datetime as _dt
    stamp = _dt.datetime.now().strftime("%Y-%m-%d")
    body_text = _flatten(message, _LINKEDIN_NOTE_LIMIT)
    vault = _LINKEDIN_VAULT
    if isinstance(vault, Path) and vault.is_dir():
        stem = f"{_VAULT_NOTE_PREFIX}{stamp}-linkedin-msg-{_slug(who)}"
        target = vault / f"{stem}.md"
        n = 2
        while target.exists() and n <= 20:
            target = vault / f"{stem}-{n}.md"
            n += 1
        text = (f"# Mensagem do LinkedIn — {who}\n\n"
                f"- chegada: {_time.strftime('%Y-%m-%d %H:%M')}\n"
                f"- origem: e-mail de notificação (fluxo UP-6)\n"
                f"- link: {link or 'não informado'}\n\n"
                f"{body_text}\n")
        try:
            target.write_text(text, encoding="utf-8")
            _log(f"mensagem do LinkedIn guardada em {target.name}")
        except Exception as e:
            _log(f"não consegui guardar a mensagem do LinkedIn: {e}")
        return
    _append_linkedin_log(f"NOTA {stamp} {who}: {body_text}", link)


def _append_linkedin_log(line: str, link: str = "") -> None:
    text = _flatten(line, _LINKEDIN_NOTE_LIMIT)
    try:
        path = Path(_LINKEDIN_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"[{_dt_now()}] {text}"
                         + (f" | link: {_flatten(link, 300)}" if link else "")
                         + "\n")
    except Exception as e:
        _log(f"o aviso do LinkedIn não foi registrado: {e}")


def _dt_now() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


# ── seleção de fluxo ─────────────────────────────────────────────────────────

def _when_hits(flow: dict, item: dict, body: str) -> bool:
    when = flow.get("when") or {}
    if not when:
        return True
    match = flow.get("match") or "contains"
    for key, pattern in when.items():
        if key == "body":
            haystacks = (body,)
        elif key == "from":
            haystacks = (str(item.get("from") or ""),
                         str(item.get("sender") or ""))
        else:
            haystacks = (str(item.get("subject") or ""),)
        hit = False
        for text in haystacks:
            if not text:
                continue
            if match == "regex":
                try:
                    hit = bool(re.search(pattern, text, re.IGNORECASE))
                except re.error:
                    hit = False
            else:
                hit = pattern.casefold() in text.casefold()
            if hit:
                break
        if not hit:
            return False
    return True


def select_flow(flows: list[dict], item: dict, body: str) -> dict | None:
    """O PRIMEIRO fluxo cujo ``when`` casa com cabeçalho + corpo (ordem = prioridade)."""
    for flow in flows or []:
        try:
            if _when_hits(flow, item, body):
                return flow
        except Exception:
            continue
    return None


def flow_from_rule(rule: dict | None) -> dict | None:
    """A regra de filtro pode FORÇAR o tipo de fluxo pelo campo ``tipo``.

    É assim que o LinkedIn entra em produção sem plugin: a regra do filtro
    diz ``dispatch: true`` + ``tipo: "linkedin_notification"``. Sem ``tipo``,
    vale a seleção normal dos fluxos. Tipo desconhecido devolve ``None`` com
    aviso no log — a gente não adivinha o que o dono quis executar.
    """
    rule = rule if isinstance(rule, dict) else {}
    tipo = str(rule.get("tipo") or "").strip().lower()
    if not tipo:
        return None
    if tipo not in FLOW_TYPES:
        _log(f"tipo de fluxo de e-mail desconhecido: {tipo[:40]}")
        return None
    name = _one_line(rule.get("label") or tipo, 40) or tipo
    return {"name": name, "type": tipo, "when": {},
            "allow": list(ACTION_ROUTES), "match": "contains",
            "watch": _one_line(rule.get("watch") or "", _WATCH_LIMIT)}


# ── roteamento para o executor correto ──────────────────────────────────────

def _default_executor(action: str, params: dict, item: dict,
                      acct: dict) -> bool:
    """Rota interna: lembrete/agendamento -> actions.reminder; tarefa -> actions.tasks.

    Só cria item (fila/agenda do dono). Não existe rota para envio de
    e-mail: corpo de terceiro nunca manda, e este módulo nunca confirma.
    """
    text = _one_line(params.get("text") or "", _TEXT_LIMIT)
    if not text:
        return False
    try:
        if action in ("lembrete", "agendamento"):
            from actions.reminder import reminder
            date = str(params.get("date") or "")
            time_ = str(params.get("time") or "")
            if not date or not time_:
                return False
            message = _one_line(f"[e-mail {acct.get('name') or '?'}] {text}",
                                _TEXT_LIMIT)
            out = str(reminder({"date": date, "time": time_,
                               "message": message}) or "")
            # Sucesso é a única frase que começa assim; recusa e erro
            # ("That time has already passed", "I couldn't ...") ficam fora.
            return out.lower().startswith("reminder set")
        if action == "tarefa":
            from actions.tasks import tasks
            out = str(tasks({"action": "add", "title": text,
                             "kind": "task",
                             "payload": {"origin": "email_flow",
                                         "account": str(
                                             acct.get("name") or "")[:40]}})
                     or "")
            try:
                return bool(json.loads(out).get("ok"))
            except Exception:
                return False
    except Exception as e:
        _log(f"executor do fluxo de e-mail falhou: {type(e).__name__}")
        return False
    return False


def _route(action: str, params: dict, item: dict, acct: dict) -> bool:
    handler = _executor or _default_executor
    try:
        return bool(handler(action, dict(params), dict(item), dict(acct)))
    except Exception as e:
        _log(f"executor do fluxo de e-mail falhou: {type(e).__name__}")
        return False


def _speak(payload: dict) -> bool | None:
    """True = anunciado; False = recusado (tentar de novo); None = sem gancho."""
    if _speak_hook is None:
        return None
    try:
        return bool(_speak_hook(dict(payload)))
    except Exception as e:
        _log(f"anúncio do fluxo de e-mail falhou: {type(e).__name__}")
        return False


def _notify(payload: dict) -> bool:
    """Aviso do LinkedIn: pelo gancho registrado, senão registrado em disco.

    Sem gancho (nenhum processo vivo escutando) nada é falado e nada é
    inventado — a linha vai para ``logs/linkedin-notifications.log``. O
    retorno False é transiente de fala: o recibo NÃO é gasto.
    """
    if _notify_hook is not None:
        try:
            return bool(_notify_hook(dict(payload)))
        except Exception as e:
            _log(f"o aviso do LinkedIn falhou: {type(e).__name__}")
            return False
    _append_linkedin_log(f"AVISO: {payload.get('text') or ''}")
    return True


# ── histórico de vistos: um recibo, uma execução ────────────────────────────

def _load_seen() -> dict[str, None]:
    global _seen
    with _lock:
        if _seen is not None:
            return _seen
    loaded: dict[str, None] = {}
    try:
        raw = json.loads(_seen_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for key in raw:
                loaded[str(key)] = None
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"não consegui ler o histórico de fluxos executados: {e}")
    with _lock:
        if _seen is None:
            _seen = loaded
        return _seen


def _save_seen(seen: dict[str, None]) -> None:
    keys = list(seen.keys())[-_SEEN_MAX:]
    try:
        _seen_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _seen_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_seen_path)
    except Exception as e:
        _log(f"não consegui gravar o histórico de fluxos executados: {e}")


def _mark_seen(key: str) -> None:
    with _lock:
        seen = _load_seen()
        if key in seen:
            return
        seen[key] = None
        snapshot = dict(seen)
    _save_seen(snapshot)


def _handle_code(body: str, item: dict, name: str) -> tuple[str, bool, str]:
    """Codigo de verificacao no corpo -> falado pelo gancho. (acao, ok, motivo)"""
    found = detect_code(body, subject=item.get("subject") or "",
                        sender=item.get("from") or item.get("sender") or "")
    if not found or not str(found.get("code") or ""):
        return ("nenhuma", True, "sem_codigo")
    spoken = _speak({"code": found["code"],
                     "app": found.get("app") or "",
                     "sender": found.get("sender") or "",
                     "subject": found.get("subject") or "",
                     "origin": "email",
                     "account": name})
    if spoken is None or not spoken:
        return ("codigo", False, "fala_indisponivel")
    return ("codigo", True, "anunciado")


def _handle_linkedin(body: str, item: dict, flow: dict,
                     name: str, acct: dict) -> tuple[str, bool, str]:
    """Notificacao do LinkedIn lida do corpo -> aviso (ou silencio). (acao, ok, motivo)

    Ordem de seguranca: primeiro o portao do remetente (so dominio
    linkedin.com), depois o codigo (a regra do LinkedIn NUNCA engole um
    codigo de verificacao), depois a marca de evento. Vaga e update de
    empresa sao silentos por default — só voltam a avisar se o assunto bater
    na regex ``watch``. Mensagem privada e guardada como texto (vault Jarvis,
    senao logs/) DEPOIS do aviso entregue: transiente de fala nao pode gerar
    nota duplicada na varredura seguinte. Nada aqui abre navegador nem roda
    OAuth.
    """
    if not is_linkedin_sender(item):
        return ("nenhuma", True, "remetente_invalido")
    action, ok, motivo = _handle_code(body, item, name)
    if action == "codigo":
        return (action, ok, motivo)
    info = parse_linkedin(body, subject=item.get("subject") or "",
                          watch=flow.get("watch") or "", item=item)
    if info is None:
        return ("nenhuma", True, "sem_notificacao")
    kind = str(info.get("event_kind") or "notificacao")
    if not info.get("announce"):
        return ("nenhuma", True, f"silenciado_{kind}")
    text = linkedin_phrase(info)
    if not text:
        return ("nenhuma", True, "sem_notificacao")
    warned = _notify({"text": text, "source": "linkedin", "event_kind": kind,
                      "from_name": info.get("from_name") or "",
                      "link": info.get("link_principal") or "",
                      "account": name, "uid": str(item.get("uid") or "")[:20],
                      "address": str(acct.get("address") or "")[:60]})
    if not warned:
        return ("aviso", False, "aviso_indisponivel")
    if kind == "mensagem":
        _write_note(info.get("from_name") or "", body,
                    info.get("link_principal") or "")
    return ("aviso", True, f"notificado_{kind}")


# ── dispatcher ───────────────────────────────────────────────────────────────

def dispatch_flow(acct: dict, item: dict, rule: dict | None,
                  body: str) -> dict | None:
    """Passos 5-6 do FLUXO EMAIL: corpo -> fluxo -> execução -> evento.

    Chama só para e-mail cuja regra de filtro tem ``dispatch: true`` (o
    portão é do ``core/email.py``). A regra pode ainda FORÇAR o tipo pelo
    campo ``tipo`` (é como o LinkedIn entra: ``tipo: linkedin_notification``).
    Devolve o evento ``email_flow_executed``
    (dict seguro: conta, uid, nome/tipo do fluxo, ação e motivo — nunca
    corpo, remetente, assunto ou o dígito do código) ou None quando nenhum
    fluxo casa / o recibo já foi executado. Nunca levanta: erro de fluxo é
    linha de log, a varredura segue.
    """
    acct = acct if isinstance(acct, dict) else {}
    item = item if isinstance(item, dict) else {}
    name = _one_line(acct.get("name") or "?", 40)
    uid = str(item.get("uid") or "")[:20]
    try:
        flows = load_flows()
        forced = flow_from_rule(rule)
        if (rule or {}).get("tipo") and forced is None:
            return None          # tipo pedido e inexistente: nada executado
        flow = forced or select_flow(flows, item, str(body or ""))
        if flow is None:
            return None
        key = (f"{str(acct.get('address') or name).casefold()}|{uid}"
               f"|{flow['name']}")
        if key in _load_seen():
            return None
        action = "nenhuma"
        ok = True
        motivo = ""
        if flow["type"] == "leitura_codigo":
            action, ok, motivo = _handle_code(body, item, name)
        elif flow["type"] == "aviso_simples":
            action = "aviso"
            motivo = "aviso_simples"
        elif flow["type"] == "linkedin_notification":
            action, ok, motivo = _handle_linkedin(body, item, flow, name, acct)
        else:  # acao_automatica
            findings = parse_body(body)
            picked = next((f for f in findings
                           if f["action"] in (flow.get("allow") or [])), None)
            if picked is None:
                motivo = "sem_instrucao"
            elif picked["action"] in ("lembrete", "agendamento") \
                    and not (picked["date"] and picked["time"]):
                # Validacao dura: sem data E hora explicitas no texto do
                # proprio dono-em-confianca, nada é criado.
                ok = False
                motivo = "sem_data_hora"
            else:
                action = picked["action"]
                ok = _route(action, {"text": picked["text"],
                                     "date": picked["date"],
                                     "time": picked["time"]}, item, acct)
                motivo = "executado" if ok else "executor_recusou"
        event = {"account": name, "uid": uid, "flow": flow["name"],
                 "type": flow["type"], "action": action, "ok": ok,
                 "motivo": motivo,
                 "rule_action": str((rule or {}).get("action") or "")}
        # Transiente de fala (sessão não pronta) NÃO gasta o recibo: a
        # varredura seguinte tenta de novo. Todo resto é decisão tomada.
        if ok or motivo not in _RETRY_MOTIVOS:
            _mark_seen(key)
        _log(f"fluxo {name}: uid={uid or '?'} fluxo={flow['name']} "
             f"tipo={flow['type']} acao={action} ok={'sim' if ok else 'nao'} "
             f"motivo={motivo}")
        if callable(_event_hook):
            try:
                _event_hook(dict(event))
            except Exception as e:
                _log(f"o evento do fluxo de e-mail não foi entregue: "
                     f"{type(e).__name__}")
        return event
    except Exception as e:
        _log(f"dispatch de fluxo de e-mail falhou: {type(e).__name__}")
        return None
