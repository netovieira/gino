#!/usr/bin/env bash
# montar-discos.sh — aponta os dois HDs para o que já roda neles.
#
#   sda1 label "timemachine"     -> /srv/timemachine  (share [TimeMachine] do Samba)
#   sdb1 label "nextcloud-data"  -> /media/nextcloud  (dados do Nextcloud snap)
#
# A amarração é por UUID, não por /dev/sdX: se você trocar a ordem dos cabos SATA
# nada quebra. O nofail deixa a máquina subir normalmente se um disco estiver
# desligado.
#
# Rodar:  sudo bash montar-discos.sh
# Pode rodar quantas vezes quiser — o que já estiver feito é pulado.

set -uo pipefail

UUID_TM="2642c1e7-e874-4055-84e4-0fc6ee16f804"
UUID_NC="efa0bfbf-cdaa-4da3-9383-74bf15b044a6"

MNT_TM="/srv/timemachine"
MNT_NC="/media/nextcloud"
DATA_NC="${MNT_NC}/data"
CONF_NC="/var/snap/nextcloud/current/nextcloud/config/config.php"
OCC="/snap/bin/nextcloud.occ"
OPCOES="defaults,noatime,nofail,x-systemd.device-timeout=15s"

say()  { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '    \033[33matenção\033[0m %s\n' "$*"; }
die()  { printf '    \033[31merro\033[0m   %s\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "precisa de root: sudo bash $0"

# ── 1. Solta as montagens temporárias do gerenciador de arquivos ──────────────
# Mesmo sistema de arquivos montado em dois lugares confunde o espaço livre e
# atrapalha o desmonte no desligamento.
say "Desmontando as montagens temporárias do Nautilus"
for p in /run/media/*/timemachine /run/media/*/nextcloud-data; do
    [ -d "$p" ] || continue
    mountpoint -q "$p" || continue
    if umount "$p" 2>/dev/null; then
        ok "desmontei $p"
    else
        warn "não desmontei $p (feche o Nautilus e rode de novo; sigo em frente)"
    fi
done

# ── 2. /etc/fstab ────────────────────────────────────────────────────────────
say "Ligando os discos ao /etc/fstab (por UUID)"
BACKUP="/etc/fstab.bak-$(date +%Y%m%d-%H%M%S)"
cp -a /etc/fstab "$BACKUP" && ok "backup do fstab em $BACKUP"

fstab_add() {  # $1=uuid  $2=ponto  $3=comentário
    local uuid=$1 ponto=$2 comentario=$3
    if grep -qE "^UUID=${uuid}[[:space:]]" /etc/fstab; then
        ok "fstab já aponta para $ponto"
        return 0
    fi
    {
        echo ""
        echo "# ${comentario}"
        echo "UUID=${uuid} ${ponto} ext4 ${OPCOES} 0 2"
    } >> /etc/fstab
    ok "fstab: ${ponto}"
}

montar() {  # $1=ponto
    local ponto=$1 unidade
    mkdir -p "$ponto"
    if mountpoint -q "$ponto"; then
        ok "$ponto já está montado"
        return 0
    fi
    systemctl daemon-reload
    # Preferir a unidade do systemd: fica registrada, dá para parar/iniciar
    # depois e o desmonte no shutdown é ordenado.
    unidade=$(systemd-escape -p --suffix=mount "$ponto")
    if systemctl start "$unidade" 2>/dev/null; then
        ok "montei $ponto ($(findmnt -rno SOURCE "${ponto}"))"
    elif mount "$ponto"; then
        ok "montei $ponto direto pelo mount ($(findmnt -rno SOURCE "${ponto}"))"
    else
        die "não consegui montar $ponto — o disco está ligado? (dmesg | tail)"
    fi
}

# ── 3. Time Machine ──────────────────────────────────────────────────────────
say "Time Machine — HD \"timemachine\" em ${MNT_TM}"
fstab_add "$UUID_TM" "$MNT_TM" "Time Machine: HD \"timemachine\""
montar "$MNT_TM"

# O share tem force user = timemachine, então é o usuário timemachine que
# escreve. A raiz do disco veio do mkfs com dono anthero.
chown timemachine:timemachine "$MNT_TM" && chmod 700 "$MNT_TM" \
    && ok "dono do share: timemachine (700)"
if runuser -u timemachine -- test -w "$MNT_TM"; then
    ok "timemachine consegue escrever no disco"
else
    warn "timemachine NÃO consegue escrever em $MNT_TM"
fi

# O smbd gera as regras de AppArmor a partir do smb.conf a cada start e o
# caminho do share não mudou, então só é preciso garantir o serviço de pé.
if systemctl enable --now smbd >/dev/null 2>&1; then
    ok "smbd ativo e habilitado no boot"
fi

USUARIOS_SAMBA=$(pdbedit -L 2>/dev/null | cut -d: -f1)
case "$USUARIOS_SAMBA" in
    *timemachine*)
        ok "usuário do Samba 'timemachine' já existe" ;;
    *)
        if [ -t 0 ]; then
            warn "usuário do Samba 'timemachine' não existe — o Mac não vai conseguir entrar"
            echo "    Escolha a senha que o Mac vai usar (não aparece na tela, digite 2x):"
            if smbpasswd -a -s timemachine; then
                ok "usuário do Samba criado"
            else
                warn "não criou — rode depois: sudo smbpasswd -a timemachine"
            fi
        else
            warn "usuário do Samba 'timemachine' não existe — rode: sudo smbpasswd -a timemachine"
        fi ;;
