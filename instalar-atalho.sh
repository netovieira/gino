#!/usr/bin/env bash
# instalar-atalho.sh — põe o JARVIS no menu de aplicativos do GNOME.
#
# Depois disto, digitar "Jarvis" na busca do GNOME (tecla Super) mostra ele como
# se fosse um app, com ícone. Clicar abre na bandeja; se já estiver rodando,
# apenas avisa que ele está lá.
#
# Rodar:  bash instalar-atalho.sh
# Tirar:  bash instalar-atalho.sh remover

set -uo pipefail

PROJ="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/applications/jarvis.desktop"
ICONES="$HOME/.local/share/icons/hicolor"
ICO="$PROJ/config/jarvis.ico"

say()  { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '    \033[33matenção\033[0m %s\n' "$*"; }
die()  { printf '    \033[31merro\033[0m   %s\n' "$*"; exit 1; }

# ── Remoção ──────────────────────────────────────────────────────────────────
if [ "${1:-}" = "remover" ]; then
    say "Removendo o atalho"
    rm -f "$APP"
    ok "atalho removido"
    for s in 16 22 24 32 48 64 128 256; do
        rm -f "$ICONES/${s}x${s}/apps/jarvis.png"
    done
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$HOME/.local/share/applications" 2>/dev/null
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -f -t "$ICONES" 2>/dev/null
    ok "ícones removidos — o JARVIS continua instalado em $PROJ"
    exit 0
fi

[ -f "$ICO" ] || die "não achei o ícone em $ICO"
[ -f "$PROJ/run-jarvis.sh" ] || die "não achei $PROJ/run-jarvis.sh"

# ── 1. Permissões dos lançadores ─────────────────────────────────────────────
say "Preparando os lançadores"
[ -f "$PROJ/jarvis-abrir.sh" ] || die "não achei $PROJ/jarvis-abrir.sh"
chmod +x "$PROJ/jarvis-abrir.sh" "$PROJ/run-jarvis.sh"
ok "jarvis-abrir.sh e run-jarvis.sh executáveis"

# ── 2. Ícone: extrai cada tamanho do .ico para o tema hicolor ────────────────
# O GNOME não lê .ico para ícone de aplicativo de forma confiável; PNG por
# tamanho no tema hicolor é o caminho garantido.
say "Instalando o ícone"
# O python de sistema tem PIL e PyGObject (gi); o venv tem PIL mas não tem gi.
PY="$(command -v python3)"
[ -n "$PY" ] || PY="$PROJ/.venv/bin/python"
[ -n "$PY" ] || die "não achei python para converter o ícone"
"$PY" -c "import PIL" 2>/dev/null || PY="$PROJ/.venv/bin/python"

"$PY" - "$ICO" "$ICONES" <<'PYTHON' || die "falhou ao converter/instalar o ícone"
import os, sys
from PIL import Image

ico_path, dest = sys.argv[1], sys.argv[2]
ico = Image.open(ico_path)
disponiveis = sorted(ico.ico.sizes()) if hasattr(ico, "ico") else [ico.size]

for alvo in (16, 22, 24, 32, 48, 64, 128, 256):
    menores = [s for s in disponiveis if s[0] <= alvo]
    origem = menores[-1] if menores else disponiveis[0]
    img = ico.ico.getimage(origem) if hasattr(ico, "ico") else ico
    img = img.convert("RGBA")
    if img.size != (alvo, alvo):
        img = img.resize((alvo, alvo), Image.LANCZOS)
    pasta = os.path.join(dest, f"{alvo}x{alvo}", "apps")
    os.makedirs(pasta, exist_ok=True)
    img.save(os.path.join(pasta, "jarvis.png"), "PNG")
    print(f"    {alvo}x{alvo} <- {origem[0]}x{origem[1]}")
PYTHON
ok "ícones em $ICONES"

# ── 3. O atalho em si ────────────────────────────────────────────────────────
say "Criando o atalho"
mkdir -p "$(dirname "$APP")"
cat > "$APP" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=JARVIS
GenericName=Assistente de voz
Comment=Assistente pessoal por voz: fica na bandeja e responde quando você diz "Jarvis"
Keywords=jarvis;assistente;assistant;voz;ia;ai;microfone;
Exec=$PROJ/jarvis-abrir.sh
Path=$PROJ
Icon=jarvis
Terminal=false
StartupNotify=false
Categories=Utility;AudioVideo;
X-GNOME-UsesNotifications=true
DESKTOP
ok "$APP"

# ── 4. Avisa o GNOME para recarregar ─────────────────────────────────────────
say "Atualizando os caches do GNOME"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null
    ok "banco de aplicativos atualizado"
else
    warn "update-desktop-database não existe — o GNOME lê a pasta sozinho, sigo"
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$ICONES" 2>/dev/null \
        && ok "cache de ícones atualizado" \
        || warn "cache de ícones não atualizou (não costuma atrapalhar)"
fi

if command -v desktop-file-validate >/dev/null 2>&1; then
    if erro=$(desktop-file-validate "$APP" 2>&1); then
        ok "atalho válido"
    else
        warn "o validador reclamou:"
        printf '%s\n' "$erro" | sed 's/^/      /'
    fi
fi

# ── 5. Teste final: o ícone é resolvido pelo tema? ───────────────────────────
say "Testando"
if "$PY" - <<'PYTHON' 2>/dev/null
import sys
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
tema = Gtk.IconTheme.new()
sys.exit(0 if tema.has_icon("jarvis") else 1)
PYTHON
then
    ok "o tema enxerga o ícone 'jarvis'"
else
    warn "não deu para testar pela API do GTK (o atalho funciona igual)"
fi

echo
echo "    Abra a busca do GNOME (tecla Super) e digite: Jarvis"
echo "    Pode levar alguns segundos na primeira vez — depois fica imediato."
