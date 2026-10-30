#!/usr/bin/env bash
# Permite que a Alice rode comandos como root SEM digitar senha — mas só quando
# VOCÊ autoriza por voz ("pode executar" / "pode usar sudo").
#
# O QUE ESTE SCRIPT FAZ
#   Escreve /etc/sudoers.d/jarvis-nopasswd liberando `sudo` sem senha para o seu
#   usuário. É o que faz `sudo -n` funcionar. Valida com `visudo -cf` antes de
#   deixar o arquivo — um sudoers quebrado deixa a máquina sem sudo nenhum.
#
# O QUE ELE NÃO FAZ
#   Não guarda senha, não cria senha, não mexe em mais nada. A autorização vive
#   no JARVIS (core/rootgate.py): janela de 2 minutos, aberta só por uma frase
#   sua, consumida no primeiro comando.
#
# O RISCO, DIRETO
#   Com sudo sem senha, o único portão é a frase + a lista de comandos
#   catastróficos que o actions/root_exec.py recusa. O microfone ouve a sala
#   (música, TV). Se isso te incomodar, use a versão restrita: descomente o
#   bloco ALLOWLIST abaixo e libere só o que você realmente quer.
#
# RODAR UMA VEZ:  ./autorizar-sudo.sh
# DESFAZER:       sudo rm /etc/sudoers.d/jarvis-nopasswd
set -euo pipefail

RULE=/etc/sudoers.d/jarvis-nopasswd
USER_NAME="${SUDO_USER:-$USER}"

log()  { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✔\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✘ %s\033[0m\n" "$*" >&2; exit 1; }

command -v sudo >/dev/null || die "sudo não está instalado"
[ "$(id -u)" != "0" ] || die "rode como usuário normal: ./autorizar-sudo.sh"

if [ -f "$RULE" ]; then
    ok "já existe: $RULE"
    sudo -n true 2>/dev/null && ok "sudo sem senha já funciona" || warn "existe mas o sudo -n não passou"
    exit 0
fi

log "Escrevendo $RULE para o usuário $USER_NAME (vai pedir a sua senha)"
sudo tee "$RULE" >/dev/null <<EOF
# Alice (JARVIS) — sudo sem senha para comandos que VOCÊ autoriza por voz.
# A autorização é dada no próprio assistente (core/rootgate.py): janela curta,
# aberta por uma frase sua, consumida no primeiro comando.
# Remover com: sudo rm $RULE
$USER_NAME ALL=(ALL) NOPASSWD: ALL

# ── versão restrita (opcional) ───────────────────────────────────────────────
# Para limitar a superfície, troque a linha acima por uma lista explícita, por
# exemplo (descomente e ajuste):
#
# $USER_NAME ALL=(ALL) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt, /usr/bin/dpkg
# $USER_NAME ALL=(ALL) NOPASSWD: /usr/bin/systemctl
# $USER_NAME ALL=(ALL) NOPASSWD: /usr/bin/snap
#
# Atenção: liberar apt-get equivale, na prática, a liberar root — um pacote pode
# rodar scripts de instalação como root. A lista restringe o acidente, não o
# mal-intencionado.
EOF

log "Validando o sudoers"
sudo chmod 0440 "$RULE"
if ! sudo visudo -cf "$RULE" >/dev/null 2>&1; then
    sudo rm -f "$RULE"
    die "o arquivo não passou no visudo — removi para não quebrar o sudo"
fi
ok "visudo aprovou"

log "Testando (não vai pedir senha)"
if sudo -n true 2>/dev/null; then
    ok "sudo sem senha funcionando"
else
    die "sudo -n ainda falhou — confira se o seu usuário está correto em $RULE"
fi

cat <<'EOF'

────────────────────────────────────────────────────────────────────────
Pronto. Agora o fluxo é:

  1. você pede algo que precisa de root ("instala o adb");
  2. a Alice fala "preciso da sua autorização para isto: ...";
  3. você responde "pode executar" (ou "tem minha autorização");
  4. ela executa na hora e fala o resultado.

A autorização dura 2 minutos, vale para UM comando, e fecha sozinha.
Desfazer tudo:  sudo rm /etc/sudoers.d/jarvis-nopasswd
────────────────────────────────────────────────────────────────────────
EOF
