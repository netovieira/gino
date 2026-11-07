"""
YouTube numa janela de Chrome que o JARVIS controla.

Por que uma janela separada: o navegador padrão do usuário roda sem porta de
depuração, e a sessão é Wayland — não há como pausar ou pular faixa dentro do
Chrome que a pessoa já usa. Uma janela lançada por nós, com perfil próprio,
resolve as duas pontas: o vídeo toca numa tela de verdade, com a conta logada, e
o `play`/`pause`/`next` acontecem por automação.

CONTA
    O perfil é ~/.jarvis_profiles/youtube. O login do Google é feito UMA vez,
    dentro desta janela (ação `login` do youtube_video); os cookies ficam nesse
    diretório e valem nas próximas sessões. Não usamos o perfil do Chrome do
    usuário de propósito: o Chrome aberto trava o perfil, e o Playwright cairia
    num perfil vazio no meio do pedido.

TRANSPORTE
    O player é o do próprio YouTube: clicar em `.ytp-play-button` e
    `.ytp-next-button` é o mesmo que o dedo do usuário nesses botões — sem
    depender de teclas do sistema, que o Wayland bloqueia para outras janelas.

O APP DO YOUTUBE MUSIC NÃO É DESTA JANELA
    "toca o YouTube Music" nomeia o aplicativo instalado (o PWA que o Chrome
    lança com `--app-id`), não uma busca no site. Quem abre esse app é
    `actions/media_control.py`; as funções `is_music_app` / `music_remainder`
    abaixo existem só para o pedido sair daqui e ir para lá. Deixá-lo aqui
    seria abrir mais uma aba do Chrome — o que a validação UV-5 chama de
    "não é o app".
"""
from __future__ import annotations

import asyncio
import re
import threading
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

PROFILE_DIR = Path.home() / ".jarvis_profiles" / "youtube"
HOME_URL = "https://www.youtube.com/"
LOGIN_URL = ("https://accounts.google.com/ServiceLogin"
             "?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F")

_SEARCH_SEL = "ytd-video-renderer a#video-title"
_TITLE_SEL = "h1.ytd-watch-metadata yt-formatted-string, h1.title"
_PLAY_BTN = ".ytp-play-button"
_NEXT_BTN = ".ytp-next-button"
_AVATAR = "#avatar-btn, button#avatar-btn"

_CHROME_CANDIDATES = ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                      "/usr/bin/chromium", "/usr/bin/chromium-browser")


def _chrome_exe() -> Optional[str]:
    for p in _CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None


# ── o pedido é o APP do YouTube Music? ──────────────────────────────────────
#
# Palavras e formas que o STT devolve para o aplicativo instalado. A
# comparação é sem acento e sem caixa porque a mesma frase chega do microfone
# como "YouTube Music", "youtube música" ou "yt music".
_MUSIC_APP_WORDS = ("youtube music", "youtube musica", "youtube-musica",
                    "youtube-music", "you tube music", "yt music", "ytmusic",
                    "music youtube")
_MUSIC_APP_RE = re.compile(
    r"music\.?youtube\.com"
    r"|\byou\s*tube[\s\-_]*m[uú]sica?"
    r"|\byt[\s\-_]*m[uú]sica?"
    r"|\bm[uú]sica?\s+you\s*tube\b"
    r"|\bm[uú]sica?\s+do\s+youtube\b", re.I)
# Sobras que não fazem parte do nome da música: artigos, o verbo do pedido e
# os cortesia de sempre ("abre o youtube music" -> nada além do app).
_MUSIC_FILLER_RE = re.compile(
    r"\b(?:o|a|os|as|um|uma|the|app|aplicativo|site|plataforma|p[áa]gina|"
    r"abre|abrir|inicia|iniciar|roda|lanca|toca|tocar|play|p[oe]e|coloca|"
    r"bota|pra|pro|para|por|mim|agora|a[ií]|favor|meu|minha)\b", re.I)
# Preposição só faz barulho na sobra quando sobra sozinha nas pontas; no meio
# ela pode pertencer ao título ("música do filme X" fica como está).
_MUSIC_EDGE_PREP_RE = re.compile(
    r"^(?:\s*(?:de|do|da|dos|das|no|na|nos|nas)\b|\b(?:de|do|da|dos|das|"
    r"no|na|nos|nas)\s*$)", re.I)
# Quem pede um vídeo não pede o aplicativo: "o clipe do YouTube Music Awards"
# volta para a janela controlada, onde o vídeo de fato toca.
_NOT_APP_RE = re.compile(
    r"\b(?:video|videos|clipe|clipes|show|shows|concerto|festival|awards|"
    r"premiacao|trailer|documentario|entrevista|coletanea|compilado|"
    r"novidades|review|tutorial)\b")


