"""Códigos de verificação (OTP) em texto de e-mail ou WhatsApp.

O DETECTOR
    Um código só conta quando está PERTO de uma palavra que pede um código:
    "código", "code", "verification", "verificação", "OTP", "login", "senha",
    "token", "PIN"… A janela é curta de propósito (MAX_GAP): um número solto no
    meio de um e-mail de pedido não é um OTP, e o inverso — "código do pedido
    12345" — é descartado pela lista de contexto negativo (rastreio, boleto,
    CPF, cupom…).

    Aceita 4 a 8 dígitos, com ou sem separador ("482 913", "123-456"), e
    alfanumérico de 4 a 8 com pelo menos uma letra e um dígito ("A1B2C3",
    "G-483920").

O REMETENTE
    `app` é o site/aplicativo quando ele aparece no assunto ou no texto
    ("código de verificação do WhatsApp" → WhatsApp; "para entrar no
    Instagram" → Instagram) e, no e-mail, cai para o nome de exibição ou o
    domínio do remetente (no-reply@accounts.google.com → Google). Um número de
    WhatsApp de pessoa física não vira "app": sem @, o remetente é só o nome
    de quem mandou.
"""
from __future__ import annotations

import re
import unicodedata

MIN_CODE = 4
MAX_CODE = 8
MAX_GAP = 90           # distância máxima entre o código e a palavra-chave
NEGATIVE_WINDOW = 36   # contexto que transforma "código" em código de pedido

_KEYWORD_RE = re.compile(
    r"\b(?:"
    r"c[óo]digo|codigo|code|otp|one[\s-]?time(?:\s+(?:pass(?:word|code)?|code))?|"
    r"verification|verifica[cç][aã]o|verificar|"
    r"confirma[cç][aã]o|confirmation|"
    r"login|log[\s-]?in|sign[\s-]?in|signin|"
    r"autentica[cç][aã]o|authenticat(?:ion|e)|"
    r"passcode|password|senha|token|pin"
    r")\b",
    re.IGNORECASE)

# Código de pedido, rastreio, boleto, CPF…: perto do número, invalida o achado.
_NEGATIVE_RE = re.compile(
    r"(?:\b(?:rastre(?:io|amento|ar)|tracking|pedido|"
    r"order\s*(?:number|no\.?|id)|protocolo|barras|barcode|c[ée]p|cpf|cnpj|"
    r"cupom|coupon|promo[cç][aã]o|desconto|boleto|fatura|invoice|valor|"
    r"pre[cç]o|price|telefone|phone|celular|copyright|"
    r"direitos\s+reservados)\b)|©",
    re.IGNORECASE)

_TOKEN_RE = re.compile(
    r"(?<![\w])(?:"
    r"\d{3,4}[ -]\d{3,4}"                    # 482 913 / 123-456 / 1234-5678
    r"|[A-Za-z0-9]{1,4}-[A-Za-z0-9]{2,7}"    # G-483920 / AB12-CD34
    r"|[A-Za-z0-9]{4,8}"                     # 123456 / A1B2C3
    r")(?![\w])")

# Preposição + nome próprio: "do WhatsApp", "no Instagram", "da Amazon".
_APP_AFTER_PREP_RE = re.compile(
    r"\b(?:do|da|de|dos|das|from|no|na|pelo|pela|for|on)\s+"
    r"(?:(?:aplicativo|app|site|sistema|servi[cç]o|portal)"
    r"(?:\s+(?:do|da|de))?\s+)?"
    r"([A-Z][\w&+.'-]*(?:\s+[A-Z][\w&+.'-]*){0,2})")

# "Google verification code", "WhatsApp: seu código…".
_APP_BEFORE_KEYWORD_RE = re.compile(
    r"\b([A-Z][\w&+.'-]*(?:\s+[A-Z][\w&+.'-]*){0,2})\s*"
    r"(?:[-–—:|]\s*)?"
    r"(?i:c[óo]digo|codigo|code|verification|verifica[cç][aã]o|otp|"
    r"login|sign[\s-]?in)\b")

# "conta Google", "site do Banco Inter".
_APP_AFTER_NOUN_RE = re.compile(
    r"\b(?:conta|app|aplicativo|site|portal|servi[cç]o)\s+"
    r"(?:d[oa]s?\s+)?"
    r"([A-Z][\w&+.'-]*(?:\s+[A-Z][\w&+.'-]*){0,2})")

_DISPLAY_RE = re.compile(r'^\s*"?([^"<]{1,80}?)"?\s*<[^>]+>\s*$')
_DOMAIN_NAMES = {
    "whatsapp": "WhatsApp", "github": "GitHub", "icloud": "iCloud",
    "mercadolivre": "Mercado Livre", "mercadopago": "Mercado Pago",
    "linkedin": "LinkedIn", "youtube": "YouTube", "paypal": "PayPal",
}

_STOPWORDS = {
    "seu", "sua", "seus", "suas", "your", "the", "this", "these", "este",
    "esta", "esse", "essa", "codigo", "code", "verificacao", "verification",
    "seguranca", "security", "conta", "account", "login", "acesso", "access",
    "password", "senha", "one", "time", "otp", "token", "pin", "passcode",
    "app", "aplicativo", "site", "sistema", "servico", "service", "portal",
    "equipe", "team", "suporte", "support", "nao", "no", "not", "responda",
    "responder", "email", "e-mail", "mensagem", "message", "confirmation",
    "confirmacao", "autenticacao", "authentication", "uso", "unico", "new",
    "novo", "nova", "acesso", "acessar", "entrar", "from", "para", "pela",
    "pelo", "dos", "das", "assunto", "subject", "fwd", "fw", "enc", "re", "res",
}

