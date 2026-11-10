"""O portão de quem ensina apelido — decisão 2 do dono, um caso por teste.

A frase dele (09/11/2026): "Outras vozes precisam de minha permissão ou
autorização momentânea." O portão (``actions.home_control._portao_do_apelido``)
reaproveita os mecanismos que JÁ existem — nenhum mecanismo paralelo:

* fator do dono NA HORA: ``voice_identity.is_owner_turn()`` — voz confirmada
  por voiceprint OU senha digitada (paridade plena de 06/11/2026);
* permissão prévia EXPLÍCITA: grant nominal de ``core/voice_grants`` (que só
  ele cria, com ``confirmed_by`` de dono) cobrindo
  ``execucao/apelidos_da_casa``;
* autorização momentânea NAQUELE turno: a janela de 120 s de
  ``core/actiongate``, aberta só pela voz dele ou pela senha dele.

Os quatro casos obrigatórios da rodada, cada um travado aqui:

1. terceiro sem permissão NÃO escreve — e a falha deixa trilha (quem pediu,
   o que foi pedido, estacionado em ``actiongate.mark_pending``);
2. terceiro com autorização momentânea dele escreve com origem correta;
3. senha digitada dele equivale à voz (grava com fator honesto, sem segredo);
4. nada apaga apelido existente sem ELE confirmar — e apagar é caneta só dele.

Hermetismo: o mapa de apelidos vive em ``tmp_path``; a identidade de voz e as
janelas são estado DE PROCESSO e são limpas antes e depois de cada teste; os
grants ficam em loja temporária injetada por ``voice_grants.set_store``. O
``config/home_aliases.json`` do dono é guardado por md5 no teste final e por
uma prova de que a falha fechada não chega nem a chamar a escrita.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from actions import home_control as hc
from core import actiongate, owner_auth, voice_grants, voice_identity

#: A casa resumida: a luz é a cena (realidade medida em 10/11/2026).
CASA = [
    {"entity_id": "scene.fan_backlight", "state": "unknown",
     "attributes": {"friendly_name": "Luz do teto"}},
    {"entity_id": "switch.tomada_do_escritorio", "state": "off",
     "attributes": {"friendly_name": "Tomada do Escritório"}},
]


def _rede(*args, **kwargs):
    raise AssertionError(f"teste tocou a rede: {args}")


@pytest.fixture
def casa(monkeypatch, tmp_path):
    """REST cego + estados de fixture + mapa isolado + portões zerados."""
    mapa = tmp_path / "home_aliases.json"
    monkeypatch.setattr(hc, "_api", _rede)
    monkeypatch.setattr(hc, "_states", lambda force=False: list(CASA))
    monkeypatch.setattr(hc, "_alias_path", mapa)
    monkeypatch.delenv("JARVIS_HOME_ALIASES", raising=False)
    voice_identity.clear()
    actiongate.close()
    voice_grants.set_store(None)
    yield mapa
    voice_identity.clear()
    actiongate.close()
    voice_grants.set_store(None)


@pytest.fixture
def grants(tmp_path):
    """Loja de grants temporária, injetada no módulo (o padrão é NEGAR)."""
    loja = voice_grants.GrantStore(tmp_path / "voice_grants.json")
    voice_grants.set_store(loja)
    return loja


# ── as bocas de cada fator (o que o main grava no slot por processo) ────────

def _voz_do_dono() -> None:
    voice_identity.set_recent(who="owner", person="dono", privileged=True,
                              confirmed_by="voiceprint_owner")


def _senha_do_dono() -> None:
    voice_identity.set_recent(who="owner", person="dono", privileged=True,
                              confirmed_by="senha_digitada")


def _voz_de_terceiro(person: str = "valmira") -> None:
    voice_identity.set_recent(who="other", person=person, privileged=False,
                              confirmed_by="")


def _ensinar(alias: str = "luz do quarto", entidade: str = "Luz do teto") -> str:
    return hc.home_control({"action": "apelido", "alias": alias,
                            "entidade": entidade})


def _esquecer(alias: str = "luz do quarto", **extra) -> str:
    params = {"action": "esquecer_apelido", "alias": alias}
    params.update(extra)
    return hc.home_control(params)


def _ler(path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("apelidos") or []


# ── 1. terceiro sem permissão: falha fechada, com trilha ────────────────────

def test_terceiro_sem_permissao_nao_escreve_e_deixa_trilha(casa):
    _voz_de_terceiro("valmira")
    out = _ensinar()
    assert "Não anotei" in out
    assert "só pelo dono" in out and "valmira" in out
    assert not casa.exists() or _ler(casa) == []
    # A trilha: quem pediu e o que foi pedido, estacionado no portão de ações.
    pedido = actiongate.pending()
    assert pedido is not None
    assert pedido["title"] == "home_control"
    assert "valmira" in pedido["message"] and "apelido" in pedido["message"]


def test_terceiro_nao_sobrescreve_o_pendente_do_proprio_dono(casa):
    """O slot de pendente é um só: o pedido do dono (um envio, por exemplo)
    não pode ser varrido pela tentativa de um terceiro."""
    actiongate.mark_pending("send_message", "para a oficina")
    _voz_de_terceiro("valmira")
    assert "Não anotei" in _ensinar()
    assert actiongate.pending()["title"] == "send_message"


def test_automacao_sem_voz_nenhuma_nao_escreve(casa):
    """Origem não declarada (agendador, rotina, modelo sozinho): recusa seca.
    Slot vazio não é permissão — é exatamente a lacuna que o portão fecha."""
    voice_identity.clear()
    assert "Não anotei" in _ensinar()
    assert not casa.exists() or _ler(casa) == []
    assert "quem=?" in actiongate.pending()["message"]


def test_portao_vem_antes_da_busca_terceiro_nao_sonda_a_casa(casa):
    """A recusa acontece antes de resolver a entidade: o ensino não pode ser
    usado por visitante como sonda do que existe na casa."""
    _voz_de_terceiro("valmira")
    out = _ensinar("coisa inexistente", "Aparelho Que Não Existe")
    assert "só pelo dono" in out
    assert "Não encontrei" not in out and "não existe" not in out


# ── 2. autorização momentânea dada NAQUELE turno ────────────────────────────

def test_terceiro_com_autorizacao_momentanea_do_dono_escreve_com_origem(casa):
    _voz_de_terceiro("valmira")
    # O dono disse "pode" (voz) — a janela de ação de 120 s abriu.
    actiongate.open_window(factor=owner_auth.FACTOR_VOICE)
    out = _ensinar()
    assert "Anotado" in out and "scene.fan_backlight" in out
    entry = _ler(casa)[0]
    assert entry["origem"] == hc.ORIGEM_ALIAS_JANELA
    assert entry["quem"] == "valmira"
    assert entry["fator"] == "janela_do_dono"
    assert entry["ensinado_em"] == date.today().isoformat()
    # Momentânea quer dizer momentânea: fechada a janela, recusa de novo.
    actiongate.close()
    voice_identity.clear()
    _voz_de_terceiro("valmira")
    assert "Não anotei" in _ensinar("luz da sala", "Luz do teto")
    assert [e["apelido"] for e in _ler(casa)] == ["luz do quarto"]


def test_janela_aberta_por_envio_de_15_min_nao_autoriza_ensino(casa):
    """A janela antecipada de envio é do escopo de envio: não vira permissão
    de escrever no mapa da casa (uma coisa não abre a porta da outra)."""
    _voz_de_terceiro("valmira")
    actiongate.arm_send_window(factor=owner_auth.FACTOR_VOICE)
    assert "Não anotei" in _ensinar()
    assert not casa.exists() or _ler(casa) == []


# ── 3. senha digitada dele equivale à voz ───────────────────────────────────

def test_senha_digitada_do_dono_ensina_igual_a_voz_dele(casa):
    _senha_do_dono()
    assert "Anotado" in _ensinar("luz do quarto", "Luz do teto")
    entrada_texto = _ler(casa)[0]
    assert entrada_texto["origem"] == hc.ORIGEM_ALIAS_DONO
    assert entrada_texto["quem"] == "dono"
    # O fator fica registrado sem segredo nenhum no arquivo.
    assert entrada_texto["fator"] == "owner_password"
    _voz_do_dono()
    assert "Anotado" in _ensinar("luz do teto", "Luz do teto")
    entrada_voz = _ler(casa)[1]
    assert entrada_voz["origem"] == hc.ORIGEM_ALIAS_DONO
    assert entrada_voz["fator"] == "voz_do_dono"
    texto = casa.read_text(encoding="utf-8").lower()
    for proibido in ("token", "senha", "secret", "api_key"):
        assert proibido not in texto


# ── permissão prévia explícita (grant dado por ele antes) ───────────────────

def test_permissao_previa_do_dono_ensina_com_origem_do_grant(casa, grants):
    _voz_de_terceiro("valmira")
    assert "Não anotei" in _ensinar()  # sem grant, não passa
    grants.add_grant("valmira",
                     voice_grants.Scope(voice_grants.SCOPE_EXECUTE,
                                        hc.ALIAS_GRANT_SCOPE),
                     owner_ok=True, confirmed_by="voiceprint_owner")
    assert "Anotado" in _ensinar()
    entry = _ler(casa)[0]
    assert entry["origem"] == hc.ORIGEM_ALIAS_GRANT
    assert entry["quem"] == "valmira"
    assert entry["fator"] == "permissao_do_dono"


def test_grant_de_outro_escopo_nao_ensina(casa, grants):
    """Leitura concedida não vira escrita no mapa: o padrão é NEGAR."""
    grants.add_grant("valmira", voice_grants.Scope(voice_grants.SCOPE_READ),
                     owner_ok=True, confirmed_by="voiceprint_owner")
    _voz_de_terceiro("valmira")
    assert "Não anotei" in _ensinar()
    assert not casa.exists() or _ler(casa) == []


def test_grant_nao_vem_de_terceiro_e_nem_se_autoconcede(casa, grants):
    """Grant só nasce do veredito privilegiado DELE: confirmed_by de terceiro
    (ou owner_ok falso) não cria permissão — e sem grant, não ensina."""
    grants.add_grant("valmira",
                     voice_grants.Scope(voice_grants.SCOPE_EXECUTE,
                                        hc.ALIAS_GRANT_SCOPE),
                     owner_ok=True, confirmed_by="frase_de_terceiro")
    grants.add_grant("joana",
                     voice_grants.Scope(voice_grants.SCOPE_EXECUTE,
                                        hc.ALIAS_GRANT_SCOPE),
                     owner_ok=False, confirmed_by="voiceprint_owner")
    _voz_de_terceiro("valmira")
    assert "Não anotei" in _ensinar()
    _voz_de_terceiro("joana")
    assert "Não anotei" in _ensinar("luz da sala", "Luz do teto")
    assert not casa.exists() or _ler(casa) == []


# ── 4. nada apaga apelido existente sem ELE confirmar ───────────────────────

def test_nada_apaga_apelido_existente_sem_o_dono_confirmar(casa, grants):
    _voz_do_dono()
    assert "Anotado" in _ensinar()
    # O próprio dono pergunta antes de apagar o que já existe.
    pergunta = _esquecer()
    assert "Confirma" in pergunta and "scene.fan_backlight" in pergunta
    assert [e["apelido"] for e in _ler(casa)] == ["luz do quarto"]
    # Terceiro — mesmo com janela momentânea aberta ou com grant — não apaga.
    _voz_de_terceiro("valmira")
    actiongate.open_window(factor=owner_auth.FACTOR_VOICE)
    assert "Não esqueci" in _esquecer(confirmar=True)
    assert "só se apaga pelo dono" in _esquecer(confirmar=True)
    actiongate.close()
    grants.add_grant("valmira",
                     voice_grants.Scope(voice_grants.SCOPE_EXECUTE,
                                        hc.ALIAS_GRANT_SCOPE),
                     owner_ok=True, confirmed_by="voiceprint_owner")
    assert "Não esqueci" in _esquecer(confirmar=True)
    assert [e["apelido"] for e in _ler(casa)] == ["luz do quarto"]
    # E a caneta final é dele: confirmação dada, apelido sai.
    _voz_do_dono()
    assert "não é mais atalho" in _esquecer(confirmar=True)
    assert _ler(casa) == []


def test_apelido_inexistente_nao_precisa_de_confirmacao_para_recusar(casa):
    """Esquecer o que não está no mapa não é apagar nada: recusa seca, mapa
    intocado — o portão do dono continua na frente."""
    _voz_do_dono()
    assert "Não esqueci" in _esquecer("luz que nunca houve")
    assert not casa.exists() or _ler(casa) == []


# ── a leitura também não inventa dono ───────────────────────────────────────

def test_leitura_nao_inventa_origem_dono_para_entrada_sem_origem(casa):
    """Entrada antiga SEM origem no disco é lida como ``nao_declarada`` —
    o mapa nunca alega que o dono ensinou o que não sabe quem ensinou."""
    casa.write_text(json.dumps({"version": 1, "regra": "teste",
                                "atualizado_em": "2026-11-09",
                                "apelidos": [{"apelido": "luz do quarto",
                                              "entidade": "scene.fan_backlight",
                                              "nome": "Luz do teto",
                                              "dominio": "scene",
                                              "desde": "2026-11-09"}]}),
                    encoding="utf-8")
    entries, _reason = hc.alias_entries()
    assert entries[0]["origem"] == actiongate.ORIGIN_UNDECLARED
    assert entries[0]["quem"] == "" and entries[0]["fator"] == ""


# ── o mapa real do dono: nenhum teste escreve nele ──────────────────────────

def _md5_mapa_real() -> str | None:
    try:
        return hashlib.md5(hc.DEFAULT_ALIAS_PATH.read_bytes()).hexdigest()
    except Exception:
        return None


def test_falha_fechada_nao_chama_a_escrita_nem_com_o_mapa_real(monkeypatch):
    """Sem identidade, a recusa acontece antes de qualquer escrita — provado
    com o caminho APONTANDO para o arquivo real e a escrita virando alarme."""
    monkeypatch.setattr(hc, "_alias_path", hc.DEFAULT_ALIAS_PATH)
    monkeypatch.setattr(hc, "_api", _rede)

    def _alarme(*args, **kwargs):
        raise AssertionError("falha fechada tentou escrever no mapa")

    monkeypatch.setattr(hc, "_write_alias_doc", _alarme)
    voice_identity.clear()
    assert "Não anotei" in _ensinar()
    assert "Não esqueci" in _esquecer("luz", confirmar=True)


def test_estes_testes_nunca_escrevem_no_mapa_real_do_dono(casa, grants):
    """Guarda final, padrão de ``test_silence_mode.py``: toda a rodada acima
    roda de novo com o mapa em tmp_path e o md5 do arquivo real não muda."""
    antes = _md5_mapa_real()
    _voz_de_terceiro("valmira")
    assert "Não anotei" in _ensinar()
    actiongate.open_window(factor=owner_auth.FACTOR_VOICE)
    assert "Anotado" in _ensinar()
    actiongate.close()
    _senha_do_dono()
    assert "Anotado" in _ensinar("luz do teto", "Luz do teto")
    assert "Confirma" in _esquecer()
    assert "não é mais atalho" in _esquecer(confirmar=True)
    assert _md5_mapa_real() == antes, (
        "config/home_aliases.json do dono foi tocado pelo teste")
