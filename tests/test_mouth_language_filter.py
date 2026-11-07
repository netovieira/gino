"""UV-2: a boca não fala inglês nem jargão técnico (filtro de idioma do TTS).

Origem: P3/UV-2 — ``clean_sentence`` existia, mas vazamento de inglês e texto
técnico ainda saía no vivo. O corpus ``LIVE_LEAKS`` abaixo é o que o
activity.log de 07/11/2026 registrou na saída de voz; nada disso pode voltar.

Lei que o portão serve (skills/conversation/SKILL.md, dono em 31/10/2026):
"nunca vaze palavra em inglês na fala; termo técnico inglês sem equivalente
sai em português corrido ou não sai".
"""
from __future__ import annotations

import re
import time

import pytest

from core.flows import guard as _guard
from core.flows import mouth as mouth_mod
from core.flows.config import FlowsConfig
from core.flows.mouth import (
    ALLOWED_ENGLISH_NAMES,
    ENGLISH_WORDS,
    PORTUGUESE_WORDS,
    TECH_TERMS_PT,
    Mouth,
    clean_sentence,
    english_leak,
    jargon_leak,
    speech_leak,
    translate_tech_terms,
)
from core.flows.router import Router

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# ── corpora ────────────────────────────────────────────────────────────────

# O que vazou no vivo (logs/activity.log, 07/11/2026) e mais o mesmo tipo de
# frase que o modelo local devolve quando pensa em inglês.
LIVE_LEAKS = (
    "Extract final suite counts",
    "Identify test at ~10% position via collection order",
    "Get subset counts and check global run progress",
    "Show diff stats and diff.",
    "Inspect slice run error",
    "Rerun full suite showing final summary lines",
    "Find file_processor usages",
    "Rerun the failing tests only",
    "a leitura _check_material_proof implementation.",
    "Opening the app now",
    "Working on it, please wait",
    "I will check the file and return the result",
    "The server is running",
    "Spotify is playing your playlist",
    "$ pytest -q tests/test_mouth_language_filter.py",
    "commit 84aeded pushed to main",
    "core/flows/mouth.py changed",
    "Mudança no arquivo config.yaml, criador.",
    "O trabalho agora usa fetchData, criador.",
)

# Fala legítima do assistente (boca e caminhos de voz): nada aqui pode ser
# calado — é o preço de um filtro que não emudece a casa.
SPOKEN_OK = (
    "Já te digo, criador.",
    "Entendi, criador.",
    "Certo, criador.",
    "Deixa eu organizar isso, criador.",
    "Bem, criador; máquina alegre e sem incêndio hoje.",
    "Bom dia, criador. A casa está de pé.",
    "Spotify aberto e volume em 40%.",
    "Queen tocando no Spotify.",
    "O trabalho em segundo plano segue com uma mudança no código.",
    "O trabalho em segundo plano tem uma etapa nova: O plano está dividido.",
    "Reiniciar é irreversível: não faço isso sem a tua voz.",
    "A escuta agora espera o fim real da minha fala.",
    "Dezoito entregas de hoje já estão medidas, criador.",
    "Nada foi executado, criador; o pedido ficou no meio.",
    "Diga pode cancelar se for isso mesmo.",
    "A busca automática devolveu as stacks de react e typescript.",
    "O cadastro guiado funcionou na primeira tentativa.",
    "A conta ficou feia, criador: quarenta e quatro ecos na fila.",
    "Abri os espelhos, criador, mas tenho que te dizer o que eles mostram.",
    "A árvore principal está limpa; o que estava sujo era outro caminho.",
)


def _config(**kw) -> FlowsConfig:
    base = dict(enabled=True, mouth_enabled=True)
    base.update(kw)
    return FlowsConfig(**base)


def _caller(text, *, error=None):
    """Um `stream_ollama` falso: devolve exatamente a frase pedida."""
    def caller(messages, max_tokens, **kw):
        return {"text": "" if error else text, "ttft_ms": 5.0,
                "first_sentence_ms": 6.0, "end_ms": 7.0, "error": error,
                "aborted": False}
    return caller


# ── 1. a limpeza de sempre continua igual ──────────────────────────────────

def test_limpeza_basica_preservada():
    assert clean_sentence('  "Já te digo, criador."  ') == "Já te digo, criador."
    assert clean_sentence("Frase: Entendi, criador.") == "Entendi, criador."
    assert clean_sentence("resposta: certo, criador") == "certo, criador"
    assert clean_sentence("gino: bom dia, criador") == "bom dia, criador"
    assert clean_sentence("Uma linha.\noutra linha") == "Uma linha."
    assert clean_sentence("muitos    espaços   aqui") == "muitos espaços aqui"
    assert clean_sentence("") == ""
    assert clean_sentence(None) == ""


# ── 2. dicionário de termos técnicos -> pt-BR ──────────────────────────────

