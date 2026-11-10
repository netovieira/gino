"""Owner password for typed authorization (UI and WhatsApp).

The voiceprint remains the primary factor. Since the owner's decision of
06/11/2026 the typed password is not a narrow fallback: it is a FULL
authorization factor, equal to his voice, in every privileged class
(actions, sends, job replies, sudo, restart, power and e-mail). The
password is never stored in plaintext and never logged.

Storage is ``plugin_config.owner_auth.password_hash`` in
``config/api_keys.json`` (gitignored). The UI form writes the plaintext once;
``memory.config_manager.save_plugin_config`` hashes it before persisting.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

PREFIX = "pbkdf2_sha256"
REDACTED = "[senha oculta]"

# ── Veredito compartilhado de fatores ───────────────────────────────────────
# A lei do dono (decisão por voz, 06/11/2026): "eu posso liberar QUALQUER
# coisa com a senha ou com a minha voz". O fator aceito para autorização
# privilegiada é a VOZ DO DONO (voiceprint) OU A SENHA DIGITADA CORRETA
# (texto no app ou no WhatsApp direto dele — não existe campo próprio de
# senha). A senha é fator PLENO em TODAS as classes privilegiadas: ação,
# envio, job, sudo/root, reinício, energia e e-mail. NÃO existe lista de
# classes em que a senha é banida — a lista antiga ("reinício, energia e
# sudo só com voz") foi invenção do agente, corrigida por ordem do dono.
# Todo portão que distingue fatores cita estas constantes e o veredito
# ``authorized_factor`` abaixo: actiongate (janelas de ação/envio),
# job_grant (resposta a planos), rootgate (sudo) e as confirmações de
# reinício/energia/e-mail em main._owner_password_grant.
FACTOR_VOICE = "voz do dono"
FACTOR_TEXT = "senha digitada"
FACTORS = (FACTOR_VOICE, FACTOR_TEXT)


# ── As DUAS portas nomeadas em toda recusa (decisão 14, dono, 10/11/2026) ───
# "A confirmação por voz minha e por senha confirma e permite qualquer coisa
# que você imaginar ... ela é como é a senha master e a minha voz é a voz do
# criador." Consequência prática: TODO texto de recusa de portão que exige a
# confirmação do dono nomeia as duas portas, no mesmo pé, em qualquer canal —
# envio, e-mail, reinício/energia, suspensão, publicação. Um portão que cita
# só a voz mente sobre a lei dele.
# A frase nomeia os FATORES (as constantes acima), nunca o valor do segredo;
# por isso é segura para ir ao log, à fala do modelo e ao corpo 403.
def doors(lang: str = "pt") -> str:
    """A frase curta das duas portas do dono. Nunca contém o segredo."""
    if str(lang or "pt").lower().startswith("en"):
        return (f"{FACTOR_VOICE} (say 'pode' out loud — there is no popup to "
                f"click) or {FACTOR_TEXT} (typed in the app or in his own "
                "WhatsApp)")
    return (f"{FACTOR_VOICE} (ele diz 'pode' em voz alta) ou "
            f"{FACTOR_TEXT} (ele digita no app ou no WhatsApp dele)")


# Accepted typed forms: "senha: X", "senha=X", "autorizar: X", "/autorizar X".
PASSWORD_RE = re.compile(
    r"^\s*(?:(?:/|\.)autorizar\s+|(?:autorizar|senha)\s*[:=]\s*)"
    r"(\S{1,128})\s*$",
    re.IGNORECASE)


def password_from_command(text: str) -> str | None:
    """The password in a typed command, or None when this is normal text."""
    match = PASSWORD_RE.match(str(text or ""))
    return match.group(1).strip() if match else None


def is_password_command(text: str) -> bool:
    return password_from_command(text) is not None


def redact_command(text: str) -> str:
    """Never persist/log the password; keep the fact that it was typed."""
    return REDACTED if is_password_command(text) else str(text or "")
ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Return an encoded PBKDF2 hash; the plaintext is discarded here."""
    raw = str(password or "")
    if not raw:
        raise ValueError("senha vazia")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt,
                                 ITERATIONS)
    return f"{PREFIX}${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a typed password against the stored hash."""
    raw = str(password or "")
    stored = str(encoded or "")
    if not raw or not stored:
        return False
    try:
        prefix, iterations, salt_hex, digest_hex = stored.split("$", 3)
        if prefix != PREFIX:
            return False
        rounds = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    if not (1 <= rounds <= 5_000_000) or not salt or not expected:
        return False
    try:
        actual = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt,
                                     rounds)
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


def configured_hash() -> str:
    """The stored hash, or '' when no password is configured."""
    try:
        from memory.config_manager import get_plugin_config
        return str(get_plugin_config("owner_auth").get("password_hash") or "")
    except Exception:
        return ""


def is_configured() -> bool:
    return bool(configured_hash())


def verify(password: str) -> bool:
    """Verify a typed password against the configured hash."""
    return verify_password(password, configured_hash())


def authorized_factor(password: str = "", voice_ok: bool = False) -> str:
    """O ÚNICO ponto do veredito "o dono autorizou" (decisão 06/11/2026).

    Devolve o fator que autorizou — FACTOR_VOICE quando o voiceprint confirmou
    a fala como sendo do dono, FACTOR_TEXT quando a senha digitada está
    correta — ou "" (nada). O mesmo veredito vale para TODAS as classes
    privilegiadas (ação, envio, job, sudo, reinício, energia, e-mail): não
    existe lista de classes em que a senha seja banida.

    Invariantes: a senha é lida aqui apenas para verificação e nunca fica,
    é logada ou retorna; texto digitado sem senha, senha errada, voz
    desconhecida, persona, subagente e o modelo jamais obtêm fator.
    """
    if voice_ok:
        return FACTOR_VOICE
    if password and verify(password):
        return FACTOR_TEXT
    return ""
