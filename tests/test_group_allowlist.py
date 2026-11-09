"""MATRIZ DE RISCO -- allow-list de grupos (ordem literal do dono, 09/11/2026).

Formato exigido pelo dono: **bug previsivel -> codigo preventivo -> teste que
forca o local**. ``core/group_policy.py`` existia sem um unico teste; cada linha
abaixo e o local exato (arquivo:simbolo) e a assercao que quebra se ele regredir.

| #  | BUG PREVISIVEL                                                       | CODIGO PREVENTIVO                                                                           | TESTE QUE FORCA O LOCAL                                         |
|----|-----------------------------------------------------------------------|-----------------------------------------------------------------------------------------------|------------------------------------------------------------------|
| a  | Contato fora da allow-list fala no grupo e recebe resposta                | ``group_policy.group_verdict`` -> ``is_allowed`` (allow-list POR CONTATO, vale em todo grupo)  | ``test_a_*``: veredito negativo + respondedor nunca chamado        |
| b  | Contato permitido, frase sem meu nome e sem pergunta direta, respondida   | ``group_verdict`` -> ``_addressee`` + ``classify_context`` (narracao/exclamativa vencem)        | ``test_b_*``: ``allowed=False`` com o motivo dito no log           |
| c  | Letra de musica / citacao contendo meu nome vira pergunta                 | ``group_policy.is_citation`` (nome so dentro de aspas, ou depois de "diz/falou/refrao")         | ``test_c_*``: ``citacao``/``trecho_de_musica`` e silencio          |
| d  | Pergunta direta autorizada fica sem resposta (a lei virou mudez)          | ``group_verdict`` completo + ``whatsapp_watch._process_group``                                  | ``test_d_*``: ``allowed=True``, respondedor 1x, formato espelho    |
| e  | Allow-list corrompida/vazia re-semeada em silencio = porta aberta         | ``group_policy._read_raw``/``allowlist_status`` + portao de falha fechada no veredito           | ``test_e_*``: lixo, lista vazia, truncado; arquivo intacto         |
| f  | Regra de grupo bloqueia a ENTRADA dele no chat direto                     | ``whatsapp_watch.is_candidate``/``_process``: ``_is_direct`` ganha da flag de grupo              | ``test_f_*``: candidato + handler do app, jamais ``_process_group`` |
| g  | Persona sem numero alcancada/representada pelo numero do assistente       | ``group_policy._addressee`` + ``persona_reachable_by_number`` (le ``core.personas.direct_whatsapp``) | ``test_g_*``: sem numero, sem campo, cadastro ilegivel -> recusa  |
| 2  | Dono ausente -> chat visto, lido e dado sensivel dito                     | ``group_verdict`` (presenca ``None``/``False`` fecha) + ``privacy_verdict``/``law_line``         | ``test_2_*``: invisivel, sem adivinhar presenca, vazar bloqueado   |
| 4  | Terceiro autoriza terceiro por texto; dono revogavel                      | ``authorize_contact`` exige fator do dono; ``revoke_contact`` recusa o dono                      | ``test_4_*``: sem fator nao entra; com voz/senha entra em todo grupo |
| 5  | Voz desconhecida vira ninguem, ou vira dono                               | ``core/group_reply.py:_register_unknown_voice`` -> ``voice_visitors`` (nivel visitante)          | ``test_5_*``: registro, teto de visitante, sem duplicata           |

Defeito aberto (arquivo que NAO e meu, registrado em ``memory/defeitos/``):
``main.py:_whatsapp_autoreply`` ainda tem ``if group: return False`` (regra 4
revogada pelo dono em 09/11/2026), entao o grupo aprovado so existe no caminho
do watcher -- o turno do app em grupo continua morto.

Nenhum numero real: os JIDs sao fakes de teste, o cadastro de personas e
injetado (``whatsapp_watch.set_personas_source``) e o leitor de numero
(``core.personas.direct_whatsapp``) e o real, nunca editado aqui.
"""
from __future__ import annotations

