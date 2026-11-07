"""LinkedIn: integracao com a API oficial via OAuth 2.0.

O caminho escolhido foi a API REST oficial do LinkedIn (v2), nao scraping nem
automacao de navegador. A API oficial eh estavel, documentada e nao quebra com
mudancas de layout. O preco eh o escopo limitado de acoes — postar texto,
comentar e buscar perfis —, que eh exatamente o que o assistente precisa.

AUTENTICACAO (OAuth 2.0 Authorization Code Flow):
    1. O modulo gera uma URL de autorizacao com client_id + escopos.
    2. O dono abre no navegador, autoriza, e o LinkedIn redireciona com um code.
    3. O code eh trocado por um access_token + refresh_token.
    4. O access_token eh guardado em config/linkedin.json (modo 0600).
    5. Quando expira, o refresh_token renova sem intervencao do dono.

ACOES SUPORTADAS:
    - post(text, visibility="anyone"): publica um post no feed do dono.
    - comment(post_id, text): comenta num post existente.
    - get_profile(): retorna nome, headline e foto do dono autenticado.
    - search_people(query, limit=5): busca pessoas por nome/keyword.

PRIVACIDADE (regra dura):
    - As credenciais (client_secret, tokens) NUNCA sao logadas.
    - O config/linkedin.json tem permissao 0600 (so o dono le).
    - Posts e comentarios sao acoes IRREVERSIVEIS — o plugin pede confirmacao.

RECIBOS HMAC:
    - Cada acao gera um recibo via core.tool_receipts.make_receipt().
    - O recibo prova que a acao foi executada pelo proprio processo.

    from core import linkedin
    linkedin.configure(client_id="...", client_secret="...")
    url = linkedin.get_authorization_url()   # dono abre no navegador
    linkedin.handle_callback(code="...")     # troca code por tokens
    linkedin.post("Olá rede!")              # publica
    linkedin.get_profile()                  # dados do dono
"""
# LinkedIn nao tem plugin proprio. Notificacoes chegam por core/email_flows.py
# tipo=linkedin_notification. OAuth mantido inerte para futuro uso se decisao
# do dono mudar.
#
# Nada aqui foi desativado nem deletado: o fluxo OAuth 2.0 (configure,
# get_authorization_url, handle_callback, refresh_access_token) e as acoes de
# API continuam inteiros, prontos para uma futura ativacao se o dono mudar de
# rota. O que muda e a decisao de caminho (07/11/2026): o monitoramento de
# LinkedIn e UMA REGRA do fluxo de leitura de e-mail — o filtro do
# core/email.py casa o remetente @linkedin.com com ``dispatch: true`` e
# ``tipo: linkedin_notification``, e core/email_flows.py decide o que avisar.
# Nada novo chama este modulo.
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from core import tool_receipts as _receipts

# ── caminhos e constantes ────────────────────────────────────────────────────

_BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _BASE_DIR / "config" / "linkedin.json"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
_PROFILE_URL = "https://api.linkedin.com/v2/me"
_POST_URL = "https://api.linkedin.com/v2/ugcPosts"
_COMMENT_URL = "https://api.linkedin.com/v2/socialActions/{urn}/comments"
_SEARCH_URL = "https://api.linkedin.com/v2/people?q=search&keywords={query}&count={limit}"

_SCOPES = ["r_liteprofile", "r_emailaddress", "w_organization_social", "r_organization_social"]
_REDIRECT_URI = "http://localhost:8012/linkedin/callback"

# ── estado em memoria ────────────────────────────────────────────────────────

_config: dict[str, Any] = {}
_tokens: dict[str, Any] = {}
_hmac_key: Optional[bytes] = None


def _load_hmac_key() -> Optional[bytes]:
    """Carrega a chave HMAC uma vez (lazy)."""
    global _hmac_key
    if _hmac_key is not None:
        return _hmac_key
    try:
        _hmac_key = _receipts.load_key()
    except Exception:
        _hmac_key = None
    return _hmac_key


def _issue_receipt(action: str, args: dict, result: Any) -> Optional[dict]:
    """Gera recibo HMAC para a acao. Falhas sao silenciadas."""
    key = _load_hmac_key()
    if key is None:
        return None
    try:
        return _receipts.make_receipt(action, args, result, key=key)
    except Exception:
        return None


# ── persistencia de config ───────────────────────────────────────────────────

def _load_config() -> dict[str, Any]:
    """Le config/linkedin.json. Retorna dict vazio se nao existir."""
    global _config
    if _config:
        return _config
    try:
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
        _config = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        _config = {}
    return _config


