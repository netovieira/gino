"""``config/home_aliases.json`` e a resolução de nomes da casa — prevenção, não prosa.

O caso real (WhatsApp do dono, 09/11/2026 17:35): "acende a luz do quarto".
O alvo foi procurado só no domínio chutado ("luz" → ``light``), a casa não tem
nenhuma entidade ``light`` e a resposta foi "a casa não tem luz nenhuma". A luz
existia: é a cena ``scene.fan_backlight``, chamada **Luz do teto**. A reclamação
não era sobre essa luz — era sobre a classe do defeito: nome ensinado que morre
na conversa em vez de virar arquivo ("regra do dono vira arquivo").

Este arquivo trava o mecanismo inteiro, um caso por teste:

* prioridade: apelido → nome em TODO domínio relevante → igualdade frouxa →
  nunca negar em seco (candidatos + pergunta);
* o mapa persiste em ``config/home_aliases.json`` e é relido do disco a cada
  chamada (o nome novo vale sem reiniciar — precedente de ``core/flows/config.py``
  e da alavanca ``core/workflow_gate.py``);
* as quatro falhas previsíveis: JSON ausente, JSON corrompido, apelido
  apontando para entidade que sumiu, nome repetido em dois domínios;
* registro e esquecimento pela boca do dono.

Hermetismo: o Home Assistant é o ``AssistenteFalso`` abaixo (estados de
fixture e REST que anota o POST de serviço em ``calls`` em vez de discar —
qualquer outra fala pela rede é ``AssertionError``). O mapa de apelidos de
cada teste vive em ``tmp_path``; o arquivo real do dono não é lido nem escrito.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from actions import home_control as hc

#: A casa do dono de verdade: NENHUMA entidade ``light``, a luz é uma cena.
CASA_DO_DONO = [
    {"entity_id": "scene.fan_backlight", "state": "2026-11-09T20:36:22+00:00",
     "attributes": {"friendly_name": "Luz do teto"}},
    {"entity_id": "fan.quarto_room_ceiling_fan_and_light_ventilador",
     "state": "off", "attributes": {"friendly_name": "Ventilador"}},
    {"entity_id": "switch.tomada_dupla_wi_fi_socket_1", "state": "off",
     "attributes": {"friendly_name": "Caixa de Som"}},
    {"entity_id": "climate.ar_condicionado", "state": "heat_cool",
     "attributes": {"friendly_name": "Ar Condicionado"}},
    {"entity_id": "media_player.55_crystal_uhd", "state": "off",
     "attributes": {"friendly_name": '55" Crystal UHD'}},
    {"entity_id": "sensor.temperatura", "state": "23",
     "attributes": {"friendly_name": "Temperatura externa"}},
]

#: Uma casa com luzes de verdade, para os testes que não podem depender do mapa.
CASA_COM_LUZES = [
    {"entity_id": "light.luz_da_sala", "state": "on",
     "attributes": {"friendly_name": "Luz da Sala"}},
    {"entity_id": "light.luz_do_quarto", "state": "off",
     "attributes": {"friendly_name": "Luz do Quarto"}},
    {"entity_id": "switch.tomada_do_escritorio", "state": "off",
     "attributes": {"friendly_name": "Tomada do Escritório"}},
    {"entity_id": "scene.filme", "state": "unknown",
     "attributes": {"friendly_name": "Cena Filme"}},
]


class AssistenteFalso:
    """O cliente do Home Assistant em teste: estados fixos, zero rede.

    ``calls`` registra cada chamada de serviço que o plugin tentou, no formato
    exato do REST (domínio, serviço, dados) — o que prova o payload sem tocar
    na rede. Qualquer outra fala (GET, outro caminho, outro verbo) cai no
    ``AssertionError`` do ``_api``.
    """

    def __init__(self, states: list[dict]) -> None:
        self.states = list(states)
        self.calls: list[tuple[str, str, dict]] = []

    def api(self, method: str, path: str, payload: dict | None = None):
        prefix = "/api/services/"
        if method == "POST" and path.startswith(prefix):
            domain, _, service = path[len(prefix):].partition("/")
            self.service(domain, service, payload or {})
            return None
        raise AssertionError(
            f"teste tocou a rede: {method} {path} (use o estado de fixture)")

    def service(self, domain: str, service: str, data: dict) -> None:
        self.calls.append((domain, service, dict(data)))


@pytest.fixture
def casa(monkeypatch, tmp_path, request):
    """Estados de fixture + REST cego + mapa de apelidos isolado no tmp."""
    states = getattr(request, "param", CASA_DO_DONO)
    fake = AssistenteFalso(states)
    mapa = tmp_path / "home_aliases.json"
    monkeypatch.setattr(hc, "_api", fake.api)
    monkeypatch.setattr(hc, "_states", lambda force=False: list(fake.states))
    monkeypatch.setattr(hc, "_alias_path", mapa)
    monkeypatch.delenv("JARVIS_HOME_ALIASES", raising=False)
    fake.mapa = mapa
    fake.alias = mapa
    return fake


def _gravar_mapa(path: Path, apelidos: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "regra": "teste",
                                "atualizado_em": "2026-11-09",
                                "apelidos": apelidos}, ensure_ascii=False),
                    encoding="utf-8")


def _ler_mapa(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── o caso do dono, travado de ponta a ponta ────────────────────────────────

def test_luz_do_quarto_acende_a_cena_e_nao_o_dominio_chutado(casa):
    """O incidente de 17:35: alvo "luz do quarto" → scene.fan_backlight."""
    _gravar_mapa(casa.mapa, [{"apelido": "luz do quarto",
                             "entidade": "scene.fan_backlight",
                             "nome": "Luz do teto", "dominio": "scene"}])
    out = hc.home_control({"action": "ligar", "entity_id": "luz do quarto"})
    assert out == "Cena Luz do teto ativada."
    assert casa.calls == [("scene", "turn_on",
                          {"entity_id": "scene.fan_backlight"})]


def test_apelido_nao_precisa_de_verbo_na_frase(casa):
    """"acende a luz do quarto" e "luz do quarto" são o mesmo apelido."""
    _gravar_mapa(casa.mapa, [{"apelido": "luz do quarto",
                             "entidade": "scene.fan_backlight",
                             "nome": "Luz do teto", "dominio": "scene"}])
    entity, error = hc.resolve("acende a luz do quarto", casa.states)
    assert error == ""
    assert entity["entity_id"] == "scene.fan_backlight"


def test_apelido_simples_luz_vale_na_casa_sem_light(casa):
    """"luz" sozinho: o mapa manda, o domínio chutado não fecha a porta."""
    _gravar_mapa(casa.mapa, [{"apelido": "luz",
                             "entidade": "scene.fan_backlight",
                             "nome": "Luz do teto", "dominio": "scene"}])
    entity, error = hc.resolve("luz", casa.states)
    assert error == ""
    assert entity["entity_id"] == "scene.fan_backlight"


# ── prioridade (a) → (b) → (c) → (d) ────────────────────────────────────────

def test_apelido_tem_prioridade_sobre_o_nome_parecido(casa):
    """O que o dono ensinou vence o fuzzy: prioridade (a)."""
    _gravar_mapa(casa.mapa, [{"apelido": "caixa de som",
                             "entidade": "switch.tomada_dupla_wi_fi_socket_1",
                             "nome": "Caixa de Som", "dominio": "switch"}])
    entity, error = hc.resolve("caixa de som", casa.states)
    assert error == ""
    assert entity["entity_id"] == "switch.tomada_dupla_wi_fi_socket_1"


def test_nome_humano_e_procurado_em_todo_dominio_relevante(casa):
    """Prioridade (b): o nome está em ``scene``, e eu chutei ``light``."""
    entity, error = hc.resolve("luz do teto", casa.states)
    assert error == ""
    assert entity["entity_id"] == "scene.fan_backlight"


def test_busca_em_todos_os_dominios_atinge_climate_fan_e_media(casa):
    """(b) não é só light/scene: clima, ventilador, tv e cortina também."""
    want = {"ar condicionado": "climate.ar_condicionado",
            "ventilador": "fan.quarto_room_ceiling_fan_and_light_ventilador",
            "55 crystal uhd": "media_player.55_crystal_uhd"}
    for nome, alvo in want.items():
        entity, error = hc.resolve(nome, casa.states)
        assert error == "", nome
        assert entity["entity_id"] == alvo


def test_igualdade_frouxa_sem_acento_sem_pontuacao(casa):
    """Prioridade (c): "LUZ DO TETO!", "luz-do-teto" e "tomada do escritorio"."""
    for variante in ("LUZ DO TETO!", "luz-do-teto", " Luz   do  teto ",
                     "luz.do.teto"):
        entity, error = hc.resolve(variante, casa.states)
        assert error == "", variante
        assert entity["entity_id"] == "scene.fan_backlight"
    entity, error = hc.resolve("tomada do escritorio", CASA_COM_LUZES)
    assert error == ""
    assert entity["entity_id"] == "switch.tomada_do_escritorio"


def test_nada_de_recusa_seca_devolve_os_parecidos_e_pergunta(casa):
    """Prioridade (d): "luz do banheiro" nunca mais é 'não existe' mudo."""
    entity, error = hc.resolve("luz do banheiro", casa.states)
    assert entity is None
    assert "luz do banheiro" in error
    assert "Luz do teto" in error and "scene.fan_backlight" in error
    assert any(marker in error for marker in ("?", "Qual", "Era um desses"))


def test_a_palavra_luz_sozinha_pergunta_qual_em_vez_de_errar(casa):
    """Sem mapa e com várias luzes, "luz" pergunta — não escolhe em sorteio."""
    entity, error = hc.resolve("luz", CASA_COM_LUZES)
    assert entity is None
    assert "Qual deles?" in error


# ── prevenção: um caso, um teste ────────────────────────────────────────────

def test_prevencao_json_ausente_busca_continua_e_status_diz_o_motivo(casa):
    """Sem arquivo: nada quebra, a busca por nome segue, a sonda vê o vazio."""
    assert not casa.mapa.exists()
    entries, reason = hc.alias_entries()
    assert entries == [] and "ausente" in reason
    entity, error = hc.resolve("luz do teto", casa.states)
    assert error == "" and entity["entity_id"] == "scene.fan_backlight"
    status = hc.aliases_status()
    assert status["ok"] is False and status["estado"] == "ausente"


def test_prevencao_json_corrompido_ignora_mapa_e_recusa_escrever_por_cima(casa):
    """Arquivo podre não decide nada e não é coberto por um default."""
    casa.mapa.write_text('{"apelidos": [ {"apelido":', encoding="utf-8")
    entries, reason = hc.alias_entries()
    assert entries == [] and "corrompido" in reason
    entity, error = hc.resolve("luz do teto", casa.states)
    assert error == "" and entity["entity_id"] == "scene.fan_backlight"
    got = hc.save_alias("luz do quarto", "scene.fan_backlight", "Luz do teto")
    assert got["ok"] is False and "corrompido" in got["reason"]
    # o lixo do dono continua no disco, intacto
    assert casa.mapa.read_text(encoding="utf-8").startswith('{"apelidos": [ {')


def test_prevencao_apelido_aponta_para_entidade_que_sumiu(casa):
    """Alvo trocado: a frase diz qual apelido ficou velho e o que existe agora."""
    _gravar_mapa(casa.mapa, [{"apelido": "luz do quarto",
                             "entidade": "light.luz_do_quarto",
                             "nome": "Luz do Quarto", "dominio": "light"}])
    entity, error = hc.resolve("luz do quarto", casa.states)
    assert entity is None
    assert "light.luz_do_quarto" in error and "não existe mais" in error
    assert "Luz do teto" in error  # o candidato certo, sem adivinhar
    assert casa.calls == []


def test_prevencao_nome_repetido_em_dois_dominios_pergunta_em_vez_de_escolher():
    """Dois "Luz do Teto" (scene e light): ambiguidade dita, nunca sorteio."""
    estados = [
        {"entity_id": "scene.luz_do_teto", "state": "unknown",
         "attributes": {"friendly_name": "Luz do Teto"}},
        {"entity_id": "light.luz_do_teto", "state": "off",
         "attributes": {"friendly_name": "Luz do Teto"}},
    ]
    entity, error = hc.resolve_entity("luz do teto", estados)
    assert entity is None
    assert "Tem mais de um" in error
    assert "scene.luz_do_teto" in error and "light.luz_do_teto" in error


def test_prevencao_apelido_duplicado_para_entidades_diferentes_e_conflito(casa):
    """O mesmo nome gravado duas vezes, alvos diferentes: resolve = conflito."""
    _gravar_mapa(casa.mapa, [
        {"apelido": "luz", "entidade": "scene.fan_backlight",
         "nome": "Luz do teto", "dominio": "scene"},
        {"apelido": "luz", "entidade": "switch.tomada_dupla_wi_fi_socket_1",
         "nome": "Caixa de Som", "dominio": "switch"},
    ])
    entity, error = hc.resolve("luz", casa.states)
    assert entity is None
    assert "conflito" in error.lower() or "trocar" in error
    assert casa.calls == []


def test_prevencao_registro_aponta_para_entidade_inexistente_recusa_ancora(casa):
    """Apelido nasce ancorado em entidade real: alvo falso não entra no mapa."""
    out = hc.home_control({"action": "apelido", "alias": "luz da varanda",
                          "entidade": "luz da varanda"})
    assert "Não anotei" in out
    assert not casa.mapa.exists() or _ler_mapa(casa.mapa)["apelidos"] == []


def test_prevencao_trocar_exigido_para_mudar_o_apontamento(casa):
    """Regravar para outra entidade sem trocar=true é recusado."""
    _gravar_mapa(casa.mapa, [{"apelido": "luz",
                             "entidade": "scene.fan_backlight",
                             "nome": "Luz do teto", "dominio": "scene"}])
    got = hc.save_alias("luz", "switch.tomada_dupla_wi_fi_socket_1",
                        "Caixa de Som")
    assert got["ok"] is False and "trocar" in got["reason"]
    assert _ler_mapa(casa.mapa)["apelidos"][0]["entidade"] == "scene.fan_backlight"
    ok = hc.save_alias("luz", "switch.tomada_dupla_wi_fi_socket_1",
                       "Caixa de Som", trocar=True)
    assert ok["ok"] is True
    assert _ler_mapa(casa.mapa)["apelidos"][0]["entidade"] == (
        "switch.tomada_dupla_wi_fi_socket_1")


# ── o registro pela boca do dono ────────────────────────────────────────────

def test_registrar_apelido_pelo_nome_humano_ancora_na_entidade_real(casa):
    """'a luz do quarto é a cena Luz do teto' → mapa com o id verdadeiro."""
    out = hc.home_control({"action": "apelido", "alias": "luz do quarto",
                          "entidade": "Luz do teto"})
    assert "Anotado" in out and "scene.fan_backlight" in out
    entry = _ler_mapa(casa.mapa)["apelidos"][0]
    assert entry["apelido"] == "luz do quarto"
    assert entry["entidade"] == "scene.fan_backlight"
    assert entry["nome"] == "Luz do teto"


def test_apelido_registrado_resolve_sem_reiniciar_o_processo(casa):
    """O valor novo vale na chamada seguinte: o mapa é lido do disco sempre."""
    _gravar_mapa(casa.mapa, [])
    assert hc.alias_entries()[0] == []
    hc.save_alias("luz do quarto", "scene.fan_backlight", "Luz do teto")
    again, _reason = hc.alias_entries()
    assert [e["apelido"] for e in again] == ["luz do quarto"]
    entity, error = hc.resolve("luz do quarto", casa.states)
    assert error == "" and entity["entity_id"] == "scene.fan_backlight"


def test_registrar_apelido_idempotente_nao_duplica(casa):
    """Dois registros do mesmo nome e mesma entidade = uma entrada."""
    hc.home_control({"action": "apelido", "alias": "luz",
                     "entidade": "scene.fan_backlight"})
    hc.home_control({"action": "apelido", "alias": "luz",
                     "entidade": "scene.fan_backlight"})
    assert len(_ler_mapa(casa.mapa)["apelidos"]) == 1


def test_mapa_guarda_so_nome_e_entidade_nunca_segredo(casa):
    """O arquivo é config, não cofre: só as chaves conhecidas entram."""
    hc.home_control({"action": "apelido", "alias": "luz",
                     "entidade": "scene.fan_backlight"})
    doc = _ler_mapa(casa.mapa)
    assert set(doc) == {"version", "regra", "atualizado_em", "apelidos"}
    for entry in doc["apelidos"]:
        assert set(entry) <= set(hc.ALIAS_FIELDS)
    texto = json.dumps(doc, ensure_ascii=False).lower()
    for proibido in ("token", "senha", "secret", "api_key"):
        assert proibido not in texto


def test_listar_e_esquecer_apelido_pelo_plugin(casa):
    """"quais são meus apelidos" e "esquece a luz" respondem e gravam."""
    hc.home_control({"action": "apelido", "alias": "luz",
                     "entidade": "scene.fan_backlight"})
    out = hc.home_control({"action": "apelidos"})
    assert "luz → Luz do teto (scene.fan_backlight)" in out
    esquecido = hc.home_control({"action": "esquecer_apelido", "alias": "luz"})
    assert "não é mais atalho" in esquecido
    assert hc.alias_entries()[0] == []


def test_consultar_apelido_sem_entidade_devolve_o_que_esta_gravado(casa):
    _gravar_mapa(casa.mapa, [{"apelido": "luz",
                             "entidade": "scene.fan_backlight",
                             "nome": "Luz do teto", "dominio": "scene"}])
    out = hc.home_control({"action": "apelido", "alias": "luz"})
    assert "está anotado como Luz do teto" in out


def test_cena_nao_desliga_e_isso_e_dito_com_alternativa(casa):
    """turn_off em cena: recusa explicada, sem sumiço e sem serviço enviado."""
    _gravar_mapa(casa.mapa, [{"apelido": "luz",
                             "entidade": "scene.fan_backlight",
                             "nome": "Luz do teto", "dominio": "scene"}])
    out = hc.home_control({"action": "desligar", "entity_id": "luz"})
    assert "cena" in out.lower()
    assert casa.calls == []


def test_list_light_vazio_mostra_onde_o_nome_pode_estar(casa):
    """A frase mentirosa ("não tem luz nenhuma") não sai mais sem os vizinhos."""
    out = hc.home_control({"action": "list", "domain": "light"})
    assert "Nenhuma entidade do domínio 'light'" in out
    assert "scene" in out and "Em outros domínios existe" in out


def test_action_desconhecida_continua_recusada_sem_rede(casa):
    out = hc.home_control({"action": "registrar_tudo_em_light"})
    assert "Ação desconhecida" in out
    assert casa.calls == []
