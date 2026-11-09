"""O mapa de apelidos da casa: o nome ensinado pelo dono vira arquivo (09/11/2026).

Cada teste abaixo fecha um buraco PREVISÍVEL do veredito de 17:35 — "acende a
luz do quarto" virou "a casa não tem luz" porque a busca olhou só o domínio
chutado (``light``) e a luz do quarto do dono é a CENA ``scene.fan_backlight``
("Luz do teto"), que no Home Assistant dele não existe nenhum ``light.*``.

Nada aqui toca a rede: o Home Assistant é um cliente falso que devolve estados
de fixture e registra em memória os serviços chamados (``hc._api`` é a única
porta de rede do plugin, e é ela que se patcheia).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from actions import home_control as hc

#: O que existe na casa do dono (medido no /api/states em 09/11/2026): cena.
CASA_DO_DONO = [
    {"entity_id": "scene.fan_backlight", "state": "2026-11-09T20:36:22+00:00",
     "attributes": {"friendly_name": "Luz do teto"}},
    {"entity_id": "switch.tomada_do_escritorio", "state": "off",
     "attributes": {"friendly_name": "Tomada do Escritório"}},
    {"entity_id": "switch.ar_condicionado_ionizador", "state": "off",
     "attributes": {"friendly_name": "Ar Condicionado Ionizador"}},
    {"entity_id": "sensor.temperatura_sala", "state": "23",
     "attributes": {"friendly_name": "Temperatura da Sala"}},
]


class FakeHomeAssistant:
    """O cliente falso: responde ``/api/states`` e anota cada serviço chamado."""

    def __init__(self, states):
        self.states = [dict(entity) for entity in states]
        self.calls: list[tuple] = []

    def api(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == "/api/states":
            return [dict(entity) for entity in self.states]
        return {}

    @property
    def services(self) -> list[tuple[str, dict]]:
        return [(path, payload) for _method, path, payload in self.calls
                if path.startswith("/api/services/")]


@pytest.fixture(autouse=True)
def _sem_rede_e_sem_cache(tmp_path, monkeypatch):
    """Nenhum teste disca o Home Assistant e nenhum estado velho vaza do anterior."""
    hc.configure_aliases(tmp_path / "home_aliases.json")
    hc._invalidate()
    monkeypatch.setattr(hc, "_token", lambda: "token-de-teste")
    yield
    hc.configure_aliases(None)
    hc._invalidate()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Liga o plugin ao Home Assistant falso e devolve o cliente."""
    def _bind(states):
        fake = FakeHomeAssistant(states)
        monkeypatch.setattr(hc, "_api", fake.api)
        hc._invalidate()
        return fake
    return _bind