_AUTOMATED_SENDERS = {
    "no-reply", "noreply", "nao-responda", "naoresponda", "no_reply",
    "donotreply", "do-not-reply", "mailer-daemon",
}


def _fold(text: object) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in value if not unicodedata.combining(c)).casefold()


def _clean_code(token: str) -> str:
    """O token é um código plausível? Devolve o código, ou "" se não for."""
    raw = str(token or "").strip()
    compact = re.sub(r"[\s-]+", "", raw)
    if not (MIN_CODE <= len(compact) <= MAX_CODE):
        return ""
    if not any(ch.isdigit() for ch in compact):
        return ""
    has_letter = any(ch.isalpha() for ch in compact)
    if not has_letter and "-" in raw:
        # "2026-10" parece data, não código; 3-3/4-4 já vieram pela 1ª regra.
        sides = raw.split("-")
        if len(sides) != 2 or not all(MIN_CODE - 1 <= len(s) <= 4 for s in sides):
            return ""
    return compact


def _gap(candidate: dict, keyword: tuple[int, int]) -> int:
    if candidate["end"] <= keyword[0]:
        return keyword[0] - candidate["end"]
    if keyword[1] <= candidate["start"]:
        return candidate["start"] - keyword[1]
    return 0


def _find_code(text: str) -> dict | None:
    keywords = [(m.start(), m.end()) for m in _KEYWORD_RE.finditer(text)]
    if not keywords:
        return None

    raw = []
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0)
        code = _clean_code(token)
        if not code or _KEYWORD_RE.search(token):
            continue
        raw.append({"code": code, "start": m.start(), "end": m.end(),
                    "token": token})
    if not raw:
        return None

    # "1234 5678" casa inteiro e também em pedaços: fica o maior.
    raw.sort(key=lambda c: (c["start"], -(c["end"] - c["start"])))
    candidates = []
    for cand in raw:
        if any(cand["start"] >= kept["start"] and cand["end"] <= kept["end"]
               for kept in candidates):
            continue
        candidates.append(cand)

    best = None
    for cand in candidates:
        keyword = min(keywords, key=lambda k: _gap(cand, k))
        distance = _gap(cand, keyword)
        if distance > MAX_GAP:
            continue
        window = text[max(0, cand["start"] - NEGATIVE_WINDOW):
                      min(len(text), cand["end"] + NEGATIVE_WINDOW)]
        if _NEGATIVE_RE.search(window):
            continue
        score = -distance
        if cand["start"] >= keyword[1]:
            score += 12                      # o padrão comum: "código é 123456"
        if cand["code"].isdigit():
            score += 4
        if best is None or score > best[0]:
            best = (score, cand)

    if best is None:
        return None
    cand = best[1]
    return {
        "code": cand["code"],
        "start": cand["start"],
        "end": cand["end"],
        "matched": text[max(0, cand["start"] - 40):
                        min(len(text), cand["end"] + 40)].strip(),
    }


def _tidy(name: str) -> str:
    value = re.sub(r"\s+", " ", str(name or "")).strip()
    return value.strip(" \"'“”‘’.,;:!?()[]{}<>-–—")


def _trim_stop(name: str) -> str:
    words = _tidy(name).split()
    while words and _fold(words[0]) in _STOPWORDS:
        words.pop(0)
    while words and _fold(words[-1]) in _STOPWORDS:
        words.pop()
    return " ".join(words)


def _app_from_sender(sender: object) -> str:
    value = str(sender or "").strip()
    display = _DISPLAY_RE.match(value)
    if display:
        name = _tidy(display.group(1))
        if name and "@" not in name and _fold(name) not in _AUTOMATED_SENDERS:
            return name
    if "@" not in value:
        return ""                            # pessoa no WhatsApp, não um app
    domain = value.rsplit("@", 1)[1].strip("<> ")
    labels = [part for part in domain.lower().split(".") if part]
    if len(labels) < 2:
        return ""
    brand = labels[-2]
    if brand in ("com", "net", "org", "co", "gov", "edu", "mail", "email"):
        return ""
    if brand in _DOMAIN_NAMES:
        return _DOMAIN_NAMES[brand]
    return brand[:1].upper() + brand[1:]


def _extract_app(subject: object, text: object, sender: object) -> str:
    for source in (subject, text):
        value = str(source or "")
        if not value:
            continue
        for rx in (_APP_BEFORE_KEYWORD_RE, _APP_AFTER_PREP_RE,
                   _APP_AFTER_NOUN_RE):
            for match in rx.finditer(value):
                name = _trim_stop(match.group(1))
                if name:
                    return name
    return _app_from_sender(sender)


def detect(text: object, *, subject: object = "",
           sender: object = "") -> dict | None:
    """Um OTP no texto (e assunto do e-mail), ou None.

    Devolve `{"code", "app", "sender", "subject", "matched"}`. Não guarda nem
    imprime o texto original: quem chama decide se e como anunciar.
    """
    body = str(text or "")
    subj = str(subject or "")
    combined = (subj + "\n" + body).strip() if subj else body
    if not combined:
        return None
    found = _find_code(combined)
    if found is None:
        return None
    return {
        "code": found["code"],
        "app": _extract_app(subj, body, sender),
        "sender": str(sender or "").strip(),
        "subject": subj.strip(),
        "matched": found["matched"],
    }
