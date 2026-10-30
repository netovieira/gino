#!/usr/bin/env bash
# Cérebro local do JARVIS no Ollama — instalação no SEU usuário, sem sudo.
#
# Por que este modelo: a 3070 tem 8 GB. Um 7B em Q4_K_M ocupa ~4,7 GB e deixa
# ~1 GB de folga para o cache de contexto (8k tokens) e para a área de trabalho.
# Um 8B Q4 também cabe, mas sem margem; um 14B não cabe. Qwen2.5-7B-Instruct é
# o melhor equilíbrio da faixa: português bom, tool calling confiável e SEM modo
# "pensando" (o raciocínio do Qwen3 comeria os tokens da resposta falada).
#
# Uso:     ./instalar-cerebro-local.sh
# Trocar:  MODEL=qwen3:8b ./instalar-cerebro-local.sh
#          MODEL=hermes3:8b ./instalar-cerebro-local.sh     (melhor em ferramentas)
#          MODEL=qwen2.5:3b ./instalar-cerebro-local.sh     (mais rápido, mais burro)
set -euo pipefail

MODEL="${MODEL:-qwen2.5:7b-instruct}"
DEST="$HOME/.local/ollama"
MODELS="$HOME/.local/share/ollama-models"
# Chaves e logs em casa própria: o ~/.ollama desta máquina pertence a nobody
# (sobrou de outra instalação) e o Ollama se recusa a ler a própria chave de um
# diretório que não é dele. Esta versão ancora a chave em $HOME/.ollama e IGNORA
# OLLAMA_HOME para ela — então o que resolve é rodar o servidor com outro HOME.
# Assim não mexemos em nada que não é nosso, nem precisamos de sudo.
OLLAMA_HOME_DIR="$HOME/.local/share/ollama-home"
PORT="${PORT:-11434}"
# O release atual publica .tar.zst — o antigo .tgz do site devolve 404. O
# /latest/ do GitHub redireciona para a tag do dia, então a URL não envelhece.
URL="https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst"

log() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!  %s\033[0m\n' "$*"; }

# ── 1. o binário ────────────────────────────────────────────────────────────
OLLAMA_BIN=""
if command -v ollama >/dev/null 2>&1; then
    OLLAMA_BIN="$(command -v ollama)"
    log "Ollama já instalado no sistema: $OLLAMA_BIN"
elif [ -x "$DEST/bin/ollama" ]; then
    OLLAMA_BIN="$DEST/bin/ollama"
    log "Ollama já instalado em $DEST"
else
    log "Baixando o Ollama (~1,5 GB) para $DEST"
    mkdir -p "$DEST"
    curl -fL --progress-bar "$URL" -o "$DEST/ollama.tar.zst"
    tar --zstd -xf "$DEST/ollama.tar.zst" -C "$DEST" || {
        warn "tar --zstd falhou — instale o zstd (sudo apt install zstd) e rode de novo"
        exit 1; }
    rm -f "$DEST/ollama.tar.zst"
    OLLAMA_BIN="$DEST/bin/ollama"
    [ -x "$OLLAMA_BIN" ] || { echo "Falhou: $OLLAMA_BIN não existe"; exit 1; }
fi

export OLLAMA_HOST="127.0.0.1:$PORT"
export OLLAMA_MODELS="$MODELS"
mkdir -p "$MODELS" "$OLLAMA_HOME_DIR"

# ── 2. o servidor ───────────────────────────────────────────────────────────
respondendo() {
    curl -sf -m 3 "http://127.0.0.1:$PORT/api/tags" >/dev/null 2>&1
}

# O servidor velho segura a porta até morrer de verdade. Um pull que cai nele
# herda o ambiente velho — foi assim que um pull chegou no ~/.ollama de nobody
# mesmo com o serviço já reiniciado com OLLAMA_HOME novo.
porta_livre() {
    for _ in $(seq 1 30); do respondendo || return 0; sleep 0.5; done
    return 1
}

log "Subindo o servidor (reinicia sempre: o ambiente pode ter mudado)"
pkill -f "ollama serve" 2>/dev/null || true     # inclusive um serve manual esquecido
porta_livre || warn "o servidor antigo não soltou a porta a tempo"
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    # Unidade de usuário: sobe no login, reinicia sozinha, sem sudo.
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/jarvis-ollama.service" <<EOF
[Unit]
Description=Ollama (cérebro local do JARVIS)
After=network-online.target

[Service]
ExecStart=$OLLAMA_BIN serve
Environment=OLLAMA_HOST=127.0.0.1:$PORT
Environment=OLLAMA_MODELS=$MODELS
Environment=HOME=$OLLAMA_HOME_DIR
Environment=OLLAMA_KEEP_ALIVE=30m
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable jarvis-ollama.service
    systemctl --user start jarvis-ollama.service
else
    warn "systemd --user indisponível — subindo com nohup (não volta sozinho no login)"
    HOME="$OLLAMA_HOME_DIR" nohup "$OLLAMA_BIN" serve > "$DEST/serve.log" 2>&1 &
fi
for _ in $(seq 1 40); do respondendo && break; sleep 1; done
respondendo || {
    echo "O servidor não subiu. Veja: journalctl --user -u jarvis-ollama -n 40"
    exit 1; }

# ── 3. o modelo ─────────────────────────────────────────────────────────────
log "Baixando $MODEL (uma vez, ~4,7 GB)"
HOME="$OLLAMA_HOME_DIR" "$OLLAMA_BIN" pull "$MODEL"

# ── 4. o teste que importa: ele responde E chama ferramenta? ────────────────
log "Teste com ferramentas (é isso que o JARVIS faz)"
python3 - "$MODEL" "$PORT" <<'PY'
import json, sys, time, urllib.request
model, port = sys.argv[1], sys.argv[2]
tools = [
    {"type": "function", "function": {"name": "open_app",
     "description": "Abre um aplicativo pelo nome.",
     "parameters": {"type": "object", "properties": {"app": {"type": "string"}},
                    "required": ["app"]}}},
    {"type": "function", "function": {"name": "weather_report",
     "description": "Previsão do tempo de uma cidade.",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                    "required": ["city"]}}},
]
def call(messages, with_tools=True):
    body = {"model": model, "messages": messages, "max_tokens": 200, "stream": False}
    if with_tools:
        body["tools"] = tools
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"})
    t = time.monotonic()
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read())
    return time.monotonic() - t, d["choices"][0]["message"]

el, msg = call([{"role": "user", "content": "Como está o tempo em Fortaleza?"}])
txt = (msg.get("content") or "").strip().replace("\n", " ")
calls = [c.get("function", {}).get("name") for c in (msg.get("tool_calls") or [])]
print(f"   ferramenta: {el:5.1f}s  chamou={calls or 'nenhuma'}  texto={txt[:60]!r}")

el2, msg2 = call([{"role": "system", "content": "Responda em uma frase curta, em português."},
                  {"role": "user", "content": "Tudo bem com você?"}])
print(f"   conversa:   {el2:5.1f}s  -> {(msg2.get('content') or '').strip()[:70]!r}")
PY

log "Onde o modelo está rodando"
HOME="$OLLAMA_HOME_DIR" "$OLLAMA_BIN" ps 2>/dev/null || true

cat <<EOF

Pronto. Para o JARVIS usar este cérebro:
  ⚙ → PLUGIN SETTINGS → ECONOMY BRAIN
     PROVIDER ......... custom   (Ollama/OpenAI-compatible)
     BASE URL ......... http://127.0.0.1:$PORT/v1
     MODEL ............ $MODEL
     API KEY .......... ollama   (o Ollama ignora, mas o campo não pode ficar vazio)

Ou, pelo arquivo: config/api_keys.json → plugin_config.brain
     "provider": "custom", "base_url": "http://127.0.0.1:$PORT/v1",
     "model": "$MODEL", "api_key": "ollama",
     "fallback_provider": "groq"     ← fica de reserva para quando o PC estiver ocupado
EOF