def write_map(path: Path, entries: list[dict]) -> Path:
    """O mapa como o plugin o grava: doc com ``apelidos``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "atualizado_em": "2026-11-09",
                                "apelidos": entries}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def alias(apelido: str, entidade: str, nome: str = "Luz do teto") -> dict:
    return {"apelido": apelido, "entidade": entidade, "nome": nome,
            "dominio": entidade.split(".", 1)[0], "desde": "2026-11-09",
            "origem": "dono"}


# ── (a) o apelido ensinado ───────────────────────────────────────────────────

def test_apelido_bate_em_cena_e_a_cena_e_ativada(client):
    """O caso das 17:35: "luz do quarto" é cena e cena ACENDE com turn_on."""
    fake = client(CASA_DO_DONO)
    write_map(hc.alias_path(), [alias("luz do quarto", "scene.fan_backlight")])
    out = hc.home_control({"action": "ligar", "entity_id": "luz do quarto"})
    assert fake.services == [("/api/services/scene/turn_on",
                              {"entity_id": "scene.fan_backlight"})], out
    assert "Luz do teto" in out


def test_apelido_atingido_pela_fala_completa(client):
    """"acende a luz do quarto" e "luz do quarto" são o mesmo apelido."""
    fake = client(CASA_DO_DONO)
    write_map(hc.alias_path(), [alias("luz do quarto", "scene.fan_backlight")])
    hc.home_control({"action": "turn_on", "entity_id": "a luz do quarto"})
    assert fake.services == [("/api/services/scene/turn_on",
                              {"entity_id": "scene.fan_backlight"})]


def test_apelido_para_duas_entidades_diferentes_nao_sorteia(client):
    """Conflito no mapa: nada é escolhido no chute, a recusa diz os dois alvos."""
    fake = client(CASA_DO_DONO)
    write_map(hc.alias_path(), [alias("luz", "scene.fan_backlight"),
                               alias("luz", "switch.tomada_do_escritorio",
                                     nome="Tomada do Escritório")])
    out = hc.home_control({"action": "ligar", "entity_id": "luz"})
    assert fake.services == []
    assert "entidades diferentes" in out


def test_apelido_aponta_para_entidade_sumida_cai_na_lista_de_candidatos(client):
    """Mapa ensinado antes do aparelho sumir: diz o alvo morto E os parecidos."""
    fake = client(CASA_DO_DONO)
    write_map(hc.alias_path(), [alias("luz do quarto", "scene.luz_do_quarto")])
    out = hc.home_control({"action": "ligar", "entity_id": "luz do quarto"})
    assert fake.services == []
    assert "scene.luz_do_quarto" in out and "não existe mais" in out
    assert "Luz do teto" in out and "desses" in out


def test_apelido_nao_rouba_nome_mais_especifico(client):
    """O apelido nu "luz" não engole "luz da sala": nome vence apelido torto."""
    fake = client([{"entity_id": "light.luz_da_sala", "state": "off",
                    "attributes": {"friendly_name": "Luz da Sala"}},
                   *CASA_DO_DONO])
    write_map(hc.alias_path(), [alias("luz", "scene.fan_backlight")])
    entity, error = hc.resolve("luz da sala", fake.states)
    assert error == "" and entity["entity_id"] == "light.luz_da_sala"


# ── (b) o nome humano em QUALQUER domínio, não só o chutado ──────────────────

def test_nome_de_luz_que_na_casa_e_uma_tomada(client):
    """A palavra "abajur" chuta ``light``; o nome mora em ``switch`` -- acha."""
    states = [{"entity_id": "switch.abajur", "state": "off",
               "attributes": {"friendly_name": "Abajur"}}, *CASA_DO_DONO]
    fake = client(states)
    entity, error = hc.resolve("abajur", states)
    assert error == "" and entity["entity_id"] == "switch.abajur"
    hc.home_control({"action": "ligar", "entity_id": "abajur"})
    assert fake.services == [("/api/services/switch/turn_on",
                              {"entity_id": "switch.abajur"})]


def test_ar_condicionado_que_na_casa_e_um_switch(client):
    """Domínio sem nenhuma entidade não fecha a porta: o nome é procurado fora."""
    entity, error = hc.resolve("ar condicionado ionizador", CASA_DO_DONO)
    assert error == ""
    assert entity["entity_id"] == "switch.ar_condicionado_ionizador"


def test_nunca_negar_em_seco_devolve_candidatos_e_pergunta(client):
    """(d) nada existe: candidatos parecidos + pergunta, jamais "não tem luz"."""
    client(CASA_DO_DONO)
    entity, error = hc.resolve("luz do banho", CASA_DO_DONO)
    assert entity is None
    assert "Não encontrei" in error and "Era um desses" in error
    assert "Nenhuma entidade do domínio" not in error


def test_dois_dominios_com_nome_igual_devolvem_escolha(client):
    """light.portao e switch.portao com o mesmo nome: pergunta, não o primeiro."""
    states = [
        {"entity_id": "light.portao_da_garagem", "state": "off",
         "attributes": {"friendly_name": "Portão da Garagem"}},
        {"entity_id": "switch.portao_da_garagem", "state": "on",
         "attributes": {"friendly_name": "Portão da Garagem"}},
    ]
    fake = client(states)
    entity, error = hc.resolve("portao da garagem", states)
    assert entity is None
    assert "Tem mais de um" in error and "Qual?" in error
    assert "light.portao_da_garagem" in error
    assert "switch.portao_da_garagem" in error
    assert fake.services == []


# ── (c) acento, pontuação e caixa não importam ───────────────────────────────

def test_acento_caixa_e_pontuacao_nao_importam_no_nome(client):
    client(CASA_DO_DONO)
    for query in ("TOMADA DO ESCRITORIO", "tomada do escritorio",
                  "Tomada, do Escritório!", "tomada_do_escritorio"):
        entity, error = hc.resolve(query, CASA_DO_DONO)
        assert error == "", query
        assert entity["entity_id"] == "switch.tomada_do_escritorio", query


def test_acento_caixa_e_pontuacao_nao_importam_no_apelido(client):
    """O apelido é comparado dobrado: "Luz do Quarto" gravado vale p/ "luz QUARTO"."""
    write_map(hc.alias_path(), [alias("Luz do Quarto", "scene.fan_backlight")])
    for query in ("luz do quarto", "LUZ DO QUARTO", "luz-do-quarto"):
        entry, conflict = hc.alias_for(query)
        assert conflict == "" and entry is not None, query
        assert entry["entidade"] == "scene.fan_backlight", query


# ── o arquivo podre ou ausente nunca derruba a casa ──────────────────────────

def test_mapa_corrompido_ignora_apelidos_e_a_busca_por_nome_funciona(client):
    fake = client(CASA_DO_DONO)
    hc.alias_path().write_text('{"apelidos": [ {"apelido"', encoding="utf-8")
    out = hc.home_control({"action": "ligar",
                           "entity_id": "tomada do escritorio"})
    assert fake.services == [("/api/services/switch/turn_on",
                              {"entity_id": "switch.tomada_do_escritorio"})], out
    entries, reason = hc.alias_entries()
    assert entries == [] and "corrompido" in reason


def test_mapa_ausente_ignora_apelidos_sem_excecao(client):
    fake = client(CASA_DO_DONO)
    out = hc.home_control({"action": "ligar", "entity_id": "tomada do escritorio"})
    assert fake.services == [("/api/services/switch/turn_on",
                              {"entity_id": "switch.tomada_do_escritorio"})], out
    entries, reason = hc.alias_entries()
    assert entries == [] and "ausente" in reason


def test_mapa_valido_mas_com_entradas_sujas_so_descarta_o_sujo(client):
    hc.alias_path().write_text(json.dumps({"apelidos": [
        "texto solto", {"apelido": "", "entidade": "scene.fan_backlight"},
        {"apelido": "sem entidade", "entidade": "nada_por_aqui"},
        alias("luz do quarto", "scene.fan_backlight"),
    ]}, ensure_ascii=False), encoding="utf-8")
    entries, _reason = hc.alias_entries()
    assert [entry["entidade"] for entry in entries] == ["scene.fan_backlight"]


# ── lido do disco A CADA chamada: o nome novo vale sem reiniciar ──────────────

def test_mapa_e_lido_a_cada_chamada_sem_reiniciar(client):
    fake = client([*CASA_DO_DONO,
                   {"entity_id": "scene.sala_de_estar", "state": "off",
                    "attributes": {"friendly_name": "Luz da Sala"}}])
    write_map(hc.alias_path(), [alias("luz do quarto", "scene.fan_backlight")])
    hc.home_control({"action": "ligar", "entity_id": "luz do quarto"})
    # O dono corrigiu o mapa no meio do processo (editou o arquivo, nada reiniciou).
    write_map(hc.alias_path(), [alias("luz do quarto", "scene.sala_de_estar",
                                      nome="Luz da Sala")])
    hc.home_control({"action": "ligar", "entity_id": "luz do quarto"})
    assert [payload for _path, payload in fake.services] == [
        {"entity_id": "scene.fan_backlight"}, {"entity_id": "scene.sala_de_estar"}]


def test_apelido_registrado_pelo_plugin_ja_valer_na_chamada_seguinte(client):
    """O ensino mecânico: grava em disco e resolve na chamada seguinte, ao vivo."""
    fake = client(CASA_DO_DONO)
    got = hc.save_alias("luz do quarto", "scene.fan_backlight", "Luz do teto")
    assert got["ok"] is True
    entity, error = hc.resolve("luz do quarto", fake.states)
    assert error == "" and entity["entity_id"] == "scene.fan_backlight"


# ── o caso mediado do dono tem de estar no disco, não só no código ────────────

def test_config_do_dono_tem_o_caso_mediado():
    """config/home_aliases.json: os três nomes da luz do quarto -> a cena real."""
    repo_map = Path(hc.__file__).resolve().parent.parent / "config" / "home_aliases.json"
    hc.configure_aliases(repo_map)
    entries, warning = hc.alias_entries()
    assert warning == "", warning
    by_alias = {hc.fold_name(entry["apelido"]): entry["entidade"]
                for entry in entries}
    for nome in ("luz do quarto", "luz", "luz do teto"):
        assert by_alias.get(nome) == "scene.fan_backlight", nome
    text = repo_map.read_text(encoding="utf-8")
    for segredo in ("token", "password", "secret", "key"):
        assert segredo not in text.lower()
