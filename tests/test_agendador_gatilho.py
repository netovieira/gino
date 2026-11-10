"""Provas do AGENDADOR (mecanismo A) — um teste por regra ditada pelo dono.

Plano: ``PLANO-2026-11-09-fluxo-agendamento-e-avisos-por-evento`` (gate
APROVADA 09/11/2026 22:00, fonte whatsapp). Cada teste abaixo nomeia a regra
dele na docstring; nenhum deles inventa comportamento.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import schedule as ferramenta  # noqa: E402
from core import obsidian, scheduler  # noqa: E402

GATILHO_VALIDO = {"tipo": "funcao", "modulo": "core.scheduler",
                  "funcao": "agora", "destino": ["voz"]}


@pytest.fixture
def cofre(tmp_path, monkeypatch):
    """Cofre isolado + estado do módulo limpo (o cache é do processo)."""
    root = tmp_path / "Jarvis"
    monkeypatch.setattr(obsidian, "VAULT", root)
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(scheduler, "REGISTRO_SCRIPTS",
                        tmp_path / "config" / "schedule_scripts.json")
    monkeypatch.setattr(scheduler, "_cache", {"assinatura": None, "linhas": {}})
    monkeypatch.setattr(scheduler, "_scripts_cache",
                        {"assinatura": None, "lista": []})
    monkeypatch.setattr(scheduler, "_PORTAS", {})
    monkeypatch.setattr(scheduler, "_pendente_aviso", set())
    monkeypatch.setattr(scheduler, "_pedidos_aprovacao", [])
    monkeypatch.setattr(scheduler, "_ja_acordei", False)
    monkeypatch.delenv("JARVIS_VAULT_NAME_POLICY", raising=False)
    return root


def amanha(hora: int = 9, minuto: int = 0) -> str:
    alvo = (datetime.now() + timedelta(days=1)).replace(
        hour=hora, minute=minuto, second=0, microsecond=0)
    return alvo.isoformat()


def linha_vencida(linha: dict, minutos: int = 30) -> dict:
    linha["quando"] = (datetime.now().astimezone()
                       - timedelta(minutes=minutos)).isoformat()
    scheduler._gravar(linha)
    scheduler.carregar(forcar=True)
    return linha


# ── regra 1: todo agendamento carrega um gatilho executável ──────────────────

def test_sem_gatilho_nao_agenda_e_a_recusa_nomeia_o_que_faltou(cofre):
    saida = scheduler.registrar(amanha(), "consulta", {},
                                origem="fala_do_dono")
    assert saida["ok"] is False
    assert "gatilho" in saida["recusa"]
    assert scheduler.listar() == []


def test_gatilho_texto_livre_ou_shell_nao_e_tipo(cofre):
    for bruto in ({"tipo": "shell", "script": "ls /tmp && echo oi"},
                  {"tipo": "texto", "script": "avisar o dono"},
                  {"tipo": "", "modulo": "core.scheduler", "funcao": "agora"}):
        saida, erros = scheduler.validar_gatilho(bruto)
        assert saida is None, bruto
        assert any("funcao/script" in erro for erro in erros), erros


def test_funcao_que_nao_existe_e_recusada_na_criacao(cofre):
    bruto = {"tipo": "funcao", "modulo": "core.scheduler",
             "funcao": "nao_existe_aqui", "destino": ["voz"]}
    saida, erros = scheduler.validar_gatilho(bruto)
    assert saida is None
    assert any("não existe" in erro or "não é chamável" in erro
               for erro in erros), erros


def test_modulo_fora_da_lista_fechar_nao_e_importado(cofre):
    # "os.system" é chamável: a trava não é 'existe', é onde pode morar.
    saida, erros = scheduler.validar_gatilho(
        {"tipo": "funcao", "modulo": "os", "funcao": "system"})
    assert saida is None
    assert any("fora da lista" in erro for erro in erros), erros


def test_argumento_invalido_e_recusado_na_criacao(cofre):
    bruto = {"tipo": "funcao", "modulo": "core.scheduler", "funcao": "agora",
             "argumentos": [{"nome": "adiante", "tipo": "int",
                             "valor": "três horas"}]}
    saida, erros = scheduler.validar_gatilho(bruto)
    assert saida is None
    assert any("não é int" in erro for erro in erros), erros

    bruto_iso = {"tipo": "funcao", "modulo": "core.scheduler",
                 "funcao": "agora",
                 "argumentos": [{"nome": "dia", "tipo": "iso8601",
                                 "valor": "amanhã"}]}
    _, erros_iso = scheduler.validar_gatilho(bruto_iso)
    assert any("ISO-8601" in erro for erro in erros_iso), erros_iso


def test_bool_de_fala_vira_bool_de_verdade(cofre):
    norm, erros = scheduler.validar_gatilho(
        {"tipo": "funcao", "modulo": "core.scheduler", "funcao": "agora",
         "argumentos": [{"nome": "urgente", "tipo": "bool",
                         "valor": "sim"}]})
    assert erros == []
    assert norm["argumentos"][0]["valor"] is True


# ── regra 2: a fonte da verdade é NOTA NO COFRE, com cache em memória ────────

def test_cada_agendamento_e_uma_nota_nomeada_pelo_padrao(cofre):
    saida = scheduler.registrar(amanha(), "pagar a conta", GATILHO_VALIDO,
                                origem="fala_do_dono")
    assert saida["ok"]
    nota = Path(saida["nota"])
    assert nota.is_file()
    assert nota.parent.name == "Memoria"
    # o nome passou pelo portão do cofre (build_name + check_name)
    assert obsidian.name_errors(nota.name) == []
    corpo = nota.read_text(encoding="utf-8")
    assert "pagar a conta" in corpo
    assert json.loads(corpo.split("```json")[1].split("```")[0])["titulo"] \
        == "pagar a conta"


def test_atualizar_reescreve_a_mesma_nota_e_nao_duplica(cofre):
    """Risco 9: nota de ontem + nota de hoje para o mesmo id = fonte dupla."""
    saida = scheduler.registrar(amanha(), "adiar depois", GATILHO_VALIDO,
                                origem="fala_do_dono")
    linha = saida["linha"]
    nota_original = Path(linha["nota"])
    # simula a virada de dia: regravar não pode abrir uma segunda nota
    linha["quando"] = amanha(15)
    scheduler._gravar(linha)
    scheduler.carregar(forcar=True)
    notas = list((cofre / "Memoria").glob("TAREFA-*-agendamento-*.md"))
    assert len(notas) == 1
    assert notas[0] == nota_original or notas[0].name == nota_original.name
    assert scheduler.erro_de_leitura() == []


def test_cache_em_memoria_nao_rele_o_cofre_a_cada_chamada(cofre, monkeypatch):
    scheduler.registrar(amanha(), "consulta", GATILHO_VALIDO,
                        origem="fala_do_dono")
    scheduler.carregar()
    chamadas = {"n": 0}
    origem = scheduler._dados_da_nota

    def contadora(path):
        chamadas["n"] += 1
        return origem(path)

    monkeypatch.setattr(scheduler, "_dados_da_nota", contadora)
    for _ in range(30):
        scheduler.listar()
    assert chamadas["n"] == 0, "o laço relê o disco a cada passada"

    # e quando o dono mexe na nota, o cache percebe sozinho
    linha = scheduler.listar()[0]
    scheduler._gravar(linha | {"titulo": "consulta nova"})
    scheduler.carregar()
    assert scheduler.listar()[0]["titulo"] == "consulta nova"


def test_nota_fora_do_padrao_e_reportada_em_vez_de_silencio(cofre):
    scheduler.registrar(amanha(), "consulta", GATILHO_VALIDO,
                        origem="fala_do_dono")
    nota = next((cofre / "Memoria").glob("TAREFA-*-agendamento-*.md"))
    nota.write_text("# sem bloco json\n", encoding="utf-8")
    scheduler.carregar(forcar=True)
    assert scheduler.listar() == []
    assert any("sem bloco json" in erro for erro in scheduler.erro_de_leitura())


def test_falha_de_escrita_devolve_recusa_nao_sucesso_mudo(cofre, monkeypatch):
    def estourar(*a, **k):
        raise OSError("disco cheio")

    monkeypatch.setattr(obsidian, "write_note", estourar)
    monkeypatch.setattr(obsidian, "_atomic_write", estourar)
    saida = scheduler.registrar(amanha(), "consulta", GATILHO_VALIDO,
                                origem="fala_do_dono")
    assert saida["ok"] is False
    assert "cofre" in saida["recusa"]


# ── regra 3: hora falada — sem parser novo, sem fallback "agora" ─────────────

@pytest.mark.parametrize("texto", [
    "hoje 14:30", "14:30", "14h30", "2026-11-12 08:00", "2026-11-12T08:00:00",
])
def test_hora_falada_vira_iso(texto, cofre):
    iso, erro = scheduler.interpretar_quando(texto)
    assert erro == ""
    assert iso.startswith("2026-") if "2026" in texto else True
    assert datetime.fromisoformat(iso).tzinfo is not None


@pytest.mark.parametrize("texto", ["", "daqui a três séculos",
                                   "um dia desses", "hoje", "qualquer hora"])
def test_hora_que_nao_bate_e_recusada_e_nao_agenda_agora(texto, cofre):
    iso, erro = scheduler.interpretar_quando(texto)
    assert iso is None
    assert erro
    assert scheduler.registrar(texto, "x", GATILHO_VALIDO,
                               origem="fala_do_dono")["ok"] is False


def test_agendar_o_passado_e_recusado(cofre):
    passado = (datetime.now() - timedelta(hours=2)).isoformat()
    saida = scheduler.registrar(passado, "coisa velha", GATILHO_VALIDO,
                                origem="fala_do_dono")
    assert saida["ok"] is False
    assert "já passou" in saida["recusa"]


# ── regra 4: sem teto de simultaneidade (decisão 5, "opcao c") ───────────────

def test_quarenta_agendamentos_abertos_sem_recusa_de_teto(cofre):
    for i in range(40):
        saida = scheduler.registrar(amanha(9), f"coisa {i}", GATILHO_VALIDO,
                                    origem="fala_do_dono")
        assert saida["ok"], (i, saida)
    assert len(scheduler.listar()) == 40
    # e nenhuma das palavras de teto aparece num registro deste módulo
    fonte = Path(scheduler.__file__).read_text(encoding="utf-8")
    assert "max_active" not in fonte
    assert "LimitReached" not in fonte


# ── regra 5: destino default são os dois (decisão 7, "opcao a") ──────────────

def test_sem_destino_nomeado_o_default_e_voz_e_whatsapp(cofre):
    bruto = {"tipo": "funcao", "modulo": "core.scheduler", "funcao": "agora"}
    norm, erros = scheduler.validar_gatilho(bruto)
    assert erros == []
    assert norm["destino"] == ["voz", "whatsapp"]


def test_destino_fora_do_enum_e_recusado(cofre):
    bruto = dict(GATILHO_VALIDO, destino=["telegram"])
    _, erros = scheduler.validar_gatilho(bruto)
    assert any("fora da lista" in erro for erro in erros), erros


def test_rede_social_recusada_enquanto_linkedin_esta_desligado(cofre,
                                                                monkeypatch):
    """Ele desligou a rota em 07/11; agendar não reabilita plugin (3.2)."""
    monkeypatch.setattr(scheduler, "_plugin_ligado", lambda nome: False)
    bruto = dict(GATILHO_VALIDO, destino=["rede-social"])
    _, erros = scheduler.validar_gatilho(bruto)
    assert any("rede-social" in erro and "desligado" in erro
               for erro in erros), erros
    assert scheduler.registrar(amanha(), "postar", bruto,
                               origem="fala_do_dono")["ok"] is False


def test_arquivo_baixar_recusado_porque_a_rota_nao_existe(cofre):
    """3.2: dos cinco exemplos dele, este ainda não tem alvo na casa."""
    bruto = dict(GATILHO_VALIDO, destino=["arquivo-baixar"])
    _, erros = scheduler.validar_gatilho(bruto)
    assert any("download genérico" in erro for erro in erros), erros


# ── regra 6: cancelar ("esquece aquilo") — b com d ───────────────────────────

def test_um_candidato_resolve_mas_so_depois_de_confirmado(cofre):
    scheduler.registrar(amanha(), "pagar a luz", GATILHO_VALIDO,
                        origem="fala_do_dono")
    primeiro = scheduler.cancelar(origem="fala_do_dono",
                                  texto="pagar a luz")
    assert primeiro["ok"] is False
    assert "Confirma" in primeiro["pergunta"]
    assert len(scheduler.listar()) == 1, "cancelou sem confirmação"
    dito = scheduler.cancelar(origem="fala_do_dono", texto="pagar a luz",
                              confirmacao=True)
    assert dito["ok"]
    assert scheduler.listar() == []


def test_mais_de_um_candidato_pergunta_em_vez_de_sorteiar(cofre):
    for titulo in ("marcar dentista", "marcar eletricista"):
        scheduler.registrar(amanha(), titulo, GATILHO_VALIDO,
                            origem="fala_do_dono")
    saida = scheduler.cancelar(origem="fala_do_dono", texto="marcar")
    assert saida["ok"] is False
    assert "pergunta" in saida
    assert len(saida["candidatos"]) == 2
    assert len(scheduler.listar()) == 2, "sorteou um cancelamento"


def test_cancelar_exige_origem_e_terceiro_nunca_cancele(cofre):
    scheduler.registrar(amanha(), "consulta", GATILHO_VALIDO,
                        origem="fala_do_dono")
    saida = scheduler.cancelar(origem="fala_visitante", texto="consulta")
    assert saida["ok"] is False
    assert "terceiro" in saida["recusa"]
    assert len(scheduler.listar()) == 1


def test_o_que_eu_registrei_eu_esqueco_mas_a_persona_veda(cofre):
    """Tabela 5.3: eu cancelo o meu; persona não cancela o que é dele."""
    scheduler.registrar(amanha(), "minha coisa", GATILHO_VALIDO,
                        origem="automatica")
    saida = scheduler.cancelar(origem="automatica", persona="selma",
                               texto="minha coisa")
    assert saida["ok"] is False
    assert "não achei" in saida["recusa"]


# ── regra 7: personas pedem aprovação, não registram (decisão 3) ─────────────

def test_persona_sem_ordem_dele_vira_pedido_e_nada_e_gravado(cofre):
    saida = scheduler.registrar(amanha(), "aviso da persona", GATILHO_VALIDO,
                                origem="automatica", persona="selma")
    assert saida["ok"] is False
    assert "aprovação" in saida["recusa"]
    assert scheduler.listar() == [], "a nota do cofre foi escrita mesmo recusando"
    assert list((cofre / "Memoria").glob("TAREFA-*-agendamento-*.md")) == []
    pedidos = scheduler.pedidos_aprovacao()
    assert len(pedidos) == 1 and pedidos[0]["persona"] == "selma"


def test_persona_sob_solicitacao_dele_registra(cofre):
    """Fala dele: 'apenas sob minha solicitacao' — aí a persona pode."""
    saida = scheduler.registrar(amanha(), "por ordem dele", GATILHO_VALIDO,
                                origem="fala_do_dono", persona="selma")
    assert saida["ok"]
    assert saida["linha"]["persona"] == "selma"


def test_origem_nao_declarada_e_recusada(cofre):
    saida = scheduler.registrar(amanha(), "sem origem", GATILHO_VALIDO)
    assert saida["ok"] is False
    assert "origem" in saida["recusa"]


def _arquivo_do_cofre(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _sem_escrita(monkeypatch):
    """Qualquer escrita no cofre estoura: prova a ORDEM, não só o resultado.

    O defeito caçado gravava a nota e devolvia a recusa depois. Um assert de
    arquivo sobrando não pega escrita que apagou em cima; um spy que levanta
    pega. Devolve o desarme -- ``monkeypatch.undo()`` aqui também desfaria o
    cofre isolado da fixture e a próxima leitura cairia no cofre verdadeiro.
    """
    escrita_real = (obsidian.write_note, obsidian._atomic_write)

    def vigia(*a, **k):
        raise AssertionError("escreveu no cofre antes de recusar")

    monkeypatch.setattr(obsidian, "write_note", vigia)
    monkeypatch.setattr(obsidian, "_atomic_write", vigia)

    def desarmar():
        monkeypatch.setattr(obsidian, "write_note", escrita_real[0])
        monkeypatch.setattr(obsidian, "_atomic_write", escrita_real[1])

    return desarmar


def test_persona_e_barrada_antes_de_qualquer_escrita_no_cofre(cofre,
                                                             monkeypatch):
    """Regra 7 (decisão 3) na ordem: primeiro a recusa, e nada é gravado."""
    scheduler.pasta_das_notas()          # aquece o cofre antes da foto
    antes = _arquivo_do_cofre(cofre)
    _sem_escrita(monkeypatch)

    saida = scheduler.registrar(amanha(), "aviso da persona", GATILHO_VALIDO,
                                origem="automatica", persona="selma")
    assert saida["ok"] is False
    assert "aprovação" in saida["recusa"]
    assert scheduler.listar() == []
    assert _arquivo_do_cofre(cofre) == antes, "sobrou nota no cofre"

    # o mesmo pelo porto de texto: a action não tem caminho próprio de escrita
    corpo = json.loads(ferramenta.schedule({
        "acao": "add", "quando": amanha(), "titulo": "pelo porto",
        "gatilho": GATILHO_VALIDO, "origem": "automatica",
        "persona": "selma"}))
    assert corpo["status"] == "recusado"
    assert _arquivo_do_cofre(cofre) == antes

    # nem pelo porto de voz
    falado: list[str] = []
    voz = json.loads(ferramenta.schedule({
        "acao": "add", "quando": amanha(), "titulo": "pela boca",
        "gatilho": GATILHO_VALIDO, "origem": "automatica",
        "persona": "selma"}, speak=falado.append))
    assert voz["status"] == "recusado"
    assert falado == []
    assert _arquivo_do_cofre(cofre) == antes

    pedidos = scheduler.pedidos_aprovacao()
    assert len(pedidos) == 3
    assert {p["persona"] for p in pedidos} == {"selma"}


def test_persona_tambem_nao_pospor_nem_cadastra_por_conta_propria(cofre,
                                                                 monkeypatch,
                                                                 tmp_path):
    """A barreira cobre toda escrita: pospor e cadastrar gatilho são escrita."""
    linha = scheduler.registrar(amanha(9), "pagar a luz", GATILHO_VALIDO,
                                origem="fala_do_dono")["linha"]
    quando_antes = linha["quando"]
    desarmar = _sem_escrita(monkeypatch)

    saida = scheduler.pospor(linha["id"], amanha(15), origem="automatica",
                             persona="selma")
    assert saida["ok"] is False
    assert "aprovação" in saida["recusa"]
    assert scheduler.linha_de(linha["id"])["quando"] == quando_antes, \
        "pospor da persona mexeu no registro dele"

    script = tmp_path / "solto.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cadastro = scheduler.cadastrar_script(str(script), "teste",
                                          origem="automatica", persona="selma")
    assert cadastro["ok"] is False
    assert "aprovação" in cadastro["recusa"]
    assert scheduler.scripts_cadastrados() == [], \
        "a persona abriu capacidade no cadastro fechado"

    acoes = {p["acao"] for p in scheduler.pedidos_aprovacao()}
    assert acoes == {"pospor um agendamento", "cadastrar um script de "
                                                "gatilho"}, acoes
    # e a barreira não barra o dono: por fala dele, o mesmo pospor passa
    desarmar()
    dito = scheduler.pospor(linha["id"], amanha(15), origem="fala_do_dono")
    assert dito["ok"], dito
    assert scheduler.linha_de(linha["id"])["quando"] != quando_antes


# ── validação dura: nome de nota sai do padrão do cofre, nunca da mão ────────

def _nomes_fora_do_padrao_na_mao(fonte: str) -> list[str]:
    """Nomes ".md" montados à mão que NÃO sejam padrão de busca (glob)."""
    arvore = ast.parse(fonte)
    permitidos = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Call) and getattr(no.func, "attr", "") == "glob":
            for arg in list(no.args) + [kw.value for kw in no.keywords]:
                permitidos.add(id(arg))
    soltos = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.JoinedStr) and id(no) not in permitidos:
            cauda = next((v.value for v in reversed(no.values)
                          if isinstance(v, ast.Constant)
                          and isinstance(v.value, str)), "")
            if str(cauda).endswith(".md"):
                soltos.append(str(cauda))
    return soltos


def test_todo_nome_de_nota_passa_por_build_name_e_check_name(cofre,
                                                            monkeypatch):
    """Regra 3/5 do contrato: nome é o padrão do cofre, nunca string própria."""
    vistos = {"build": [], "check": []}
    build_real, check_real = obsidian.build_name, obsidian.check_name

    def espia_build(tipo, title, day=None):
        nome = build_real(tipo, title, day=day)
        vistos["build"].append(nome)
        return nome

    def espia_check(name):
        vistos["check"].append(str(name))
        return check_real(name)

    monkeypatch.setattr(obsidian, "build_name", espia_build)
    monkeypatch.setattr(obsidian, "check_name", espia_check)

    saida = scheduler.registrar(amanha(), "pagar a luz", GATILHO_VALIDO,
                                origem="fala_do_dono")
    linha = saida["linha"]
    scheduler.pospor(linha["id"], amanha(15), origem="fala_do_dono")
    scheduler.cancelar(origem="fala_do_dono", id=linha["id"],
                       confirmacao=True)

    assert vistos["build"], "nenhum nome veio de obsidian.build_name"
    assert vistos["check"], "nenhum nome passou por obsidian.check_name"
    notas = sorted((cofre / "Memoria").glob("*.md"))
    assert len(notas) == 1, "reescrita abriu nota nova (fonte de verdade dupla)"
    assert notas[0].name in vistos["check"]
    assert obsidian.name_errors(notas[0].name) == []
    assert scheduler.nome_da_nota(scheduler.carregar()[linha["id"]]) \
        == notas[0].name

    fonte = Path(scheduler.__file__).read_text(encoding="utf-8")
    assert _nomes_fora_do_padrao_na_mao(fonte) == [], \
        "o módulo monta nome .md à mão fora de um glob"
    arvore = ast.parse(fonte)
    build_calls = [no for no in ast.walk(arvore)
                   if isinstance(no, ast.Attribute)
                   and no.attr in {"build_name", "write_note"}]
    assert build_calls, "nenhuma nota sai do portão de nomes do cofre"


# ── regra 8: um único ponto de disparo; o que o disparo nunca faz ────────────

def test_disparo_unico_por_vencimento(cofre):
    falados: list[str] = []
    scheduler.ponte(voz=falados.append)
    linha = scheduler.registrar((datetime.now().astimezone()
                                 + timedelta(seconds=1)).isoformat(),
                                "aviso único", GATILHO_VALIDO,
                                origem="fala_do_dono")["linha"]
    mais_tarde = datetime.now().astimezone() + timedelta(seconds=5)
    primeiro = scheduler.tick(agora_dt=mais_tarde)
    segundo = scheduler.tick(agora_dt=mais_tarde + timedelta(seconds=5))
    assert [e["tipo"] for e in primeiro if e["tipo"] == "disparo"] == ["disparo"]
    assert [e for e in segundo if e.get("id") == linha["id"]] == []
    assert linha["id"] not in [e.get("id") for e in segundo]
    scheduler.carregar(forcar=True)
    assert scheduler.listar(abertos_only=False)[0]["disparou_em"], \
        "ficou aberto depois de falar"


def test_gatilho_roda_por_uma_unica_funcao(cofre):
    """Lei da casa: o mesmo `executar_gatilho` serve ao turno e ao laço."""
    rodadas: list[tuple] = []

    def sondinha(*args, **kwargs):
        rodadas.append((args, kwargs))
        return "rodou"

    scheduler.sondinha = sondinha  # type: ignore[attr-defined]
    try:
        gatilho = {"tipo": "funcao", "modulo": "core.scheduler",
                   "funcao": "sondinha", "destino": ["voz"]}
        falado: list[str] = []
        saida = scheduler.executar_gatilho(gatilho, boca=falado.append)
        assert saida["ok"], saida
        assert rodadas, "o gatilho não chamou a função cadastrada"
        assert falado == ["rodou"], falado
    finally:
        del scheduler.sondinha  # type: ignore[attr-defined]


def test_script_e_argv_vetorial_nunca_string_de_shell(cofre, tmp_path,
                                                      monkeypatch):
    script = tmp_path / "gatilho.sh"
    script.write_text("#!/bin/sh\necho \"argv=$#\" $1\n", encoding="utf-8")
    script.chmod(0o755)
    cadastro = scheduler.cadastrar_script(str(script), "teste",
                                          origem="fala_do_dono")
    assert cadastro["ok"], cadastro
    chamadas: list[list] = []
    real = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok",
                                       stderr="")

    def finge(cmd, **kw):
        chamadas.append([cmd, kw])
        assert kw.get("shell") is False
        return real

    monkeypatch.setattr(scheduler.subprocess, "run", finge)
    gatilho = {"tipo": "script", "script": str(script),
               "argumentos": [{"nome": "alvo", "tipo": "str",
                               "valor": "com espaço; e ponto"}],
               "destino": ["voz"]}
    norm, erros = scheduler.validar_gatilho(gatilho)
    assert erros == [], erros
    saida = scheduler.executar_gatilho(norm, boca=lambda t: None)
    assert saida["ok"], saida
    cmd, kw = chamadas[0]
    assert cmd == [str(script), "com espaço; e ponto"], cmd
    assert kw.get("shell") is False


def test_script_fora_do_cadastro_nao_e_gatilho(cofre, tmp_path):
    script = tmp_path / "solto.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    _, erros = scheduler.validar_gatilho({"tipo": "script",
                                          "script": str(script)})
    assert any("cadastro fechado" in erro for erro in erros), erros


def test_cadastrar_script_de_terceiro_e_recusado(cofre, tmp_path):
    script = tmp_path / "x.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    saida = scheduler.cadastrar_script(str(script), origem="fala_visitante")
    assert saida["ok"] is False


def test_disparo_de_destino_autorizado_pede_autorizacao_na_hora(cofre,
                                                                monkeypatch):
    """3.5: tier não afrouxa no disparo — sem fator dele, fica pendente."""
    monkeypatch.setattr(scheduler, "_tier_da_ferramenta", lambda nome: "medium")
    monkeypatch.setattr(scheduler.actiongate, "authorized", lambda: False)
    saida = scheduler.executar_gatilho(
        dict(GATILHO_VALIDO, destino=["arquivo-subir"]),
        linha={"titulo": "subir", "texto": "subir"})
    assert saida["pendente"], saida
    assert "autorização" in saida["pendente"]


def test_disparo_anda_sem_boca_nao_e_sucesso_mudo(cofre):
    saida = scheduler.executar_gatilho(dict(GATILHO_VALIDO, destino=["voz"]),
                                       linha={"titulo": "t", "texto": "t"})
    assert saida["ok"] is False
    assert "boca" in saida["recusa"]


def test_o_modulo_nao_toca_grant_nem_abre_janela_ast(cofre):
    """Risco 3: prova estática — nenhuma chamada a grant/janela no módulo."""
    arvore = ast.parse(Path(scheduler.__file__).read_text(encoding="utf-8"))
    proibidos = {"consume", "arm", "open_window", "arm_send_window",
                 "aprovar_plano", "registrar_plano"}
    achados = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Attribute) and no.attr in proibidos:
            achados.add(no.attr)
        if isinstance(no, ast.Name) and no.id in proibidos:
            achados.add(no.id)
    assert achados == set(), achados


# ── regra 9: acordar com vencidos (decisão 6, a regra ditada por ele) ────────

def test_acordar_avisa_contagem_da_resumo_e_pergunta(cofre):
    falados: list[str] = []
    scheduler.ponte(voz=falados.append,
                    whatsapp=lambda t: falados.append("WA:" + t))
    for titulo in ("a", "b", "c"):
        linha = scheduler.registrar(amanha(), titulo, GATILHO_VALIDO,
                                    origem="fala_do_dono")["linha"]
        linha_vencida(linha, minutos=40)
    saida = scheduler.acordar()
    assert saida["quantidade"] == 3
    texto = falados[0]
    assert "Tem 3 lembretes" in texto, texto
    assert "Quer ouvir um a um?" in texto, texto
    assert any(fala.startswith("WA:") for fala in falados), falados
    assert len(falados) == 2, "cada vencido falou sozinho — era um resumo só"


def test_resumo_de_vencidos_nao_se_repete_apos_reinicio(cofre):
    """A marca é persistida na nota: 'reiniciar' não re-spamma o recado."""
    scheduler.ponte(voz=lambda t: None)
    linha = scheduler.registrar(amanha(), "velho", GATILHO_VALIDO,
                                origem="fala_do_dono")["linha"]
    linha_vencida(linha, minutos=90)
    primeiro = scheduler.acordar()["quantidade"]
    monkeycache = {"assinatura": None, "linhas": {}}
    scheduler._cache = monkeycache          # simula o processo novo
    scheduler._ja_acordei = False
    segundo = scheduler.acordar()["quantidade"]
    assert primeiro == 1 and segundo == 0


def test_um_a_um_fala_e_marca_cada_vencido(cofre):
    falados: list[str] = []
    scheduler.ponte(voz=falados.append)
    linha = scheduler.registrar(amanha(), "consulta velha", GATILHO_VALIDO,
                                origem="fala_do_dono")["linha"]
    linha_vencida(linha, minutos=60)
    saida = scheduler.um_a_um()
    assert saida["falados"] == [linha["id"]]
    assert "consulta velha" in "".join(falados)
    assert scheduler.vencidos() == []


def test_vencido_dentro_da_graca_dispara_normalmente(cofre):
    falados: list[str] = []
    scheduler.ponte(voz=falados.append)
    linha = scheduler.registrar(amanha(), "quase", GATILHO_VALIDO,
                               origem="fala_do_dono")["linha"]
    linha_vencida(linha, minutos=1)
    eventos = scheduler.tick()
    assert any(e["tipo"] == "disparo" for e in eventos), eventos
    assert any("quase" in fala for fala in falados), falados


# ── regra 10: o laço novo não abriu uma quarta porta de hora ─────────────────

def test_o_modulo_nao_abre_thread_nem_laco_proprio(cofre):
    fonte = Path(scheduler.__file__).read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    importados = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            importados.update(a.name for a in no.names)
        elif isinstance(no, ast.ImportFrom) and no.module:
            importados.add(no.module)
    assert "threading" in importados            # só o lock, sem Thread(
    assert "threading.Thread" not in fonte
    assert "subprocess.Popen" not in fonte
    assert "import main" not in fonte and "from main" not in fonte
    assert "PyQt" not in fonte and "PySide" not in fonte
