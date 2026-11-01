"""Pre-action narration: humane murmurs, jargon lock, silence rules."""
from __future__ import annotations

from pathlib import Path

import pytest

from core import narration

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "models", "memory",
             "tests", "logs"}


def _policy(monkeypatch=None, delay=None):
    if monkeypatch is not None:
        monkeypatch.delenv(narration.DELAY_ENV, raising=False)
    return narration.NarrationPolicy(delay_s=delay)


def _asst(name, args, user_text, persona_name=""):
    return ("mcp__jarvis__jarvis_call",
            {"name": name, "args": args}, user_text, persona_name)


def test_default_delay_is_five_seconds(monkeypatch):
    monkeypatch.delenv(narration.DELAY_ENV, raising=False)
    assert narration.narration_delay() == 5.0
    assert narration.NarrationPolicy().delay_s == 5.0


def test_delay_is_configurable_by_env(monkeypatch):
    monkeypatch.setenv(narration.DELAY_ENV, "7.5")
    assert narration.NarrationPolicy().delay_s == 7.5
    monkeypatch.setenv(narration.DELAY_ENV, "not-a-number")
    assert narration.NarrationPolicy().delay_s == 5.0


@pytest.mark.parametrize("tool_name,tool_input", [
    ("mcp__jarvis__jarvis_call",
     {"name": "media_control", "args": {"action": "play", "query": "one more time"}}),
    ("mcp__jarvis__jarvis_call",
     {"name": "media_control", "args": {"action": "volume", "value": "up"}}),
    ("mcp__jarvis__jarvis_call",
     {"name": "open_app", "args": {"app": "spotify"}}),
    ("mcp__jarvis__jarvis_call",
     {"name": "computer_settings", "args": {"setting": "brightness"}}),
    ("mcp__jarvis__memory_search", {"query": "aniversário"}),
    ("mcp__jarvis__jarvis_call",
     {"name": "tasks", "args": {"action": "status"}}),
])
def test_local_instant_actions_never_narrate(tool_name, tool_input):
    policy = _policy(delay=0.0)
    assert policy.plan(tool_name, tool_input, user_text="faz isso") is None


@pytest.mark.parametrize("user_text", [
    "Gino, você está bem?",
    "tudo bem com você?",
    "obrigado, valeu",
    "bom dia",
    "quem é você?",
])
def test_social_question_never_narrates_even_for_an_external_action(user_text):
    policy = _policy(delay=0.0)
    tool_input = {"name": "whatsapp_control",
                  "args": {"action": "send", "to": "Maria", "text": "oi"}}
    assert policy.plan("mcp__jarvis__jarvis_call", tool_input,
                       user_text=user_text) is None


def test_external_effect_action_narrates_immediately():
    """A WhatsApp send must be announced at ~1 s, not only after the 5 s wait."""
    policy = _policy(delay=5.0)
    tool_input = {"name": "whatsapp_control",
                  "args": {"action": "send", "to": "Maria", "text": "oi"}}
    plan = policy.plan("mcp__jarvis__jarvis_call", tool_input,
                       user_text="manda um oi pra Maria")
    assert plan is not None
    assert "Maria" in plan.text
    assert "mensagem" in plan.text.lower()
    assert narration.is_technical(plan.text) is False
    assert plan.immediate is True
    assert plan.delay_s == 0.0
    assert policy.decide(plan, 0.0).narrate is True
    assert policy.decide(plan, 1.0).narrate is True
    assert policy.decide(plan, 9.0, spoken=True).narrate is False


def test_slow_action_without_external_effect_still_waits_for_the_threshold():
    policy = _policy(delay=5.0)
    tool_input = {"name": "qoder_agent",
                  "args": {"task": "revisar o main.py"}}
    plan = policy.plan("mcp__jarvis__jarvis_call", tool_input,
                       user_text="pede pro Qoder revisar o main")
    assert plan is not None
    assert plan.immediate is False
    assert plan.delay_s == 5.0
    assert policy.decide(plan, 1.0).narrate is False
    assert policy.decide(plan, 4.99).narrate is False
    assert policy.decide(plan, 5.0).narrate is True


def test_slow_job_gets_a_task_specific_line():
    policy = narration.NarrationPolicy(delay_s=5.0)
    tool_input = {"name": "qoder_agent",
                  "args": {"task": "revisar o main.py"}}
    plan = policy.plan("mcp__jarvis__jarvis_call", tool_input,
                       user_text="pede pro Qoder revisar o main")
    assert plan is not None
    assert plan.text in narration.variants("investigar")
    assert narration.is_technical(plan.text) is False
    assert narration.is_generic(plan.text) is False
    assert "pensando" not in plan.text.lower()


