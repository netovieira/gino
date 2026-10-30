#!/usr/bin/env bash
# Habilita o Mac a ser controlado pela Alice (JARVIS no Linux) via SSH.
#
# O QUE ESTE SCRIPT FAZ
#   1. Liga Remote Login (ssh) — precisa sudo, você digita a senha do Mac.
#   2. Instala, via Homebrew, `libimobiledevice` + `ideviceinstaller` e, via
#      pip --user, o `pymobiledevice3`. Estes dão à Alice screenshot confiável,
#      syslog, crash reports, apps listados e — o que só é possível no macOS —
#      toque/deslizar/digitar na tela do iPhone através do `developer dvt`.
#   3. Cola a chave pública da Alice em ~/.ssh/authorized_keys (idempotente:
#      adicionar de novo não duplica).
#   4. Descobre o IP local do Mac e imprime o JSON que vai no
#      config/api_keys.json do JARVIS em plugin_config.mac.
#
# O QUE RODAR ANTES, NO LINUX (uma vez só)
#   cd ~/jarvis && ./preparar-bridge-mac.sh
# Ele gera ~/.ssh/jarvis_mac_key (se não existir) e imprime uma linha tipo
#   ssh-copy-id -i ~/.ssh/jarvis_mac_key.pub anthero@SEU-MAC.local
# Depois você copia o comando abaixo e roda no Mac.
#
# COMO RODAR ESTE SCRIPT NO MAC
#   curl -fsSL http://<IP_DO_LINUX>:8010/static/setup-mac.sh | bash
#   — ou —
#   scp setup-mac.sh anthero@mac.local:~/ && ssh anthero@mac.local 'bash setup-mac.sh'
#   — ou —
#   cole o arquivo no Mac e rode `bash setup-mac.sh` no Terminal.
#
# Se você rodar sem a chave da Alice existir ainda, o script gera um
# placeholder e avisa; o passo de authorized_keys fica pra depois.

set -euo pipefail

JARVIS_PUBKEY_DEFAULT="$HOME/.ssh/jarvis_mac_key.pub"

log()  { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✔\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✘ %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "este script roda no macOS, não em $(uname -s)"

# Rodar o arquivo inteiro como sudo quebra duas coisas: (a) $HOME passa a ser
# /var/root e as chaves vão pro lugar errado, (b) o TCC do macOS não concede
# Full Disk Access por uid — só por app, então `sudo` aqui não ajuda o
# systemsetup do passo 1. O script chama sudo pontualmente quando precisa.
if [ "$(id -u)" = "0" ]; then
    die "não rode com sudo: ./setup-mac.sh  (o script chama sudo só onde precisa)"
fi

log "1/5 · Remote Login"
# Em macOS 13+ o `systemsetup -setremotelogin on` só funciona se o Terminal
# (iTerm.app / Warp / etc.) tiver Full Disk Access em Privacidade & Segurança.
# `sudo` NÃO resolve — o TCC é por app, não por uid. Detectamos a recusa e
# guiamos o usuário pelo caminho de um clique em System Settings.
if systemsetup -getremotelogin 2>/dev/null | grep -qi '^Remote Login: On'; then
    ok "Remote Login já está ligado"
else
    OUT="$(sudo systemsetup -setremotelogin on 2>&1 || true)"
    if echo "$OUT" | grep -qi "Full Disk Access"; then
        warn "o Terminal não tem Full Disk Access — o macOS recusa systemsetup"
        cat <<'HINT'

  ─────────────────────────────────────────────────────────────────
  Liga Remote Login na GUI (é o caminho sem sudo, 5 segundos):

    1)  abra System Settings
    2)  General  →  Sharing
    3)  ligue a chave  "Remote Login"
    4)  (opcional) em "Allow access for"  deixe "All users"
    5)  feche System Settings

  Se você preferir que DAQUI PRA FRENTE o script ligue/desligue
  Remote Login via CLI, dê Full Disk Access ao Terminal:

    System Settings → Privacy & Security → Full Disk Access
    → ativa Terminal.app (ou iTerm/Warp) → reabra o Terminal.
  ─────────────────────────────────────────────────────────────────

