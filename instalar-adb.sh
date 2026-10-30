#!/usr/bin/env bash
# Instala o `adb` (Android Debug Bridge) neste Linux e prepara o ambiente pra
# Alice falar com um Android por USB ou Wi-Fi.
#
# Ubuntu/Debian tem dois pacotes parecidos:
#   adb                     -> cliente, é o que a gente quer
#   android-tools-adb       -> nome antigo (Debian <12); mesmo binário
#   android-sdk-platform-tools -> alternativa da Google, inclui fastboot
#
# O serviço `adb server` roda no seu usuário (não precisa de root); só a
# regra udev pra USB exige sudo, e ela é opcional (dá pra rodar como plugdev).
set -euo pipefail

log()  { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✔\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }

if command -v adb >/dev/null 2>&1; then
    ok "adb já instalado: $(adb version | head -1)"
else
    log "Instalando adb via apt (vai pedir a sua senha sudo UMA vez)"
    sudo apt-get update
    sudo apt-get install -y adb
    ok "adb instalado"
fi

log "Grupo plugdev (pra USB sem sudo toda vez)"
if getent group plugdev >/dev/null 2>&1; then
    if id -nG "$USER" | tr ' ' '\n' | grep -qx plugdev; then
        ok "$USER já está no grupo plugdev"
    else
        warn "adicionando $USER ao plugdev (uma sessão nova resolve)"
        sudo usermod -aG plugdev "$USER"
        warn "depois disso você PRECISA deslogar/logar (ou reboot) pra USB funcionar sem sudo"
    fi
else
    warn "esta distro não tem o grupo plugdev; o adb vai pedir sudo em cada acesso USB — use Wi-Fi"
fi

log "Regra udev opcional (evita 'no permissions' no USB)"
RULE=/etc/udev/rules.d/51-android.rules
if [ -f "$RULE" ]; then
    ok "regra udev já existe em $RULE"
else
    warn "pra evitar erro de permissão por USB, rode (uma vez, com sudo):"
    cat <<'EOF'
        echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0666", GROUP="plugdev"' \
          | sudo tee /etc/udev/rules.d/51-android.rules
        sudo chmod a+r /etc/udev/rules.d/51-android.rules
        sudo udevadm control --reload-rules && sudo udevadm trigger
EOF
    warn "(18d1 é o vendor ID genérico da Google; pra fabricantes específicos use o ID correto)"
fi

log "Versão + status do servidor"
adb version | sed 's/^/  /'
adb start-server 2>&1 | sed 's/^/  /' || true
adb devices | sed 's/^/  /'

cat <<EOF

────────────────────────────────────────────────────────────────────────
Pronto. Agora você precisa parear o PROJETOR:

  • na TV/projetor Android:  Configurações → Dispositivo preferido →
    Opções do desenvolvedor → Depuração sem fio (Wireless debugging) → LIGAR
  • ali aparece um IP e porta, e um botão "Emparelhar dispositivo com código"
    que mostra um código de 6 dígitos.
  • no Linux, rode:  ./parear-android.sh

Se preferir por USB, plugue o cabo e o projetor já aparece em `adb devices`
depois de aceitar o diálogo "Permitir depuração USB?" na tela dele.
────────────────────────────────────────────────────────────────────────
EOF
