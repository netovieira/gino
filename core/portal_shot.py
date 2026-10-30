"""
Screenshot da tela inteira no GNOME/Wayland, pelo XDG Desktop Portal.

Por que este arquivo existe, e por que ele roda fora do venv:

- `mss` (o capturador padrão) fala X11. Numa sessão Wayland ele fotografa só o
  XWayland — as janelas nativas não aparecem, e o resultado é uma imagem preta
  de ~11 KB. Medido nesta máquina.
- `org.gnome.Shell.Screenshot` responde `Screenshot is not allowed` para
  qualquer cliente que não seja o próprio shell.
- O único caminho é `org.freedesktop.portal.Screenshot`, que pede autorização ao
  usuário na primeira vez (o GNOME lembra depois). O portal responde por sinal
  assíncrono, o que exige uma conexão D-Bus de verdade — e a única biblioteca
  D-Bus disponível aqui é o PyGObject do python do sistema, ausente no venv.

Uso:  python3 core/portal_shot.py <arquivo-de-saida.png> [timeout_segundos]
Sai com código 0 e o arquivo escrito, ou com código != 0 e o motivo no stderr.
"""
from __future__ import annotations

import os
import sys
import urllib.parse

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Screenshot"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("uso: portal_shot.py <saida.png> [timeout]", file=sys.stderr)
        return 2
    out_path = os.path.abspath(argv[1])
    timeout = float(argv[2]) if len(argv) > 2 else 30.0

    try:
        import gi
        gi.require_version("Gio", "2.0")      # noqa: F401  (importa para o nome)
        from gi.repository import Gio, GLib
    except Exception as e:
        print(f"sem PyGObject: {e}", file=sys.stderr)
        return 3

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception as e:
        print(f"sem barramento de sessão: {e}", file=sys.stderr)
        return 3

    token = f"jarvis{os.getpid()}"
    # interactive=False: the whole screen, now, with no area picker. The user
    # asked the assistant to look — making them drag a rectangle first is a
    # question they already answered. GNOME may still show its one-time
    # permission dialog; that is consent, not selection.
    opts = {"handle_token": GLib.Variant("s", token),
            "interactive": GLib.Variant("b", False)}
    try:
        ret = bus.call_sync(
            PORTAL_BUS, PORTAL_PATH, PORTAL_IFACE, "Screenshot",
            GLib.Variant("(sa{sv})", ("", opts)),
            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 10_000, None)
        request_path = ret.unpack()[0]
    except Exception as e:
        print(f"portal recusou a chamada: {e}", file=sys.stderr)
        return 4

    loop = GLib.MainLoop()
    result: dict = {}

    def _on_response(_conn, _sender, _path, _iface, _signal, params):
        code, results = params.unpack()
        result["code"] = code
        uri = (results or {}).get("uri")
        result["uri"] = str(uri) if uri else ""
        loop.quit()

    bus.signal_subscribe(PORTAL_BUS, "org.freedesktop.portal.Request",
                         "Response", request_path, None,
                         Gio.DBusSignalFlags.NONE, _on_response)
    GLib.timeout_add_seconds(int(max(5, timeout)), lambda: loop.quit())
    loop.run()

    if "code" not in result:
        print("sem resposta do portal (autorização não veio a tempo)", file=sys.stderr)
        return 5
    if result["code"] != 0:
        print(f"captura cancelada ou negada (código {result['code']})", file=sys.stderr)
        return 6
    uri = result.get("uri") or ""
    if not uri.startswith("file://"):
        print(f"resposta sem arquivo: {uri!r}", file=sys.stderr)
        return 7

    src = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        with open(out_path, "wb") as fh:
            fh.write(data)
        try:
            os.unlink(src)          # o portal deixa o arquivo em /run/user/...
        except OSError:
            pass
    except Exception as e:
        print(f"não consegui ler a captura: {e}", file=sys.stderr)
        return 8
    if len(data) < 1024:
        print(f"captura suspeita de vazia ({len(data)} bytes)", file=sys.stderr)
        return 9
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
