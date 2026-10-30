"""
O que está aberto nesta máquina — de verdade, sem adivinhar.

Duas tentativas anteriores foram descartadas por medição, não por opinião:

- `gdbus`/barramento de sessão lista TODO serviço que registra um nome
  (`Keyring`, `ScreenSaver`, `Dconf`, `Instance14306`); separar app de serviço
  por nome é adivinhação e enchia a resposta de lixo.
- `wmctrl`/`xdotool` não existem aqui e, no Wayland do GNOME, não veriam as
  janelas nativas de qualquer forma.

O que sobra é o que sempre esteve certo: **processos**. Um app aberto é um
processo rodando, e a lista curada abaixo diz quais nomes de processo são apps
que uma pessoa reconhece. Curto e correto vence longo e meio errado.
"""
from __future__ import annotations

# Executável → nome que uma pessoa reconhece. Só o que aparece aqui é citado:
# varrer /proc e adivinhar nomes enchia a resposta de helpers do sistema.
_APPS = {
    # O binário do Chrome no Linux chama-se só "chrome" (e são ~15 processos
    # dele): sem esta linha, o navegador mais usado da máquina não aparecia.
    "chrome": "Chrome", "google-chrome": "Chrome", "google-chrome-stable": "Chrome",
    "chromium": "Chromium", "chromium-browser": "Chromium",
    "firefox": "Firefox", "brave": "Brave", "microsoft-edge": "Edge",
    "opera": "Opera", "vivaldi": "Vivaldi",
    "code": "VS Code", "code-insiders": "VS Code", "cursor": "Cursor",
    "zed": "Zed", "sublime_text": "Sublime Text", "pycharm": "PyCharm",
    "idea": "IntelliJ", "android-studio": "Android Studio",
    "nautilus": "Arquivos", "thunar": "Arquivos", "dolphin": "Arquivos",
    "nemo": "Arquivos",
    "thunderbird": "Thunderbird", "evolution": "Evolution",
    "spotify": "Spotify", "discord": "Discord", "slack": "Slack",
    "telegram-desktop": "Telegram", "telegram": "Telegram",
    "whatsapp": "WhatsApp", "signal-desktop": "Signal",
    "zoom": "Zoom", "teams": "Teams", "skype": "Skype",
    "vlc": "VLC", "mpv": "mpv", "totem": "Vídeos", "rhythmbox": "Rhythmbox",
    "audacity": "Audacity", "obs": "OBS",
    "gnome-terminal": "Terminal", "konsole": "Terminal", "tilix": "Terminal",
    "alacritty": "Terminal", "kitty": "Terminal", "xterm": "Terminal",
    "gnome-text-editor": "Textos", "gedit": "Textos", "kate": "Kate",
    "libreoffice": "LibreOffice", "soffice": "LibreOffice",
    "onlyoffice": "OnlyOffice", "wps": "WPS",
    "gimp": "GIMP", "inkscape": "Inkscape", "krita": "Krita",
    "blender": "Blender", "darktable": "Darktable", "shotwell": "Shotwell",
    "gnome-calculator": "Calculadora",
}
# Deliberadamente fora da lista: gnome-calendar e evolution rodam como serviço
# de segundo plano nesta máquina (alarme/notificação) e apareciam como "abertos"
# sem janela nenhuma. Um app só entra aqui se, aberto, tem janela de verdade.

# Nunca cite o próprio assistente nem o navegador que ele controla por conta.
_SELF_MARKERS = ("jarvis", "main.py", "jarvis_profiles")


def open_apps(limit: int = 14) -> list[str]:
    """Apps gráficos abertos agora, em português, deduplicados e ordenados."""
    names: list[str] = []
    seen: set[str] = set()
    try:
        import psutil
    except Exception:
        return []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            exe = (proc.info.get("name") or "").strip()
            cmd = " ".join(proc.info.get("cmdline") or [])
            if cmd and any(m in cmd for m in _SELF_MARKERS):
                continue
            label = _APPS.get(exe)
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(label)
        except Exception:
            continue
    names.sort(key=str.lower)
    return names[:limit]