HINT
        printf "  Quando tiver ligado a chave, pressione ENTER pra continuar: "
        read -r _
        if ! systemsetup -getremotelogin 2>/dev/null | grep -qi '^Remote Login: On'; then
            die "Remote Login continua desligado — confere se você ligou a chave em System Settings → Sharing"
        fi
        ok "Remote Login ligado"
    elif [ -n "$OUT" ] && ! echo "$OUT" | grep -qi "on"; then
        die "não consegui ligar Remote Login: $OUT"
    else
        ok "Remote Login ligado"
    fi
fi

log "2/5 · Homebrew + libs de iPhone"
# Se o usuário já tem o Xcode completo instalado, registrar o dev dir dele é
# o que basta: sem isso o `install.sh` do Homebrew dispara o
# `xcode-select --install` nativo, que abre um diálogo "Searching online for
# the command line tools" capaz de travar atrás de proxy/IPv6 por muito tempo
# — mesmo quando as ferramentas já existem no Xcode.
if ! xcode-select -p >/dev/null 2>&1; then
    XCODE_DIR="$(ls -d /Applications/Xcode*.app/Contents/Developer 2>/dev/null | head -1 || true)"
    if [ -n "$XCODE_DIR" ]; then
        warn "Xcode encontrado em $XCODE_DIR — registrando como toolchain"
        sudo xcode-select -s "$XCODE_DIR"
        ok "xcode-select -> $XCODE_DIR"
    else
        log "Nem Xcode nem Command Line Tools — disparando xcode-select --install"
        warn "se aparecer 'Searching online for the command line tools' e travar,"
        warn "cancela, instala o Xcode da App Store, e roda de novo — o script vai"
        warn "detectar ele sozinho."
        xcode-select --install || true
        printf "  Quando o diálogo de instalação terminar, pressione ENTER: "
        read -r _
    fi
fi
if ! command -v clang >/dev/null 2>&1; then
    die "clang continua fora do PATH — registra o Xcode na mão: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
fi

if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew não está instalado; rodando o instalador oficial (peça a senha de novo)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon coloca em /opt/homebrew; Intel, /usr/local
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
    [ -x /usr/local/bin/brew ]    && eval "$(/usr/local/bin/brew shellenv)"
fi
brew --version >/dev/null
brew list libimobiledevice  >/dev/null 2>&1 || brew install libimobiledevice
brew list ideviceinstaller  >/dev/null 2>&1 || brew install ideviceinstaller
# Não instalar `usbmuxd` via brew — no macOS o daemon já vem com o sistema
# (/usr/libexec/usbd) e o formula Homebrew tem esse nome só em Linux. Uma
# linha com `brew install usbmuxd` abortaria o `set -e` aqui.

# Persiste o PATH do brew (Apple Silicon em /opt/homebrew; Intel em /usr/local)
# pra `idevice_id` etc. estarem disponíveis em toda sessão nova.
for pfx in /opt/homebrew /usr/local; do
    if [ -x "$pfx/bin/brew" ]; then
        SHELL_LOGIN="$(getent passwd "$USER" 2>/dev/null | awk -F: '{print $7}' || echo /bin/zsh)"
        TARGET_RC="$HOME/.zprofile"
        LINE="eval \"($pfx/bin/brew shellenv)\""
        if ! grep -Fq "$LINE" "$TARGET_RC" 2>/dev/null; then
            echo "$LINE" >> "$TARGET_RC"
            ok "adicionado $LINE em $TARGET_RC"
        fi
        eval "$("$pfx/bin/brew" shellenv)"
        break
    fi
done

