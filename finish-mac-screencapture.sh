#!/usr/bin/env bash
# Completa o setup do Mac pra Alice: instala um LaunchAgent que roda dentro
# da sessão Aqua do usuário logado, escuta um flag file e, quando ele muda,
# tira a screenshot. Alice (via SSH) só toca no flag e puxa o PNG.
#
# Por que isto é preciso: sobre SSH, nem `screencapture -x` nem
# `sudo launchctl asuser <uid> screencapture -x` conseguem alcançar o
# WindowServer no macOS moderno — mesmo root tem o bootstrap namespace
# errado. A UNICA forma confiável é fazer o serviço rodar como um
# LaunchAgent já residente na GUI session, disparado por um WatchPaths.
#
# Uma vez só, depois do setup-mac.sh. Idempotente.

set -euo pipefail

STATE_DIR="$HOME/Library/Application Support/Jarvis"
TRIGGER="$STATE_DIR/needs-screenshot"
SHOT="$STATE_DIR/screenshot.png"
STAMP="$STATE_DIR/last-taken"
BIN_DIR="$HOME/bin"
BIN="$BIN_DIR/jarvis-take-screenshot.sh"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT="$AGENT_DIR/com.jarvis.screenshot-watcher.plist"
LABEL="com.jarvis.screenshot-watcher"

log() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()  { printf "  \033[1;32m✔\033[0m %s\n" "$*"; }
die() { printf "  \033[1;31m✘ %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "este script roda no macOS"

log "Preparando diretórios"
mkdir -p "$STATE_DIR" "$BIN_DIR" "$AGENT_DIR"
ok "$STATE_DIR"

log "Instalando o script-tomador ($BIN)"
cat > "$BIN" <<EOF
#!/bin/bash
# Disparado pelo launchd (WatchPaths) dentro da sessão Aqua do usuário.
# Tira a screenshot e grava o timestamp. Não precisa de sudo — este
# processo já É a sessão do usuário.
set -e
STATE="$STATE_DIR"
/usr/sbin/screencapture -x "\$STATE/screenshot.png"
/bin/date +%s > "\$STATE/last-taken"
EOF
chmod +x "$BIN"
ok "$BIN"

log "Instalando o LaunchAgent ($AGENT)"
cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>              <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$BIN</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>$TRIGGER</string>
    </array>
    <key>RunAtLoad</key>          <false/>
    <key>ThrottleInterval</key>   <integer>1</integer>
    <key>StandardOutPath</key>    <string>$STATE_DIR/watcher.out</string>
    <key>StandardErrorPath</key>  <string>$STATE_DIR/watcher.err</string>
</dict>
</plist>
EOF
chmod 644 "$AGENT"
# cria o flag file uma vez (o WatchPaths só dispara em mudança de mtime, mas
# o launchd precisa de um path existente pra registerar o watcher)
touch "$TRIGGER"
ok "$AGENT"

log "Recarregando o agente"
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$AGENT"
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
ok "LaunchAgent $LABEL carregado"

log "Self-test: pede uma screenshot agora"
BEFORE="$(cat "$STAMP" 2>/dev/null || echo 0)"
touch "$TRIGGER"
for i in 1 2 3 4 5 6; do
    sleep 1
    AFTER="$(cat "$STAMP" 2>/dev/null || echo 0)"
    if [ "$AFTER" != "$BEFORE" ] && [ -f "$SHOT" ]; then
        ok "screenshot tomada: $SHOT ($(wc -c <"$SHOT" | tr -d ' ') bytes)"
        echo
        echo "────────────────────────────────────────────────────────────────"
        echo "Pronto. Do Linux, a Alice usa o plugin mac_control direto:"
        echo "    ssh -i ~/.ssh/jarvis_mac_key \$USER@<mac> \"touch $TRIGGER\""
        echo "    ssh ... \"base64 < $SHOT\""
        echo "────────────────────────────────────────────────────────────────"
        exit 0
    fi
done
# se chegou aqui, não veio — imprime o log do agente pra debug
echo "  ✘ o self-test não completou em 6 s. Últimas linhas do watcher:"
[ -f "$STATE_DIR/watcher.err" ] && sed 's/^/    /' "$STATE_DIR/watcher.err"
[ -f "$STATE_DIR/watcher.out" ] && sed 's/^/    /' "$STATE_DIR/watcher.out"
echo "  (uma permissão em 'Screen Recording' provavelmente é pedida: System"
echo "   Settings → Privacy & Security → Screen Recording → ligue 'bash')"
exit 1