def _save_config(data: dict[str, Any]) -> None:
    """Persiste config/linkedin.json com permissao 0600."""
    global _config
    _config = dict(data)
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = _CONFIG_PATH.with_name(_CONFIG_PATH.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, _CONFIG_PATH)


def _load_tokens() -> dict[str, Any]:
    """Le tokens do config."""
    global _tokens
    cfg = _load_config()
    _tokens = cfg.get("tokens", {})
    return _tokens


def _save_tokens(tokens: dict[str, Any]) -> None:
    """Persiste tokens no config."""
    global _tokens
    _tokens = dict(tokens)
    cfg = _load_config()
    cfg["tokens"] = _tokens
    _save_config(cfg)


# ── configuracao ─────────────────────────────────────────────────────────────

def configure(client_id: str, client_secret: str, redirect_uri: Optional[str] = None) -> None:
    """Define credenciais OAuth. Chamado uma vez no setup."""
    cfg = _load_config()
    cfg["client_id"] = client_id.strip()
    cfg["client_secret"] = client_secret.strip()
    if redirect_uri:
        cfg["redirect_uri"] = redirect_uri.strip()
    _save_config(cfg)


def get_client_id() -> str:
    cfg = _load_config()
    return str(cfg.get("client_id") or "").strip()


def get_client_secret() -> str:
    cfg = _load_config()
    return str(cfg.get("client_secret") or "").strip()


def get_redirect_uri() -> str:
    cfg = _load_config()
    return str(cfg.get("redirect_uri") or _REDIRECT_URI).strip()


def is_configured() -> bool:
    """True quando client_id e client_secret estao definidos."""
    return bool(get_client_id() and get_client_secret())


def is_authenticated() -> bool:
    """True quando ha access_token valido (ou refresh_token para renovar)."""
    tokens = _load_tokens()
    return bool(tokens.get("access_token"))


# ── OAuth 2.0 ────────────────────────────────────────────────────────────────

def get_authorization_url(state: Optional[str] = None) -> str:
    """Gera URL de autorizacao OAuth 2.0 para o dono abrir no navegador."""
    if not is_configured():
        raise RuntimeError("LinkedIn nao configurado: chame configure() primeiro")
    
    params = {
        "response_type": "code",
        "client_id": get_client_id(),
        "redirect_uri": get_redirect_uri(),
        "scope": " ".join(_SCOPES),
        "state": state or "jarvis-linkedin-auth",
    }
    return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"


def handle_callback(code: str, redirect_uri: Optional[str] = None) -> dict[str, Any]:
    """Troca o authorization code por access_token + refresh_token.
    
    Retorna o dict de tokens (sem logar o client_secret).
    """
    if not is_configured():
        raise RuntimeError("LinkedIn nao configurado")
    
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code.strip(),
        "redirect_uri": redirect_uri or get_redirect_uri(),
        "client_id": get_client_id(),
        "client_secret": get_client_secret(),
    }).encode("utf-8")
    
    req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Falha ao trocar code por tokens: {exc}") from exc
    
    tokens = {
        "access_token": body.get("access_token", ""),
        "refresh_token": body.get("refresh_token", ""),
        "expires_in": body.get("expires_in", 0),
        "expires_at": time.time() + body.get("expires_in", 0),
        "scope": body.get("scope", ""),
    }
    _save_tokens(tokens)
    
    receipt = _issue_receipt("linkedin_auth", {"code_hash": _receipts.hash_sha256(code)}, tokens)
    
    return tokens


def refresh_access_token() -> dict[str, Any]:
    """Renova o access_token usando o refresh_token."""
    tokens = _load_tokens()
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise RuntimeError("Sem refresh_token: faca autorizacao novamente")
    
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": get_client_id(),
        "client_secret": get_client_secret(),
    }).encode("utf-8")
    
    req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Falha ao renovar token: {exc}") from exc
    
    new_tokens = {
        "access_token": body.get("access_token", ""),
        "refresh_token": body.get("refresh_token", refresh),
        "expires_in": body.get("expires_in", 0),
        "expires_at": time.time() + body.get("expires_in", 0),
        "scope": body.get("scope", ""),
    }
    _save_tokens(new_tokens)
    return new_tokens