@pytest.mark.parametrize("entrada,esperada", [
    ("o commit passou", "A entrega passou"),
    ("no commit de hoje", "Na entrega de hoje"),
    ("dos builds", "Das compilações"),
    ("os tests falharam", "Os testes falharam"),
    ("Dezoito commits hoje", "Dezoito entregas hoje"),
    ("sem commit", "sem entrega"),
    ("rodei os testes e o build", "rodei os testes e a compilação"),
    ("a suite fechou", "A bateria de testes fechou"),
    ("o deploy saiu", "A publicação saiu"),
    ("o token expirou", "O código de acesso expirou"),
])
def test_termo_tecnico_vira_nome_em_portugues(entrada, esperada):
    assert translate_tech_terms(entrada) == esperada


def test_artigo_e_caixa_seguem_a_frase():
    assert translate_tech_terms("Commit falhou") == "Entrega falhou"
    assert translate_tech_terms("O commit falhou") == "A entrega falhou"
    # nenhum artigo enfiado onde o português não pede
    assert " o " not in translate_tech_terms("trinta commits")


def test_traducao_nao_inventa_assercao_nem_anuncio():
    """A troca nunca pode criar palavra proibida pelo guard."""
    for value in TECH_TERMS_PT.values():
        assert not _guard.has_assertion(value), value
        assert not _guard.announcement(value), value


def test_traducao_e_dentro_do_guard_da_boca():
    """Toda frase traduzida continua falável para o guard (fase brief)."""
    for frase in ("o commit passou", "a suite fechou", "o deploy saiu"):
        dita = clean_sentence(frase)
        ok, why = _guard.guard(dita, None, "brief")
        assert ok, (dita, why)


# ── 3. nomes próprios autorizados passam intactos ──────────────────────────

@pytest.mark.parametrize("nome", ["Spotify", "WhatsApp", "Queen", "Netflix"])
def test_nome_proprio_autorizado_nao_e_vazamento(nome):
    assert nome in ALLOWED_ENGLISH_NAMES or nome.lower() in {
        n.lower() for n in ALLOWED_ENGLISH_NAMES}
    frase = f"Achei o {nome} ligado aqui"
    assert speech_leak(frase) == ""
    assert nome in clean_sentence(frase)


def test_nome_autorizado_nao_e_traduzido():
    assert clean_sentence("Queen tocando no Spotify") == \
        "Queen tocando no Spotify"


def test_whatsapp_em_ingles_continua_cortado():
    # nome autorizado não salva frase inteira em inglês
    assert clean_sentence("WhatsApp is sending the message") == ""


# ── 4. o que vazou no vivo não volta ───────────────────────────────────────

@pytest.mark.parametrize("vazada", LIVE_LEAKS)
def test_vazamento_do_vivo_nao_sai_da_boca(vazada):
    assert clean_sentence(vazada) == ""
    assert speech_leak(vazada)


def test_metrica_de_sucesso_zero_palavra_em_ingles_na_saida():
    """grep de palavras em inglês na saída de voz = zero (exceto nome próprio).

    Corpus inteiro: os vazamentos do vivo mais a fala legítima. O que sobra de
    cada frase limpa não pode conter nenhuma palavra dos léxicos de inglês.
    """
    for frase in LIVE_LEAKS + SPOKEN_OK:
        saida = clean_sentence(frase)
        if not saida:
            continue
        vazas = [w for w in (m.lower() for m in _WORD_RE.findall(saida))
                 if w in ENGLISH_WORDS]
        assert not vazas, (frase, saida, vazas)


def test_fala_portuguesa_nunca_e_calada():
    for frase in SPOKEN_OK:
        assert clean_sentence(frase) != "", frase


def test_fala_portuguesa_nao_e_reescrita_sem_preciso():
    # só troca de termo técnico mexe na frase; o resto passa byte a byte
    for frase in SPOKEN_OK:
        assert clean_sentence(frase) in (frase, translate_tech_terms(frase))


# ── 5. jargão de código é cortado, não traduzido ───────────────────────────

@pytest.mark.parametrize("codigo", [
    "mudei _check_material_proof aqui",
    "a funcao fetchData quebrou",
    "rode com a flag --no-verify",
    "editei core/flows/mouth.py",
    "o arquivo config.yaml mudou",
    "skills/conversation, commit 84aeded",
])
def test_identificador_de_codigo_cal_a_frase(codigo):
    assert clean_sentence(codigo) == ""
    assert jargon_leak(codigo)


def test_motivo_do_vazamento_e_descritivo():
    assert "caminho" in jargon_leak("mudei core/login.py")
    assert "jargao" in jargon_leak("rodei apt_get install")
    assert "ingles" in english_leak("Show the diff and stats")
    assert speech_leak("Já te digo, criador.") == ""