import json
import time

import pytest

from core import group_policy as gp
from core import voice_visitors as vv
from core import whatsapp
from core import whatsapp_watch as w

OWNER = "5511999990000"
FRIEND = "5521888887777"
OUTSIDER = "5531777777777"
GROUP_A = "120363400000000001@g.us"
GROUP_B = "120363400000000002@g.us"

PERSONA_SEM = {"id": "aline", "name": "Aline",
               "direct_whatsapp": False, "whatsapp_account": ""}
PERSONA_COM = {"id": "thais", "name": "Thais", "direct_whatsapp": True,
               "whatsapp_account": "trabalho"}


# ── montagem do mundo (nenhum arquivo real, nenhum WhatsApp real) ──────────

@pytest.fixture()
def policy(tmp_path, monkeypatch):
    """Allow-list em arquivo proprio, presenca injetada, chat direto fixo."""
    path = tmp_path / "group_allowlist.json"
    monkeypatch.setattr(gp, "_PATH", path)
    monkeypatch.setattr(gp, "_presence_source", lambda group, acct: True)
    monkeypatch.setattr(gp, "_presence_cache", {})
    monkeypatch.setattr(whatsapp, "owner_numbers", lambda: {OWNER})
    monkeypatch.setattr(w, "_trigger_name", "Gino")
    monkeypatch.setattr(w, "_trigger_norm", "gino")
    monkeypatch.setattr(w, "_is_direct",
                        lambda account, item: str(item.get("chat") or "")
                        == OWNER)
    monkeypatch.setattr(w, "_handler", None)
    monkeypatch.setattr(w, "_group_responder", None)
    monkeypatch.setattr(whatsapp, "account_number", lambda account: "")
    monkeypatch.setattr(vv, "_STORE", tmp_path / "visitantes.json")
    w.set_personas_source(lambda: [dict(PERSONA_SEM)])
    _allow(path, FRIEND)
    w.reset()
    yield path
    w.set_personas_source(None)


def _allow(path, *jids):
    """Escreve a allow-list no formato do config (o dono sempre presente)."""
    contatos = [{"jid": OWNER, "nome": "Dono", "tipo": "contato",
                 "autorizado_por": "padrao", "data": "2026-11-09",
                 "dono": True, "irrevogavel": True}]
    for jid in jids:
        if jid == OWNER:
            continue
        contatos.append({"jid": jid, "nome": f"p{jid[-3:]}",
                         "tipo": "contato", "autorizado_por": "padrao",
                         "data": "2026-11-09", "dono": False})
    path.write_text(json.dumps(
        {"version": 1, "regra": "teste", "atualizado_em": "2026-11-09",
         "contatos": contatos}, ensure_ascii=False), encoding="utf-8")


def grupo(text, sender=FRIEND, group=GROUP_A, **kw):
    """Item de grupo como chega do neonize: o chat e o GRUPO, o autor separado."""
    item = {"chat": group, "group": True, "sender": sender,
            "sender_name": "Dono" if sender == OWNER else "Cunhado",
            "text": text, "id": f"M{abs(hash((text, sender, group)))}",
            "ts": time.time()}
    item.update(kw)
    return item


def direto(text="que horas sao?", **kw):
    """Item do chat DIRETO do dono -- a entrada dele."""
    item = {"chat": OWNER, "name": "Dono", "text": text,
            "id": f"D{abs(hash(text))}", "ts": time.time()}
    item.update(kw)
    return item


def pedindo():
    """Item que passa em TODOS os gates (contato, presenca, nome, pergunta)."""
    return grupo("Gino, que horas sao?")