def test_persona_ask_names_the_persona():
    policy = narration.NarrationPolicy(delay_s=5.0)
    plan = policy.plan("mcp__jarvis__persona_ask", {"persona": "fulana"},
                       user_text="pede pra ela revisar", persona_name="Fulana")
    assert plan is not None
    assert "Fulana" in plan.text
    assert narration.is_technical(plan.text) is False
    assert plan.immediate is False


def test_generic_cues_are_rejected():
    for text in (
            "Estou pensando...",
            "Deixa eu analisar isso.",
            "Deixa eu ver.",
            "Deixa eu ver isso.",
            "Um instante, estou pensando...",
            "Estou pensando numa solução...",
            "Só um instante.",
            "Esse caminho não vai dar certo; pensando numa alternativa...",
            "Pensando em outra abordagem.",
            "Procurando uma solução para isso.",
            "Isso não vai dar certo."):
        assert narration.is_generic(text), text
        plan = narration.NarrationPlan(text=text, delay_s=0.0)
        assert narration.NarrationPolicy(delay_s=0.0).decide(plan, 10.0).narrate is False


def test_a_concrete_image_after_the_lead_is_not_generic():
    for text in ("deixa eu ver onde isso encalha",
                 "deixa eu ver onde o microfone encalha"):
        assert narration.is_generic(text) is False, text


def test_specific_action_cues_are_not_flagged_as_generic():
    policy = narration.NarrationPolicy(delay_s=5.0)
    whatsapp = policy.plan(
        "mcp__jarvis__jarvis_call",
        {"name": "whatsapp_control",
         "args": {"action": "send", "to": "Maria", "text": "oi"}},
        user_text="manda um oi pra Maria")
    qoder = policy.plan(
        "mcp__jarvis__jarvis_call",
        {"name": "qoder_agent", "args": {"task": "revisar o main.py"}},
        user_text="pede pro Qoder revisar o main")
    for plan in (whatsapp, qoder):
        assert plan is not None
        assert narration.is_generic(plan.text) is False, plan.text


def test_no_generic_template_survives_in_code():
    """The old fixed cues must not come back outside the denylist itself."""
    markers = ("estou pensando", "deixa eu pensar", "deixa eu analisar",
               "pensando em", "pensando numa", "um instante", "só um instante",
               "procurando uma solução", "não vai dar certo")
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".mjs"):
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path == ROOT / "core" / "narration.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for marker in markers:
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)}: {marker}")
    assert offenders == []


def test_social_question_with_a_real_command_is_not_social():
    policy = narration.NarrationPolicy(delay_s=0.0)
    tool_input = {"name": "whatsapp_control",
                  "args": {"action": "send", "to": "Maria", "text": "oi"}}
    plan = policy.plan("mcp__jarvis__jarvis_call", tool_input,
                       user_text="tudo bem? manda um oi pra Maria")
    assert plan is not None


def test_same_phrase_is_not_repeated_in_consecutive_turns():
    policy = narration.NarrationPolicy(delay_s=0.0)

    def send_to(name):
        tool_input = {"name": "whatsapp_control",
                      "args": {"action": "send", "to": name, "text": "oi"}}
        return policy.plan("mcp__jarvis__jarvis_call", tool_input,
                           user_text=f"manda um oi pra {name}")

    first = policy.decide(send_to("Maria"), 10.0, turn="turno-1")
    assert first.narrate is True
    policy.note_spoken(first.text, "turno-1")

    # The very same sentence in the next turn is silence, not a stutter.
    again = policy.decide(send_to("Maria"), 10.0, turn="turno-2")
    assert again.narrate is False

    # A different target is a different sentence, so it may speak.
    other = policy.decide(send_to("João"), 10.0, turn="turno-3")
    assert other.narrate is True
    policy.note_spoken(other.text, "turno-3")
    assert policy.would_repeat(send_to("João").text, "turno-4") is True
    assert policy.would_repeat(send_to("Maria").text, "turno-4") is False


TECHNICAL_SAMPLES = (
    "vou rodar o pytest agora",
    "o tool_bridge quebrou",
    "fiz o deploy do backend",
    "olhei o log do main.py",
    "a thread travou",
    "abri a porta 8080",
    "chamei o Qoder",
    "JSON inválido na resposta",
    "o mpris não responde",
    "o dconf salvou",
    "o gsettings mudou",
    "o ruff reclamou",
    "deu timeout",
    "preciso indexar os arquivos",
    "vou compilar tudo",
    "executar os testes",
    "criei um script novo",
    "o agente respondeu",
    "o caminho /home/x/core/narration.py",
)


def test_jargon_lock_flags_technical_text():
    for text in TECHNICAL_SAMPLES:
        assert narration.is_technical(text), text
    for text in (
            "deixa eu ver onde o microfone encalha",
            "isso tem dois fios e eu só achei um",
            "tô remendando o abajur, falta alinhar",
            "Maria recebe a mensagem daqui a pouco"):
        assert narration.is_technical(text) is False, text


