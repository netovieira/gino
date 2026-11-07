"""LinkedIn como REGRA do dispatcher de corpo de e-mail (UP-6 / UP-4 fechada).

Decisão do dono (07/11/2026): "o linkedin, nao precisa de algo especifico,
ele precisa apenas ser um filtro no novo fluxo de leitura dos emails". Não
existe plugin nem rota própria: a notificação chega pelo `core/email.py`, a
regra de filtro tem ``dispatch: true`` + ``tipo: linkedin_notification`` e
``core/email_flows.py`` decide o que fazer do corpo.

O que o teste vigia, e por quê:
  * parser: convite de conexão, mensagem privada, vaga e update de empresa
    viram tipos diferentes, com quem enviou, link e resumo;
  * defaults conservadores: convite e mensagem avisam; vaga e update de
    empresa são silentos até o assunto bater na regex ``watch``;
  * portão de remetente: corpo que não veio de @linkedin.com não vira aviso
    nenhum (``remetente_invalido``) — nome bonito no cabeçalho não basta;
  * a regra do LinkedIn NUNCA engole código de verificação: código no corpo
    continua no caminho do ``leitura_codigo`` (mesmo gancho de fala);
  * regra de filtro: ``tipo``/``watch`` sobrevivem à validação de
    ``core/email.py``; tipo inexistente é limpo (nada de adivinhação);
  * dispatcher chama o parser certo (``linkedin_notification``);
  * mensagem privada guardada como texto: nota ``NOTA-<data>-linkedin-
    msg-<slug>.md`` no vault, senão linha em ``logs/``;
  * nada abre navegador, nada roda OAuth, nada de corpo no evento nem no
    jsonl; um recibo, uma execução (transiente de aviso tenta de novo).

Nada aqui fala com a rede: IMAP é fake, cofre e históricos são temporários,
e os ganchos são coletores em memória. Os corpos são FIXTURES escritos por
nós — nenhum e-mail real aparece aqui.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main                                          # noqa: F401
from core import email as mail
from core import email_flows as flows
from tests.test_email import (FakeIMAP, FakeUI, _full_message, _header,
                              vault)  # noqa: F401  (vault = fixture emprestado)

# ── fixtures ────────────────────────────────────────────────────────────────

LINKEDIN_ITEM = {"uid": "1", "from": "messages-noreply@linkedin.com",
                 "sender": "LinkedIn", "subject": "Novidade na sua rede"}
RULE = {"field": "from", "pattern": "@linkedin\\.com", "match": "regex",
        "action": "avisa", "dispatch": True, "label": "linkedin",
        "tipo": "linkedin_notification", "watch": ""}

# FIXTURES (texto inventado para este teste; nunca um e-mail real).
CORPO_CONVITE = (
    "Olá Anthero, Beatriz Lima quer conectar-se com você na sua rede do "
    "LinkedIn. Aceitar convite: https://www.linkedin.com/mngprm/invite/abc "
    "— Visite o perfil de Beatriz Lima em https://www.linkedin.com/in/beatriz-lima")
CORPO_MENSAGEM = (
    "Beatriz Lima enviou uma mensagem. Olá, tudo bem com aquele projeto? "
    "Responder: https://www.linkedin.com/messaging/thread/4471/")
CORPO_VAGA = (
    "Vagas recomendadas para você: engenheiro de dados. Ver vaga: "
    "https://www.linkedin.com/jobs/view/3300044/")
CORPO_EMPRESA = (
    "Post da empresa Contoso no seu feed: lançamos o produto novo. "
    "https://www.linkedin.com/feed/update/111222333")
CORPO_CODIGO = (
    "Seu código de verificação do LinkedIn: 284519. Ele expira em 15 minutos. "
    "https://www.linkedin.com/uas/login-submit")


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """Histórico de vistos, vault e log de LinkedIn em disco temporário.

    O reset já veio do `vault` (via `mail.reset`); aqui só redirecionamos os
    caminhos — escrever no repositório real ou no vault do dono está fora.
    """
    monkeypatch.setattr(flows, "_seen_path", tmp_path / "email_flow_seen.json")
    monkeypatch.setattr(flows, "_LINKEDIN_VAULT", tmp_path / "vault")
    monkeypatch.setattr(flows, "_LINKEDIN_LOG",
                        tmp_path / "logs" / "linkedin-notifications.log")
    yield tmp_path


@pytest.fixture()
def hooks(store):
    """Ganchos de aviso/fala/evento coletores; devolve as listas."""
    warned: list[dict] = []
    spoken: list[dict] = []
    events: list[dict] = []
    flows.set_notify_hook(lambda p: (warned.append(p), True)[1])
    flows.set_speak_hook(lambda p: (spoken.append(p), True)[1])
    flows.set_event_hook(lambda p: events.append(p))
    return {"warned": warned, "spoken": spoken, "events": events}


def _connect(monkeypatch, fake):
    monkeypatch.setattr(mail, "_imap_open", lambda _a: fake)


def _dispatch(item, body, rule=None, hooks_seen=None):
    return flows.dispatch_flow({"name": "principal", "address": "eu@gmail.com"},
                               dict(item), dict(rule or RULE), body)


# ── 1-3: parser — QUEM, O QUE, LINK, RESUMO por tipo de corpo ───────────────

def test_parser_convite_de_conexao_avisa():
    info = flows.parse_linkedin(CORPO_CONVITE, subject="Novo convite de conexão",
                                item=LINKEDIN_ITEM)
    assert info["event_kind"] == "convite_conexao"
    assert info["from_name"] == "Beatriz Lima"
    assert info["link_principal"].startswith("https://www.linkedin.com/")
    assert "conectar-se" in info["resumo"]
    assert info["announce"] is True
    assert flows.linkedin_phrase(info) == ("LinkedIn: Beatriz Lima te mandou "
                                           "um convite de conexão.")


def test_parser_mensagem_privada_avisa_e_ped_leitura():
    info = flows.parse_linkedin(CORPO_MENSAGEM, subject="Você recebeu uma mensagem",
                                item=LINKEDIN_ITEM)
    assert info["event_kind"] == "mensagem"
    assert info["from_name"] == "Beatriz Lima"
    assert "messaging/thread" in info["link_principal"]
    assert info["announce"] is True
    assert "quer ler" in flows.linkedin_phrase(info)


def test_parser_vaga_e_empresa_sao_silenciosos_sem_watch():
    vaga = flows.parse_linkedin(CORPO_VAGA, subject="Vagas para você",
                                item=LINKEDIN_ITEM)
    empresa = flows.parse_linkedin(CORPO_EMPRESA,
                                   subject="Atualização da Contoso",
                                   item=LINKEDIN_ITEM)
    assert vaga["event_kind"] == "vaga" and vaga["announce"] is False
    assert empresa["event_kind"] == "notificacao_empresa"
    assert empresa["announce"] is False
    # ... e voltam a avisar quando o assunto bate na regex configurada.
    vigiada = flows.parse_linkedin(CORPO_VAGA,
                                   subject="Vaga dos meus sonhos: engenheiro",
                                   watch=r"vaga dos meus sonhos",
                                   item=LINKEDIN_ITEM)
    assert vigiada["event_kind"] == "vaga" and vigiada["announce"] is True
    # regex quebrada não vira aviso: fica silênciosa.
    quebrada = flows.parse_linkedin(CORPO_VAGA, subject="Vaga dos meus sonhos",
                                    watch="[", item=LINKEDIN_ITEM)
    assert quebrada["announce"] is False


# ── 4: portão de remetente ──────────────────────────────────────────────────

def test_corpo_sem_remetente_valido_nada_aciona(hooks):
    spoof = {"uid": "2", "from": "loja@exemplo.com", "sender": "Beatriz Lima",
             "subject": "Convite de conexão"}
    assert flows.is_linkedin_sender(spoof) is False
    ev = _dispatch(spoof, CORPO_CONVITE)
    assert ev["type"] == "linkedin_notification"
    assert ev["action"] == "nenhuma" and ev["ok"] is True
    assert ev["motivo"] == "remetente_invalido"
    assert hooks["warned"] == [] and hooks["spoken"] == []


def test_marca_desconhecida_sem_linkedin_devolve_none():
    assert flows.parse_linkedin("Boleto pago com sucesso.", subject="Recibo",
                                item=LINKEDIN_ITEM) is None


# ── 5: a regra de filtro carrega o tipo ─────────────────────────────────────

def test_regra_de_filtro_guarda_tipo_e_watch():
    rule = mail._clean_rule({"from": "@linkedin\\.com", "match": "regex",
                             "action": "avisa", "dispatch": True,
                             "tipo": "linkedin_notification", "label": "linkedin"})
    assert rule["dispatch"] is True
    assert rule["tipo"] == "linkedin_notification"
    assert rule["watch"] == ""
    # watch vem no JSON e é validado como regex; regex quebrada sai vazia.
    vigia = mail._clean_rule({"from": "linkedin", "action": "avisa",
                              "dispatch": True, "tipo": "linkedin_notification",
                              "watch": "vaga dos meus sonhos"})
    assert vigia["watch"] == "vaga dos meus sonhos"
    quebrada = mail._clean_rule({"from": "linkedin", "action": "avisa",
                                 "dispatch": True,
                                 "watch": "([", "tipo": "linkedin_notification"})
    assert quebrada["watch"] == ""
    # tipo que não existe no dispatcher é limpo — não adivinhamos o dono.
    inventado = mail._clean_rule({"from": "linkedin", "action": "avisa",
                                  "dispatch": True, "tipo": "abrir_linkedin"})
    assert inventado["tipo"] == ""


def test_flow_from_rule_forca_o_tipo_da_regra():
    flow = flows.flow_from_rule(RULE)
    assert flow["type"] == "linkedin_notification" and flow["name"] == "linkedin"
    assert flow["watch"] == ""
    assert flows.flow_from_rule({"action": "avisa"}) is None
    assert flows.flow_from_rule({"tipo": "sem_existe"}) is None


# ── 6: o dispatcher chama o parser certo ────────────────────────────────────

def test_dispatcher_chama_o_parser_linkedin(hooks, monkeypatch):
    vistos: list[tuple] = []

    def espiao(body, subject="", watch="", item=None):
        vistos.append((body[:20], subject, watch))
        return {"from_name": "Beatriz Lima", "event_kind": "mensagem",
                "link_principal": "https://www.linkedin.com/messaging/thread/1",
                "resumo": "Beatriz Lima enviou uma mensagem.",
                "announce": True}

    monkeypatch.setattr(flows, "parse_linkedin", espiao)
    ev = _dispatch(LINKEDIN_ITEM, CORPO_MENSAGEM)
    assert vistos and vistos[0][1] == "Novidade na sua rede"
    assert ev["type"] == "linkedin_notification" and ev["action"] == "aviso"
    assert ev["motivo"] == "notificado_mensagem" and ev["ok"] is True
    assert hooks["warned"] and hooks["warned"][0]["source"] == "linkedin"
    # privacidade: nem o evento nem a linha do activity levam corpo/link.
    assert "messaging/thread" not in str(ev) and "LinkedIn: Beatriz" in str(hooks["warned"][0])


# ── 7: fim a fim pela varredura (regra dispatch + tipo) ─────────────────────

def test_email_linkedin_chega_pela_varredura_e_avisa(vault, monkeypatch, hooks):
    mail.add_account("gmail", "eu@gmail.com", notify=True,
                     filters=[{"from": "@linkedin\\.com", "match": "regex",
                               "action": "avisa", "dispatch": True,
                               "tipo": "linkedin_notification"}])
    fake = FakeIMAP({b"12": _full_message(
        "Convite de conexao para sua rede", CORPO_CONVITE,
        sender="LinkedIn <messages-noreply@linkedin.com>",
        message_id="<l12>")})
    _connect(monkeypatch, fake)
    seen: list[dict] = []
    delivered = mail.poll_once(hook=seen.append)
    assert [m["subject"] for m in delivered] == ["Convite de conexao para sua rede"]
    assert len(hooks["events"]) == 1
    ev = hooks["events"][0]
    assert ev["type"] == "linkedin_notification" and ev["motivo"] == "notificado_convite_conexao"
    assert hooks["warned"][0]["text"] == ("LinkedIn: Beatriz Lima te mandou um "
                                          "convite de conexão.")
    stored = (mail._MEMORY_DIR / "mail_inbox_mail-gmail.jsonl").read_text()
    assert "mngprm" not in stored and "Beatriz" not in stored


def test_ramal_embutido_funciona_sem_fluxo_configurado(vault, monkeypatch, hooks):
    """Nada de plugin: mesmo sem DSL no config, o LinkedIn tem ramal próprio."""
    flows.set_event_hook(lambda p: hooks["events"].append(p))
    assert flows.load_flows()[0]["type"] == "linkedin_notification"
    vault.setdefault("mail", {})["flows"] = [
        {"name": "acoes", "type": "acao_automatica", "when": {"from": "escola"}},
    ]
    nomes = [f["type"] for f in flows.load_flows()]
    assert nomes == ["linkedin_notification", "acao_automatica"]


# ── 8-9: mensagem privada guardada como texto ───────────────────────────────

def test_mensagem_privada_vira_nota_no_vault(vault, hooks, store):
    vault_dir = store / "vault"
    vault_dir.mkdir()
    ev = _dispatch(dict(LINKEDIN_ITEM, uid="20"), CORPO_MENSAGEM)
    assert ev["motivo"] == "notificado_mensagem"
    notas = sorted(p.name for p in vault_dir.iterdir())
    assert len(notas) == 1
    assert notas[0].startswith("NOTA-") and "-linkedin-msg-beatriz-lima" in notas[0]
    texto = (vault_dir / notas[0]).read_text(encoding="utf-8")
    assert "tudo bem com aquele projeto" in texto
    assert "linkedin.com/messaging" in texto


def test_aviso_transiente_nao_duplica_a_nota(vault, store):
    """Fala indisponível tenta de novo: a nota sai uma vez só, não duas."""
    item = dict(LINKEDIN_ITEM, uid="26")
    vault_dir = store / "vault"
    vault_dir.mkdir()
    flows.set_notify_hook(lambda p: False)
    assert _dispatch(item, CORPO_MENSAGEM)["motivo"] == "aviso_indisponivel"
    assert list(vault_dir.iterdir()) == []
    flows.set_notify_hook(lambda p: True)
    assert _dispatch(item, CORPO_MENSAGEM)["motivo"] == "notificado_mensagem"
    assert len(list(vault_dir.iterdir())) == 1


def test_sem_vault_a_nota_vai_para_o_log(vault, store):
    log = store / "logs" / "linkedin-notifications.log"
    ev = _dispatch(dict(LINKEDIN_ITEM, uid="21"), CORPO_MENSAGEM)
    assert ev["ok"] is True
    linhas = log.read_text(encoding="utf-8").splitlines()
    assert any("NOTA" in ln and "Beatriz Lima" in ln for ln in linhas)
    assert any("AVISO" in ln and "LinkedIn:" in ln for ln in linhas)
    assert not (store / "vault").exists()


def test_sem_gancho_de_aviso_o_texto_sai_em_disc(vault, store):
    """Processo fora do assistente vivo: nada é falado, nada é inventado."""
    flows.set_notify_hook(None)
    ev = _dispatch(dict(LINKEDIN_ITEM, uid="22"), CORPO_CONVITE)
    assert ev["ok"] is True and ev["motivo"] == "notificado_convite_conexao"
    log = (store / "logs" / "linkedin-notifications.log").read_text(encoding="utf-8")
    assert "AVISO: LinkedIn: Beatriz Lima" in log


# ── 10: código de verificação não é engolido pela regra do LinkedIn ─────────

def test_codigo_do_linkedin_continua_lido(hooks):
    ev = _dispatch(dict(LINKEDIN_ITEM, uid="23",
                        subject="Confira seu código"), CORPO_CODIGO)
    assert ev["action"] == "codigo" and ev["motivo"] == "anunciado"
    assert hooks["spoken"] and hooks["spoken"][0]["origin"] == "email"
    assert hooks["warned"] == []


# ── 11: um recibo, uma execução; transiente de aviso tenta de novo ──────────

def test_recibo_unico_e_transiente_de_aviso(hooks, store):
    item = dict(LINKEDIN_ITEM, uid="24")
    flows.set_notify_hook(lambda p: False)
    ev = _dispatch(item, CORPO_MENSAGEM)
    assert ev["ok"] is False and ev["motivo"] == "aviso_indisponivel"
    # recibo NÃO gasto: tenta de novo com o gancho pronto
    flows.set_notify_hook(lambda p: (hooks["warned"].append(p), True)[1])
    ev2 = _dispatch(item, CORPO_MENSAGEM)
    assert ev2["ok"] is True and ev2["motivo"] == "notificado_mensagem"
    assert _dispatch(item, CORPO_MENSAGEM) is None
    assert len(hooks["warned"]) == 1


def test_vaga_nao_gasta_aviso_e_fica_muda(hooks, store):
    ev = _dispatch(dict(LINKEDIN_ITEM, uid="25", subject="Vagas para você"),
                   CORPO_VAGA)
    assert ev["ok"] is True and ev["action"] == "nenhuma"
    assert ev["motivo"] == "silenciado_vaga"
    assert hooks["warned"] == []
    assert not (store / "logs" / "linkedin-notifications.log").exists()


# ── 12: nada de navegador, nada de OAuth ────────────────────────────────────

def test_nenhum_caminho_abre_navegador_ou_oauth(vault, hooks, store, monkeypatch):
    import webbrowser

    def proibido(*a, **k):
        raise AssertionError("o fluxo de e-mail abriu um navegador")

    monkeypatch.setattr(webbrowser, "open", proibido)
    monkeypatch.setattr(webbrowser, "open_new", proibido)
    monkeypatch.setattr(webbrowser, "open_new_tab", proibido)
    sys.modules.pop("core.linkedin", None)
    for uid, corpo in (("30", CORPO_CONVITE), ("31", CORPO_MENSAGEM),
                       ("32", CORPO_VAGA), ("33", CORPO_EMPRESA)):
        assert _dispatch(dict(LINKEDIN_ITEM, uid=uid), corpo) is not None
    assert "core.linkedin" not in sys.modules


def test_aviso_do_main_survive_sem_gancho(vault, store):
    """A linha do activity.log (evento) continua só com metadados."""
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj._flows_monitor_event = lambda src, text: None
    main.JarvisLive._mail_flow_executed(obj, {
        "account": "principal", "uid": "7", "flow": "linkedin",
        "type": "linkedin_notification", "action": "aviso", "ok": True,
        "motivo": "notificado_convite_conexao"})
    line = str(obj.ui.lines[-1])
    assert "email_flow_executed" in line
    # main.py corta cada campo: o tipo chega truncado, nunca o corpo.
    assert "tipo=linkedin_notificatio" in line
    assert "motivo=notificado_convite_conexao" in line
    assert "fluxo=linkedin" in line