class Espiao:
    """Espia o que saiu. Nem o WhatsApp real nem o core de resposta sao usados."""

    def __init__(self, monkeypatch):
        self.saidas = []
        self.turnos = []

        def responder(account, item, verdict):
            self.saidas.append({"account": account,
                                "chat": str(item.get("chat")),
                                "formato": verdict.reply_format,
                                "autor": verdict.sender})
            return True

        def handler(account, item):
            self.turnos.append(dict(item))
            return True

        monkeypatch.setattr(w, "_group_responder", responder)
        monkeypatch.setattr(w, "_handler", handler)
        monkeypatch.setattr(w, "_seen", {})
        monkeypatch.setattr(w, "_sent", {})
        monkeypatch.setattr(w.actiongate, "allow_whatsapp_reply",
                            lambda account, chat, who: 60.0)
        monkeypatch.setattr(w.actiongate, "revoke_whatsapp_reply",
                            lambda account, chat: None)

    @property
    def enviadas(self):
        return len(self.saidas)


# ── (e) allow-list vazia ou corrompida: nada de grupo, falha FECHADA ───────

def test_e_arquivo_corrompido_veredito_negativo_e_arquivo_nao_e_reescrito(
        policy):
    """BUG PREVISIVEL: JSON lixo era re-semeado com o dono e abria a porta.

    LOCAL: ``group_policy._read_raw``/``allowlist_status`` + o portao de falha
    fechada dentro de ``group_verdict``. FORCA: a pergunta que passaria em tudo
    continua silencio, e o lixo na mao do dono nao e destruido pelo programa.
    """
    policy.write_text("{ isso nao e json")
    antes = policy.read_text(encoding="utf-8")
    assert gp.allowlist_status()["ok"] is False
    got = gp.group_verdict(pedindo(), owner_present=True)
    assert got.allowed is False
    assert "allow-list indisponivel" in got.reason
    assert gp.is_group_allowed(pedindo(), owner_present=True) is False
    assert policy.read_text(encoding="utf-8") == antes


@pytest.mark.parametrize("lixo", [
    "[]",                                        # nao e documento
    '{"version": 1}',                            # sem contatos
    '{"contatos": "5521888887777"}',             # contatos nao e lista
    '{"contatos": [{"jid": "5521888887777"}',    # truncado no meio
    "",                                          # vazio de verdade
])
def test_e_todo_formato_de_lixo_fecha_a_leitura(policy, lixo):
    policy.write_text(lixo)
    assert gp.allowlist_status()["ok"] is False
    assert gp.group_verdict(pedindo(), owner_present=True).allowed is False


def test_e_allowlist_explicitemente_vazia_e_falha_fechada(policy):
    """``contatos: []`` e "ninguem autorizado" -- nao convite para reinserir."""
    policy.write_text(json.dumps({"version": 1, "contatos": []}))
    assert gp.allowlist_status()["ok"] is False
    assert gp.group_verdict(pedindo(), owner_present=True).allowed is False


def test_e_arquivo_ausente_semeia_so_o_dono(policy):
    """Ordem 1: comecando so por ele. Ausente nao e corrupcao: e instalacao nova."""
    policy.unlink()
    assert gp.allowlist_status()["ok"] is True
    assert [c["jid"] for c in gp.entries()] == [OWNER]
    dono = gp.group_verdict(grupo("Gino, que horas sao?", sender=OWNER),
                            owner_present=True)
    assert dono.allowed is True
    assert gp.group_verdict(pedindo(), owner_present=True).allowed is False


# ── (a) contato fora da allow-list em grupo = ignorado ─────────────────────

def test_a_contato_fora_da_allow_list_ignorado_mesmo_perguntando(policy):
    """LOCAL: ``group_verdict`` -> ``is_allowed``. A lista e POR CONTATO."""
    got = gp.group_verdict(grupo("Gino, que horas sao?", sender=OUTSIDER),
                           owner_present=True)
    assert got.allowed is False
    assert "allow-list" in got.reason
    assert gp.is_allowed(OUTSIDER) is False


