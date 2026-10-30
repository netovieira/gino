#!/usr/bin/env bash
# jarvis-abrir.sh — usado pelo atalho do menu de aplicativos.
#
# O JARVIS já sobe na bandeja no login (autostart). Se você clicar no atalho com
# ele rodando (mas com a janela escondida na bandeja), abrir de novo criaria uma
# segunda instância disputando microfone e wake word. Então: se já estiver de pé,
# manda SIGUSR1 pra ele MOSTRAR a janela de volta, em vez de só avisar.
set -uo pipefail

PROJ="$(cd "$(dirname "$0")" && pwd)"

# Acha o PID do main.py cujo diretório de trabalho é este projeto — olhar só o
# nome do processo pegaria qualquer python main.py da máquina.
pid_rodando() {
    local p
    for p in $(pgrep -f 'main\.py' 2>/dev/null); do
        [ "$(readlink -f "/proc/$p/cwd" 2>/dev/null)" = "$PROJ" ] && { echo "$p"; return 0; }
    done
    return 1
}

if PID="$(pid_rodando)"; then
    # SIGUSR1 → o QTimer da thread principal mostra/raiseia a janela (≤1,5 s).
    kill -USR1 "$PID" 2>/dev/null || true
    notify-send -i "$PROJ/config/jarvis.ico" -a JARVIS \
        "JARVIS" "Aqui está — já estava na bandeja." 2>/dev/null \
    || printf 'JARVIS já estava rodando; mostrando a janela.\n'
    exit 0
fi

exec "$PROJ/run-jarvis.sh" --tray
