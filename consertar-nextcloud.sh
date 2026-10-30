#!/usr/bin/env bash
# consertar-nextcloud.sh — conserta o Nextcloud depois de mover os dados para o HD.
#
# O erro "diretório de dados inválido / não é possível criar o diretório data"
# não é do Nextcloud: é permissão. O disco montou em /media/nextcloud com dono
# anthero e modo 700. O Nextcloud roda como root dentro do snap, mas o AppArmor
# do snap bloqueia a capability dac_override — então o root não consegue
# atravessar uma pasta que não é dele, e todo acesso a /media/nextcloud/data
# vira "permissão negada" (visível no journal como dac_read_search DENIED).
#
# De quebra isso também explica a lentidão: sem acesso, o serviço
# nextcloud-fixer falha e reinicia em loop, disparando centenas de php.bin por
# minuto (chegou a 322 processos e um núcleo inteiro em 100%).
#
# Rodar:  sudo bash consertar-nextcloud.sh

set -uo pipefail

MNT_NC="/media/nextcloud"
DATA_NC="${MNT_NC}/data"
OLD_NC="/var/snap/nextcloud/common/nextcloud/data"
CONF_NC="/var/snap/nextcloud/current/nextcloud/config/config.php"
OCC="/snap/bin/nextcloud.occ"

say()  { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '    \033[33matenção\033[0m %s\n' "$*"; }
die()  { printf '    \033[31merro\033[0m   %s\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "precisa de root: sudo bash $0"

# ── 1. Para o loop antes de mexer ─────────────────────────────────────────────
# Enquanto o dado estiver inacessível o fixer reinicia sem parar; parar o snap
# evita que alguém escreva no diretório no meio do conserto.
say "Parando o Nextcloud (para o loop de reinício)"
if snap stop nextcloud >/dev/null 2>&1; then
    ok "parado"
else
    warn "não consegui parar — sigo mesmo assim"
fi
pkill -f 'nextcloud-fixer' 2>/dev/null
pkill -f '/nextcloud/.*/bin/php' 2>/dev/null
ok "sem php.bin rodando: $(pgrep -cf '/nextcloud/.*bin/php' || echo 0)"

# ── 2. O conserto de verdade: a pasta-pai precisa ser atravessável ────────────
say "Corrigindo o dono e a permissão do ponto de montagem"
[ -d "$MNT_NC" ] || die "$MNT_NC não existe — o disco está montado? (findmnt $MNT_NC)"
mountpoint -q "$MNT_NC" || die "$MNT_NC não está montado — rode: sudo mount $MNT_NC"
antes=$(stat -c '%U:%G %a' "$MNT_NC")
chown root:root "$MNT_NC"
chmod 0755 "$MNT_NC"
ok "$MNT_NC: $antes -> $(stat -c '%U:%G %a' "$MNT_NC")"

# ── 3. A pasta de dados: root, 0770, nunca legível por todos ──────────────────
say "Ajustando a pasta de dados"
[ -d "$DATA_NC" ] || die "$DATA_NC não existe — a cópia não foi feita"
chown -R root:root "$DATA_NC"
chmod 0770 "$DATA_NC"
ok "$DATA_NC: $(stat -c '%U:%G %a' "$DATA_NC")"

# ── 4. O arquivo que o Nextcloud procura para validar o diretório ─────────────
# Sem .ncdata ele considera o diretório inválido mesmo com tudo certo.
say "Conferindo o marcador .ncdata"
marcador="${DATA_NC}/.ncdata"
if [ -f "$marcador" ]; then
    ok ".ncdata já existia"
else
    printf '# Nextcloud data directory\n' > "$marcador"
    ok ".ncdata criado"
fi
chown root:root "$marcador"
chmod 0640 "$marcador"

# ── 5. Confere o que o config.php aponta ──────────────────────────────────────
say "Conferindo o datadirectory no config.php"
[ -f "$CONF_NC" ] || die "$CONF_NC não existe"
atual=$(grep -oP "^\s*'datadirectory'\s*=>\s*'\K[^']*" "$CONF_NC" | head -1)
if [ "$atual" = "$DATA_NC" ]; then
    ok "datadirectory = $atual"
else
    warn "datadirectory = '$atual' — corrigindo para $DATA_NC"
    cp -a "$CONF_NC" "${CONF_NC}.bak-$(date +%Y%m%d-%H%M%S)"
    sed -i "s|^\s*'datadirectory'\s*=>\s*'[^']*'|  'datadirectory' => '${DATA_NC}'|" "$CONF_NC"
    php -l "$CONF_NC" >/dev/null 2>&1 && ok "config.php corrigido e válido" \
        || warn "config.php alterado, mas o php -l não rodou aqui (confira depois)"
fi

# ── 6. Sobe de novo e espera ──────────────────────────────────────────────────
say "Subindo o Nextcloud"
snap start nextcloud >/dev/null 2>&1 || warn "snap start reclamou"

printf '    aguardando o Apache'
pronto=0
for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 http://localhost/status.php >/dev/null 2>&1; then
        pronto=1
        break
    fi
    printf '.'
    sleep 2
done
echo
[ "$pronto" = 1 ] && ok "Apache respondendo" || warn "Apache não respondeu em 120s"

# ── 7. Verificação ────────────────────────────────────────────────────────────
say "Conferindo"
resposta=$(curl -fsS --max-time 5 http://localhost/status.php 2>/dev/null || true)
case "$resposta" in
    *'"installed":true'*'"maintenance":false'*)
        ok "Nextcloud no ar, fora de manutenção" ;;
    *'"maintenance":true'*)
        warn "no ar, mas em manutenção — desligando"
        runuser -u root -- "$OCC" maintenance:mode --off >/dev/null 2>&1
        ok "manutenção desligada" ;;
    *)
        warn "status.php não confirmou:"
        [ -n "$resposta" ] && printf '    %s\n' "$resposta" | cut -c1-300 ;;
