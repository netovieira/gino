"""Fluxo do painel GUIA — leitura do disco, somente-leitura, redação.

Não é unidade isolada: o painel é instanciado como o app o instancia
(GuideOverlay como overlay de um widget central, com QApplication real em
offscreen), e as quatro provas da ordem do dono são assertedas juntas:

(a) as seis seções fixas presentes;
(b) o texto veio do disco — mexer um arquivo temporário mexe o painel;
(c) nenhum widget de edição existe;
(d) segredo/dado pessoal não aparece — [retido] aparece no lugar.

Todos os valores "secretos" deste arquivo são fictícios, escritos aqui para
provar que são retidos.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# Arquivos que o painel espelha; copiados para o espelho temporário do teste.
_MIRROR = [
    "skills/conversation/SKILL.md",
    "skills/execution/SKILL.md",
    "skills/actions/SKILL.md",
    "skills/voice/SKILL.md",
    "skills/personas/SKILL.md",
    "skills/fluxos/SKILL.md",
    "skills/fluxos/FLUXOS.md",
    "skills/whatsapp/SKILL.md",
    "skills/memory/SKILL.md",
    "core/flows/guard.py",
    "core/flows/mouth.py",
    "core/flows/config.py",
    "core/flows/live.py",
    "core/flows/router.py",
    "core/flows/orchestrator.py",
    "core/free_models.py",
    "core/rootgate.py",
    "core/actiongate.py",
    "core/job_grant.py",
    "core/owner_decision.py",
    "core/restart.py",
    "core/chat_memory.py",
    "core/memory_index.py",
]


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def mirror_root(tmp_path):
    """Cópia da árvore de regras em tmp — o 'arquivo temporário' do teste."""
    for rel in _MIRROR:
        src = REPO / rel
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return tmp_path


def _all_text(overlay) -> str:
    """Todo texto visível no painel, página por página."""
    chunks = []
    tabs = overlay._tabs
    for i in range(tabs.count()):
        page = tabs.widget(i)
        for edit in page.findChildren(_qTextEdit()):
            chunks.append(edit.toPlainText())
        for lab in page.findChildren(_qLabel()):
            chunks.append(lab.text())
    return "\n".join(chunks)


def _qTextEdit():
    from PyQt6.QtWidgets import QTextEdit
    return QTextEdit


def _qLabel():
    from PyQt6.QtWidgets import QLabel
    return QLabel


# ── (a) as sete seções, montadas como o app monta ───────────────────────────

def test_seis_secoes_presentes(qapp, mirror_root):
    from ui import GuideOverlay
    ov = GuideOverlay(parent=None, base_dir=mirror_root)
    titles = [ov._tabs.widget(i).objectName() for i in range(ov._tabs.count())]
    assert ov._tabs.count() == 7, titles
    expect = {"MINHAS REGRAS (o assistente/orquestador)", "REGRAS DA BOCA",
              "REGRAS DO CÉREBRO", "REGRAS DO ORQUESTRADOR / GATES",
              "MEMÓRIA", "EXECUÇÃO CONCRETA (FASE3-TASK4)", "LINKS"}
    assert set(titles) == expect
    # cada seção não é vazia: texto de regra aparece
    assert len(_all_text(ov)) > 10_000


# ── (b) o painel é disco, não código ────────────────────────────────────────

def test_mudar_arquivo_muda_painel(qapp, mirror_root):
    from ui import GuideOverlay
    ov = GuideOverlay(parent=None, base_dir=mirror_root)
    alvo = mirror_root / "skills" / "whatsapp" / "SKILL.md"
    marca = "MARCA-DE-TESTE-GUIA-42"
    assert marca not in _all_text(ov)

    alvo.write_text(alvo.read_text(encoding="utf-8") + f"\n\n{marca}\n",
                    encoding="utf-8")
    ov._reload()                       # o botão RECARREGAR, como no app
    assert marca in _all_text(ov)

    # e sem recarregar o disco continua sendo a fonte: outro arquivo
    guard = mirror_root / "core" / "flows" / "guard.py"
    guard.write_text(guard.read_text(encoding="utf-8") +
                     "\n# LEI-NOVA-GUIA-77\n", encoding="utf-8")
    from core.guide_panel import sections
    secs = sections(mirror_root)
    boca = [s for s in secs if s["short"] == "BOCA"][0]
    assert any("LEI-NOVA-GUIA-77" in it["text"] for it in boca["items"])


# ── (c) somente leitura: nada de widget de edição ──────────────────────────

def test_nenhum_widget_de_edicao(qapp, mirror_root):
    from PyQt6.QtWidgets import (QCheckBox, QComboBox, QLineEdit,
                                 QPlainTextEdit, QAbstractSpinBox, QTextEdit,
                                 QPushButton)
    from ui import GuideOverlay
    ov = GuideOverlay(parent=None, base_dir=mirror_root)

    for cls in (QLineEdit, QPlainTextEdit, QCheckBox, QComboBox,
                QAbstractSpinBox):
        assert ov.findChildren(cls) == [], f"{cls.__name__} no painel GUIA"

    # todo QTextEdit do painel é leitor: readOnly e sem flags de edição
    from PyQt6.QtCore import Qt
    editable = Qt.TextInteractionFlag.TextEditable
    for edit in ov.findChildren(QTextEdit):
        assert edit.isReadOnly()
        flags = edit.textInteractionFlags()
        assert not (flags & editable), "QTextEdit editável no GUIA"

    # os únicos botões são os permitidos: copiar, abrir, recarregar, fechar
    verbos_edicao = ("salvar", "editar", "aplicar", "submit", "delete",
                     "remover", "escrever")
    for btn in ov.findChildren(QPushButton):
        t = btn.text().lower()
        assert not any(v in t for v in verbos_edicao), f"botão de edição: {t}"
        assert (t in ("fechar", "⧉  copiar tudo", "⟳  recarregar do disco")
                or t in ("⧉", "📂")), f"ação não permitida no GUIA: {t!r}"


# ── (d) segredo e dado pessoal não aparecem ────────────────────────────────

def test_segreto_e_dado_pessoal_retidos(qapp, mirror_root):
    from ui import GuideOverlay
    alvo = mirror_root / "skills" / "whatsapp" / "SKILL.md"
    # valores FICTÍCIOS, escritos só para provar que são retidos
    alvo.write_text(
        alvo.read_text(encoding="utf-8") +
        "\napi_key: FAKEFAKEXYZ123456789\n"
        "senha = fakehunter2secret\n"
        "CPF do dono 999.888.777-66\n"
        "telefone +55 11 97777-6666\n"
        "hash 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n",
        encoding="utf-8")
    ov = GuideOverlay(parent=None, base_dir=mirror_root)
    visivel = _all_text(ov)
    for vazamento in ("FAKEFAKEXYZ123456789", "fakehunter2secret",
                      "999.888.777-66", "97777-6666",
                      "0123456789abcdef0123456789abcdef"):
        assert vazamento not in visivel
    assert "[retido]" in visivel


# ── o painel real (raiz do projeto) também monta e não quebra ──────────────

def test_painel_real_do_projeto(qapp):
    from ui import GuideOverlay
    ov = GuideOverlay(parent=None)            # base_dir = BASE_DIR, como no app
    assert ov._tabs.count() == 7
    visivel = _all_text(ov)
    assert "guard" in visivel or "Guardas da boca" in visivel
    # a seção LINKS aponta para o arquivo-fonte de cada regra
    assert any(s["short"] == "LINKS" for s in ov._sections)
    assert all(it["path"] for s in ov._sections
               for it in s["items"] if "path" in it)