def test_a_pergunta_valida_de_quem_nao_esta_na_lista_nada_envia(policy,
                                                                monkeypatch):
    """O que o veredito recusa nunca chega ao respondedor -- nem uma palavra."""
    _ = Espiao(monkeypatch)
    assert w._process("principal",
                      grupo("Gino, que horas sao?", sender=OUTSIDER)) is False
    assert _.enviadas == 0


def test_a_contato_permitido_vale_em_todos_os_grupos(policy):
    """Autorizacao por contato, nao por grupo (ordem 1 do dono)."""
    for group in (GROUP_A, GROUP_B, "120363400000000009@g.us"):
        assert gp.group_verdict(grupo("Gino, que horas sao?", group=group),
                                owner_present=True).allowed is True


def test_a_autor_desconhecido_no_grupo_e_silencio(policy):
    """Sem autor nao ha como aplicar allow-list: falha fechada, nunca adivinha."""
    item = {"chat": GROUP_A, "group": True, "sender": "", "text": "Gino, oi?",
            "ts": time.time()}
    got = gp.group_verdict(item, owner_present=True)
    assert got.allowed is False
    assert "remetente" in got.reason


# ── (b) permitido, sem meu nome e sem pergunta direta = ignorado ───────────

def test_b_sem_nome_e_sem_pergunta_e_silencio_com_motivo_dito(policy):
    """Papo de terceiros e LIDO e ignorado, com o motivo no log (nunca mudo)."""
    got = gp.group_verdict(grupo("que tempo hoje?"), owner_present=True)
    assert got.allowed is False
    assert "chamou" in got.reason
    linha = gp.describe(got)
    assert "decisao=silencio" in linha and "motivo=" in linha


def test_b_com_meu_nome_mas_sem_pergunta_continua_silencio(policy):
    """"Gino, que loucura esse jogo" tem palavra interrogativa e nao pergunta."""
    got = gp.group_verdict(grupo("Gino, que loucura esse jogo"),
                           owner_present=True)
    assert got.allowed is False
    assert got.kind == "narracao"


def test_b_narracao_de_conversa_alheia_e_silencio(policy):
    """Quem narra o que aconteceu nao esta perguntando nada."""
    assert gp.classify_context("ai a gente chegou atrasado, Gino") == "narracao"
    assert gp.group_verdict(grupo("ai a gente chegou atrasado, Gino"),
                            owner_present=True).allowed is False


# ── (c) letra de musica / citacao contendo meu nome = ignorado ─────────────

@pytest.mark.parametrize("frase,esperado", [
    ("\u266a Gino, me leva no altar \u266a", "trecho_de_musica"),
    ('o refrao diz "Gino, onde voce esta?"', "citacao"),
    ("ele gritou: 'Gino, some daqui!' e riu", "citacao"),
    ("a musica diz Gino, volta pra mim?", "citacao"),
])
def test_c_letra_e_citacao_com_meu_nome_sao_ignoradas(policy, frase, esperado):
    """O ``?`` de dentro da citacao nao e pergunta para o assistente.

    LOCAL: ``group_policy.is_citation``, chamado antes do teste de ``?`` em
    ``classify_context`` -- sem isso a letra cantada virava turno.
    """
    assert gp.classify_context(frase) == esperado
    got = gp.group_verdict(grupo(frase), owner_present=True)
    assert got.allowed is False
    assert got.kind == esperado


def test_c_meu_nome_fora_das_aspas_a_pergunta_e_minha(policy):
    """Contra-exemplo que impede a regra de virar censor: citar nao cala."""
    got = gp.group_verdict(grupo('Gino, o que significa "carpe diem"?'),
                           owner_present=True)
    assert got.allowed is True
    assert got.kind == "pergunta"


# ── (d) pergunta direta = atendida ─────────────────────────────────────────

def test_d_pergunta_direta_de_contato_permitido_e_atendida(policy, monkeypatch):
    """Caminho completo: veredito positivo E o respondedor executado uma vez."""
    _ = Espiao(monkeypatch)
    assert gp.group_verdict(pedindo(), owner_present=True).allowed is True
    assert w._process("principal", pedindo()) is True
    assert _.enviadas == 1
    assert _.saidas[0]["chat"] == GROUP_A


