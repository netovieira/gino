#!/usr/bin/env bash
# contas-nextcloud.sh — lista, cria e redefine contas do Nextcloud snap.
#
# O Nextcloud não tem "tela de cadastro": quem cria conta é um administrador,
# pela interface web (Configurações > Usuários) ou por este script. A primeira
# conta (admin) nasceu na instalação — este script mostra qual é e, se você
# esqueceu a senha, redefine.
#
# Rodar:  sudo bash contas-nextcloud.sh

set -uo pipefail

OCC="/snap/bin/nextcloud.occ"

say()  { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '    \033[33matenção\033[0m %s\n' "$*"; }
die()  { printf '    \033[31merro\033[0m   %s\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "precisa de root: sudo bash $0"
[ -x "$OCC" ] || die "$OCC não existe — o snap está instalado?"

occ() { "$OCC" --no-interaction "$@" 2>&1; }

# Usuário com senha: pede duas vezes e devolve em SENHA.
pedir_senha() {
    local a b
    while :; do
        read -rsp '    senha (não aparece): ' a; echo
        [ ${#a} -ge 10 ] || { warn "mínimo de 10 caracteres (exigência padrão do Nextcloud)"; continue; }
        read -rsp '    repita a senha: ' b; echo
        [ "$a" = "$b" ] || { warn "não conferem, de novo"; continue; }
        SENHA=$a
        return 0
    done
}

# ── 1. Quem já existe ────────────────────────────────────────────────────────
say "Contas que já existem"
lista=$(occ user:list)
if [ -z "$lista" ] || printf '%s' "$lista" | grep -qi "no users"; then
    warn "nenhuma conta cadastrada (instalação sem admin?)"
    vazio=1
else
    vazio=0
    printf '%s\n' "$lista" | sed 's/^- /      /; s/^/    /'
fi

echo
say "Grupos"
grupos=$(occ group:list)
if [ -n "$grupos" ]; then
    printf '%s\n' "$grupos" | sed 's/^- /      /; s/^/    /'
    admins=$(printf '%s' "$grupos" | grep -E '^- admin:' | sed 's/^- admin: *//')
    [ -n "$admins" ] && ok "administrador(es): $admins"
else
    warn "nenhum grupo"
fi

# ── 2. Sem nenhuma conta: cria a principal, que já é admin ───────────────────
if [ "$vazio" = 1 ]; then
    say "Criando a conta principal"
    read -rp '    nome de usuário (ex: anthero): ' uid
    [ -n "$uid" ] || die "nome vazio"
    read -rp '    nome de exibição (ex: Anthero): ' nome
    pedir_senha
    saida=$(OC_PASS="$SENHA" occ user:add --password-from-env -g admin \
        --display-name "${nome:-$uid}" "$uid")
    if [ $? -eq 0 ]; then
        ok "conta '$uid' criada como administrador"
    else
        warn "$(printf '%s' "$saida" | tail -3)"
        die "não deu para criar a conta"
    fi
    echo
    echo "    Entre em http://localhost, faça o primeiro acesso e pronto."
    exit 0
fi

# ── 3. Menu ──────────────────────────────────────────────────────────────────
say "O que você quer fazer?"
cat <<'EOF'
    1) redefinir a senha de uma conta que já existe
    2) criar uma conta nova
    3) tornar uma conta existente administradora
    4) só sair
EOF
read -rp '    escolha [1-4]: ' opcao

case "$opcao" in
    1)
        read -rp '    usuário: ' uid
        [ -n "$uid" ] || die "nome vazio"
        occ user:info "$uid" >/dev/null || die "usuário '$uid' não existe"
        pedir_senha
        if saida=$(OC_PASS="$SENHA" occ user:resetpassword --password-from-env "$uid"); then
            ok "senha de '$uid' trocada"
        else
            warn "$(printf '%s' "$saida" | tail -3)"
            die "não deu para trocar a senha"
        fi
        ;;
    2)
        read -rp '    nome de usuário: ' uid
        [ -n "$uid" ] || die "nome vazio"
        read -rp '    nome de exibição: ' nome
        pedir_senha
        if saida=$(OC_PASS="$SENHA" occ user:add --password-from-env \
               --display-name "${nome:-$uid}" "$uid"); then
            ok "conta '$uid' criada"
        else
            warn "$(printf '%s' "$saida" | tail -3)"
            die "não deu para criar (nome já existe?)"
        fi
        ;;
    3)
        read -rp '    usuário: ' uid
        [ -n "$uid" ] || die "nome vazio"
        if occ group:adduser admin "$uid" >/dev/null; then
            ok "'$uid' agora é administrador"
        else
            die "não deu — confira se o usuário existe"
        fi
        ;;
    *)
        echo "    nada feito."
        exit 0
        ;;
esac

# ── 4. Estado final ──────────────────────────────────────────────────────────
say "Como está agora"
occ user:list | sed 's/^- /      /; s/^/    /'
echo
echo "    Site:        http://localhost"
echo "    Usuários:    http://localhost/settings/users   (precisa estar logado)"
echo
echo "    Pela interface web você cria as demais contas sem usar o terminal."
