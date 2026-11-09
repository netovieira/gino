"""Persona SEM número (ordem do dono, 09/11/2026).

O dono tirou o WhatsApp direto da Aline e do Arnaldo e deixou só o Gino com
número. As duas continuam ATIVAS e continuam respondendo quando ele as chama
pelo nome. O que este arquivo trava:

  1. o cadastro: ``direct_whatsapp`` é o que dá o número — vazio/não-declarado
     é FALSO, e sem o campo verdadeiro não existe conta dela (a conta apagada
     no mesmo salvamento é o "remova os whats" num passo só);
  2. vazio no ``whatsapp_account`` passou a significar NENHUMA conta (ordem
     07/11/2026): quem quer a principal diz "principal" por extenso, e ela é a
     conta pareada viva, nunca um número hardcoded;
  3. RECEBIMENTO: persona sem número nunca é roteada por conta (fator
     ``conta_atribuida`` morre) e nunca vira turno por chegar na conta; por
     VOCATIVO no chat do Gino ela continua viva;
  4. SAÍDA: persona sem número não envia por conta nenhuma — a recusa acontece
     antes de qualquer resolução de destino; pela conta principal do assistente
     a resposta dela sai normal. Persona com número faz como sempre fez.

Nenhum número real: as contas são nomes de teste e o número do assistente é
lido do cadastro de contas, não escrito aqui.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import actiongate, personas, whatsapp, whatsapp_watch  # noqa: E402
from memory import config_manager  # noqa: E402

OWNER_CHAT = "5511999990000"
THIRD_CHAT = "5521888887777"

ACCOUNTS = [
    {"name": "principal", "uuid": "jarvis", "default": False, "paired": True},
    {"name": "trabalho", "uuid": "jarvis-trabalho", "default": True,
     "paired": True},
]

FREE_MODEL = {
    "value": "Qwen3.8-Flash",
    "displayName": "Qwen3.8-Flash",
    "isFree": True,
    "priceFactor": 0.0,
    "originalPriceFactor": 0.0,
    "availableContextWindows": [200000],
    "maxInputTokens": 200000,
    "efforts": ["medium"],
    "defaultEffort": "medium",
}


def _persona(name: str, account: str = "", direct=None) -> dict:
    record = {"id": name.lower(), "name": name, "aliases": [],
              "whatsapp_account": account}
    if direct is not None:
        record["direct_whatsapp"] = direct
    return record


# ── 1 e 2: o cadastro ──────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    memory.mkdir()
    skills = tmp_path / "skills" / "caveman"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# caveman\n", encoding="utf-8")
    monkeypatch.setattr(personas, "PERSONAS_PATH", memory / "personas.json")
    monkeypatch.setattr(personas, "MODELS_PATH", memory / "models.json")
    monkeypatch.setattr(personas, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(personas, "_whatsapp_accounts", lambda: list(ACCOUNTS))
    # A checagem de voz é outra regra do dono; este arquivo trava o número de
    # WhatsApp e nunca bate na porta do Fish Audio.
    if hasattr(personas, "validate_voice_id"):
        monkeypatch.setattr(personas, "validate_voice_id",
                            lambda voice_id, **_kw: "ok")
    (memory / "models.json").write_text(
        json.dumps({"models": [FREE_MODEL]}), encoding="utf-8")
    return memory


def fields(**overrides) -> dict:
    base = {"name": "Aline", "gender": "f", "voice_id": "fish-ref-abc123",
            "personality": "especialista em interface, direta",
            "skills": [], "stacks": ["react"], "model": "Qwen3.8-Flash",
            "reasoning": "medium", "context_size": 200000}
    base.update(overrides)
    return base


def test_vazio_e_nao_declarado_significa_sem_numero(store):
    persona = personas.upsert(fields())
    assert persona["direct_whatsapp"] is False
    assert persona["whatsapp_account"] == ""
    assert personas.own_account(persona) == ""
    assert personas.direct_whatsapp(persona) is False
    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(
        encoding="utf-8"))
    assert stored["personas"]["aline"]["direct_whatsapp"] is False


def test_registro_antigo_com_conta_mas_sem_o_campo_fica_sem_numero(store):
    """A conta herdada de um cadastro antigo não vale nada sem o booleano."""
    Path(personas.PERSONAS_PATH).write_text(json.dumps({
        "version": 1,
        "personas": {"aline": dict(fields(), whatsapp_account="trabalho")},
        "retired": {}}, ensure_ascii=False), encoding="utf-8")
    persona = personas.get("aline")
    assert persona["direct_whatsapp"] is False
    assert persona["whatsapp_account"] == ""
    assert personas.own_account(persona) == ""


def test_conta_declarada_para_persona_nova_da_numero(store):
    """Definir a conta explicitamente já é o número: não precisa dizer duas vezes."""
    persona = personas.upsert(fields(whatsapp_account="trabalho"))
    assert persona["direct_whatsapp"] is True
    assert persona["whatsapp_account"] == "trabalho"
    assert personas.own_account(persona) == "trabalho"


def test_direct_sem_conta_e_recusado_com_motivo_falado(store):
    with pytest.raises(ValueError, match="exige a conta"):
        personas.upsert(fields(direct_whatsapp=True))
    assert personas.list_personas() == []


def test_direct_false_apaga_a_conta_no_mesmo_salvamento(store):
    """O "remova os whats da Aline" é um passo só e não deixa conta fantasma."""
    personas.upsert(fields(whatsapp_account="trabalho"))
    persona = personas.upsert({"id": "aline", "direct_whatsapp": False})
    assert persona["direct_whatsapp"] is False
    assert persona["whatsapp_account"] == ""
    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(
        encoding="utf-8"))
    assert stored["personas"]["aline"]["whatsapp_account"] == ""


def test_direct_aceita_palavra_dita_pelo_dono(store):
    personas.upsert(fields(whatsapp_account="trabalho"))
    assert personas.upsert({"id": "aline",
                            "direct_whatsapp": "não"})["direct_whatsapp"] is False
    # O número não volta por acaso: só com a palavra dita de novo.
    assert personas.upsert(fields(whatsapp_account="trabalho"))[
        "direct_whatsapp"] is False
    assert personas.upsert({"id": "aline", "direct_whatsapp": "sim",
                            "whatsapp_account": "trabalho"}
                           )["direct_whatsapp"] is True


def test_palavra_bizantina_de_direct_whatsapp_e_recusada(store):
    with pytest.raises(ValueError, match="direct_whatsapp inválido"):
        personas.upsert(fields(direct_whatsapp="talvez"))


def test_vazio_na_conta_e_nenhuma_conta_nunca_a_principal(store):
    """Ordem 07/11/2026: vazio = nenhum; a principal só se diz por extenso."""
    assert personas._resolve_whatsapp_account("") == ""
    assert personas._resolve_whatsapp_account("   ") == ""
    assert personas._resolve_whatsapp_account("principal") == "principal"
    assert personas._resolve_whatsapp_account("trabalho") == "trabalho"
    assert personas._resolve_whatsapp_account(
        "jarvis-trabalho") == "trabalho"
    with pytest.raises(ValueError, match="não está pareado"):
        personas._resolve_whatsapp_account("pessoal")


def test_principal_e_a_conta_pareada_viva_e_nao_um_numero_hardcoded(monkeypatch):
    """Nenhuma conta se chama "principal": a palavra cai na conta default viva."""
    monkeypatch.setattr(personas, "_whatsapp_accounts", lambda: [
        {"name": "Jarvis", "uuid": "jarvis"},
        {"name": "Jarvis Trabalho", "uuid": "jarvis-trabalho"},
    ])
    with mock.patch.object(whatsapp, "default_account_name",
                           return_value="Jarvis Trabalho"):
        name = personas._resolve_whatsapp_account("principal")
    assert name == "Jarvis Trabalho"
    assert not any(char.isdigit() for char in name), \
        "a conta é um nome pareado, nunca um número escrito no código"
    with mock.patch.object(whatsapp, "default_account_name", return_value=""):
        with pytest.raises(ValueError, match="não está pareado"):
            personas._resolve_whatsapp_account("principal")


def test_conta_do_assistente_e_lida_viva_e_ele_tem_numero_direto(monkeypatch):
    """O Gino não é persona: o número dele é a conta principal, lida agora."""
    monkeypatch.setattr(personas, "_whatsapp_accounts", lambda: list(ACCOUNTS))
    with mock.patch.object(whatsapp, "default_account_name",
                           return_value="trabalho"):
        assert personas.assistant_account() == "trabalho"
    assert personas.MAIN_ASSISTANT_DIRECT_WHATSAPP is True


# ── 3: recebimento e roteamento por conta ──────────────────────────────────

@pytest.fixture
def watch():
    """Observador com fonte de personas injetada, sem I/O real."""
    calls: list[tuple[str, dict]] = []

    actiongate.close()
    whatsapp_watch.reset()
    whatsapp_watch.set_assistant_name("Gino")
    whatsapp_watch.set_direct_chat("principal", OWNER_CHAT)
    patches = [
        mock.patch.object(whatsapp, "default_account_name",
                          return_value="principal"),
        mock.patch.object(whatsapp, "account_number", return_value=""),
        mock.patch.object(whatsapp, "list_accounts", return_value=[]),
        mock.patch.object(config_manager, "get_plugin_setting",
                          side_effect=lambda _ns, key, default=None:
                              OWNER_CHAT if key == "owner_chat" else default),
    ]
    for patch in patches:
        patch.start()
    whatsapp_watch.start(lambda account, item: (calls.append((account, item)),
                                                 True)[1])
    yield calls
    actiongate.close()
    whatsapp_watch.stop()
    whatsapp_watch.reset()
    whatsapp_watch.set_personas_source(None)
    whatsapp_watch.set_direct_chat("", "")
    for patch in patches:
        patch.stop()


def item(text: str, **overrides) -> dict:
    entry = {"ts": int(time.time()), "chat": THIRD_CHAT, "name": "Contato",
             "from_me": False, "group": False, "text": text,
             "id": f"msg-{text[:12]}", "media": None}
    entry.update(overrides)
    return entry


def test_persona_sem_numero_nao_e_roteada_pela_conta(watch):
    whatsapp_watch.set_personas_source(
        lambda: [_persona("Aline", "trabalho", direct=False)])
    assert whatsapp_watch.addressed_persona("bom dia", "trabalho") is None
    assert whatsapp_watch.handle("trabalho", item("chegou o pedido")) is False
    assert watch == []


def test_persona_sem_numero_no_cadastro_nao_recebe_por_conta(watch, monkeypatch):
    """A fonte viva (cadastro real) também recusa: conta apagada, sem flag."""
    monkeypatch.setattr(personas, "list_personas", lambda: [
        {"id": "aline", "name": "Aline", "aliases": [],
         "direct_whatsapp": False, "whatsapp_account": "trabalho"}])
    whatsapp_watch.set_personas_source(None)
    assert whatsapp_watch.addressed_persona("oi", "trabalho") is None
    assert whatsapp_watch.handle("trabalho", item("chegou o pedido")) is False
    assert watch == []


def test_persona_com_numero_continua_roteada_pela_conta(watch):
    whatsapp_watch.set_personas_source(
        lambda: [_persona("Aline", "trabalho", direct=True)])
    hit = whatsapp_watch.addressed_persona("bom dia", "TRABALHO")
    assert hit == {"slug": "aline", "name": "Aline",
                   "factor": "conta_atribuida"}
    assert whatsapp_watch.handle("trabalho", item("chegou o pedido")) is True
    assert [account for account, _ in watch] == ["trabalho"]


def test_persona_sem_numero_responde_por_vocativo_no_chat_do_gino(watch):
    whatsapp_watch.set_personas_source(
        lambda: [_persona("Aline", "", direct=False)])
    hit = whatsapp_watch.addressed_persona("Aline, revisa o card", "principal")
    assert hit["factor"] == "vocativo"
    assert hit["slug"] == "aline"
    assert whatsapp_watch.handle(
        "principal", item("Aline, revisa o card", chat=OWNER_CHAT,
                          from_me=True)) is True
    assert len(watch) == 1


def test_sem_nome_e_sem_conta_nenhuma_persona_e_acionada(watch):
    whatsapp_watch.set_personas_source(
        lambda: [_persona("Aline", "", direct=False),
                 _persona("Arnaldo", "")])
    assert whatsapp_watch.addressed_persona("bom dia", "trabalho") is None
    assert whatsapp_watch.handle("trabalho", item("bom dia")) is False
    assert watch == []


# ── 4: saída ───────────────────────────────────────────────────────────────

@pytest.fixture
def registry(monkeypatch):
    """Cadastro real de personas para a rota de saída consultar."""
    records = {
        "aline": {"id": "aline", "name": "Aline", "aliases": [],
                  "direct_whatsapp": False, "whatsapp_account": ""},
        "carlos": {"id": "carlos", "name": "Carlos", "aliases": [],
                   "direct_whatsapp": True, "whatsapp_account": "trabalho"},
    }
    monkeypatch.setattr(personas, "get",
                        lambda key: dict(records[str(key).casefold()])
                        if str(key).casefold() in records else None)
    monkeypatch.setattr(personas, "assistant_account", lambda: "principal")
    monkeypatch.setattr(personas, "_resolve_whatsapp_account",
                        lambda value: {"jarvis-trabalho": "trabalho"}.get(
                            str(value).casefold(), str(value)))
    return records


def _blocked(speaker: str, account: str) -> str:
    return whatsapp.persona_send_block(speaker, account)


def test_saida_persona_sem_numero_recusada_em_conta_ajena(registry):
    assert "não tem WhatsApp próprio" in _blocked("Aline", "trabalho")
    assert _blocked("Aline", "principal") == ""
    assert _blocked("Aline", "") == ""


def test_saida_persona_com_numero_nao_e_bloqueada(registry):
    assert _blocked("Carlos", "trabalho") == ""
    assert _blocked("Carlos", "principal") == ""


def test_saida_do_assistente_nunca_e_bloqueada(registry, monkeypatch):
    monkeypatch.setattr(whatsapp, "current_speaker", lambda: "Gino")
    assert _blocked("Gino", "trabalho") == ""
    assert _blocked("Quem não é persona", "trabalho") == ""


def test_send_real_recusa_antes_de_resolver_destino(registry, monkeypatch):
    """A recusa não toca rede nem destino: nada é montado, nada é assinado."""
    seen: list[str] = []
    monkeypatch.setattr(whatsapp, "current_speaker", lambda: "Gino")

    def _spy(account=None):
        seen.append(str(account))
        raise KeyError("passou do portão")

    monkeypatch.setattr(whatsapp, "_A", _spy)
    ok, reason = whatsapp.send(THIRD_CHAT, "Card revisado.", account="trabalho",
                               speaker="Aline")
    assert ok is False
    assert "não tem WhatsApp próprio" in reason
    assert seen == [], "a recusa tem de acontecer antes de qualquer destino"


def test_send_real_libera_pela_conta_do_assistente(registry, monkeypatch):
    """Pelo chat do Gino a resposta dela sai — o portão só corta conta alheia."""
    monkeypatch.setattr(whatsapp, "current_speaker", lambda: "Gino")
    reached: list[str] = []

    def _spy(account=None):
        reached.append(str(account))
        raise KeyError("passou do portão")

    monkeypatch.setattr(whatsapp, "_A", _spy)
    ok, reason = whatsapp.send(OWNER_CHAT, "Card revisado.",
                               account="principal", speaker="Aline")
    assert reached == ["principal"], "a conta do assistente não é bloqueada"
    assert ok is False and "passou" in reason


# ── 5: dev_audit — a asserção estática enxerga o campo e a rota ────────────

def test_dev_audit_tem_assercao_do_numero_direto():
    from core import dev_audit
    probe = next((a for a in dev_audit.REGISTER
                  if a.id == "persona.sem-numero"), None)
    assert probe is not None, "a asserção não está registrada no dev_audit"
    assert probe.probe_type == "estatica"
    verdict, reason = probe.probe()
    assert verdict == dev_audit.OK, reason


def test_dev_audit_quebra_se_a_rota_esquecer_o_campo(monkeypatch):
    """Sem o filtro do cadastro na rota de conta, a sonda tem de dar falha."""
    import ast

    from core import dev_audit
    real = dev_audit._tree
    source = (Path(dev_audit._ROOT) / "core" / "whatsapp_watch.py").read_text(
        encoding="utf-8")
    stripped = ast.parse(source.replace("_persona_own_account", "_conta_crua"))

    def fake_tree(rel: str):
        return stripped if rel.endswith("whatsapp_watch.py") else real(rel)

    monkeypatch.setattr(dev_audit, "_tree", fake_tree)
    verdict, reason = dev_audit._sonda_persona_sem_numero()
    assert verdict == dev_audit.FALHA
    assert "numero proprio" in reason


# ── 6: cadastro sem teto e com cargo (ordens do dono, 09/11/2026) ───────────
#
# O teto duro de 12 personas caiu. O guard órfão que sobrou da retirada só
# estourava quando o branch era percorrido -- ``import core.personas`` passava
# limpo e a NameError aparecia na primeira persona cadastrada depois da 12ª.
# Estes testes percorrem exatamente esse caminho, com a lista já cheia, e
# travam o cargo que o dono pediu (Gino orquestrador, cada um com o seu).

def test_decima_terceira_persona_nao_da_nameerror(store):
    """Com 12 já cadastradas, criar mais uma tem de funcionar e nomear-se."""
    assert not hasattr(personas, "MAX_PERSONAS"), (
        "MAX_PERSONAS voltou: não existe teto de cadastro")
    for index in range(12):
        personas.upsert(fields(name=f"Pessoa {index}", voice_id=f"v{index}"))
    assert len(personas.list_personas()) == 12

    # Aqui estava o ``len(data["personas"]) >= MAX_PERSONAS`` órfão.
    decima_terceira = personas.upsert(fields(name="Pessoa 12",
                                             voice_id="v12"))
    assert decima_terceira["name"] == "Pessoa 12"
    assert len(personas.list_personas()) == 13

    # E a 14ª, 15ª: nenhuma delas pede licença a um limite que não existe.
    personas.upsert(fields(name="Pessoa 13", voice_id="v13"))
    personas.upsert(fields(name="Pessoa 14", voice_id="v14"))
    assert len(personas.list_personas()) == 15


def test_atualizar_com_lista_cheia_nao_esbarra_em_limite_algum(store):
    """Editar quem já existe nunca foi o branch do teto, e continua aberto."""
    for index in range(13):
        personas.upsert(fields(name=f"Gente {index}", voice_id=f"g{index}"))
    antes = personas.get("gente-0")
    depois = personas.upsert(fields(name="Gente 0", voice_id="g0",
                                    personality="mudou o texto"))
    assert depois["id"] == antes["id"]
    assert depois["personality"] == "mudou o texto"
    assert len(personas.list_personas()) == 13


def test_role_presente_no_cadastro_e_respeitado(store):
    """Cargo é campo do cadastro: entra, volta e é lido pelo nome."""
    persona = personas.upsert(fields(name="Dete", role="Analista de QA"))
    assert persona["role"] == "Analista de QA"
    assert personas.resolve_role(persona) == "Analista de QA"
    assert personas.resolve_role("dete") == "Analista de QA"
    stored = json.loads(Path(personas.PERSONAS_PATH).read_text(
        encoding="utf-8"))
    assert stored["personas"]["dete"]["role"] == "Analista de QA"


def test_role_maximo_e_o_do_codigo_e_o_excedido_e_recusado_com_medida(store):
    """MAX_ROLE é um limite de texto, não de quantidade: 120 caracteres."""
    limite = personas.MAX_ROLE
    assert limite == 120
    na_medida = personas.upsert(fields(name="Na Medida",
                                       role="x" * limite))
    assert len(na_medida["role"]) == limite
    with pytest.raises(ValueError, match=str(limite)):
        personas.upsert(fields(name="Cargao", role="x" * (limite + 1)))
    assert personas.get("cargao") is None


def test_role_nao_aceita_segredo(store):
    with pytest.raises(ValueError, match="segredos"):
        personas.upsert(fields(name="Cargo Vazado",
                               role="use api_key=abc para falar comigo"))


def test_cargo_sem_cadastro_usa_a_tabela_do_dono_e_quem_nao_existe_devolve_vazio(
        store):
    """``CASCADE_PERSONAS`` menciona nomes que não existem: fallback, nunca erro.

    Aisha, Bernardo e Ariel não foram criadas por ordem do dono. Isso só é
    tolerado porque ``resolve_role``/``in_cascade``/``model_for`` resolvem em
    silêncio quando o registro falta -- se algum dia virar exceção, chamar uma
    persona inexistente derruba o turno do assistente.
    """
    assert "aisha" in personas.CASCADE_PERSONAS
    assert personas.get("aisha") is None  # não existe, e é assim que fica
    assert personas.in_cascade("aisha") is False
    assert personas.resolve_role("aisha") == \
        "Analista de Monitoramento e Segurança"
    assert personas.resolve_role("bernardo") == "Engenheiro de Arquitetura"
    assert personas.resolve_role("ariel") == "Designer de Interface"
    assert personas.resolve_role("quem-nao-existe-de-mim") == ""
    # model_for não levanta para quem não existe: devolve um cadastro utilizável.
    escolha = personas.model_for("aisha")
    assert isinstance(escolha, dict) and escolha.get("model")
    plain = personas.upsert(fields(name="Zeca"))
    assert personas.resolve_role(plain) == ""
