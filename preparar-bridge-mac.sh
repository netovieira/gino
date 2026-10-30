#!/usr/bin/env bash
# Prepara o lado Linux (JARVIS) para falar com o Macbook via SSH.
# Gera um keypair dedicado (~/.ssh/jarvis_mac_key), sem passphrase, e imprime
# a linha que você precisa autorizar no Mac (o setup-mac.sh já faz isso por
# você se colar a chave pública lá).
set -euo pipefail

KEY="$HOME/.ssh/jarvis_mac_key"

log() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()  { printf "  \033[1;32m✔\033[0m %s\n" "$*"; }

mkdir -p ~/.ssh && chmod 700 ~/.ssh

if [ -f "$KEY" ]; then
    ok "chave já existia em $KEY — mantendo"
else
    log "Gerando chave ed25519 dedicada à Alice (sem passphrase)"
    ssh-keygen -t ed25519 -N "" -C "jarvis@$(hostname -s)" -f "$KEY" >/dev/null
    ok "$KEY criado"
fi

chmod 600 "$KEY"
PUB="$(cat "$KEY.pub")"

cat <<EOF

Chave pública da Alice:
    $PUB

Guarde essa linha. Ela vai ser pedida pelo setup-mac.sh quando você rodá-lo
no Mac (ou, se preferir fazer à mão, adicione no ~/.ssh/authorized_keys do
Mac).

Para copiar essa chave pro Mac já autorizada (se você já tiver outro acesso
SSH lá), rode uma vez, substituindo USUARIO e MAC:

    ssh-copy-id -i "$KEY.pub" USUARIO@MAC.local

Ou, direto por SSH+cat:

    cat "$KEY.pub" | ssh USUARIO@MAC.local 'umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys'

Quando o Mac estiver acessível, edite no JARVIS:
    config/api_keys.json → plugin_config.mac
      { "host": "<IP_do_Mac>", "user": "<seu_usuario_mac>",
        "key_path": "~/.ssh/jarvis_mac_key" }
EOF