def test_lexicos_nao_se_sobrepem():
    """Traduzir tem prioridade sobre calar; nome autorizado sobre os dois."""
    chaves = {k.lower() for k in TECH_TERMS_PT}
    nomes = {n.lower() for n in ALLOWED_ENGLISH_NAMES}
    assert not chaves & set(ENGLISH_WORDS)
    assert not nomes & set(ENGLISH_WORDS)
    assert not nomes & chaves


# ── 6. portão idempotente e barato ─────────────────────────────────────────

@pytest.mark.parametrize("frase", LIVE_LEAKS + SPOKEN_OK)
def test_clean_sentence_e_idempotente(frase):
    uma = clean_sentence(frase)
    assert clean_sentence(uma) == uma


def test_portao_nao_segura_o_caminho_rapido():
    started = time.perf_counter()
    for _ in range(200):
        clean_sentence("Já te digo, criador; os commits viraram entregas.")
    assert (time.perf_counter() - started) < 1.5


# ── 7. integração com a boca: modelo em inglês cai no template ────────────

def test_brief_em_ingles_vira_template_e_registra_o_motivo():
    decision = Router().parse("tocar Queen")
    mouth = Mouth(_config(), caller=_caller("Playing Queen now"))
    result = mouth.brief("tocar Queen", decision)
    assert "Playing" not in result.sentence
    assert result.fixed is True
    assert mouth.guard_trips == 1
    assert "ingles" in result.guard_tripped
    # a palavra original do modelo fica só no raw, para auditoria
    assert result.raw == "Playing Queen now"


def test_answer_em_ingles_e_silencio_nao_resposta_errada():
    decision = Router().parse("como você está?")
    mouth = Mouth(_config(), caller=_caller("I am fine, thanks"))
    result = mouth.answer("como você está?", decision)
    assert result.sentence == ""
    assert mouth.guard_trips == 1
    assert "ingles" in result.guard_tripped


def test_complement_com_fato_em_ingles_nao_anexa_o_vazamento():
    decision = Router().parse("abrir o Spotify")
    facts = [{"spoken": "the player is open", "detail": "", "assertion": True}]
    mouth = Mouth(_config(), caller=_caller("Tudo certo por aqui"))
    result = mouth.complement("abrir o Spotify", decision, facts)
    assert "is open" not in result.sentence
    assert clean_sentence(result.sentence) == result.sentence


def test_complement_tradiz_o_termo_do_modelo():
    decision = Router().parse("abrir o Spotify")
    facts = [{"spoken": "Spotify aberto", "detail": "", "assertion": True}]
    mouth = Mouth(_config(),
                  caller=_caller("O commit passou, Spotify aberto"))
    result = mouth.complement("abrir o Spotify", decision, facts)
    assert "commit" not in result.sentence
    assert "entrega" in result.sentence


def test_andamento_em_ingles_usa_o_template_da_etapa():
    mouth = Mouth(_config(), caller=_caller("Extract final suite counts"))
    result = mouth.announce_progress("pytest -q", stage="testes")
    assert result.sentence == \
        "O trabalho em segundo plano segue com uma rodada de testes."
    assert result.fixed is True
    assert "ingles" in result.guard_tripped
    assert mouth.guard_trips == 1


def test_andamento_nao_recita_linha_ingles_do_espelho():
    """Modelo morto: o template não pode falar o que o espelho gravou em inglês."""
    mouth = Mouth(_config(), caller=_caller("", error="Ollama não respondeu"))
    result = mouth.announce_progress("Show diff stats and diff", stage="")
    assert result.sentence == "O trabalho em segundo plano tem novidade."
    assert clean_sentence(result.sentence) == result.sentence


def test_andamento_em_portugues_continua_normal():
    mouth = Mouth(_config(),
                  caller=_caller("Mudança no código do login, criador."))
    result = mouth.announce_progress("editar core/login.py", stage="edicao")
    assert result.sentence == "Mudança no código do login, criador."
    assert result.fixed is False
    assert mouth.guard_trips == 0


def test_gate_nao_muda_o_que_o_guard_ja_cortava():
    """Assertividade do UP-8 continua: asserção sem fato não sai, em inglês ou não."""
    mouth = Mouth(_config(), caller=_caller("Pronto, criador, já abri tudo"))
    decision = Router().parse("tocar Queen")
    result = mouth.brief("tocar Queen", decision)
    assert "pronto" not in result.sentence.lower()
    assert mouth.guard_trips == 1


def test_modulo_continua_sem_dependencias_novas():
    """O portão de idioma é puro: nada de Qt, áudio, rede ou main.py."""
    import ast

    with open(mouth_mod.__file__, encoding="utf-8") as fh:
        arvore = ast.parse(fh.read())
    raizes: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.ImportFrom):
            raizes.add((no.module or "").split(".")[0])
        elif isinstance(no, ast.Import):
            raizes.update(alias.name.split(".")[0] for alias in no.names)
    assert raizes <= {"", "__future__", "json", "re", "threading", "time",
                      "dataclasses", "pathlib", "typing", "requests",
                      "guard", "config", "router"}

