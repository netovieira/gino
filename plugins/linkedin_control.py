"""LinkedIn por voz: postar, comentar, buscar pessoas e ver perfil.

Este plugin e a boca do modulo core/linkedin.py. Quem fala com a API do
LinkedIn eh o core; este arquivo so traduz acoes de voz em chamadas.

CONFIGURACAO (uma vez so):
    1. Criar app em https://www.linkedin.com/developers/apps
    2. Pegar client_id e client_secret
    3. Definir redirect_uri como http://localhost:8012/linkedin/callback
    4. Chamar linkedin.configure(client_id, client_secret)

AUTENTICACAO:
    "conectar linkedin"  -> action: auth_url  (retorna URL para o dono abrir)
    O dono autoriza no navegador, copia o code da URL de redirect, e:
    "codigo abc123"      -> action: auth_callback, code: abc123

ACOES:
    "posta no linkedin que..."   -> action: post, text: ...
    "comenta no post 123 que..." -> action: comment, post_id: 123, text: ...
    "busca fulano no linkedin"   -> action: search, query: fulano
    "meu perfil linkedin"        -> action: profile

PRIVACIDADE:
    - Posts e comentarios sao IRREVERSIVEIS. O bridge pede confirmacao (medium).
    - Credenciais ficam em config/linkedin.json (0600), nunca logadas.
    - Recibos HMAC sao gerados para cada acao (core/tool_receipts.py).
"""
from __future__ import annotations

from core import linkedin

PLUGIN = {
    "name": "linkedin_control",
    "description": (
        "Works with the user's LinkedIn account: posts text updates, comments "
        "on posts, searches for people, and retrieves the authenticated profile. "
        "Use for anything mentioning LinkedIn, professional network, posts, "
        "connections, or career-related social actions."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "One of: auth_url, auth_callback, post, comment, search, profile"
                ),
            },
            "text": {
                "type": "STRING",
                "description": "Post content or comment text",
            },
            "post_id": {
                "type": "STRING",
                "description": "LinkedIn post URN or ID (for commenting)",
            },
            "query": {
                "type": "STRING",
                "description": "Search query for finding people",
            },
            "code": {
                "type": "STRING",
                "description": "Authorization code from OAuth callback",
            },
            "visibility": {
                "type": "STRING",
                "description": "Post visibility: 'anyone' (public) or 'connections'",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Max search results (default 5, max 25)",
            },
        },
        "required": ["action"],
    },
}


def linkedin_control(action: str, **kwargs) -> str:
    """Handler principal do plugin. Traduz acoes de voz em chamadas ao core."""
    action = str(action or "").strip().lower()
    
    try:
        if action == "auth_url":
            return _action_auth_url()
        
        elif action == "auth_callback":
            code = str(kwargs.get("code") or "").strip()
            return _action_auth_callback(code)
        
        elif action == "post":
            text = str(kwargs.get("text") or "").strip()
            visibility = str(kwargs.get("visibility") or "anyone").strip().lower()
            return _action_post(text, visibility)
        
        elif action == "comment":
            post_id = str(kwargs.get("post_id") or "").strip()
            text = str(kwargs.get("text") or "").strip()
            return _action_comment(post_id, text)
        
        elif action == "search":
            query = str(kwargs.get("query") or "").strip()
            limit = int(kwargs.get("limit") or 5)
            return _action_search(query, limit)
        
        elif action == "profile":
            return _action_profile()
        
        else:
            return f"Acao '{action}' nao reconhecida. Use: auth_url, auth_callback, post, comment, search, profile."
    
    except Exception as exc:
        return f"Erro no LinkedIn: {exc}"


def _action_auth_url() -> str:
    """Gera URL de autorizacao OAuth."""
    if not linkedin.is_configured():
        return "LinkedIn nao configurado. Chame linkedin.configure(client_id, client_secret) primeiro."
    
    url = linkedin.get_authorization_url()
    return f"Abra esta URL no navegador para autorizar o LinkedIn:\n{url}"


def _action_auth_callback(code: str) -> str:
    """Troca code por tokens."""
    if not code:
        return "Codigo de autorizacao vazio. Copie o 'code' da URL de redirect."
    
    try:
        tokens = linkedin.handle_callback(code)
        return "Autenticacao LinkedIn concluida com sucesso. Tokens salvos."
    except Exception as exc:
        return f"Falha na autenticacao: {exc}"


def _action_post(text: str, visibility: str) -> str:
    """Publica post no feed."""
    if not text:
        return "Texto do post vazio. Dite o conteudo que quer publicar."
    
    if not linkedin.is_authenticated():
        return "LinkedIn nao autenticado. Faca autorizacao primeiro (action: auth_url)."
    
    result = linkedin.post(text, visibility)
    post_id = result.get("post_id", "desconhecido")
    return f"Post publicado com sucesso! ID: {post_id}"


def _action_comment(post_id: str, text: str) -> str:
    """Comenta num post."""
    if not post_id:
        return "post_id vazio. Informe o ID ou URN do post."
    if not text:
        return "Texto do comentario vazio. Dite o que quer comentar."
    
    if not linkedin.is_authenticated():
        return "LinkedIn nao autenticado. Faca autorizacao primeiro."
    
    result = linkedin.comment(post_id, text)
    comment_id = result.get("comment_id", "desconhecido")
    return f"Comentario publicado! ID: {comment_id}"


def _action_search(query: str, limit: int) -> str:
    """Busca pessoas."""
    if not query:
        return "Query de busca vazia. Dite o nome ou keyword."
    
    if not linkedin.is_authenticated():
        return "LinkedIn nao autenticado. Faca autorizacao primeiro."
    
    people = linkedin.search_people(query, limit)
    if not people:
        return f"Nenhuma pessoa encontrada para '{query}'."
    
    lines = [f"Encontrei {len(people)} pessoa(s) para '{query}':"]
    for i, person in enumerate(people, 1):
        name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
        headline = person.get("headline", "")
        lines.append(f"{i}. {name} - {headline}")
    
    return "\n".join(lines)


def _action_profile() -> str:
    """Retorna dados do perfil autenticado."""
    if not linkedin.is_authenticated():
        return "LinkedIn nao autenticado. Faca autorizacao primeiro."
    
    profile = linkedin.get_profile()
    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    headline = profile.get("headline", "")
    return f"Seu perfil LinkedIn:\nNome: {name}\nHeadline: {headline}"