@pytest.mark.parametrize("frase", [
    "Gino, voce consegue ver meu calendario amanha?",
    "Gino, da pra chegar a que horas no aeroporto?",
    'Gino, "sera que da"?',
])
def test_d_perguntas_diretas_sao_atendidas(policy, frase):
    got = gp.group_verdict(grupo(frase), owner_present=True)
    assert got.allowed is True, got.reason


def test_d_entrada_de_audio_espe_lhe_o_formato(policy):
    """Ordem 3 do dono: audio entra, audio sai; o veredito declara o espelho."""
    item = grupo("", media={"kind": "mensagem de voz", "ext": ".ogg"})
    got = gp.group_verdict(item, owner_present=True)
    assert got.reply_format == "audio"
    assert got.needs_transcript is True   # o nome so pode ser julgado no ouvindo


def test_d_nada_e_respondido_duas_vezes(policy, monkeypatch):
    """O mesmo evento que chega por duas rotinas recebe uma resposta so."""
    _ = Espiao(monkeypatch)
    item = grupo("Gino, que horas sao?", id="MESMA")
    assert w._process("principal", dict(item)) is True
    assert w._process("principal", dict(item)) is False
    assert _.enviadas == 1


# ── (f) a entrada dele no chat direto NUNCA e bloqueada por regra de grupo ─

@pytest.mark.parametrize("extra", [
    {},
    {"group": True},
    {"from_me": True},
    {"group": True, "from_me": True},
])
def test_f_entrada_direta_do_dono_jamais_barrada_por_grupo(policy, extra):
    """LOCAL: ``whatsapp_watch.is_candidate`` -- o que define o chat direto e o
    CHAT, nao um rotulo de grupo colado por engano no evento."""
    assert w.is_candidate(direto(**extra), "principal") is True


@pytest.mark.parametrize("lixo", ["{ lixo", '{"contatos": []}'])
def test_f_chat_direto_funciona_com_allow_list_quebrada(policy, lixo,
                                                        monkeypatch):
    """Falha fechada de grupo nao pode fechar a porta da frente dele."""
    policy.write_text(lixo)
    assert gp.allowlist_status()["ok"] is False
    _ = Espiao(monkeypatch)
    assert w.is_candidate(direto(), "principal") is True
    assert w.handle("principal", direto()) is True
    assert len(_.turnos) == 1
    assert _.enviadas == 0


def test_f_rota_de_grupo_nao_engole_o_chat_direto(policy, monkeypatch):
    """Mensagem dele com flag de grupo vai ao handler do app, nunca ao grupo."""
    _ = Espiao(monkeypatch)
    assert w._process("principal", direto(group=True)) is True
    assert len(_.turnos) == 1
    assert _.enviadas == 0


def test_f_dono_irrevogavel_e_a_presenca_dele_no_proprio_chat_e_fato(policy):
    """Ele ja vem autorizado por padrao e nao perde a autorizacao -- e no chat
    direto dele a presenca nem e consultada (nada de RPC, nada de duvida)."""
    got = gp.revoke_contact(OWNER)
    assert got["ok"] is False
    assert "padrao" in got["reason"] or "irrevog" in got["reason"]
    assert gp.is_allowed(OWNER) is True
    assert gp.owner_present(direct=True) is True


def test_f_grupo_sem_o_dono_nao_rouba_a_entrada_dele(policy, monkeypatch):
    """Contraste honesto: a mesma pergunta em grupo sem o dono = silencio."""
    _ = Espiao(monkeypatch)
    monkeypatch.setattr(gp, "_presence_source", lambda group, acct: False)
    assert w.is_candidate(pedindo(), "principal") is True   # pre-filtro barato
    assert w._process("principal", pedindo()) is False
    assert _.enviadas == 0
    assert w._process("principal", direto()) is True        # a dele segue viva


