"""Recibos criptograficos HMAC para tool calls.

Cada tool call executado pelo bridge gera um recibo assinado com HMAC-SHA256.
O recibo prova que o resultado foi produzido pelo proprio processo — o modelo
nao tem acesso a chave, portanto nao pode forjar recibos.

Estrutura do recibo (uma linha JSONL):
  call_id      — UUID unico por call
  tool         — nome da acao executada
  args_hash    — SHA-256 dos argumentos serializados
  result_hash  — SHA-256 do resultado serializado
  timestamp    — ISO-8601 UTC
  nonce        — bytes aleatorios (hex) para garantir unicidade mesmo com
                 chamada e resultado identicos
  hmac         — HMAC-SHA256(call_id + tool + args_hash + result_hash +
                 timestamp + nonce) com a chave secreta

A chave vem da variavel de ambiente JARVIS_RECEIPT_HMAC_KEY. Quando ausente,
um valor padrao e gerado e gravado em logs/.receipt_key (modo 0600). A chave
nunca e logada — apenas o HMAC resultante.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ENV_KEY = "JARVIS_RECEIPT_HMAC_KEY"
_DEFAULT_KEY_FILE = ".receipt_key"
_HMAC_DIGEST_SIZE = 32  # SHA-256


def _resolve_key(receipts_path: Optional[Path] = None) -> bytes:
    """Retorna a chave HMAC.

    Prioridade:
      1. Variavel de ambiente JARVIS_RECEIPT_HMAC_KEY
      2. Arquivo .receipt_key ao lado do arquivo de recibos (ou cwd)
      3. Gera uma chave nova e grava no arquivo
    """
    env = os.environ.get(_ENV_KEY, "").strip()
    if env:
        return env.encode("utf-8")

    if receipts_path is not None:
        key_file = receipts_path.parent / _DEFAULT_KEY_FILE
    else:
        key_file = Path.cwd() / _DEFAULT_KEY_FILE

    try:
        raw = key_file.read_bytes()
        if raw.strip():
            return raw.strip()
    except FileNotFoundError:
        pass

    key = secrets.token_hex(32).encode("utf-8")
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        try:
            os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
    except OSError:
        pass  # melhor ter uma chave efemera que nenhum recibo
    return key


def generate_nonce() -> str:
    """Nonce hex de 16 bytes (128 bits), unico por call."""
    return secrets.token_hex(16)


def hash_args(args: dict) -> str:
    """SHA-256 dos argumentos serializados de forma deterministica."""
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        blob = str(args)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def hash_result(result: Any) -> str:
    """SHA-256 do resultado serializado."""
    if isinstance(result, str):
        blob = result
    else:
        try:
            blob = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            blob = str(result)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def compute_hmac(key: bytes, call_id: str, tool: str, args_hash: str,
                 result_hash: str, timestamp: str, nonce: str) -> str:
    """HMAC-SHA256 sobre a concatenacao dos campos do recibo."""
    message = f"{call_id}|{tool}|{args_hash}|{result_hash}|{timestamp}|{nonce}"
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def make_receipt(tool: str, args: dict, result: Any, *,
                 key: Optional[bytes] = None,
                 receipts_path: Optional[Path] = None) -> dict:
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
    receipts_path : Path, opcional
        Caminho do arquivo JSONL de recibos — usado para localizar a chave
        padrao ao lado dele.
    """
    if key is None:
        key = _resolve_key(receipts_path)

    call_id = str(uuid.uuid4())
    nonce = generate_nonce()
    args_h = hash_args(args)
    result_h = hash_result(result)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mac = compute_hmac(key, call_id, tool, args_h, result_h, ts, nonce)

    return {
        "call_id": call_id,
        "tool": tool,
        "args_hash": args_h,
        "result_hash": result_h,
        "timestamp": ts,
        "nonce": nonce,
        "hmac": mac,
    }


def verify_receipt(receipt: dict, key: bytes) -> bool:
    """Verifica se o HMAC do recibo confere com a chave.

    Retorna True quando o recibo e integro e foi assinado com a mesma chave.
    """
    try:
        expected = compute_hmac(
            key,
            receipt["call_id"],
            receipt["tool"],
            receipt["args_hash"],
            receipt["result_hash"],
            receipt["timestamp"],
            receipt["nonce"],
        )
        return hmac.compare_digest(expected, receipt["hmac"])
    except (KeyError, TypeError):
        return False


def append_receipt(receipt: dict, path: Path) -> None:
    """Acrescenta uma linha JSONL ao arquivo de recibos.

    Falhas de I/O sao silenciadas — recibos nao podem quebrar um tool call.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(receipt, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
