#!/usr/bin/env bash
# Pareia um Android (projetor / TV / celular) via Wi-Fi com o adb deste Linux,
# e salva o serial no config do JARVIS pra Alice já saber com quem falar.
#
# COMO USAR
#   ./parear-android.sh                          # modo interativo
#   ./parear-android.sh 192.168.0.42 37125 448812   # direto (ip, porta pareamento, código)
#   ./parear-android.sh --usb                    # pega o device USB atual e só faz `adb devices`
#
# PRÉ REQUISITO no projetor:
#   Configurações → Dispositivo preferido → Opções do desenvolvedor →
#     Depuração USB: LIGAR  (se for por cabo)
#     Depuração sem fio: LIGAR  (se for por rede)
#   Dentro de "Depuração sem fio" há um item "Emparelhar dispositivo com
#   código" que mostra IP:PORTA e um código de 6 dígitos. Use ESSA porta no
#   pareamento; depois, a tela principal de Depuração sem fio mostra OUTRA
#   porta (a de conexão) — o script descobre e conecta sozinho.
set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="$JARVIS_DIR/config/api_keys.json"

log()  { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✔\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✘ %s\033[0m\n" "$*" >&2; exit 1; }

command -v adb >/dev/null 2>&1 || die "rode ./instalar-adb.sh antes"

adb start-server >/dev/null 2>&1 || true

MODE="pair"
if [ "${1:-}" = "--usb" ]; then
    MODE="usb"
    shift
fi

if [ "$MODE" = "usb" ]; then
    log "Procurando devices USB"
    adb devices -l
    exit 0
fi

if [ $# -ge 3 ]; then
    IP="$1"; PORT="$2"; CODE="$3"
else
    echo "Digite o IP do projetor (aparece na tela 'Depuração sem fio'):"; read -r IP
    echo "Digite a PORTA de pareamento (o número ao lado do código de 6 dígitos):"; read -r PORT
    echo "Digite o CÓDIGO de 6 dígitos que está na tela:"; read -r CODE
fi
[ -n "$IP" ] && [ -n "$PORT" ] && [ -n "$CODE" ] || die "faltou IP, porta ou código"

log "adb pair $IP:$PORT  (código: $CODE)"
# `adb pair` lê o código do stdin
if ! printf "%s\n" "$CODE" | adb pair "$IP:$PORT"; then
    die "pareamento falhou — o código expirou? reabra a tela 'Emparelhar dispositivo' no projetor"
fi
ok "pareado"

log "adb connect $IP:<porta de conexão>"
# A porta de CONEXÃO é a que aparece no topo da tela 'Depuração sem fio'
# (linha "IP e porta"). Ela é diferente da porta de pareamento.
echo "Digite a PORTA de conexão (a que aparece grande na tela de Depuração sem fio, ao lado do IP):"
read -r CONN_PORT
if [ -z "$CONN_PORT" ]; then
    warn "sem porta de conexão; tentando a mesma do pareamento"
    CONN_PORT="$PORT"
fi
if ! adb connect "$IP:$CONN_PORT"; then
    die "conexão falhou em $IP:$CONN_PORT — confere se o projetor está na mesma rede"
fi
adb devices -l
ok "conectado em $IP:$CONN_PORT"

# persistir no config do JARVIS
SERIAL="$IP:$CONN_PORT"
if [ -f "$CFG" ]; then
    python3 - "$CFG" "$SERIAL" <<'PY'
import json, sys
p, serial = sys.argv[1], sys.argv[2]
d = json.load(open(p, encoding="utf-8"))
pc = d.setdefault("plugin_config", {})
pc.setdefault("android", {})["serial"] = serial
json.dump(d, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print(f"  ✔ serial salvo em plugin_config.android.serial = {serial}")
PY
else
    warn "não achei $CFG — configura em ⚙ → PLUGIN SETTINGS → ANDROID depois"
fi

cat <<EOF

────────────────────────────────────────────────────────────────────────
Pronto. A Alice agora enxerga o projetor. Testa:

    adb -s $SERIAL shell getprop ro.product.model
    adb -s $SERIAL exec-out screencap -p > /tmp/proj.png && xdg-open /tmp/proj.png

Depois reinicia o JARVIS e fala com ela:

    "Alice, tira um screenshot do projetor"
    "Alice, clica no meio da tela do projetor"
    "Alice, roda esse vídeo no projetor: <url youtube>"
────────────────────────────────────────────────────────────────────────
EOF