esac

echo
if ver=$("$OCC" status 2>&1); then
    printf '%s\n' "$ver" | sed 's/^/    /' | head -8
else
    warn "occ status falhou:"
    printf '%s\n' "$ver" | tail -6 | sed 's/^/    /' | cut -c1-200
fi

echo
echo "    dados: $(du -sh "$DATA_NC" 2>/dev/null | cut -f1) em $DATA_NC"
echo "    disco: $(findmnt -rno SOURCE,SIZE,USED,AVAIL "$MNT_NC" 2>/dev/null)"

# ── 8. Limpeza (só depois de você confirmar no navegador) ─────────────────────
echo
if [ -d "$OLD_NC" ]; then
    echo "    A cópia antiga continua em $OLD_NC ($(du -sh "$OLD_NC" 2>/dev/null | cut -f1))."
    echo "    Abra http://localhost, confirme que seus arquivos estão lá e só então:"
    echo "        sudo rm -rf $OLD_NC"
fi

# ── 9. Extra: a ~/.ssh ficou com dono root ────────────────────────────────────
# Não é do Nextcloud, mas apareceu no journal (o seahorse não conseguia ler as
# chaves) e quebra o login por chave SSH. Como já estamos como root, aproveita.
say "Corrigindo o dono da ~/.ssh"
if [ -d /home/anthero/.ssh ]; then
    antes_ssh=$(stat -c '%U:%G' /home/anthero/.ssh)
    chown -R anthero:anthero /home/anthero/.ssh
    chmod 700 /home/anthero/.ssh
    find /home/anthero/.ssh -maxdepth 1 -type f -exec chmod 600 {} +
    chmod 644 /home/anthero/.ssh/authorized_keys 2>/dev/null
    ok "~/.ssh: $antes_ssh -> $(stat -c '%U:%G %a' /home/anthero/.ssh)"
else
    warn "/home/anthero/.ssh não existe"
fi

echo
echo "    Abra: http://localhost"
