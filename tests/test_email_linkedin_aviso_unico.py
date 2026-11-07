"""PROIBIÇÃO: um e-mail do LinkedIn, UM aviso — nunca dois (defeito de 07/11/2026).

O dono decidiu que o LinkedIn era só um filtro no fluxo de leitura de e-mails.
A decisao foi implementada duas vezes, em paralelo: o filtro avisava ("Chegou um
e-mail de LinkedIn com o assunto...") e o dispatcher de corpo avisava de novo
("LinkedIn: Fulana te mandou..."). O teto de 40 s da fala engolia a segunda
frase em algumas sessoes — era sorte, nao design.

Este arquivo nao testa comportamento: testa a INVARIANTE. Se alguem religar o
aviso simples por cima do fluxo, ou trocar o dedupe por janela de tempo, os
testes abaixo quebram.

O que fica proibido aqui:
  1. um e-mail que casa na regra de aviso E cai no fluxo ``linkedin_notification``
     produz EXATAMENTE uma frase falada e EXATAMENTE um registro em disco;
  2. a varredura repetida do MESMO e-mail nao gera segunda frase nem segundo
     registro — mesmo quando o historico de cabecalhos (``mail_inbox_*.jsonl``)
     some e o e-mail volta a parecer novo: o que segura e o RECIBO (Message-ID,
     senao o uid), nunca a janela de tempo;
  3. duas mensagens diferentes no mesmo dia continuam sendo dois avisos (o
     dedupe nao virou silenciador);
  4. quando sobra registro, ele e uma nota no cofre cujo nome passa no validador
     da REGRA de nomes — e um tipo inventado e rejeitado;
  5. nao existe segundo produtor: ``set_notify_hook`` e definido uma vez so no
     modulo de fluxos e instalado uma vez so no boot (nada de codigo morto).

Rede e disco do dono ficam de fora: IMAP e fake, cofre/historico/logs vao para
tmp e os ganchos sao coletores em memoria. Corpo e nomes sao fixtures nossos.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import email as mail
from core import email_flows as flows
from core import obsidian
from tests.test_email import FakeIMAP, _full_message, vault  # noqa: F401

# A regra em producao (config -> mail.accounts[principal].filters): aviso +
# despacho de corpo + tipo que produz o proprio aviso. E a UNICA porta.
LI_FILTER = {"from": r"@linkedin\.com", "match": "regex", "action": "avisa",
             "dispatch": True, "tipo": "linkedin_notification",
             "label": "linkedin"}

# FIXTURES inventados para este teste (nenhum e-mail real).
QUEM = "Beatriz Unica"
CORPO_CONVITE = (f"{QUEM} quer conectar-se com você na sua rede do LinkedIn. "
                 "Aceitar convite: https://www.linkedin.com/mngprm/invite/abc")
CORPO_MENSAGEM = (f"{QUEM} enviou uma mensagem. Olá, tudo bem naquele projeto? "
                  "Responder: https://www.linkedin.com/messaging/thread/4471/")


@pytest.fixture()
def palco(vault, monkeypatch, tmp_path):  # noqa: F811  (vault = config em memoria)
    """Os dois caminhos de aviso do processo vivo, como coletores, disco em tmp.

    ``chegada`` recebe o que o gancho do ``poll_once`` (em producao
    ``main._mail_new_message``, a frase de chegada) efetivamente recebeu.
    ``avisos`` recebe o que o gancho de aviso do fluxo (em producao
    ``main._mail_flow_notice``) efetivamente recebeu. A invariante e sobre a
    SOMA dos dois: em producao cada um desses itens vira UMA fala.
    """
    state: dict = {"chegada": [], "avisos": [], "eventos": [], "tmp": tmp_path}

    # `mail.reset()` chama `email_flows.reset()`, e o reset VOLTA o `_seen_path`
    # para o arquivo real do repo. Reset antes, caminho depois — na ordem
    # contrária o teste gasta recibo no histórico do dono.
    flows.reset()
    mail.reset()
    monkeypatch.setattr(flows, "_seen_path", tmp_path / "email_flow_seen.json")
    monkeypatch.setattr(flows, "_LINKEDIN_LOG",
                        tmp_path / "logs" / "linkedin-notifications.log")
    monkeypatch.setattr(obsidian, "VAULT", tmp_path / "vault")
    monkeypatch.setattr(obsidian, "PLANS_DIR", tmp_path / "qoder" / "plans")
    monkeypatch.setattr(obsidian, "ACTIVITY_LOG", tmp_path / "activity.log")
    monkeypatch.setattr(obsidian, "NEXTCLOUD_DIR", tmp_path / "Nextcloud")
    monkeypatch.delenv("JARVIS_VAULT_NAME_POLICY", raising=False)
    monkeypatch.delenv("JARVIS_OBSIDIAN_VAULT", raising=False)

    flows.set_notify_hook(lambda p: (state["avisos"].append(dict(p)), True)[1])
    flows.set_speak_hook(lambda p: (state["avisos"].append(dict(p)), True)[1])
    flows.set_event_hook(state["eventos"].append)
    mail.add_account("principal", "eu@gmail.com", notify=True,
                     filters=[dict(LI_FILTER)])
    state["varrer"] = lambda subject, corpo, recibo, uid="12": _varrer(
        monkeypatch, state, subject, corpo, recibo, uid)
    yield state
    flows.reset()
    mail.reset()


def _varrer(monkeypatch, state: dict, subject: str, corpo: str,
             recibo: str, uid: str) -> list[dict]:
    """Uma varredura de verdade com um e-mail do LinkedIn na caixa."""
    fake = FakeIMAP({uid.encode(): _full_message(
        subject, corpo, sender="LinkedIn <messages-noreply@linkedin.com>",
        message_id=recibo)})
    monkeypatch.setattr(mail, "_imap_open", lambda _a: fake)
    return mail.poll_once(hook=state["chegada"].append)


def _frases(state: dict) -> list[str]:
    """Tudo o que este e-mail pode virar audio, nos dois caminhos possiveis."""
    return [str(mail.alert_line(item) or "") for item in state["chegada"]] + [
        str(p.get("text") or "") for p in state["avisos"]]


def _registros(tmp_path: Path) -> list[str]:
    """Os registros em disco do aviso: notas do cofre + linhas do log."""
    root = tmp_path / "vault"
    notas = ([str(p.relative_to(root)) for p in sorted(root.rglob("*.md"))]
             if root.is_dir() else [])
    log = tmp_path / "logs" / "linkedin-notifications.log"
    linhas = ([f"log:{n}" for n in log.read_text(encoding="utf-8").splitlines()]
              if log.exists() else [])
    return notas + linhas


def _apaga_historico_de_cabecalhos() -> None:
    """Simula o corte do jsonl (INBOX_KEEP): o e-mail volta a parecer novo."""
    for caminho in Path(mail._MEMORY_DIR).glob("mail_inbox_*.jsonl"):
        caminho.unlink(missing_ok=True)


# ── 1. uma frase, um registro: os dois caminhos nao somam dois avisos ────────

def test_email_que_casa_no_filtro_e_no_fluxo_produz_uma_frase_so(palco):
    """"avisa" no filtro + despacho de corpo: o TOTAL de frases e 1, nao 2."""
    delivered = palco["varrer"]("Convite de conexao", CORPO_CONVITE,
                                "<li-unico@linkedin.com>")
    assert len(delivered) == 1
    assert palco["chegada"] == [], "o gancho de chegada tinha que ficar calado"
    frases = _frases(palco)
    assert len(frases) == 1, f"aviso duplo: {frases}"
    assert frases[0].startswith("LinkedIn:"), frases
    assert "convite de conexão" in frases[0].lower(), frases
    assert len(palco["eventos"]) == 1
    assert _registros(palco["tmp"]) == [], \
        "convite sem mensagem privada nao deixa registro em disco"


def test_mensagem_privada_deixa_um_unico_registro_no_cofre(palco):
    palco["varrer"]("Nova mensagem", CORPO_MENSAGEM, "<li-msg@linkedin.com>")
    assert len(_frases(palco)) == 1, _frases(palco)
    registros = _registros(palco["tmp"])
    assert len(registros) == 1, f"um e-mail, um registro: {registros}"
    assert registros[0].endswith(".md"), \
        f"mensagem privada e nota no cofre, nao linha de log: {registros}"


def test_regra_de_corpo_sem_tipo_continua_com_um_aviso_do_filtro(palco):
    """Contrapositivo: sem tipo que avisa por si, quem fala e o filtro — um so."""
    mail.update_account("principal", filters=[
        {"from": r"@linkedin\.com", "match": "regex", "action": "avisa",
         "dispatch": True}])
    palco["varrer"]("Convite de conexao", CORPO_CONVITE,
                    "<li-simples@linkedin.com>", uid="13")
    assert len(palco["chegada"]) == 1, palco["chegada"]
    assert len(_frases(palco)) == 1, _frases(palco)
    assert _registros(palco["tmp"]) == []


# ── 2. varredura repetida: quem segura e o recibo, nao a janela de tempo ─────

def test_revarredura_do_mesmo_email_nao_gera_segundo_aviso(palco):
    """O cabecalho some do jsonl (trim) e o e-mail parece novo: ainda uma frase.

    E o caso que antes virava sorte: sem historico o filtro entrega o e-mail de
    novo ao despacho, e so o RECIBO gasto impede a segunda frase.
    """
    palco["varrer"]("Nova mensagem", CORPO_MENSAGEM, "<li-repetido@linkedin.com>")
    assert len(_frases(palco)) == 1
    _apaga_historico_de_cabecalhos()
    for _ in range(3):
        assert palco["varrer"]("Nova mensagem", CORPO_MENSAGEM,
                               "<li-repetido@linkedin.com>") == []
    assert len(_frases(palco)) == 1, "recibo gasto nao pode virar segundo aviso"
    assert len(_registros(palco["tmp"])) == 1


def test_dedupe_nao_e_janela_de_tempo(palco):
    """Dois recibos diferentes no mesmo dia: dois avisos (dedupe nao e calador)."""
    palco["varrer"]("Nova mensagem", CORPO_MENSAGEM, "<li-a@linkedin.com>")
    _apaga_historico_de_cabecalhos()
    palco["varrer"]("Nova mensagem", CORPO_MENSAGEM, "<li-b@linkedin.com>",
                    uid="14")
    assert len(_frases(palco)) == 2, _frases(palco)
    assert len(_registros(palco["tmp"])) == 2


# ── 3. se vira nota, o nome obedece a REGRA do cofre ─────────────────────────

def test_nota_da_mensagem_passa_no_validador_de_nomes(palco):
    palco["varrer"]("Nova mensagem", CORPO_MENSAGEM, "<li-nome@linkedin.com>")
    notas = sorted((palco["tmp"] / "vault").rglob("*.md"))
    assert len(notas) == 1, notas
    nome = notas[0].name
    assert obsidian.name_errors(nome) == [], nome
    assert nome.split("-")[0] in obsidian.NOTE_TYPES, nome


def test_nota_de_nome_longo_passa_no_validador(palco, monkeypatch):
    """Remetente com nome quilometrico nao estoura NAME_MAX_CHARS nem escapa.

    E o que o ``open()`` solto da implementacao antiga fazia: escrevia direto,
    sem portao. Aqui o nome sai do ``build_name`` e vem valido ou nao vem.
    """
    longo = ("Antonieta Wilhelmina Barbara Castellani de Albuquerque "
             "Villanueva del Prado y Zuazola")
    corpo = (f"{longo} enviou uma mensagem. Vamos conversar sobre o acordo? "
             "Responder: https://www.linkedin.com/messaging/thread/4472/")
    palco["varrer"]("Nova mensagem", corpo, "<li-comprido@linkedin.com>",
                    uid="15")
    notas = sorted((palco["tmp"] / "vault").rglob("*.md"))
    assert len(notas) == 1, notas
    assert len(notas[0].name) <= obsidian.NAME_MAX_CHARS, notas[0].name
    assert obsidian.name_errors(notas[0].name) == [], notas[0].name


def test_tipo_inventado_de_nota_ser_rejeitado():
    """O vocabulario e fechado: um tipo nosso nao tem nota (REGRA do dono)."""
    with pytest.raises(ValueError):
        obsidian.build_name("MINUTA", "linkedin msg beatriz unica")
    with pytest.raises(ValueError):
        obsidian.build_name("LINKEDIN", "linkedin msg beatriz unica")


# ── 4. boca indisponivel: recibo livre E e-mail devolvido a varredura ────────

def test_boca_indisponivel_devolve_o_email_para_a_varredura_seguinte(palco):
    """Transiente de fala: recibo nao gasto E o e-mail volta a parecer novo.

    Os dois lados do retry. O fluxo segura o recibo (o arquivo de vistos nem
    nasce); quem desfaz o rastro de cabecalhos em disco e o ``core/email.py``
    — sem isso a varredura seguinte nem ve o e-mail, e "tenta de novo" vira
    comentario. Nada aqui toca o repo real: visto e jsonl vivem em ``tmp``.
    """
    gate = {"livre": False}

    def boca(payload: dict) -> bool:
        if not gate["livre"]:
            return False
        palco["avisos"].append(dict(payload))
        return True

    flows.set_notify_hook(boca)
    palco["varrer"]("Nova mensagem", CORPO_MENSAGEM,
                    "<li-tenta@linkedin.com>", uid="16")
    assert palco["eventos"] and palco["eventos"][-1]["retry"] is True
    assert _frases(palco) == [], "boca muda: nada falado, nada gravado"
    assert not (palco["tmp"] / "email_flow_seen.json").exists(), \
        "transiente nao pode gastar o recibo"
    assert _registros(palco["tmp"]) == []

    gate["livre"] = True
    palco["varrer"]("Nova mensagem", CORPO_MENSAGEM,
                    "<li-tenta@linkedin.com>", uid="16")
    frases = _frases(palco)
    assert len(frases) == 1, f"a segunda varredura tinha que avisar: {frases}"
    assert len(palco["eventos"]) == 2
    assert len(_registros(palco["tmp"])) == 1

    gate["livre"] = True
    _apaga_historico_de_cabecalhos()
    assert palco["varrer"]("Nova mensagem", CORPO_MENSAGEM,
                           "<li-tenta@linkedin.com>", uid="16") == []
    assert len(_frases(palco)) == 1, "recibo gasto: fim da historia"


# ── 5. um produtor so: nada de gancho duplicado morto ────────────────────────

def _defs_do_modulo(modulo, nome: str) -> int:
    arvore = ast.parse(inspect.getsource(modulo))
    return sum(1 for no in ast.walk(arvore)
               if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef))
               and no.name == nome)


def test_set_notify_hook_existe_uma_unica_vez():
    """A duplicacao da refatoracao paralela (definiu-se o gancho duas vezes) nao
    volta: definicao unica no modulo e importadores de pe."""
    assert _defs_do_modulo(flows, "set_notify_hook") == 1
    assert _defs_do_modulo(flows, "_handle_linkedin") == 1
    assert _defs_do_modulo(flows, "_announce") == 1
    from core.email_flows import set_notify_hook      # importador nao quebrou
    assert callable(set_notify_hook)


def test_boot_instala_o_gancho_de_aviso_uma_unica_vez():
    import main

    fonte = inspect.getsource(main)
    assert fonte.count("set_notify_hook(") == 1, "duas instalacoes = dois donos"
    assert fonte.count("def _mail_flow_notice") == 1


def test_o_unico_ponto_que_decide_quem_avisa_esta_no_filtro():
    """A decisao e da REGRA, em um ponto so — nunca do resultado do despacho."""
    assert mail._notice_is_delegated(LI_FILTER) is True
    assert inspect.getsource(mail).count("if _notice_is_delegated(") == 1


def test_nada_do_aviso_vai_para_o_historico_de_cabecalhos(palco):
    """Privacidade continua: o jsonl guarda cabecalho, nunca corpo nem link."""
    palco["varrer"]("Nova mensagem", CORPO_MENSAGEM, "<li-priv@linkedin.com>")
    guardado = "".join(p.read_text(encoding="utf-8")
                       for p in Path(mail._MEMORY_DIR).glob("mail_inbox_*.jsonl"))
    assert "messaging/thread" not in guardado
    assert QUEM not in guardado
    assert "tudo bem" not in guardado


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
