"""Owner password for typed authorization (UI and WhatsApp).

The voiceprint remains the primary factor. This module is the fallback the
owner asked for: when the microphone cannot recognize him, a typed password in
the UI or in the owner's WhatsApp chat opens the same short authorization
windows. The password is never stored in plaintext and never logged.

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
# A lei do dono: o fator aceito para autorização privilegiada é a VOZ DO DONO
# OU A SENHA DIGITADA (texto no app ou no WhatsApp dele — não existe campo
# próprio de senha). Todo portão que distingue fatores cita estas constantes:
# actiongate (janelas de ação/envio), job_grant (resposta a planos) e, por
# exclusão, rootgate/reinício/energia, que só aceitam FACTOR_VOICE.
FACTOR_VOICE = "voz do dono"
FACTOR_TEXT = "senha digitada"
FACTORS = (FACTOR_VOICE, FACTOR_TEXT)
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