# ── (g) persona sem numero nunca recebe por grupo ──────────────────────────

@pytest.mark.parametrize("cadastro", [
    PERSONA_SEM,                                    # direct_whatsapp=False
    {"id": "aline", "name": "Aline"},               # campo nao declarado
    {"id": "aline", "name": "Aline", "direct_whatsapp": False,
     "whatsapp_account": "principal"},              # sobra a conta alheia
])
def test_g_persona_sem_numero_nunca_recebe_por_grupo(policy, cadastro):
    """LOCAL: ``group_policy._addressee`` + ``persona_reachable_by_number``.

    A mensagem de grupo chega no numero do ASSISTENTE: rotea-la para quem nao
    tem numero seria alcancar (e falar por) ela por um numero que nao e dela.
    """
    w.set_personas_source(lambda: [dict(cadastro)])
    got = gp.group_verdict(grupo("Aline, que horas sao?"), owner_present=True)
    assert got.allowed is False
    assert got.addressed == ""
    assert "nao tem whatsapp proprio" in got.reason


def test_g_com_numero_a_persona_e_atendida_normalmente(policy):
    """A recusa nao e censor generico: quem tem numero responde como sempre."""
    w.set_personas_source(lambda: [dict(PERSONA_COM)])
    got = gp.group_verdict(grupo("Thais, que horas sao?"), owner_present=True)
    assert got.allowed is True
    assert str(got.addressed).lower() == "thais"


def test_g_robo_sem_numero_nada_envia_em_grupo(policy, monkeypatch):
    """E o caminho de saida: nem uma frase sai no grupo por ela."""
    _ = Espiao(monkeypatch)
    assert w._process("principal", grupo("Aline, que horas sao?")) is False
    assert _.enviadas == 0


def test_g_persona_sem_numero_continua_viva_no_chat_do_assistente(policy):
    """O vocativo dela no chat direto dele nao e tocado pela lei de grupo."""
    assert w.is_candidate(direto("Aline, voce esta ai?"), "principal") is True


def test_g_cadastro_ilegivel_e_recusado(policy, monkeypatch):
    """Sem ler o cadastro, nenhuma persona e alcancada por grupo (fechado)."""
    w.set_personas_source(lambda: [dict(PERSONA_COM)])
    monkeypatch.setattr(
        "core.personas.direct_whatsapp",
        lambda persona: (_ for _ in ()).throw(RuntimeError("cadastro caiu")))
    assert gp.persona_reachable_by_number(dict(PERSONA_COM)) is False
    assert gp.group_verdict(grupo("Thais, que horas sao?"),
                            owner_present=True).allowed is False


# ── ordem 2: os demais WhatsApp continuam 100% privados ───────────────────

def test_2_dono_ausente_do_grupo_e_chat_que_ele_nao_ve(policy):
    """Ordem 2 literal: "se ele não está na conversa, eu não vejo"."""
    got = gp.group_verdict(grupo("Gino, o que eu falei pro Arnaldo?"),
                           owner_present=False)
    assert got.allowed is False
    assert "dono nao esta no grupo" in got.reason
    assert got.privacy == {}          # sem presenca o conteudo nem e lido


def test_2_presenca_desconhecida_nunca_vira_pode(policy, monkeypatch):
    """``None`` (nao sei) NUNCA vira "pode": nem RPC no aparelho adivinha."""
    monkeypatch.setattr(gp, "_presence_source", lambda group, acct: None)
    got = gp.group_verdict(grupo("Gino, o que eu falei pro Arnaldo?"))
    assert got.owner_present is None
    assert got.allowed is False
    assert "presenca do dono desconhecida" in got.reason
    assert got.privacy == {}


def test_2_sem_fonte_de_presenca_e_silencio(policy, monkeypatch):
    monkeypatch.setattr(gp, "_presence_source", None)
    monkeypatch.setattr(whatsapp, "owner_in_group",
                        lambda group, account=None: None)
    assert gp.group_verdict(pedindo()).allowed is False