def _plain(text: object) -> str:
    """Minúsculas, sem acento e com espaços únicos — o formato da comparação."""
    raw = unicodedata.normalize("NFKD", str(text if text is not None else ""))
    stripped = "".join(c for c in raw if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def is_music_app(query: object) -> bool:
    """True quando o pedido nomeia o app do YouTube Music (o PWA instalado).

    "youtube music", "abre o youtube music", "music.youtube.com" -> o app.
    "daft punk", "clipe de 2020", "o clipe do YouTube Music Awards" -> não é o
    app: continua no YouTube de vídeo, na janela que esta página controla.
    """
    n = _plain(query)
    if not n or _NOT_APP_RE.search(n):
        return False
    if "music.youtube.com" in n:
        return True
    if _MUSIC_APP_RE.search(str(query if query is not None else "")):
        return True
    return any(word in n for word in _MUSIC_APP_WORDS)


def music_remainder(query: object) -> str:
    """O resto do pedido sem o nome do app ("youtube music lofi" -> "lofi").

    Vazio quando o dono só quer o aplicativo aberto: é o sinal para quem lança
    o PWA abrir a home (ou retomar o player que já está no ar) em vez de
    buscar a palavra "music" como se fosse música.

    Fora do caso "app" a frase volta como veio: nada de cortar palavra de um
    título que o dono pediu ("Daft Punk" continua "Daft Punk").
    """
    raw = str(query if query is not None else "")
    if not is_music_app(raw):
        return " ".join(raw.split())
    rest = " ".join(_MUSIC_APP_RE.sub(" ", raw).split())
    # Artigos e cortesia em voltas até a frase parar de encolher: "abre o
    # youtube music pra mim agora" só fica limpo na segunda passada.
    for _ in range(4):
        nxt = " ".join(_MUSIC_EDGE_PREP_RE.sub(
            " ", _MUSIC_FILLER_RE.sub(" ", rest)).split())
        if nxt == rest:
            break
        rest = nxt
    return rest


class _Session:
    """One Playwright Chrome, living on its own thread.

    Everything Playwright is bound to the event loop that created it, and the
    calls arrive from tool threads and from the Qt thread. A dedicated loop is
    the smallest way to keep that honest without turning the whole app async.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._pw = None
        self._ctx = None
        self._page = None

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="yt-browser")
        self._thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def call(self, coro, timeout: float = 60.0):
        self._ensure_thread()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def _launch(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError("playwright não instalado — rode "
                               "'.venv/bin/pip install playwright'") from e
        exe = _chrome_exe()
        if not exe:
            raise RuntimeError("nenhum Chrome/Chromium encontrado no sistema")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        if self._pw is None:
            self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            executable_path=exe,
            headless=False,
            viewport=None,
            ignore_default_args=["--enable-automation"],
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check",
                  "--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        print(f"[YouTube] janela pronta (perfil {PROFILE_DIR})")
        return self._page

    def is_open(self) -> bool:
        try:
            return bool(self._page is not None and not self._page.is_closed())
        except Exception:
            return False

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self._loop).result(20)
        except Exception as e:
            print(f"[YouTube] close falhou: {e}")

    async def _close(self) -> None:
        try:
            if self._ctx is not None:
                await self._ctx.close()
        finally:
            self._ctx = None
            self._page = None


_S = _Session()


# ── helpers ─────────────────────────────────────────────────────────────────
async def _dismiss_consent(page) -> None:
    """The cookie/consent wall appears once per fresh profile."""
    for sel in ("button[aria-label*='Aceitar']", "button[aria-label*='Accept']",
                "tp-yt-paper-button[aria-label*='Aceitar']",
                "button:has-text('Aceitar tudo')", "button:has-text('Accept all')"):
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=2500)
                await page.wait_for_timeout(600)
                return
        except Exception:
            continue


async def _signed_in(page) -> bool:
    try:
        return bool(await page.locator(_AVATAR).first.count())
    except Exception:
        return False


async def _is_playing(page) -> bool:
    """Ask the <video> element, not the button's label.

    The button's tooltip is localised and flips a beat after the click, which
    made the status read "paused" while a video was clearly playing.
    """
    try:
        return bool(await page.evaluate(
            "() => { const v = document.querySelector('video');"
            " return !!v && !v.paused && !v.ended; }"))
    except Exception:
        return False


async def _now_playing(page) -> dict:
    """Title + whether the player is running, read from the page itself."""
    out = {"title": "", "playing": False, "url": ""}
    try:
        out["url"] = page.url or ""
    except Exception:
        return out
    try:
        t = page.locator(_TITLE_SEL).first
        if await t.count():
            out["title"] = (await t.inner_text()).strip()
    except Exception:
        pass
    if not out["title"]:
        try:
            out["title"] = (await page.title()).replace(" - YouTube", "").strip()
        except Exception:
            pass
    out["playing"] = await _is_playing(page)
    return out


# ── public API (blocking, safe from any thread) ─────────────────────────────
def open_login() -> str:
    """Open the window at Google's sign-in page; the session is kept in the profile."""
    async def _go():
        page = await _S._launch()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        return page
    try:
        _S.call(_go(), timeout=90)
        return ("Abri a janela do YouTube. Faça o login do Google nela — "
                "fica salvo para as próximas vezes. Me avise quando terminar.")
    except Exception as e:
        return f"Não consegui abrir a janela do YouTube: {e}"


def play(query: str) -> str:
    """Search on YouTube and play the first result. Returns the title, or ''."""
    q = str(query or "").strip()
    if not q:
        return ""

    async def _play():
        page = await _S._launch()
        if "youtube.com" not in (page.url or ""):
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            await _dismiss_consent(page)
        await page.goto(f"{HOME_URL}results?search_query={quote_plus(q)}",
                        wait_until="domcontentloaded", timeout=60000)
        await _dismiss_consent(page)
        try:
            await page.wait_for_selector(_SEARCH_SEL, timeout=20000)
        except Exception:
            return ""
        link = page.locator(_SEARCH_SEL).first
        title = ""
        try:
            title = (await link.get_attribute("title") or "").strip()
        except Exception:
            pass
        await link.click()
        # The watch page paints the real title a beat later; the search card's
        # title is the fallback because it is already the right video.
        try:
            await page.wait_for_selector(_PLAY_BTN, timeout=20000)
            await page.wait_for_timeout(1200)
            if not await _is_playing(page):
                # YouTube refuses to autoplay on a profile with no interaction
                # history. A click on the play button is a real user gesture,
                # which is exactly what the browser is waiting for.
                try:
                    await page.locator(_PLAY_BTN).first.click(timeout=5000)
                    await page.wait_for_timeout(900)
                except Exception:
                    pass
        except Exception:
            pass
        info = await _now_playing(page)
        return info.get("title") or title

    try:
        return _S.call(_play(), timeout=120)
    except Exception as e:
        print(f"[YouTube] play falhou: {e}")
        return ""


def pause() -> str:
    async def _p():
        page = await _S._launch()
        info = await _now_playing(page)
        if not info["playing"]:
            return "já pausado"
        await page.locator(_PLAY_BTN).first.click(timeout=8000)
        return "pausado"
    try:
        return _S.call(_p(), timeout=40)
    except Exception as e:
        return f"falhou: {e}"


def resume() -> str:
    async def _p():
        page = await _S._launch()
        info = await _now_playing(page)
        if info["playing"]:
            return "já tocando"
        await page.locator(_PLAY_BTN).first.click(timeout=8000)
        return "tocando"
    try:
        return _S.call(_p(), timeout=40)
    except Exception as e:
        return f"falhou: {e}"


def next_track() -> str:
    async def _p():
        page = await _S._launch()
        btn = page.locator(_NEXT_BTN).first
        if not await btn.count():
            return "sem próxima"
        await btn.click(timeout=8000)
        await page.wait_for_timeout(1500)
        return "próxima"
    try:
        return _S.call(_p(), timeout=40)
    except Exception as e:
        return f"falhou: {e}"


def status() -> dict:
    """{open, title, playing, url} — cheap enough to call before every command."""
    if not _S.is_open():
        return {"open": False, "title": "", "playing": False, "url": ""}
    try:
        info = _S.call(_now_playing(_S._page), timeout=20)
        info["open"] = True
        return info
    except Exception:
        return {"open": False, "title": "", "playing": False, "url": ""}


def window_open() -> bool:
    return _S.is_open()


def signed_in() -> bool:
    """True when the controlled window carries a Google session.

    Read from the avatar button on the YouTube page — present only once an
    account is active. Used to tell the user the truth about whether what they
    asked for (music on *their* account) actually happened.
    """
    if not _S.is_open():
        return False
    try:
        return bool(_S.call(_signed_in(_S._page), timeout=20))
    except Exception:
        return False


def close() -> None:
    _S.close()
