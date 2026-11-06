"""Recibos criptograficos HMAC para tool calls.

Cada tool call executado pelo bridge gera um recibo assinado com HMAC-SHA256.
O recibo prova que o resultado foi produzido pelo proprio processo — o modelo
nao tem acesso a chave, portanto nao pode forjar recibos.

Estrutura do recibo (uma linha JSONL no audit):
  call_id      — UUID v4 unico por call
  tool         — nome da acao executada
  args_hash    — "sha256:<hex>" dos argumentos serializados
  result_hash  — "sha256:<hex>" do resultado serializado
  timestamp    — ISO-8601 UTC
  hmac         — "sha256:<mac>" HMAC-SHA256(call_id + args_hash + result_hash
                 + timestamp) com a chave secreta

A chave eh lida de config/certs/tool_hmac.key (modo 0600). Quando a variavel
de ambiente JARVIS_RECEIPT_HMAC_KEY esta presente, ela tem precedencia. A
chave nunca eh logada — apenas o HMAC resultante vai ao disco.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent / "config" / "certs" / "tool_hmac.key"
_ENV_KEY = "JARVIS_RECEIPT_HMAC_KEY"


def load_key(key_path: Optional[Path] = None) -> bytes:
    """Retorna a chave HMAC em bytes.

    Prioridade:
      1. Variavel de ambiente JARVIS_RECEIPT_HMAC_KEY
      2. Arquivo config/certs/tool_hmac.key
    """
    env = os.environ.get(_ENV_KEY, "").strip()
    if env:
        return env.encode("utf-8")

    path = key_path or _DEFAULT_KEY_PATH
    try:
        raw = path.read_bytes().strip()
        if raw:
            return raw
    except FileNotFoundError:
        pass

    raise FileNotFoundError(
        f"Chave HMAC nao encontrada em {path} nem em {_ENV_KEY}"
    )


def hash_sha256(value: str) -> str:
    """SHA-256 hex de uma string."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def hash_args(args: dict) -> str:
    """SHA-256 dos argumentos serializados de forma deterministica."""
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        blob = str(args)
    return f"sha256:{hash_sha256(blob)}"


def hash_result(result: Any) -> str:
    """SHA-256 do resultado serializado."""
    if isinstance(result, str):
        blob = result
    else:
        try:
            blob = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            blob = str(result)
    return f"sha256:{hash_sha256(blob)}"


def compute_hmac(key: bytes, call_id: str, args_hash: str,
                 result_hash: str, timestamp: str) -> str:
    """HMAC-SHA256 sobre a concatenacao dos campos do recibo."""
    message = f"{call_id}{args_hash}{result_hash}{timestamp}"
    mac = _hmac_mod.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256:{mac}"


def make_receipt(tool: str, args: dict, result: Any, *,
                 key: Optional[bytes] = None,
                 key_path: Optional[Path] = None) -> dict:
    """Gera um recibo completo para um tool call.

    Parametros
    ----------
    tool : str
        Nome da acao executada.
    args : dict
        Argumentos passados ao tool (serao hasheados, nunca logados crus).
    result : Any
        Resultado retornado pelo tool.
    key : bytes, opcional
        Chave HMAC. Quando omitida, resolve automaticamente.
    key_path : Path, opcional
        Caminho do arquivo de chave (padrao: config/certs/tool_hmac.key).
    """
    if key is None:
        key = load_key(key_path)

    call_id = str(uuid.uuid4())
    args_h = hash_args(args if isinstance(args, dict) else {})
    result_h = hash_result(result)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mac = compute_hmac(key, call_id, args_h, result_h, ts)

    return {
        "call_id": call_id,
        "tool": tool,
        "args_hash": args_h,
        "result_hash": result_h,
        "timestamp": ts,
        "hmac": mac,
    }


def verify_receipt(receipt: dict, key: bytes) -> bool:
    """Verifica se o HMAC do recibo confere com a chave.

    Retorna True quando o recibo eh integro e foi assinado com a mesma chave.
    """
    try:
        expected = compute_hmac(
            key,
            receipt["call_id"],
            receipt["args_hash"],
            receipt["result_hash"],
            receipt["timestamp"],
        )
        return _hmac_mod.compare_digest(expected, receipt["hmac"])
    except (KeyError, TypeError):
        return False