log "3/5 · pymobiledevice3 (o que habilita dvt tap/swipe/text no iPhone)"
PY=python3
$PY -m pip --version >/dev/null 2>&1 || $PY -m ensurepip --user
if $PY -m pip show pymobiledevice3 >/dev/null 2>&1; then
    $PY -m pip install --user -U pymobiledevice3
    ok "pymobiledevice3 atualizado"
else
    $PY -m pip install --user pymobiledevice3
    ok "pymobiledevice3 instalado (--user)"
fi
# Persiste o PATH do `pip install --user` pro zsh — senão o `pymobiledevice3`
# na linha de comando não acha, só `python3 -m pymobiledevice3`.
USER_BIN="$(python3 -m site --user-base 2>/dev/null)/bin"
if [ -d "$USER_BIN" ]; then
    ZRC="$HOME/.zprofile"
    LINE="export PATH=\"$USER_BIN:\$PATH\""
    if ! grep -Fq "$LINE" "$ZRC" 2>/dev/null; then
        echo "$LINE" >> "$ZRC"
        ok "adicionado $USER_BIN ao PATH em $ZRC"
    fi
fi
if ! $PY -c "import pymobiledevice3" 2>/dev/null; then
    warn "o pip instalou mas o 'import' não acha — confere $USER_BIN no seu PATH"
fi

log "4/5 · chave SSH da Alice em authorized_keys"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys

PUB="${JARVIS_PUBKEY:-}"
if [ -z "$PUB" ] && [ -f "$JARVIS_PUBKEY_DEFAULT" ]; then
    # caso você tenha copiado o .pub pra cá antes de rodar o script
    PUB="$(cat "$JARVIS_PUBKEY_DEFAULT")"
fi
if [ -z "$PUB" ]; then
    printf "  Cole aqui a linha inteira da chave publica da Alice (ssh-ed25519 AAAA… jarvis@…) e ENTER, ou ENTER vazio pra fazer depois:\n  > "
    read -r PUB || PUB=""
fi
if [ -n "$PUB" ]; then
    # remove entrada anterior para o MESMO comentário, adiciona nova — idempotente
    COMMENT="$(awk '{print $NF}' <<<"$PUB")"
    TMP="$(mktemp)"
    grep -v "$COMMENT\$" ~/.ssh/authorized_keys > "$TMP" || true
    echo "$PUB" >> "$TMP"
    mv "$TMP" ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    ok "chave adicionada (comentário: $COMMENT)"
else
    warn "sem chave — você pode editar ~/.ssh/authorized_keys manualmente depois"
fi

log "5/5 · IP local do Mac (a Alice vai usar para conectar)"
# En0 = Wi-Fi na maioria; busca em todas as interfaces ativas exceto loopback.
MAC_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
[ -z "$MAC_IP" ] && MAC_IP="$(ipconfig getifaddr en1 2>/dev/null || true)"
[ -z "$MAC_IP" ] && MAC_IP="$(ifconfig 2>/dev/null | awk '/inet /&&!/127\.0\.0\.1/{print $2; exit}')"
MAC_HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
MAC_USER="$USER"
[ -n "$MAC_IP" ] && ok "IP local: $MAC_IP" || warn "não achei IP local; olhe em System Settings → Wi-Fi"
ok "Host name: ${MAC_HOST}.local · usuário: $MAC_USER"

cat <<EOF

────────────────────────────────────────────────────────────────────────
Pronto. Cole este bloco no config/api_keys.json do JARVIS (Linux):

    "plugin_config": {
      ...
      "mac": {
        "host": "${MAC_IP:-$MAC_HOST.local}",
        "user": "${MAC_USER}",
        "key_path": "~/.ssh/jarvis_mac_key"
      }
    }

E teste do Linux:

    ssh -i ~/.ssh/jarvis_mac_key ${MAC_USER}@${MAC_IP:-$MAC_HOST.local} 'idevice_id -l'

Se retornar a UDID do iPhone plugado, a Alice está pronta para controlar.
────────────────────────────────────────────────────────────────────────
EOF