def test_2_no_chat_direto_a_presenca_e_fato_nao_consulta(policy):
    """LOCAL: ``owner_present(direct=True)`` / ``privacy_verdict`` -- sem duvida."""
    got = gp.privacy_verdict("oi", direct_owner_chat=True,
                             owner_present_in_chat=False)
    assert got.see is True and got.allowed is True
    assert gp.owner_present(direct=True) is True


@pytest.mark.parametrize("frase", [
    "o que eu falei pro Arnaldo sobre o projeto?",
    "que apps estao abertos no pc dele?",
    "qual e o salario dele?",
    "onde ele mora?",
])
def test_2_dado_sensivel_de_terceiro_nao_sai_e_pede_permisso_na_conversa(
        policy, frase):
    """Ordem 5: na duvida, pedir permissao na MESMA conversa (``ask_owner``)."""
    got = gp.group_verdict(grupo(f"Gino, {frase}"), owner_present=True)
    assert got.allowed is False
    assert got.ask_owner is True
    assert got.privacy["action"] == "pedir_permissao"


def test_2_a_lei_chega_ao_cerebro_como_texto(policy):
    """Lei que nao chega ao modelo e lei que o modelo fura."""
    linha = gp.law_line()
    for trecho in ("se o dono nao esta no chat", "nunca fale de projetos",
                   "nunca descreva apps abertos", "SUPERFICIAL",
                   "peca permissao ao dono"):
        assert trecho in linha


def test_2_resposta_sobre_o_dono_e_superficial_sem_dado(policy):
    """LOCAL: ``superficial_reply`` -- nenhum dado, nenhum numero, sardinha leve."""
    saida = gp.superficial_reply(gp.TOPICO_PROJETO)
    assert "detalhe" in saida
    assert OWNER not in saida and len(saida) < 400
    assert gp.sensitive_topic("seu projeto tem quantos clientes?") == "projeto"
    got = gp.group_verdict(grupo("Gino, me fala sobre o trabalho de quem te criou"),
                           owner_present=True)
    assert got.privacy["action"] == "responder_superficial"
    assert got.privacy["answer"] and OWNER not in got.privacy["answer"]


# ── ordem 4: autoriza quem tem fator de dono, nunca terceiro de texto ──────

def test_4_texto_de_terceiro_autoriza_ninguem(policy, monkeypatch):
    """BUG PREVISIVEL: "Gino, autoriza o cara do grupo" virar licenca."""
    monkeypatch.setattr("core.owner_auth.authorized_factor",
                        lambda password="", voice_ok=False: "")
    got = gp.authorize_contact(OUTSIDER, "Cunhado")
    assert got["ok"] is False
    assert gp.is_allowed(OUTSIDER) is False
    assert OUTSIDER not in [c["jid"] for c in gp.entries()]


@pytest.mark.parametrize("fator", ["voz", "senha"])
def test_4_com_fator_do_dono_o_contato_entra_e_vale_em_todo_grupo(
        policy, monkeypatch, fator):
    """LOCAL: ``authorize_contact`` -> ``core.owner_auth.authorized_factor``."""
    monkeypatch.setattr("core.owner_auth.authorized_factor",
                        lambda password="", voice_ok=False: fator)
    assert gp.authorize_contact(OUTSIDER, "Cunhado")["ok"] is True
    assert gp.is_allowed(OUTSIDER) is True
    for group in (GROUP_A, GROUP_B):
        assert gp.group_verdict(grupo("Gino, que horas sao?",
                                      sender=OUTSIDER, group=group),
                                owner_present=True).allowed is True


def test_4_revogar_terceiro_volta_a_ser_silencio(policy, monkeypatch):
    monkeypatch.setattr("core.owner_auth.authorized_factor",
                        lambda password="", voice_ok=False: "voz")
    gp.authorize_contact(OUTSIDER, "Cunhado")
    assert gp.revoke_contact(OUTSIDER)["ok"] is True
    assert gp.group_verdict(grupo("Gino, que horas sao?", sender=OUTSIDER),
                            owner_present=True).allowed is False