esac

# ── 4. Nextcloud ─────────────────────────────────────────────────────────────
say "Nextcloud — HD \"nextcloud-data\" em ${MNT_NC}"
fstab_add "$UUID_NC" "$MNT_NC" "Nextcloud: HD \"nextcloud-data\""
montar "$MNT_NC"

# O snap só alcança /media e /mnt, e só com esta interface ligada.
if snap connect nextcloud:removable-media 2>/dev/null; then
    ok "snap nextcloud: interface removable-media ligada"
else
    warn "não liguei nextcloud:removable-media (talvez já estivesse ligada)"
fi

if [ ! -f "$CONF_NC" ]; then
    warn "achei o snap mas não o config.php — Nextcloud ainda não foi instalado."
    warn "Crie o admin na interface web e rode este script de novo."
else
    ATUAL=$(sed -n "s/.*'datadirectory'[[:space:]]*=>[[:space:]]*'\([^']*\)'.*/\1/p" \
            "$CONF_NC" | head -1)
    printf '    data directory atual: %s\n' "${ATUAL:-?}"

    if [ "$ATUAL" = "$DATA_NC" ]; then
        ok "já está guardando tudo em $DATA_NC"
    elif [ -z "$ATUAL" ]; then
        warn "config.php sem 'datadirectory' — não mexi em nada"
    elif [ ! -d "$ATUAL" ]; then
        warn "$ATUAL não existe — não mexi em nada"
    else
        say "Movendo os dados de $ATUAL para $DATA_NC"
        "$OCC" maintenance:mode --on >/dev/null 2>&1 && ok "modo manutenção ligado"

        if [ -e "${DATA_NC}/.ocdata" ] || [ -e "${DATA_NC}/.ncdata" ]; then
            warn "este disco já tem uma estrutura do Nextcloud — não copiei nada por cima"
        else
            mkdir -p "$DATA_NC"
            if rsync -aHAX --info=stats1 "$ATUAL"/ "$DATA_NC"/; then
                ok "copiado: $(du -sh "$DATA_NC" | cut -f1) em $DATA_NC"
            else
                "$OCC" maintenance:mode --off >/dev/null 2>&1
                die "o rsync falhou — o config.php continua no lugar antigo, dá para tentar de novo"
            fi
        fi

        chown root:root "$DATA_NC" && chmod 0770 "$DATA_NC" \
            && ok "dono do diretório de dados: root (0770)"

        if "$OCC" config:system:set datadirectory --value="$DATA_NC" >/dev/null 2>&1; then
            ok "config.php: datadirectory = $DATA_NC"
        else
            CONF_BKP="${CONF_NC}.bak-$(date +%Y%m%d-%H%M%S)"
            cp -a "$CONF_NC" "$CONF_BKP"
            sed -i -E "s#('datadirectory'[[:space:]]*=>[[:space:]]*)'[^']*'#\1'${DATA_NC}'#" "$CONF_NC"
            if grep -q "'datadirectory'[^']*'${DATA_NC}'" "$CONF_NC"; then
                ok "config.php editado na mão (occ falhou) — backup em $CONF_BKP"
            else
                die "não consegui trocar o datadirectory. Backup do fstab: $BACKUP"
            fi
        fi

        snap restart nextcloud >/dev/null 2>&1 && ok "serviços do Nextcloud reiniciados"
        "$OCC" maintenance:mode --off >/dev/null 2>&1 && ok "modo manutenção desligado"
        if [ -d "$ATUAL" ] && [ "$ATUAL" != "$DATA_NC" ]; then
            warn "a cópia antiga continua ocupando $(du -sh "$ATUAL" | cut -f1) em $ATUAL"
            warn "confira no navegador que os arquivos estão lá e depois: sudo rm -rf $ATUAL"
        fi
    fi
fi

# ── 5. Conferência ───────────────────────────────────────────────────────────
say "Conferindo"
mountpoint -q "$MNT_TM" && printf '    %s -> %s\n' "$MNT_TM" "$(findmnt -rno SOURCE,SIZE,USED,AVAIL "$MNT_TM")"
mountpoint -q "$MNT_NC" && printf '    %s -> %s\n' "$MNT_NC" "$(findmnt -rno SOURCE,SIZE,USED,AVAIL "$MNT_NC")"
VER=$(curl -s --max-time 5 http://localhost/status.php 2>/dev/null)
case "$VER" in
    *'"installed":true'*'"maintenance":false'*)
        ok "Nextcloud respondendo: instalado e fora de manutenção" ;;
    *installed*)
        warn "Nextcloud respondeu, mas não está no estado esperado: $VER" ;;
    *)
        warn "Nextcloud não respondeu em http://localhost/status.php" ;;
esac
DADOS=$("$OCC" config:system:get datadirectory 2>/dev/null)
[ -n "$DADOS" ] && printf '    datadirectory: %s\n' "$DADOS"

say "No Mac"
cat <<'FIM'
    1. Finder -> ⌘K -> smb://ANTHERO-PC/TimeMachine  (usuário timemachine + a senha)
    2. Ajustes do Sistema -> Time Machine -> Adicionar disco de backup -> TimeMachine
       O macOS cria o sparsebundle sozinho ali e reenche o disco conforme o uso.
    3. Se o Time Machine recusar o share, force pelo terminal do Mac:
       sudo tmutil setdestination -p /Volumes/TimeMachine
FIM
