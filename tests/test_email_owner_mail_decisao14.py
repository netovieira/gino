"""DECISÃO 14 do dono (10/11/2026): o e-mail DELE com a senha é pedido E
aprovação no mesmo e-mail — e o VALOR da senha não aparece em lugar nenhum.

A lei, na palavra dele: "se eu te mandar um e-mail pedindo para você me
retornar esse e-mail e no corpo dele tiver a senha ou no assunto, você já tem
que entender que foi solicitado e aprovado no mesmo e-mail e me mandar a
resposta". Paridade de canal: por onde ele pode pedir, por aí ele pode aprovar
— e-mail junto com WhatsApp, voz local, texto da interface e voz da interface.
Corolário de UX (lei 4): toda recusa nomeia as DUAS portas.

O que estes testes provam, na ordem:
  1. contrato (``core.email.owner_mail_authorization``): assunto OU corpo dele
     com a senha = fator ``senha digitada``; o texto que sai já vem REDIGIDO;
  2. terceiro com o MESMO texto (inclusive a senha escrita por extenso) não
     autoriza nada — a porta do e-mail é o endereço dele;
  3. normalização de endereço: Gmail não distingue ponto nem sufixo ``+``;
  4. a forma digitada ``senha: X`` nunca fica crua no assunto gravado, de
     remetente nenhum;
  5. varredura real (``poll_once`` com IMAP falso): autoriza ANTES de qualquer
     regra de notificação e grava só o redigido no jsonl;
  6. PARIDADE: o e-mail dele chega ao PONTO ÚNICO da senha digitada
     (``main._owner_password_grant``) e abre a MESMA janela que a senha aberta
     no app ou no WhatsApp dele abre (actiongate e rootgate reais);
  7. NÃO VAZAMENTO (a segurança absoluta dele): com um valor-sentinela falso
     no assunto e no corpo, a sentinela não aparece em NENHUMA superfície de
     saída — log do módulo, log da UI (activity), fala/TTS, turno entregue ao
     cérebro (memória), texto do e-mail de resposta, nota escrita no cofre e
     jsonl do inbox;
  8. toda recusa do portão de e-mail nomeia as duas portas, no mesmo pé;
  9. isolamento: o cofre real (config/api_keys.json) e os rastros de e-mail em
     memory/ não mudam um byte.

Nada aqui fala com a rede nem com o cofre do dono: IMAP e SMTP são fakes, a
configuração vive em memória, o vault é ``tmp_path``. ``_SENTINELA`` é um
segredo de teste e jamais deve aparecer em log, relatório ou commit — só o
placeholder circula nas asserções.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import main
from actions import root_exec
from core import actiongate, owner_auth, obsidian, rootgate
from core import email as mail
from memory import config_manager

# Segredo de teste — placeholder nos relatórios; jamais logar o valor.
_SENTINELA = "senha-de-teste-NAO-LOGAR-3f7d"

_DONO = "eu@gmail.com"
_TERCEIRO = "maria@example.com"
_REAL = mail._BASE


# ── fakes de rede ───────────────────────────────────────────────────────────

def _msg(sender: str, subject: str, body: str = "",
         message_id: str = "<m1@example.com>") -> bytes:
    """Uma mensagem inteira: cabeçalho + corpo, como o servidor devolve."""
    return (f"From: {sender}\r\nSubject: {subject}\r\n"
            f"Date: Tue, 10 Nov 2026 10:00:00 -0300\r\n"
            f"Message-ID: {message_id}\r\n\r\n{body}\r\n").encode()


class _Conn:
    """Só o que core/email.py usa: select, uid(SEARCH/FETCH), logout."""

    def __init__(self, messages: dict[str, bytes] | None = None) -> None:
        self.messages: dict[str, bytes] = messages or {}
        self.readonly: bool | None = None
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.readonly = bool(readonly)
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(k.encode() for k in self.messages)]
        if command == "FETCH":
            key = str(args[0])
            raw = self.messages.get(key)
            if raw is None:
                return "OK", [None]
            return "OK", [(b"1 (UID " + key.encode() + b" BODY[] {%d}" % len(raw),
                           raw)]
        return "OK", [None]

    def logout(self):
        self.logged_out = True
        return "BYE", []


# ── os vasos de saída do dono, todos observados ─────────────────────────────

class _Vasos:
    """Cada superfície de saída do sistema vira uma lista desta rodada."""

    def __init__(self) -> None:
        self.log_modulo: list[str] = []          # core.email._log
        self.log_ui: list[str] = []              # ui.write_log → activity
        self.falas: list[str] = []               # _say_now → TTS
        self.turnos: list[str] = []              # _inject_user_turn → memória
        self.notas: list[tuple[str, str]] = []   # obsidian.write_note → cofre
        self.envios: list[tuple[str, str, str]] = []   # mail.send_now → e-mail

    def despejo(self) -> str:
        tudo: list[str] = []
        tudo += self.log_modulo
        tudo += self.log_ui
        tudo += self.falas
        tudo += self.turnos
        tudo += [f"{titulo} {corpo}" for (titulo, corpo) in self.notas]
        for destino, assunto, corpo in self.envios:
            tudo.append(f"{destino}\n{assunto}\n{corpo}")
        return "\n".join(str(item) for item in tudo)


def _live_obj(vasos: _Vasos):
    """O JarvisLive nu, no molde dos testes de senha já existentes."""
    obj = object.__new__(main.JarvisLive)
    obj._owner_text_until = 0.0
    obj._owner_text_channel = ""
    obj._owner_text_fails = 0
    obj._owner_text_locked_until = 0.0
    obj._voice_bootstrap_until = 0.0
    obj._voice_bootstrap_by = ""

    class _UI:
        muted = False

        def write_log(self, text):
            vasos.log_ui.append(str(text))

    obj.ui = _UI()
    obj._say_now = lambda text, **kw: vasos.falas.append(str(text))
    obj._arm_job_grant = lambda factor=owner_auth.FACTOR_TEXT: 0.0
    obj._run_pending_root = lambda: None
    obj._restart = None
    obj._restart_enabled = False
    obj._power = None

    def _inject(text: str) -> bool:
        vasos.turnos.append(str(text))
        return True

    obj._inject_user_turn = _inject
    return obj


@pytest.fixture()
def correio(tmp_path, monkeypatch):
    """Config em memória, rastreio em tmp, cofre falso: nada toca o dono.

    ``vasos`` collects every output surface; ``caixa`` is the fake IMAP the
    sweep reads.
    """
    data: dict[str, dict] = {}

    def get_cfg(namespace):
        return dict(data.get(namespace, {}))

    def save_cfg(namespace, values):
        data.setdefault(namespace, {}).update(values)

    monkeypatch.setattr(config_manager, "get_plugin_config", get_cfg)
    monkeypatch.setattr(config_manager, "save_plugin_config", save_cfg)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE",
                        tmp_path / "api_keys.json")

    # A conta é construída aqui dentro: os caminhos saem de tmp, nunca do dono.
    monkeypatch.setattr(mail, "_STATE_ROOT", tmp_path / "config" / "mail")
    monkeypatch.setattr(mail, "_MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(mail, "PENDING_PATH",
                        tmp_path / "memory" / "mail_send_pending.json")
    mail.reset()

    # cofre falso: a nota sai de verdade, mas em tmp_path
    monkeypatch.setattr(obsidian, "VAULT", tmp_path / "Jarvis")
    monkeypatch.setattr(obsidian, "PLANS_DIR", tmp_path / "qoder" / "plans")
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(obsidian, "NEXTCLOUD_DIR", tmp_path / "Nextcloud")
    monkeypatch.delenv("JARVIS_VAULT_NAME_POLICY", raising=False)

    # O segredo de teste: só a sentinela confere (nada de PBKDF2 real aqui).
    monkeypatch.setattr(owner_auth, "is_configured", lambda: True)
    monkeypatch.setattr(owner_auth, "verify", lambda p: p == _SENTINELA)

    vasos = _Vasos()
    monkeypatch.setattr(mail, "_log",
                        lambda msg, *a, **k: vasos.log_modulo.append(str(msg)))

    _nota_real = obsidian.write_note

    def _nota_espiã(title, body, folder="Planos", tipo=""):
        vasos.notas.append((str(title), str(body)))
        return _nota_real(title, body, folder=folder, tipo=tipo)

    monkeypatch.setattr(obsidian, "write_note", _nota_espiã)

    def _send_now(to, subject, body, account=None):
        vasos.envios.append((str(to), str(subject), str(body)))
        return True, "enviado (fake)"

    monkeypatch.setattr(mail, "send_now", _send_now)
    # O grant confirma envio estacionado; aqui não há nenhum, e nada de SMTP.
    monkeypatch.setattr(mail, "pending_send", lambda: None)

    mail.add_account("pessoal", _DONO, provider="gmail",
                     app_password_value="senha-de-app-falsa-NAO-LOGAR")

    caixa = _Conn()
    monkeypatch.setattr(mail, "_imap_open", lambda _acct: caixa)

    actiongate.close()
    rootgate.close()
    root_exec.clear_pending()
    yield vasos, caixa
    actiongate.close()
    rootgate.close()
    root_exec.clear_pending()
    mail.reset()


def _varrer(caixa: _Conn, autorizador=None) -> None:
    """A varredura real, com o gancho do app plugado como no boot."""
    mail.set_owner_mail_hook(autorizador)
    mail.poll_once(hook=lambda item, *a: None)
    assert caixa.readonly is True, "a varredura abriu a caixa em modo escrita"


def _itens_inbox() -> list[dict]:
    """O que ficou gravado nos jsonl de cabeçalhos desta rodada."""
    base = Path(mail._MEMORY_DIR)
    out: list[dict] = []
    for arquivo in sorted(base.glob("*.jsonl")) if base.exists() else []:
        for linha in arquivo.read_text(encoding="utf-8").splitlines():
            if linha.strip():
                out.append(json.loads(linha))
    return out


def _rastro_inbox() -> str:
    return "\n".join(json.dumps(item, ensure_ascii=False)
                     for item in _itens_inbox())


# ── 1-4. o contrato do e-mail que pede e aprova ─────────────────────────────

def test_assunto_do_dono_com_senha_autoriza_e_vem_redigido(correio):
    item = {"from": _DONO, "subject": f"retorne este e-mail senha: {_SENTINELA}",
            "sender": "Eu"}
    got = mail.owner_mail_authorization(item, body="")
    assert got["factor"] == owner_auth.FACTOR_TEXT
    assert _SENTINELA not in json.dumps(got, ensure_ascii=False)
    assert owner_auth.REDACTED in got["subject"]
    assert "retorne este e-mail" in got["instruction"]


def test_corpo_do_dono_com_senha_autoriza_e_nao_devolve_o_valor(correio):
    item = {"from": _DONO, "subject": "retorne este e-mail"}
    got = mail.owner_mail_authorization(item, body=f"por favor\n{_SENTINELA}\n")
    assert got["factor"] == owner_auth.FACTOR_TEXT
    assert _SENTINELA not in json.dumps(got, ensure_ascii=False)
    assert owner_auth.REDACTED in got["instruction"]


def test_terceiro_com_a_mesma_frase_nao_autoriza_nada(correio):
    """A porta do e-mail é o endereço dele: terceiro com a senha escrita por
    extenso não passa daqui (nem com o mesmo pedido de resposta)."""
    item = {"from": _TERCEIRO, "subject": f"retorne este e-mail {_SENTINELA}"}
    got = mail.owner_mail_authorization(item, body=f"{_SENTINELA}")
    assert got["factor"] == ""
    assert got["instruction"] == ""
    assert mail.is_owner_mail(item) is False


def test_endereco_do_dono_normalizado_ponto_e_mais(correio):
    """Gmail não distingue ponto nem sufixo ``+``: a conta dele é uma só."""
    assert mail._addr_key("E.u+trabalho@gmail.com") == mail._addr_key(_DONO)
    assert mail._addr_key("eu.test@gmail.com") != mail._addr_key(_DONO)
    # fora do Google o ponto é parte do endereço de verdade:
    assert mail._addr_key("eu@test.com") != mail._addr_key("e.u@test.com")
    assert mail._addr_key(_DONO) in mail.owner_addresses()
    # terceiro que escreve com o mesmo endereço "parecido" não entra:
    assert mail._addr_key(_TERCEIRO) not in mail.owner_addresses()
    assert mail._addr_key("eu+outracoisa@outrigger.com") not in (
        mail.owner_addresses())


def test_owner_addresses_sao_os_enderecos_das_contas(correio):
    assert mail._addr_key(_DONO) in mail.owner_addresses()


def test_forma_digitada_nunca_fica_crua_no_assunto_gravado(correio):
    """``_header_item`` redige a forma ``senha: X`` de QUALQUER remetente."""
    acct = list(mail._accounts.values())[0]
    item = mail._header_item(acct, "9",
                             _msg(_TERCEIRO, f"aviso senha: {_SENTINELA}"))
    assert _SENTINELA not in json.dumps(item, ensure_ascii=False)
    assert owner_auth.REDACTED in item["subject"]


# ── 5. a varredura autoriza e não deixa o valor em disco nem no log ─────────

def test_varredura_autoriza_e_grava_so_o_redigido(correio):
    vaso, caixa = correio
    caixa.messages = {
        "1": _msg(_DONO, f"retorne este e-mail senha: {_SENTINELA}",
                  body=f"preciso da resposta\n{_SENTINELA}\n",
                  message_id="<dono@example.com>"),
    }
    aprovados: list[dict] = []
    _varrer(caixa, autorizador=aprovados.append)

    assert len(aprovados) == 1
    assert aprovados[0]["factor"] == owner_auth.FACTOR_TEXT
    assert _SENTINELA not in json.dumps(aprovados[0], ensure_ascii=False)

    # o registro em disco guarda o veredito, nunca o valor
    gravado = _itens_inbox()
    assert len(gravado) == 1
    assert gravado[0].get("owner_factor") == owner_auth.FACTOR_TEXT
    assert owner_auth.REDACTED in gravado[0]["subject"]
    assert _SENTINELA not in _rastro_inbox()

    # o log da varredura diz o veredito e o nome do fator, nunca o valor
    juntos = "\n".join(vaso.log_modulo)
    assert "e-mail do dono com a senha" in juntos
    assert owner_auth.FACTOR_TEXT in juntos
    assert _SENTINELA not in juntos


def test_autorizacao_nao_depende_de_regra_de_notificacao(correio):
    """Conta com ``notify`` desligado e fora de qualquer regra continua
    autorizando: a decisão 14 roda antes dos filtros e do aviso."""
    vaso, caixa = correio
    for acct in mail._accounts.values():
        acct.notify = False
        acct.filters = [{"field": "subject", "pattern": "boletim",
                         "match": "contains", "action": "ignora"}]
    caixa.messages = {
        "7": _msg(_DONO, f"retorne este e-mail {_SENTINELA}",
                  message_id="<dono2@example.com>"),
    }
    aprovados: list[dict] = []
    _varrer(caixa, autorizador=aprovados.append)

    assert len(aprovados) == 1
    assert aprovados[0]["factor"] == owner_auth.FACTOR_TEXT
    assert _SENTINELA not in json.dumps(aprovados[0], ensure_ascii=False)


def test_terceiro_na_varredura_nao_chama_o_gancho(correio):
    vaso, caixa = correio
    caixa.messages = {
        "3": _msg(_TERCEIRO, f"retorne este e-mail senha: {_SENTINELA}",
                  message_id="<maria@example.com>"),
    }
    aprovados: list[dict] = []
    _varrer(caixa, autorizador=aprovados.append)

    assert aprovados == []                    # não autoriza nada
    gravado = _itens_inbox()
    assert len(gravado) == 1                  # o e-mail entra no inbox
    assert "owner_factor" not in gravado[0]
    # e nem a forma digitada fica crua: ``senha: X`` é redigida de qualquer um
    assert _SENTINELA not in _rastro_inbox()


# ── 6. paridade de canal: o e-mail dele é o MESMO ponto da senha digitada ───

def test_email_do_dono_abre_a_mesma_janela_da_senha(correio):
    vaso, caixa = correio
    obj = _live_obj(vaso)
    caixa.messages = {
        "1": _msg(_DONO, f"retorne este e-mail senha: {_SENTINELA}",
                  message_id="<dono3@example.com>"),
    }
    assert actiongate.authorized() is False
    assert rootgate.authorized() is False

    _varrer(caixa, autorizador=obj._mail_owner_authorization)

    # o mesmo veredito pleno da senha digitada no app ou no WhatsApp dele:
    assert actiongate.authorized() is True
    assert actiongate.window_factor() == owner_auth.FACTOR_TEXT
    assert actiongate.send_window_factor() == owner_auth.FACTOR_TEXT
    assert rootgate.authorized() is True


def test_pedido_de_resposta_volta_por_e_mail(correio):
    """Lei 2: o caminho por onde ele pergunta tem que permitir aprovar — e a
    resposta volta PELO MESMO caminho (e-mail), sem segunda janela."""
    vaso, caixa = correio
    obj = _live_obj(vaso)
    caixa.messages = {
        "2": _msg(_DONO, f"retorne este e-mail {_SENTINELA}",
                  body=f"me mande uma resposta\n{_SENTINELA}\n",
                  message_id="<dono4@example.com>"),
    }
    _varrer(caixa, autorizador=obj._mail_owner_authorization)

    assert len(vaso.envios) == 1
    destino, assunto, corpo = vaso.envios[0]
    assert destino == _DONO
    assert assunto.startswith("Re:")
    assert "Autorizei pelo seu próprio e-mail" in corpo
    assert owner_auth.FACTOR_TEXT in corpo

    # e-mail dele SEM pedido de resposta não gera resposta por e-mail
    vaso.envios.clear()
    caixa.messages = {
        "5": _msg(_DONO, f"liga o ar condicionado {_SENTINELA}",
                  message_id="<dono5@example.com>"),
    }
    _varrer(caixa, autorizador=obj._mail_owner_authorization)
    assert vaso.envios == []


# ── 7. a segurança absoluta: o valor não aparece em superfície nenhuma ──────

def test_valor_da_senha_nao_aparece_em_nenhuma_saida(correio, capsys):
    vaso, caixa = correio
    obj = _live_obj(vaso)
    caixa.messages = {
        "1": _msg(_DONO, f"retorne este e-mail senha: {_SENTINELA}",
                  body=f"preciso da resposta, por favor\n{_SENTINELA}\n",
                  message_id="<dono6@example.com>"),
    }
    _varrer(caixa, autorizador=obj._mail_owner_authorization)

    # a rodada aconteceu: há log, há turno para o cérebro, há resposta
    assert vaso.log_modulo and vaso.log_ui and vaso.turnos and vaso.envios

    # 1. todas as superfícies observadas: log do módulo, log da UI (activity),
    #    fala/TTS, memória (turno do cérebro), nota do cofre, texto do e-mail
    assert _SENTINELA not in vaso.despejo(), "A SENHA VAZOU numa saída"

    # 2. o stdout do processo (vai para logs/activity.log)
    captured = capsys.readouterr()
    assert _SENTINELA not in captured.out + captured.err

    # 3. o jsonl de cabeçalhos, gravado em disco
    assert _SENTINELA not in _rastro_inbox()

    # 4. nota do cofre: o MESMO texto que a varredura produziu, escrito pelo
    #    writer real do vault (em tmp_path) e relido do disco
    assunto_gravado = str(_itens_inbox()[0].get("subject") or "")
    nota = obsidian.write_note(
        "e-mail do dono",
        assunto_gravado + "\n" + "\n".join(vaso.turnos),
        folder="Notas")
    assert _SENTINELA not in Path(nota).read_text(encoding="utf-8")
    assert str(nota).startswith(str(Path(obsidian.VAULT))), (
        "a nota saiu do cofre falso de tmp_path")


# ── 8. corolário de UX: a recusa nomeia as duas portas ──────────────────────

def test_recusa_do_envio_nomeia_as_duas_portas(correio):
    ok, pedido = mail.request_send("joao@ex.com", "recado", "oi")
    assert ok is True
    assert owner_auth.FACTOR_VOICE in pedido
    assert owner_auth.FACTOR_TEXT in pedido
    assert "duas portas" in pedido

    ok, recusa = mail.confirm_send(voice_ok=False)
    assert ok is False
    assert owner_auth.FACTOR_VOICE in recusa
    assert owner_auth.FACTOR_TEXT in recusa
    assert "nada foi enviado" in recusa


def test_campana_de_pendentes_nomeia_as_duas_portas(monkeypatch):
    """A UI/ferramenta também não pode prometer só a voz (decisão 14)."""
    from plugins import mail_control
    monkeypatch.setattr(mail, "pending_send",
                        lambda: {"to": "joao@ex.com", "subject": "recado"})
    falado, _corpo = mail_control._do_pending()
    assert owner_auth.FACTOR_VOICE in falado
    assert owner_auth.FACTOR_TEXT in falado


def test_a_frase_das_portas_nunca_contem_um_segredo():
    """``doors()`` nomeia os FATORES; o valor do segredo não tem caminho."""
    for frase in (owner_auth.doors("pt"), owner_auth.doors("en")):
        assert owner_auth.FACTOR_VOICE in frase
        assert owner_auth.FACTOR_TEXT in frase
        assert _SENTINELA not in frase


# ── 9. isolamento: o estado real do dono fica intacto ───────────────────────

def _md5_estado_real() -> dict[str, str]:
    """Hash (nunca conteúdo) do cofre e dos rastros de e-mail do dono."""
    alvos: list[Path] = [_REAL / "config" / "api_keys.json",
                         _REAL / "memory" / "mail_send_pending.json"]
    alvos += sorted((_REAL / "memory").glob("mail_inbox_*.jsonl"))
    alvos += sorted((_REAL / "config" / "mail").rglob("state.json"))
    out: dict[str, str] = {}
    for path in alvos:
        try:
            out[str(path)] = hashlib.md5(path.read_bytes()).hexdigest()
        except Exception:
            out[str(path)] = "ausente"
    return out


def test_estes_testes_nao_tocam_no_estado_real_do_dono(correio):
    antes = _md5_estado_real()
    vaso, caixa = correio
    obj = _live_obj(vaso)
    caixa.messages = {
        "1": _msg(_DONO, f"retorne este e-mail senha: {_SENTINELA}",
                  body=f"{_SENTINELA}\n", message_id="<dono7@example.com>"),
    }
    _varrer(caixa, autorizador=obj._mail_owner_authorization)
    mail.request_send("joao@ex.com", "recado", "oi")
    mail.confirm_send(voice_ok=False)

    assert _md5_estado_real() == antes, (
        "o teste escreveu no cofre ou nos rastros de e-mail do dono")
    assert _SENTINELA not in vaso.despejo()
    assert Path(mail._MEMORY_DIR).is_relative_to(_REAL) is False
