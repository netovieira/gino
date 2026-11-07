"""UV-5 — "tocar YouTube Music" abre o APP instalado (PWA), não uma aba do Chrome.

Nenhuma janela real é aberta aqui: o ``Popen`` do ``media_control`` é um
gravador de argv, o atalho ``.desktop`` é um arquivo em ``tmp_path`` (lido
pelo código de verdade) e o activity.log é um caminho temporário. A prova é
dupla — o comando que seria executado (``--app-id``, sem ``--new-window``) e a
linha registrada no log, que é o passo 5 da tarefa.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

import actions.media_control as mc
import actions.youtube_video as yv
import core.youtube_browser as ytb

APP_ID = "cinhimbnkkaeohfgghhklpknlkffjgod"
CHROME = "/usr/bin/google-chrome"

DESKTOP_YTM = f"""[Desktop Entry]
Version=1.0
Terminal=false
Type=Application
Name=YouTube Music
Exec={CHROME} --profile-directory=Default --app-id={APP_ID}
StartupWMClass=crx_{APP_ID}
"""

DESKTOP_OTHER = """[Desktop Entry]
Type=Application
Name=Outro Atalho
Exec=/usr/bin/google-chrome --profile-directory=Default --app-id=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz
"""


@pytest.fixture
def shortcut(tmp_path):
    """Um atalho .desktop do YouTube Music (e um de enfeite) em diretório limpo."""
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "chrome-ytm-Default.desktop").write_text(DESKTOP_YTM, encoding="utf-8")
    (apps / "chrome-outro-Default.desktop").write_text(DESKTOP_OTHER, encoding="utf-8")
    with mock.patch.object(mc, "_desktop_dirs", lambda: [str(apps)]):
        yield apps


@pytest.fixture
def no_shortcut(tmp_path):
    """Nenhum PWA instalado: o desfecho honesto é a janela do Chrome."""
    apps = tmp_path / "vazio"
    apps.mkdir()
    with mock.patch.object(mc, "_desktop_dirs", lambda: [str(apps)]):
        yield apps


@pytest.fixture
def popen_argv():
    """Grava o argv de cada Popen do media_control em vez de abrir janela."""
    calls: list[list[str]] = []

    def fake_popen(argv, **_kwargs):
        calls.append(list(argv))
        return object()

    with mock.patch.object(mc.subprocess, "Popen", fake_popen), \
            mock.patch.object(mc, "_chrome_bin", lambda: CHROME):
        yield calls


@pytest.fixture(autouse=True)
def nothing_playing():
    """Isolamento do bus: ``players()`` vazio e ``mpris_call`` é uma bomba.

    Vale para todo teste do arquivo — nada aqui consulta o barramento real da
    sessão do dono (que tem um player do próprio PWA no ar). Quem precisa de
    um player aberto reescreve o mock dentro do teste.
    """
    with mock.patch.object(mc, "players", lambda: []), \
            mock.patch.object(mc, "mpris_call",
                              mock.Mock(side_effect=AssertionError("MPRIS chamado"))):
        yield


@pytest.fixture(autouse=True)
def activity_log(tmp_path):
    """O activity.log da prova, temporário; nenhuma linha da corrida real.

    Autouse porque o registro é parte do caminho: sem isto cada teste deixaria
    uma linha de prova falsa no log que o dono grepa (22 linhas assim foram
    escritas na primeira versão deste arquivo).
    """
    path = tmp_path / "activity.log"
    with mock.patch.object(yv, "ACTIVITY_LOG_PATH", path):
        yield path


def _play(query: str, player=None) -> str:
    return yv.youtube_video({"action": "play", "query": query}, player=player)


# ── detecção: o pedido é o app? ─────────────────────────────────────────────

@pytest.mark.parametrize("frase, resto", [
    ("YouTube Music", ""),
    ("youtube music", ""),
    ("abre o youtube music", ""),
    ("toca o YouTube Music", ""),
    ("music.youtube.com", ""),
    ("yt music", ""),
    ("ytmusic", ""),
    ("youtube música", ""),
    ("music do youtube", ""),
    ("Music YouTube", ""),
    ("o youtube music pra mim agora", ""),
    ("youtube music lofi", "lofi"),
    ("toca youtube music de daft punk", "daft punk"),
])
def test_detecta_o_app_e_a_sobra(frase, resto):
    assert ytb.is_music_app(frase) is True
    assert ytb.music_remainder(frase) == resto


@pytest.mark.parametrize("frase", [
    "Daft Punk", "clipes de 2020", "playlist do spotify", "youtube", "musica",
    "o video do youtube music awards 2024", "clipe do youtube music",
    "show do youtube music ao vivo", "", "   ", None, 123, 0, [],
])
def test_nao_e_o_app(frase):
    assert ytb.is_music_app(frase) is False


def test_fora_do_app_a_frase_volta_integra():
    """Sem o nome do app não há o que cortar: o título fica como o dono pediu."""
    assert ytb.music_remainder("Daft Punk") == "Daft Punk"


# ── o pedido do app sai da janela controlada e vai para o PWA ───────────────

def test_app_abre_no_pwa_e_nao_em_aba_do_chrome(shortcut, popen_argv, nothing_playing,
                                                activity_log):
    with mock.patch.object(ytb, "play",
                           mock.Mock(side_effect=AssertionError("janela controlada"))) as win:
        resposta = _play("YouTube Music")

    assert "app instalado" in resposta
    assert win.call_args_list == []
    assert len(popen_argv) == 1
    argv = popen_argv[0]
    assert argv[0] == CHROME
    assert f"--app-id={APP_ID}" in argv
    assert "--profile-directory=Default" in argv
    # A aba do Chrome é exatamente o que a métrica recusa.
    assert "--new-window" not in argv
    assert not any("music.youtube.com/search" in part for part in argv)


def test_query_toca_dentro_do_app(shortcut, popen_argv, activity_log):
    """Com sobra no pedido, o vídeo abre DENTRO do app, pela URL de atalho."""
    watch_id = "FGBhQbmPwH8"
    fake = mock.Mock()
    fake.get.return_value = mock.Mock(text=f'{{"videoId":"{watch_id}"}}')
    with mock.patch.object(mc, "requests", fake), \
            mock.patch.object(mc, "_await_playback", return_value=True) as nudge:
        resposta = _play("youtube music daft punk")

    assert "app do YouTube Music" in resposta
    assert nudge.call_args_list
    argv = popen_argv[0]
    assert f"--app-id={APP_ID}" in argv
    assert f"--app-launch-url-for-shortcuts-menu-item=https://music.youtube.com" \
           f"/watch?v={watch_id}" in argv
    assert "--new-window" not in argv


def test_player_ja_aberto_e_retomado_pelo_mpris(shortcut, popen_argv):
    """Com o app já no ar (pausado), tocar = Play no MPRIS, sem nova janela."""
    player = "org.mpris.MediaPlayer2.chromium.instance107133"
    with mock.patch.object(mc, "players", lambda: [player]), \
            mock.patch.object(mc, "mpris_call", return_value=(True, f"Play enviado")) as call:
        resposta = _play("YouTube Music")

    assert "Play enviado" in resposta
    assert call.call_args_list[0].args == (player, "Play")
    assert popen_argv == []


def test_sem_pwa_instalado_a_resposta_e_honesta(no_shortcut, popen_argv, activity_log):
    """Sem atalho instalado o desfecho é a janela do Chrome — e o log diz isso."""
    with mock.patch.object(mc, "_scrape_video_id", return_value=""):
        resposta = _play("YouTube Music")

    assert "na tela" in resposta
    assert popen_argv[0] == [CHROME, "--new-window", "https://music.youtube.com"]
    linha = activity_log.read_text(encoding="utf-8")
    assert "pwa=0" in linha
    assert "aberto=1" in linha
    assert "app_id=ausente" in linha


# ── prova material: o evento no activity.log (passo 5 da UV-5) ─────────────

def test_evento_registrado_no_activity_log(shortcut, popen_argv, nothing_playing,
                                           activity_log):
    _play("YouTube Music")

    linhas = activity_log.read_text(encoding="utf-8").splitlines()
    assert len(linhas) == 1
    linha = linhas[0]
    assert linha.startswith("[20") and "] [Media] youtube_music_pwa " in linha
    assert "origem=youtube_video" in linha
    assert "pwa=1" in linha
    assert f"app_id={APP_ID}" in linha
    assert "perfil=Default" in linha
    assert 'alvo="home"' in linha
    assert "aberto=1" in linha
    assert "YouTube Music aberto no app instalado" in linha


def test_evento_registra_a_busca_do_pedido(shortcut, popen_argv, activity_log):
    fake = mock.Mock()
    fake.get.return_value = mock.Mock(text='{"videoId":"FGBhQbmPwH8"}')
    with mock.patch.object(mc, "requests", fake), \
            mock.patch.object(mc, "_await_playback", return_value=True):
        _play("youtube music lofi")

    linha = activity_log.read_text(encoding="utf-8")
    assert 'alvo="lofi"' in linha
    assert "pwa=1" in linha
    assert "aberto=1" in linha


def test_falha_do_app_nao_vira_sucesso(shortcut, popen_argv, nothing_playing,
                                        activity_log):
    with mock.patch.object(mc.subprocess, "Popen",
                           mock.Mock(side_effect=OSError("sem display"))):
        resposta = _play("YouTube Music")

    assert "Não consegui" in resposta or "sem display" in resposta
    linha = activity_log.read_text(encoding="utf-8")
    assert "aberto=0" in linha
    assert "pwa=0" in linha


def test_registro_nao_quebra_a_resposta(shortcut, popen_argv, nothing_playing, tmp_path):
    """Activity.log inacessível: o dono recebe a resposta mesmo assim."""
    bloqueado = tmp_path / "bloqueado"
    bloqueado.write_text("sou um arquivo", encoding="utf-8")
    with mock.patch.object(yv, "ACTIVITY_LOG_PATH", bloqueado / "activity.log"):
        resposta = _play("YouTube Music")

    assert "app instalado" in resposta


def test_sem_pwa_conhecido_o_registro_usa_a_busca_real(no_shortcut, popen_argv,
                                                        activity_log):
    """O app_id vem do atalho de verdade: sem atalho, o campo diz 'ausente'."""
    with mock.patch.object(mc, "_scrape_video_id", return_value=""):
        _play("YouTube Music")

    assert f"app_id={APP_ID}" not in activity_log.read_text(encoding="utf-8")


# ── vídeo continua na janela controlada (sem regressão) ────────────────────

def test_video_nao_music_nao_passa_pelo_app(no_shortcut, popen_argv, activity_log):
    with mock.patch.object(ytb, "play", return_value="Daft Punk - Harder, Better") as win, \
            mock.patch.object(mc, "media_control",
                              mock.Mock(side_effect=AssertionError("media_control"))) as media:
        resposta = _play("daft punk")

    assert "Tocando no YouTube" in resposta
    assert win.call_args_list[0].args == ("daft punk",)
    assert media.call_args_list == []
    assert popen_argv == []
    assert not activity_log.exists()


def test_login_continua_na_janela_controlada():
    with mock.patch.object(ytb, "open_login",
                           return_value="Abri a janela do YouTube.") as login:
        out = yv.youtube_video({"action": "login"})

    assert out == "Abri a janela do YouTube."
    assert login.call_args_list


# ── contrato das descrições que roteiam o pedido ───────────────────────────

def test_tool_youtube_video_delegao_app():
    desc = yv.TOOL["description"]
    assert "media_control" in desc
    assert "PWA" in desc


def test_plugin_music_control_nunca_abre_app():
    from plugins import music_control as hud

    desc = hud.PLUGIN["description"]
    assert "media_control" in desc
    assert "never opens an app" in desc
    assert "YouTube Music" in desc


def test_handler_do_media_control_e_o_mesmo():
    """A delegação usa o handler público: mesmo caminho do pedido por voz."""
    assert mc.TOOL["handler"] is mc.media_control


def test_assinatura_do_registro_tem_todos_os_campos(shortcut, popen_argv,
                                                    nothing_playing):
    linha = yv._music_event_line(alvo="lofi", app_id=APP_ID, perfil="Default",
                                 pwa=True, aberto=True,
                                 resposta="um\ntexto    longo")
    for campo in ("youtube_music_pwa", "origem=youtube_video", "pwa=1",
                  f"app_id={APP_ID}", "perfil=Default", 'alvo="lofi"',
                  "aberto=1"):
        assert campo in linha
    assert "\n" not in linha
    assert "um texto longo" in linha


def test_registro_diz_pwa_zero_quando_so_ha_aba():
    """Sem atalho instalado não se pode escrever pwa=1 no log."""
    linha = yv._music_event_line(alvo="", app_id="", perfil="", pwa=False,
                                 aberto=True, resposta="aberto na tela")
    assert "pwa=0" in linha
    assert "app_id=ausente" in linha
    assert 'alvo="home"' in linha


def test_subprocess_do_media_control_nao_e_shell(shortcut, popen_argv, nothing_playing):
    """Prova de que o lançamento do app não passa por shell (lista de argv)."""
    chamadas: list[tuple[list[str], dict]] = []

    def registrar(argv, **kwargs):
        chamadas.append((list(argv), kwargs))
        return object()

    with mock.patch.object(mc.subprocess, "Popen", registrar):
        _play("YouTube Music")

    argv, kwargs = chamadas[0]
    assert isinstance(argv, list) and all(isinstance(parte, str) for parte in argv)
    assert kwargs.get("shell") is not True
    assert kwargs["stderr"] is subprocess.DEVNULL
