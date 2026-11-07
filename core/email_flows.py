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
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_DEFAULT_SEEN_PATH = _BASE / "memory" / "email_flow_seen.json"

FLOW_TYPES = ("leitura_codigo", "aviso_simples", "acao_automatica")
ACTION_ROUTES = ("lembrete", "tarefa", "agendamento")
_WHEN_FIELDS = ("from", "subject", "body")
_MAX_FLOWS = 12
_MAX_FINDINGS = 8
_TEXT_LIMIT = 200
_SEEN_MAX = 2000
_RETRY_MOTIVOS = ("fala_indisponivel",)

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_lock = threading.RLock()
_seen: dict[str, None] | None = None
_seen_path = _DEFAULT_SEEN_PATH
_event_hook = None          # fn(payload: dict) -> None (activity.log)
_speak_hook = None          # fn(payload: dict) -> bool  (leitura_codigo)
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


def reset() -> None:
    """Esquece hooks, cache de vistos e caminhos (testes e ``mail.reset``)."""
    global _seen, _seen_path, _event_hook, _speak_hook, _executor
    with _lock:
        _seen = None
        _seen_path = _DEFAULT_SEEN_PATH
    _event_hook = None
    _speak_hook = None
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
    para ``acao_automatica`` (ausente = os três rotas).
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
            "match": match}


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
    """Fluxos do dono (``plugin_config.mail.flows``) ou o padrão de código.

    Sem fluxo configurado vale ``DEFAULT_FLOWS``: um ``leitura_codigo`` que
    casa com qualquer e-mail — o disparo continua sob o portão da regra
    ``dispatch: true`` do filtro, então nada roda por conta própria.
    """
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config("mail") or {}
        entries = cfg.get("flows")
    except Exception:
        entries = None
    flows = clean_flows(entries)
    return flows if flows else list(DEFAULT_FLOWS)


DEFAULT_FLOWS: list[dict] = [
    {"name": "leitura_codigo", "type": "leitura_codigo",
     "when": {}, "allow": [], "match": "contains"},
]


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


# ── dispatcher ───────────────────────────────────────────────────────────────

def dispatch_flow(acct: dict, item: dict, rule: dict | None,
                  body: str) -> dict | None:
    """Passos 5-6 do FLUXO EMAIL: corpo -> fluxo -> execução -> evento.

    Chama só para e-mail cuja regra de filtro tem ``dispatch: true`` (o
    portão é do ``core/email.py``). Devolve o evento ``email_flow_executed``
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
        flow = select_flow(flows, item, str(body or ""))
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
            found = detect_code(body, subject=item.get("subject") or "",
                                sender=item.get("from") or
                                item.get("sender") or "")
            if not found or not str(found.get("code") or ""):
                motivo = "sem_codigo"
            else:
                action = "codigo"
                spoken = _speak({"code": found["code"],
                                 "app": found.get("app") or "",
                                 "sender": found.get("sender") or "",
                                 "subject": found.get("subject") or "",
                                 "origin": "email",
                                 "account": name})
                if spoken is None:
                    ok = False
                    motivo = "fala_indisponivel"
                elif spoken:
                    motivo = "anunciado"
                else:
                    ok = False
                    motivo = "fala_indisponivel"
        elif flow["type"] == "aviso_simples":
            action = "aviso"
            motivo = "aviso_simples"
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
