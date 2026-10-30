"""
Qoder — settings form and a status tool.

This plugin exists for its PLUGIN_SETTINGS: it puts the Qoder proxy's address,
key and default model into ⚙ → PLUGIN SETTINGS → QODER, with a TEST CONNECTION
button that checks the proxy, the key and a real answer from the model.

The bridge itself lives in core/qoder.py; the delegation tool the model calls
is actions/qoder_agent.py. Nothing here talks to Qoder directly — it only reads
and writes plugin_config.qoder in config/api_keys.json.

The proxy is a container created once by ./setup-qoder.sh <PAT-do-Qoder> and
kept alive by Docker (`--restart unless-stopped`). The PAT stays in
.qoder-proxy.env; this form only ever holds the proxy's own key.
"""
from core.qoder import (DEFAULT_MODEL, MODELS, NAMESPACE, health, models,
                        settings, test_connection)

PLUGIN = {
    "name": "qoder_status",
    "description": (
        "Reports whether the local Qoder server (qoder-proxy) is up, which "
        "model is selected and which models it offers. Use when the user asks "
        "about Qoder, about the local/fallback brain, or why a delegated "
        "coding task failed."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "QODER (local model server — used by the brain and by qoder_agent)",
    "fields": [
        {"key": "base_url", "label": "Proxy address", "type": "text",
         "placeholder": "http://127.0.0.1:3000/v1"},
        {"key": "api_key", "label": "Proxy API key (PROXY_API_KEY, not the PAT)",
         "type": "password", "placeholder": "from .qoder-proxy.env"},
        {"key": "model", "label": "Default model", "type": "choice",
         "options": list(MODELS), "default": DEFAULT_MODEL},
        {"key": "workspace", "label": "Workspace (where 'write' mode saves)", "type": "text",
         "placeholder": "~/JarvisProjects"},
        {"key": "auto_start", "label": "Start the container when it is down", "type": "toggle",
         "default": True},
    ],
    "action": {"label": "TEST CONNECTION", "run": test_connection},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    from memory.config_manager import get_plugin_config

    st = settings(get_plugin_config)
    if not health(st):
        return (f"The Qoder server is not answering at {st['base_url']}. Start it with "
                "./setup-qoder.sh <PAT> — the brain's fallback answers until then.")
    try:
        ids = models(st)
    except Exception as e:
        return f"The Qoder server answered but its model list failed: {e}"

    chosen = st["model"] if st["model"] in ids else f"{st['model']} (not in the list)"
    shown = ", ".join(ids[:8]) + ("…" if len(ids) > 8 else "")
    return (f"Qoder is up at {st['base_url']}, {len(ids)} models available. "
            f"The default is {chosen}. Others: {shown}. Workspace: {st['workspace']}. "
            f"The 'qoder_agent' tool delegates coding work to these models.")