def test_every_family_has_clean_variations():
    for family in narration.FAMILIES:
        for subject in ("", "o microfone"):
            extra = {"target": "Maria"} if family == "externo" else {}
            lines = narration.variants(family, subject, **extra)
            assert len(lines) >= 4, (family, subject, lines)
            for line in lines:
                assert line.strip(), (family, subject)
                assert narration.is_technical(line) is False, line
                assert narration.is_generic(line) is False, line


def test_every_planned_cue_is_clean_and_tied_to_an_action():
    fixtures = [
        _asst("whatsapp_control",
              {"action": "send", "to": "Maria", "text": "oi"},
              "manda um oi pra Maria"),
        _asst("send_message",
              {"receiver": "João", "text": "chegando"}, "avisa o João"),
        _asst("email_control",
              {"action": "send", "to": "maria@exemplo.com", "subject": "oi"},
              "manda um e-mail pra Maria"),
        _asst("upload", {"filename": "foto.png"}, "sobe a foto"),
        _asst("purchase", {"item": "café"}, "compra o café"),
        _asst("android_control", {"command": "abrir a câmera"},
              "abre a câmera no celular"),
        _asst("browser_control", {"query": "preço do café"},
              "procura o preço do café"),
        _asst("cakto_sales", {}, "quanto vendi hoje"),
        _asst("trello_control", {"task": "mover o cartão"},
              "muda o cartão de lugar"),
        _asst("computer_control", {"task": "arrumar a janela"},
              "deixa a janela maior"),
        _asst("qoder_agent", {"task": "arrumar o abajur"},
              "conserta o abajur"),
    ]
    policy = narration.NarrationPolicy(delay_s=5.0)
    for tool_name, payload, user_text, persona in fixtures:
        plan = policy.plan(tool_name, payload, user_text=user_text,
                           persona_name=persona)
        assert plan is not None, user_text
        assert narration.is_technical(plan.text) is False, plan.text
        assert narration.is_generic(plan.text) is False, plan.text


def test_subject_shapes_the_murmur_without_jargon():
    policy = narration.NarrationPolicy(delay_s=5.0)
    plan = policy.plan(
        "mcp__jarvis__jarvis_call",
        {"name": "qoder_agent", "args": {"task": "revisar o microfone"}},
        user_text="preciso revisar o microfone")
    assert plan is not None
    assert "microfone" in plan.text
    assert plan.text in narration.variants("investigar", "o microfone")
    assert narration.is_technical(plan.text) is False


def test_technical_subject_is_never_echoed():
    policy = narration.NarrationPolicy(delay_s=5.0)
    plan = policy.plan(
        "mcp__jarvis__jarvis_call",
        {"name": "qoder_agent", "args": {"task": "revisar o main.py"}},
        user_text="revisar o main.py")
    assert plan is not None
    assert "main" not in plan.text.lower()
    assert narration.is_technical(plan.text) is False


def test_three_consecutive_turns_get_distinct_murmurs():
    policy = narration.NarrationPolicy(delay_s=5.0)
    seen = []
    for number, subject in enumerate(("o microfone", "a geladeira", "o abajur"), 1):
        plan = policy.plan(
            "mcp__jarvis__jarvis_call",
            {"name": "qoder_agent", "args": {"task": f"revisar {subject}"}},
            user_text=f"preciso revisar {subject}")
        text = plan.text if plan else ""
        decision = policy.decide(plan, 5.0, turn=f"turno-{number}")
        assert decision.narrate is True, (subject, text)
        policy.note_spoken(decision.text, f"turno-{number}")
        seen.append(decision.text)
    assert len(set(seen)) == 3, seen


def test_subjectless_turns_do_not_repeat_back_to_back():
    policy = narration.NarrationPolicy(delay_s=5.0)
    spoken = []
    for number, action in enumerate(("qoder_agent", "dev_agent", "qoder_agent"), 1):
        plan = policy.plan(
            "mcp__jarvis__jarvis_call",
            {"name": action, "args": {"task": "revisar o main.py"}},
            user_text="pede pra revisar o main")
        decision = policy.decide(plan, 5.0, turn=f"turno-{number}")
        assert decision.narrate is True, action
        policy.note_spoken(decision.text, f"turno-{number}")
        spoken.append(decision.text)
    assert len(set(spoken)) >= 2, spoken
    assert spoken[0] != spoken[1] and spoken[1] != spoken[2], spoken


def test_decide_refuses_a_technical_plan():
    policy = narration.NarrationPolicy(delay_s=0.0)
    plan = narration.NarrationPlan(text="vou rodar o pytest agora", delay_s=0.0)
    assert policy.decide(plan, 10.0).narrate is False
