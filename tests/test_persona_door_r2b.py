"""R2b — a PORTA do número de uma persona (ordem literal do dono, 10/11/2026).

A palavra dele: "se eu receber uma mensagem no seu número de WhatsApp de um
cliente, por exemplo, que é o meu WhatsApp de trabalho ... você não pode
responder esse cliente, mas se eu for direto no seu chat e mandar ... você
entende que tem permissão para responder".

Bug previsivo que este arquivo força a não voltar:
  * mensagem individual que caía na conta atribuída a uma persona era atendida
    sem que ninguém perguntasse QUEM escreveu (o fator ``conta_atribuida``
    olhava o destino e ignorava o autor) → o ``_process`` agora julga o autor
    antes de chamar o handler;
  * "pessoa permitida" não existia em WhatsApp → existe o MECANISMO
    (``config/persona_door.json``, lido a cada chamada), NASCENDO VAZIO: vazio
    é "só o criador", e nome nenhum entra por minha caneta.

| # | BUG PREVISIVEL                                     | CODIGO PREVENTIVO                                        | TESTE                          |
|---|-----------------------------------------------------|-----------------------------------------------------------|--------------------------------|
| a | Cliente no número da persona recebe resposta        | ``persona_door.decide`` + ``whatsapp_watch._process``      | ``test_a_*``                   |
| b | Criador no mesmo número fica de fora / precisa nome | ``is_creator`` (``group_policy.is_owner``, número pareado) | ``test_b_*``                   |
| c | Permitido por OUTRO número entra em qualquer um     | lista indexada pelo número (``entries(account)``)          | ``test_c_*``                   |
| d | Lista nova nascia com nome que eu inventei          | arquivo vazio por default = só o criador                   | ``test_d_*``                   |
| e | config corrompido virava porta aberta               | ``_read_raw``/``decide`` fechados para terceiro            | ``test_e_*``                   |
| f | Terceiro autorizava terceiro por texto              | ``authorize`` exige o fator do dono (voz OU senha)         | ``test_f_*``                   |
| g | A recusa sumia sem trilha                           | ``describe``: decisão, motivo, quem, conta (nunca o texto) | ``test_g_*``                   |
| h | A porta engolia o DM do dono ou o grupo             | ``direct`` não passa pela porta; grupo tem o seu veredito  | ``test_h_*``                   |

Isolamento: nenhum estado real do dono é tocado — a porta vive em
``tmp_path``, os números são falsos, o cadastro de personas é injetado e os
arquivos reais (porta e allow-list de grupos) são provados intocáveis no último
teste, com a escrita virando alarme.
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from core import group_policy as gp
from core import owner_auth
from core import persona_door as pd
from core import vocative_ledger as vl
from core import whatsapp, whatsapp_watch as w
from memory import config_manager

OWNER = "5511999990000"
CLIENT = "5521888887777"
FRIEND = "5531777776666"
STRANGER = "5541555554444"
GROUP = "120363400000000001@g.us"
PERSONA_ACCOUNT = "trabalho"
OTHER_ACCOUNT = "outronumero"

PERSONA = {"id": "thais", "name": "Thais", "direct_whatsapp": True,
           "whatsapp_account": PERSONA_ACCOUNT}


# ── o mundo do teste (arquivo próprio, números falsos, handler espião) ──────

@pytest.fixture()
def world(tmp_path, monkeypatch):
    door = tmp_path / "persona_door.json"
    logs: list[str] = []
    calls: list[tuple[str, dict]] = []

    def handler(account, item):
        calls.append((account, dict(item)))
        return True

    monkeypatch.setattr(pd, "_PATH", door)
    monkeypatch.setattr(vl, "_NOTES", {})
    monkeypatch.setattr(vl, "_ORDER", [])
    monkeypatch.setattr(w, "_trigger_name", "Gino")
    monkeypatch.setattr(w, "_trigger_norm", "gino")
    monkeypatch.setattr(w, "_handler", handler)
    monkeypatch.setattr(w, "_logger", logs.append)
    monkeypatch.setattr(w, "_seen", {})
    monkeypatch.setattr(w, "_sent", {})
    monkeypatch.setattr(w, "_group_responder",
                        lambda account, item, verdict: (
                            calls.append(("GRUPO", dict(item))), True)[1])
    # O gate de resposta é estado de processo e regra de outro portão: aqui ele
    # só observa sem tocar no actiongate real.
    monkeypatch.setattr(w.actiongate, "allow_whatsapp_reply",
                        lambda *a, **kw: 60.0)
    monkeypatch.setattr(w.actiongate, "revoke_whatsapp_reply",
                        lambda *a, **kw: None)
    monkeypatch.setattr(whatsapp, "owner_numbers", lambda: {OWNER})
    monkeypatch.setattr(whatsapp, "account_number", lambda account: "")
    monkeypatch.setattr(whatsapp, "list_accounts", lambda: [])
    monkeypatch.setattr(whatsapp, "default_account_name", lambda: "principal")
    monkeypatch.setattr(config_manager, "get_plugin_setting",
                        lambda _ns, key, default=None:
                            OWNER if key == "owner_chat" else default)
    w.set_personas_source(lambda: [dict(PERSONA)])
    yield {"door": door, "logs": logs, "calls": calls,
           "handler": lambda: calls}
    w.set_personas_source(None)


def msg(chat: str, text: str = "preciso de um orcamento ate sexta",
        **kw) -> dict:
    entry = {"chat": chat, "name": "Dono" if chat == OWNER else "Cliente",
             "from_me": False, "group": False, "text": text,
             "id": f"m-{chat}-{abs(hash((chat, text)))}", "ts": time.time(),
             "media": None}
    entry.update(kw)
    return entry


def write_door(path, account, *jids):
    """Escreve a porta no formato do config — o que a caneta dele faria."""
    permitidos = [{"jid": j, "nome": f"Permitido{j[-3:]}",
                   "autorizado_por": "senha digitada", "data": "2026-11-10",
                   "dono": False} for j in jids]
    path.write_text(json.dumps(
        {"version": 1, "regra": "teste", "atualizado_em": "2026-11-10",
         "numeros": {account: {"persona": "thais",
                               "permitidos": permitidos}}},
        ensure_ascii=False), encoding="utf-8")


# ── a: terceiro no número da persona não recebe resposta nenhuma ────────────

def test_a_terceiro_no_numero_da_persona_nao_recebe_resposta(world):
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT)) is False
    assert world["calls"] == []


def test_a_terceiro_com_nota_de_voz_tambem_nao_recebe(world):
    voice = msg(CLIENT, "[Áudio]", media={"kind": "áudio", "ext": ".ogg"})
    assert w.handle(PERSONA_ACCOUNT, voice) is False
    assert world["calls"] == []


def test_a_terceiro_que_nomeia_a_persona_para_na_porta(world):
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT, "Thais, me ajuda?")) is False
    assert world["calls"] == []


def test_a_fora_do_numero_da_persona_a_rota_normal_continua(world):
    """A porta é do NÚMERO: na conta do assistente vale a regra de sempre."""
    assert w.handle("principal", msg(CLIENT, "Gino, que horas sao?")) is True
    assert [account for account, _ in world["calls"]] == ["principal"]


# ── b: o criador no mesmo número, sem precisar do nome ──────────────────────

def test_b_criador_no_numero_da_persona_e_atendido_sem_nome(world):
    assert w.handle(PERSONA_ACCOUNT, msg(OWNER, "manda o relatório")) is True
    assert [account for account, _ in world["calls"]] == [PERSONA_ACCOUNT]
    assert any("decisao=resposta" in line and "criador" in line
               for line in world["logs"])


def test_b_eco_do_aparelho_do_dono_na_persona_e_o_dono(world):
    """from_me na conta da persona é o dono escrevendo, não um terceiro."""
    eco = msg(PERSONA_ACCOUNT, "Gino, resume isso", from_me=True)
    assert w.handle(PERSONA_ACCOUNT, eco) is True
    assert len(world["calls"]) == 1


# ── c: a permissão é DAQUELE número, não do mundo ──────────────────────────

def test_c_pessoa_permitida_naquele_numero_recebe(world):
    write_door(world["door"], PERSONA_ACCOUNT, CLIENT)
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT)) is True
    assert len(world["calls"]) == 1


def test_c_permissao_de_outro_numero_nao_empresta_porta(world):
    write_door(world["door"], OTHER_ACCOUNT, CLIENT)
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT)) is False
    assert world["calls"] == []


def test_c_a_lista_e_lida_a_cada_chamada_sem_reiniciar(world):
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT)) is False
    write_door(world["door"], PERSONA_ACCOUNT, CLIENT)
    assert w.handle(PERSONA_ACCOUNT,
                    msg(CLIENT, "agora sim? preciso de um retorno")) is True
    assert len(world["calls"]) == 1


# ── d: o mecanismo nasce vazio (só o criador), nunca com nome meu ───────────

def test_d_arquivo_ausente_e_so_o_criador(world):
    assert not world["door"].exists()
    assert pd.entries(PERSONA_ACCOUNT) == []
    assert pd.decide(PERSONA_ACCOUNT, msg(CLIENT)).allowed is False
    assert pd.decide(PERSONA_ACCOUNT, msg(OWNER)).allowed is True


def test_d_lista_vazia_e_so_o_criador(world):
    write_door(world["door"], PERSONA_ACCOUNT)
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT, "oi")) is False
    assert w.handle(PERSONA_ACCOUNT, msg(OWNER, "oi")) is True


# ── e: falha fechada quando a política não é legível ────────────────────────

def test_e_arquivo_corrompido_fecha_para_terceiro(world):
    world["door"].write_text("{isto nao e json", encoding="utf-8")
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT)) is False
    assert world["calls"] == []
    assert any("corrompido" in line or "ilegivel" in line
               for line in world["logs"])


def test_e_arquivo_corrompido_nunca_e_reescrito_por_cima(world):
    lixo = "{isto nao e json"
    world["door"].write_text(lixo, encoding="utf-8")
    pd.decide(PERSONA_ACCOUNT, msg(CLIENT))
    pd.authorize(PERSONA_ACCOUNT, FRIEND, "Cunhado", voice_ok=True)
    assert world["door"].read_text(encoding="utf-8") == lixo


def test_e_criador_continua_dentro_com_arquivo_corrompido(world):
    """A identidade do criador vem do número pareado, não do JSON."""
    world["door"].write_text("{isto nao e json", encoding="utf-8")
    assert pd.decide(PERSONA_ACCOUNT, msg(OWNER, "oi")).allowed is True


# ── f: terceiro não se auto-permite; a caneta é do dono (voz OU senha) ──────

def test_f_autorizacao_sem_fator_do_dono_nao_entra(world):
    out = pd.authorize(PERSONA_ACCOUNT, CLIENT, "Cliente")
    assert out["ok"] is False and "autorizacao do dono" in out["reason"]
    assert pd.entries(PERSONA_ACCOUNT) == []
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT)) is False
    assert world["calls"] == []


def test_f_a_senha_dele_vale_como_a_voz_dele(world, monkeypatch):
    """As duas portas pelo MESMO caminho (``owner_auth.authorized_factor``)."""
    monkeypatch.setattr(owner_auth, "configured_hash", lambda: "x$1$a$b")
    monkeypatch.setattr(owner_auth, "verify",
                        lambda password: password == "senha-dele")
    by_voice = pd.authorize(PERSONA_ACCOUNT, CLIENT, "Cliente A", voice_ok=True)
    assert by_voice["ok"] is True
    assert by_voice["entry"]["autorizado_por"] == owner_auth.FACTOR_VOICE
    by_password = pd.authorize(PERSONA_ACCOUNT, FRIEND, "Cliente B",
                               password="senha-dele")
    assert by_password["ok"] is True
    assert by_password["entry"]["autorizado_por"] == owner_auth.FACTOR_TEXT
    wrong = pd.authorize(PERSONA_ACCOUNT, STRANGER, "Intruso",
                         password="errada")
    assert wrong["ok"] is False
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT)) is True
    assert w.handle(PERSONA_ACCOUNT, msg(FRIEND)) is True
    assert w.handle(PERSONA_ACCOUNT, msg(STRANGER)) is False
    assert "senha-dele" not in json.dumps(world["logs"])


def test_f_criador_nao_pode_ser_revogado_da_porta(world):
    out = pd.revoke(PERSONA_ACCOUNT, OWNER)
    assert out["ok"] is False and "criador" in out["reason"]
    assert w.handle(PERSONA_ACCOUNT, msg(OWNER, "oi")) is True


# ── g: a recusa deixa trilha (decisão, motivo, quem, conta) ─────────────────

def test_g_recusa_deixa_trilha_com_os_quatro_campos(world):
    w.handle(PERSONA_ACCOUNT, msg(CLIENT, "orcamento pra mim"))
    lines = [line for line in world["logs"] if "decisao=recusa" in line]
    assert len(lines) == 1
    trilha = lines[0]
    assert f"conta={PERSONA_ACCOUNT}" in trilha
    assert f"contato={CLIENT}" in trilha
    assert "quem=Cliente" in trilha
    assert "persona=thais" in trilha or "persona=Thais" in trilha
    assert "terceiro no numero da persona" in trilha


def test_g_trilha_nunca_escreve_o_texto_da_mensagem(world):
    w.handle(PERSONA_ACCOUNT, msg(CLIENT, "segredo-do-cliente-xyz"))
    assert all("segredo-do-cliente-xyz" not in line for line in world["logs"])


def test_g_a_porta_usa_a_mesma_identidade_de_contato_dos_grupos(world):
    """Reuso (lei dele: "se um gate pode, você pode"): um sender_jid só."""
    item = msg(f"{CLIENT}@s.whatsapp.net")
    assert pd.sender_of(item) == gp.sender_jid(item) == CLIENT
    verdict = pd.decide(PERSONA_ACCOUNT, item)
    assert verdict.allowed is False and verdict.sender == CLIENT


def test_g_sem_autor_reconhecivel_a_porta_fecha(world):
    """Sem "quem escreveu" não existe permissão: falha fechada, não licença."""
    no_author = {"chat": CLIENT, "text": "oi", "id": "x", "ts": time.time(),
                 "group": True, "sender": ""}
    verdict = pd.decide(PERSONA_ACCOUNT, no_author)
    assert verdict.applies is True and verdict.allowed is False
    assert "autor da mensagem nao identificado" in verdict.reason
    in_group = {"chat": GROUP, "group": True, "text": "Gino, oi",
                "id": "y", "ts": time.time()}
    assert pd.decide(PERSONA_ACCOUNT, in_group).allowed is False


def test_g_quem_abre_a_porta_fica_registrado_por_pessoa(world):
    """A janela de escuta é do autor (R4), com chave de contato do WhatsApp."""
    w.handle(PERSONA_ACCOUNT, msg(OWNER, "Gino, abre o relatório"))
    assert list(vl.snapshot()) == [vl.whatsapp_key(OWNER, PERSONA_ACCOUNT)]
    w.handle(PERSONA_ACCOUNT, msg(CLIENT, "oi"))
    assert vl.last_key() == vl.whatsapp_key(OWNER, PERSONA_ACCOUNT)
    assert vl.whatsapp_key(CLIENT, PERSONA_ACCOUNT) not in vl.snapshot()


# ── h: sem regressão no DM do dono nem no grupo ─────────────────────────────

def test_h_dm_do_dono_nao_passa_pela_porta(world):
    """A entrada dele no chat direto continua a ordem de 09/11/2026."""
    assert w.is_direct("principal", msg(OWNER)) is True
    assert w.handle("principal", msg(OWNER, "oi")) is True
    assert len(world["calls"]) == 1
    assert not any("[porta persona]" in line for line in world["logs"])


def test_h_grupo_continua_julgado_pelo_veredito_de_grupo(world, monkeypatch):
    """Porta nenhuma na frente do grupo: a lei de grupos é própria (intocada)."""
    seen: list[dict] = []
    monkeypatch.setattr(w, "_process_group",
                        lambda account, item: (seen.append(dict(item)),
                                               True)[1])
    item = {"chat": GROUP, "group": True, "sender": CLIENT,
            "sender_name": "Cliente", "text": "Gino, que horas sao?",
            "id": "g-1", "ts": time.time()}
    assert w.handle(PERSONA_ACCOUNT, item) is True
    assert seen and seen[0]["chat"] == GROUP
    assert world["calls"] == []
    assert not any("[porta persona]" in line for line in world["logs"])


def test_h_conta_sem_persona_nao_tem_porta(world):
    verdict = pd.decide("principal", msg(CLIENT), direct=False)
    assert verdict.applies is False and verdict.allowed is True


def test_h_conta_ambigua_duas_personas_nao_e_porta_de_ninguem(world):
    w.set_personas_source(lambda: [
        dict(PERSONA),
        {"id": "bruna", "name": "Bruna", "direct_whatsapp": True,
         "whatsapp_account": PERSONA_ACCOUNT}])
    assert w.persona_for_account(PERSONA_ACCOUNT) is None
    assert w.handle(PERSONA_ACCOUNT, msg(CLIENT)) is False


# ── os arquivos reais dele: nenhum teste escreve neles ─────────────────────

def _md5(path) -> str | None:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except Exception:
        return None


def test_os_arquivos_reais_sao_so_lidos_e_a_escrita_e_alarme(monkeypatch):
    """Prova de hermetismo com o caminho APONTANDO para o arquivo real."""
    monkeypatch.setattr(pd, "_PATH", pd.DEFAULT_DOOR_PATH)
    antes_porta = _md5(pd.DEFAULT_DOOR_PATH)
    antes_grupos = _md5(gp.allowlist_path())

    def _alarme(*args, **kwargs):
        raise AssertionError("a porta tentou escrever no arquivo real do dono")

    monkeypatch.setattr(pd, "_write", _alarme)
    # Leitura da porta real: não escreve nada, não decide nada sozinho.
    pd.decide(PERSONA_ACCOUNT, msg(CLIENT))
    pd.entries(PERSONA_ACCOUNT)
    pd.load()
    # Sem o fator do dono a recusa vem ANTES de qualquer escrita.
    assert pd.authorize(PERSONA_ACCOUNT, CLIENT, "Cliente")["ok"] is False
    # O criador é irrevogável: morre antes da escrita também.
    assert pd.revoke(PERSONA_ACCOUNT, OWNER)["ok"] is False
    assert _md5(pd.DEFAULT_DOOR_PATH) == antes_porta
    assert _md5(gp.allowlist_path()) == antes_grupos


def test_o_arquivo_da_porta_no_repo_nasce_sem_nenhum_nome():
    """O mecanismo é meu; a lista é dele. Vazio por default = só o criador."""
    raw, status = pd._read_raw(pd.DEFAULT_DOOR_PATH)
    assert status in ("ok", "missing")
    numeros = (raw or {}).get("numeros") or {}
    assert all(not (block or {}).get("permitidos")
               for block in numeros.values())