def _ensure_valid_token() -> str:
    """Retorna access_token valido, renovando se necessario."""
    tokens = _load_tokens()
    access = tokens.get("access_token", "")
    expires_at = tokens.get("expires_at", 0)
    
    if not access:
        raise RuntimeError("Nao autenticado: faca autorizacao OAuth primeiro")
    
    # Renova 5 minutos antes do expiry (300s)
    if time.time() >= (expires_at - 300):
        tokens = refresh_access_token()
        access = tokens.get("access_token", "")
    
    return access


# ── acoes LinkedIn ───────────────────────────────────────────────────────────

def _api_request(url: str, method: str = "GET", body: Optional[dict] = None) -> dict:
    """Faz request autenticado a API do LinkedIn."""
    token = _ensure_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LinkedIn API error {exc.code}: {error_body}") from exc


def get_profile() -> dict[str, Any]:
    """Retorna dados basicos do perfil autenticado (nome, headline, foto)."""
    result = _api_request(_PROFILE_URL)
    
    profile = {
        "id": result.get("id", ""),
        "first_name": result.get("localizedFirstName", ""),
        "last_name": result.get("localizedLastName", ""),
        "headline": result.get("localizedHeadline", ""),
    }
    
    receipt = _issue_receipt("linkedin_get_profile", {}, profile)
    return profile


def post(text: str, visibility: str = "anyone") -> dict[str, Any]:
    """Publica um post no feed do dono.
    
    Args:
        text: Conteudo do post (texto puro).
        visibility: "anyone" (publico) ou "connections" (so conexoes).
    
    Returns:
        Dict com id do post criado.
    """
    if not text or not text.strip():
        raise ValueError("Texto do post nao pode ser vazio")
    
    profile = get_profile()
    author_urn = f"urn:li:person:{profile['id']}"
    
    visibility_val = "PUBLIC" if visibility == "anyone" else "CONNECTIONS"
    
    body = {
        "author": author_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {
                    "text": text.strip(),
                },
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": visibility_val,
        },
    }
    
    result = _api_request(_POST_URL, method="POST", body=body)
    
    post_id = result.get("id", "")
    response = {"post_id": post_id, "status": "published"}
    
    receipt = _issue_receipt("linkedin_post", {"text_length": len(text), "visibility": visibility}, response)
    return response


def comment(post_id: str, text: str) -> dict[str, Any]:
    """Comenta num post existente.
    
    Args:
        post_id: URN do post (ex: "urn:li:share:123456").
        text: Conteudo do comentario.
    
    Returns:
        Dict com id do comentario criado.
    """
    if not post_id or not post_id.strip():
        raise ValueError("post_id nao pode ser vazio")
    if not text or not text.strip():
        raise ValueError("Texto do comentario nao pode ser vazio")
    
    profile = get_profile()
    author_urn = f"urn:li:person:{profile['id']}"
    
    # LinkedIn espera o URN completo no endpoint
    post_urn = post_id if post_id.startswith("urn:") else f"urn:li:share:{post_id}"
    url = _COMMENT_URL.format(urn=urllib.parse.quote(post_urn, safe=""))
    
    body = {
        "actor": author_urn,
        "message": {"text": text.strip()},
    }
    
    result = _api_request(url, method="POST", body=body)
    
    comment_id = result.get("id", "")
    response = {"comment_id": comment_id, "status": "posted"}
    
    receipt = _issue_receipt("linkedin_comment", {"post_id": post_id, "text_length": len(text)}, response)
    return response


def search_people(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Busca pessoas por nome ou keyword.
    
    Args:
        query: Termo de busca.
        limit: Maximo de resultados (default 5).
    
    Returns:
        Lista de dicts com dados basicos das pessoas encontradas.
    """
    if not query or not query.strip():
        raise ValueError("Query de busca nao pode ser vazia")
    
    limit = max(1, min(int(limit), 25))  # clamp 1-25
    url = _SEARCH_URL.format(
        query=urllib.parse.quote(query.strip(), safe=""),
        limit=limit,
    )
    
    result = _api_request(url)
    
    elements = result.get("elements", [])
    people = []
    for elem in elements[:limit]:
        person = {
            "id": elem.get("id", ""),
            "first_name": elem.get("firstName", ""),
            "last_name": elem.get("lastName", ""),
            "headline": elem.get("headline", ""),
        }
        people.append(person)
    
    receipt = _issue_receipt("linkedin_search", {"query": query, "limit": limit}, people)
    return people


# ── reset (para testes) ──────────────────────────────────────────────────────

def reset() -> None:
    """Limpa estado em memoria. Usado por testes."""
    global _config, _tokens, _hmac_key
    _config = {}
    _tokens = {}
    _hmac_key = None