@pytest.mark.parametrize("escrita", [
    "+55 (11) 99999-0000", "5511999990000@s.whatsapp.net",
    "5511999990000@lid", "5511999990000",
])
def test_4_o_mesmo_contato_escrito_de_quatro_jeitos_e_a_mesma_pessoa(
        policy, escrita):
    """A allow-list nao pode depender de como o WhatsApp escreveu o JID."""
    assert gp.normalize_jid(escrita) == OWNER
    assert gp.is_allowed(escrita) is True


# ── ordem 5: voz desconhecida vira memoria com permissao de visitante ──────

class _Speaker:
    """SpeakerId falso: uma voz nova, sempre ``unknown`` (sem modelo no teste)."""

    vetor = [0.0] * 127 + [1.0]

    def __init__(self, *a, **kw):
        pass

    def embedding(self, pcm):
        return list(self.vetor) if pcm else []

    def identify(self, vec, include_auto=False):
        return {"verdict": "unknown"}


def _pcm(bytes_total=19200):
    return b"\x01\x02" * (bytes_total // 2)


@pytest.fixture()
def falantes(monkeypatch):
    from core import group_reply as gr
    monkeypatch.setattr("core.speaker_id.SpeakerId", _Speaker)
    return gr


def test_5_voz_desconhecida_vira_memoria_de_visitante(policy, falantes):
    """LOCAL: ``group_reply._register_unknown_voice`` -> ``voice_visitors``."""
    falantes._register_unknown_voice(_pcm(),
                                     grupo("Gino, que horas sao?",
                                           sender_name="Cunhado"))
    visitantes = vv.visitors()
    assert [v["nome"] for v in visitantes] == ["Cunhado"]
    visita = vv.visitor_of(name="Cunhado")
    assert visita is not None and visita["nivel"] == vv.LEVEL_VISITOR
    pode = vv.permissions(name="Cunhado")
    assert pode["ser_respondido_na_conversa"] is True
    assert pode["ter_memoria_propria"] is True
    for vedado in ("executar_acoes", "falar_dados_do_dono", "falar_de_projetos",
                   "falar_do_que_o_dono_disse",
                   "ver_tela_arquivos_ou_apps_do_pc", "escrever_perfil_de_voz",
                   "mudar_permissoes_de_quer_que_seja"):
        assert pode[vedado] is False, vedado
        assert vv.allows(name="Cunhado", scope=vedado) is False


def test_5_a_mesma_voz_nao_vira_segunda_pessoa(policy, falantes):
    """Sem numero o nome nao e chave: quem casa com registro feito so ganha amostra."""
    for _ in range(2):
        falantes._register_unknown_voice(
            _pcm(), grupo("Gino, que horas sao?", sender=OUTSIDER,
                          sender_name="Cunhado"))
    assert len(vv.visitors()) == 1
    assert len(vv.visitor_of(name="Cunhado")["amostras"]) == 2


def test_5_estalo_curto_demais_nao_gera_identidade(policy, falantes):
    """Menos de ~0,6 s nao e voiceprint: inventar identidade e pior que nada."""
    falantes._register_unknown_voice(_pcm(2000),
                                     grupo("Gino?", sender_name="Quem"))
    assert vv.visitors() == []


def test_5_upgrade_sem_a_palavra_do_dono_nao_sobe(policy, falantes):
    """O teto de visitante so levanta com prova de dono (voz confirmada/senha)."""
    falantes._register_unknown_voice(_pcm(),
                                     grupo("Gino, que horas sao?",
                                           sender_name="Cunhado"))
    got = vv.upgrade("Cunhado", scope="executar_acoes", owner_ok=False)
    assert got["ok"] is False
    assert vv.allows(name="Cunhado", scope="executar_acoes") is False
