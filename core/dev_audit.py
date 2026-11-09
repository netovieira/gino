"""core/dev_audit.py — auditoria de implementação no boot (ordem do dono, 07/11/2026).

O dono pediu, literalmente: "implementa no fluxo do startup um mecanismo que
verifica se foram implementadas novas coisas antes de fechar, auditar e
verificar se tudo foi implementado corretamente". A dor real: tarefas fechadas
no plano cujo elo no processo vivo NÃO existia — UP-8 com o gancho instalado
só dentro de teste (214 linhas ``hook=ausente`` no log), UP-10 emitindo o
evento por um caminho sem chamador em produção, LinkedIn "concluído" com
commit de comentário. Este módulo compara o que o plano AFIRMA com o que o
sistema REALMENTE mostra, e escreve a divergência onde ela dói: log de evento
e livro de defeitos.

Arquitetura (mínima, de propósito):

* REGISTRO de asserções (:data:`Assertion`): id, o que afirma, tipo de sonda
  (``estatica`` | ``viva`` | ``config``) e a sonda.
* Veredito por asserção: ``ok`` | ``falha`` | ``nao_verificado``. Sonda viva
  sem dados suficientes é ``nao_verificado`` — NUNCA ok. Honestidade acima de
  verde. Exceção dentro de sonda também vira ``nao_verificado`` com o motivo;
  uma sonda jamais derruba a auditoria, e a auditoria jamais derruba o boot.
* Saída consolidada: UMA linha estruturada ``event=dev_audit ...`` no log de
  atividade, e, para cada ``falha``, um defeito no mecanismo já existente do
  projeto (:func:`core.owner_law.open_defect` → ``memory/defeitos/abertos.jsonl``).
* Cache: uma passada por boot; ``run_audit(force=True)`` permite reexecução
  manual sem esperar reinício.
* Portão de fechamento: :func:`assert_ready_to_close` re-sonda um id e
  :func:`advise_close` é o AVISO registrado que ``core.task_mirror.finish``
  chama quando alguém encerra tarefa com asserção ``falha``/``nao_verificado``.
  É aviso, não bloqueio: nada aqui impede quem fecha de fechar.

RESTRIÇÃO PERMANENTE (lei do dono, 05/11/2026 22:27, reforçada na ordem que
criou este módulo): o mecanismo audita IMPLEMENTAÇÃO e ESTADO DO SISTEMA. Ele
NUNCA lê, grava, resume ou audita a FALA DO DONO. As sondas vivas só enxergam
linhas de EVENTO do sistema (:func:`_is_event_line`: prefixo ``event=``,
``SYS:``, ``[fluxos]``/``[Fluxos]``, ``[DevAudit]``), e os motivos produzidos
são sempre texto gerado por código — nenhuma chave de resultado, nenhum
veredito, nenhuma linha de saída carrega transcrição, fala ou conteúdo de
 utterance alheia. Se uma sonda precisar de evidência, ela olha o evento, não
a pessoa.

Não pode atrasar o boot: o chamador correto é ``main.JarvisLive._start_dev_audit``
(thread própria, depois de o serviço estar de pé). Este módulo não abre
thread, timer nem loop dentro de si.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ACTIVITY_LOG = BASE_DIR / "logs" / "activity.log"

OK = "ok"
FALHA = "falha"
NV = "nao_verificado"
VERDICTS = (OK, FALHA, NV)

#: Início deste processo (aproximação honesta do "último boot": o módulo é
#: importado no startup; linhas com timestamp anterior não interessam).
_BOOT_EPOCH = time.time()

_LINE_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\]\s*(.*)$")
#: Uma linha é de EVENTO quando o corpo (após o timestamp) começa com um dos
#: marcadores do sistema. Essa âncora no início do corpo é a barreira física
#: contra ler fala do dono: utterance nunca começa com esses prefixos.
_EVENT_BODY_RE = re.compile(
    r"^(?:event=[A-Za-z0-9_]+|SYS:|\[[Ff]luxos?\]|\[DevAudit\]|\[OwnerLaw\])")

# ── configuração (testes isolam aqui; produção usa os defaults) ─────────────
_ROOT = BASE_DIR
_ACTIVITY_LOG = DEFAULT_ACTIVITY_LOG
_LOG: Callable[[str], None] = lambda msg: print(msg)
_DEFECT_OPENER: Callable[[str], None] | None = None
_CFG: Callable[[str], dict] | None = None  # get_plugin_config(namespace), lazy
_BOOT_EPOCH_CFG = _BOOT_EPOCH

_lock = threading.RLock()
_last: dict[str, Any] | None = None
_reported: set[str] = set()  # ids já virados defeito neste processo
_event_cache: tuple[Any, Any] | None = None  # ((path, janela, mtime, tam), linhas)


def configure(*, root: Any = None, activity_log: Any = None,
              log: Callable[[str], None] | None = None,
              defect_opener: Callable[[str], None] | None = None,
              mail_config: Callable[[], dict] | None = None,
              boot_epoch: float | None = None) -> None:
    """Redireciona caminhos e canais (uso em teste e no boot do main.py)."""
    global _ROOT, _ACTIVITY_LOG, _LOG, _DEFECT_OPENER, _CFG, _BOOT_EPOCH_CFG
    with _lock:
        if root is not None:
            _ROOT = Path(root)
        if activity_log is not None:
            _ACTIVITY_LOG = Path(activity_log)
        if log is not None:
            _LOG = log
        if defect_opener is not None:
            _DEFECT_OPENER = defect_opener
        if mail_config is not None:
            _CFG = mail_config
        if boot_epoch is not None:
            _BOOT_EPOCH_CFG = float(boot_epoch)
        global _event_cache
        _event_cache = None


def reset_cache() -> None:
    """Esquece o resultado, os defeitos reportados e a janela de log."""
    global _last, _event_cache
    with _lock:
        _last = None
        _event_cache = None
        _reported.clear()


def last_result() -> dict[str, Any] | None:
    with _lock:
        return _last


def boot_id() -> str:
    return f"pid{os.getpid()}-{int(_BOOT_EPOCH_CFG)}"


# ── primitivas de sonda ─────────────────────────────────────────────────────
def _read(rel: str) -> str:
    """Texto de um arquivo do projeto (relativo à raiz configurada); '' se não há."""
    try:
        return (_ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tree(rel: str) -> ast.AST | None:
    src = _read(rel)
    if not src:
        return None
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def _find_func(node: ast.AST, name: str) -> ast.AST | None:
    """Primeira função/async def com este nome, em qualquer profundidade."""
    found: list[ast.AST] = []
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and sub.name == name:
            found.append(sub)
    return found[0] if found else None


def _calls_in(node: ast.AST) -> set[str]:
    """Nomes (com prefixo pontuado) de toda chamada dentro do nó."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        parts: list[str] = []
        f = sub.func
        while isinstance(f, ast.Attribute):
            parts.append(f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            parts.append(f.id)
        if parts:
            out.add(".".join(reversed(parts)))
    return out


def _muted_refs(node: ast.AST) -> int:
    """Quantas vezes o nó consulta a flag de mute (atributo ``muted`` ou
    ``getattr(..., "muted")``). O portão de silêncio do modo-silêncio usa a
    chave dict ``"muted"`` do ``_speech_route`` e fica FORA desta contagem de
    propósito: o que a lei proíbe é o mute da INTERFACE calar a boca."""
    n = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "muted":
            n += 1
        elif isinstance(sub, ast.Call) and _call_name(sub) == "getattr" \
                and any(isinstance(a, ast.Constant) and a.value == "muted"
                        for a in sub.args):
            n += 1
    return n


def _call_name(call: ast.Call) -> str:
    parts: list[str] = []
    f = call.func
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def event_lines_since_boot(*, limit: int = 8000) -> list[str] | None:
    """Linhas de EVENTO do sistema desde o início deste processo.

    ``None`` quando o log não existe ou não tem nenhuma linha legível na
    janela — ausência de dado NUNCA vira prova de nada (as sondas traduzem em
    ``nao_verificado``). Só entram linhas cujo corpo começa com um marcador de
    evento (:data:`_EVENT_BODY_RE`): a barreira é estrutural e vem ANTES de
    qualquer padrão — transcrição e fala humana são filtradas aqui, antes de
    qualquer comparação, e jamais chegam a uma sonda.

    Memoização por (caminho, janela, mtime, tamanho): o activity.log do dono
    é grande e as sondas vivem chamando esta função; sem o carimbo do
    arquivo, uma varrida a mais no boot custaria dezenas de MB lidos à toa.
    """
    global _event_cache
    path = Path(_ACTIVITY_LOG)
    try:
        stat = path.stat()
        stamp = (str(path), _BOOT_EPOCH_CFG, stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (str(path), _BOOT_EPOCH_CFG, None, None)
    with _lock:
        if _event_cache is not None and _event_cache[0] == stamp:
            return _event_cache[1]
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        out = None
    else:
        out = []
        for line in raw.splitlines():
            if len(out) >= limit:
                break
            m = _LINE_TS_RE.match(line.strip())
            if not m:
                continue
            try:
                ts = time.mktime(time.strptime(
                    m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S"))
            except (ValueError, OverflowError):
                continue
            body = m.group(2)
            if ts >= _BOOT_EPOCH_CFG and _EVENT_BODY_RE.match(body):
                out.append(body)
        out = out or None
    with _lock:
        _event_cache = (stamp, out)
    return list(out) if out else out


def _any_since(pred: Callable[[str], bool]) -> tuple[list[str] | None, list[str]]:
    lines = event_lines_since_boot()
    if lines is None:
        return None, []
    return lines, [ln for ln in lines if pred(ln)]


# ── registro ────────────────────────────────────────────────────────────────
@dataclass
class Assertion:
    """Uma afirmação do plano que este processo pode conferir sozinho."""
    id: str
    claims: str
    probe_type: str  # "estatica" | "viva" | "config"
    probe: Callable[[], tuple[str, str]]  # (veredito, motivo curto)


REGISTER: list[Assertion] = []


def register(a: Assertion) -> None:
    with _lock:
        for i, old in enumerate(REGISTER):
            if old.id == a.id:
                REGISTER[i] = a
                return
        REGISTER.append(a)


# ── sondas ──────────────────────────────────────────────────────────────────
def _sonda_up8_gancho() -> tuple[str, str]:
    """UP-8 (estática): o boot REAL instala o gancho de progresso.

    Caso pego da vida: o mecanismo existia, a instalação só acontecia em
    ``tests/`` e o vivo gravava ``hook=ausente``. A sonda lê o ``main.py`` de
    verdade: ``_flows_get`` (o boot da ponte) precisa chamar
    ``self._flows_progress_hook`` e essa função precisa chamar
    ``_task_mirror.set_progress_hook``. Teste chamando o auxiliar à mão não
    conta: nenhum arquivo de ``tests/`` é lido aqui.
    """
    tree = _tree("main.py")
    if tree is None:
        return NV, "main.py ilegível"
    get = _find_func(tree, "_flows_get")
    hookfn = _find_func(tree, "_flows_progress_hook")
    if get is None or hookfn is None:
        return FALHA, "boot da ponte ou funcao do gancho sumiu do main.py"
    if "_flows_progress_hook" not in " ".join(_calls_in(get)):
        return FALHA, "_flows_get nao chama _flows_progress_hook (so teste chama?)"
    if not any(c.endswith("set_progress_hook") for c in _calls_in(hookfn)):
        return FALHA, "_flows_progress_hook nao chama set_progress_hook"
    return OK, ""


def _sonda_up8_viva() -> tuple[str, str]:
    """UP-8 (viva): no log DESDE ESTE BOOT o gancho está instalado e ausente nenhum.

    Evidência aceita: ``[fluxos] progress_hook instalada=1`` ou evento
    ``task_progress`` com ``hook=installado``. Qualquer linha nova de evento
    do espelho com ``hook=ausente`` derruba o veredito — é o exato sintoma
    que a auditoria de 07/11 pegou (214 vezes). Janela sem nenhum dado é
    ``nao_verificado``, não ok.
    """
    lines, hits = _any_since(lambda ln: "hook=ausente" in ln)
    if lines is None:
        return NV, "log de evento indisponivel nesta janela de boot"
    if hits:
        return FALHA, f"{len(hits)} linha(s) nova(s) com hook=ausente"
    _, ok1 = _any_since(lambda ln: "progress_hook instalada=1" in ln)
    _, ok2 = _any_since(lambda ln: "event=task_progress" in ln
                        and "hook=installado" in ln)
    if ok1 or ok2:
        return OK, ""
    return NV, "nenhuma evidencia do gancho desde este boot (ponte nao subiu?)"


def _sonda_up10_chamador() -> tuple[str, str]:
    """UP-10 (estática): existe CHAMADOR em produção do caminho que emite o evento.

    O ``execution_complete`` vivia só em ``run_plan`` (sem chamador no vivo).
    O elo verdadeiro: ``core/flows/live.py`` — ``_gate_worker`` chama
    ``_notify_execution``, que chama ``notify_execution_complete`` do
    orquestrador. Os três elos têm de estar no arquivo, ou a sonda falha.
    """
    tree = _tree("core/flows/live.py")
    if tree is None:
        return NV, "core/flows/live.py ilegivel"
    gate = _find_func(tree, "_gate_worker")
    notify = _find_func(tree, "_notify_execution")
    if gate is None or notify is None:
        return FALHA, "funeo de portao ou notificacao sumiu do live.py"
    if "_notify_execution" not in " ".join(_calls_in(gate)):
        return FALHA, "_gate_worker nao chama _notify_execution"
    if not any(c.endswith("notify_execution_complete") for c in _calls_in(notify)):
        return FALHA, "_notify_execution nao chama notify_execution_complete"
    return OK, ""


def _sonda_aviso_email_gancho() -> tuple[str, str]:
    """Aviso do fluxo de e-mail (estática): ``set_notify_hook`` plugado no boot.

    Buraco idêntico ao UP-8: o gancho existia e só era instalado em teste.
    A sonda confere em ``main.py``: ``_start_mail`` (o boot do correio, na
    thread ``jarvis-mail-boot``) chama ``_mail_flows.set_notify_hook``.
    """
    tree = _tree("main.py")
    if tree is None:
        return NV, "main.py ilegivel"
    start = _find_func(tree, "_start_mail")
    if start is None:
        return FALHA, "_start_mail sumiu do main.py"
    if not any(c.endswith("set_notify_hook") for c in _calls_in(start)):
        return FALHA, "_start_mail nao instala set_notify_hook (so teste instala?)"
    return OK, ""


def _sonda_linkedin_rota_unica() -> tuple[str, str]:
    """LinkedIn (estática): UM produtor de aviso, e a regra delega o outro.

    O mesmo e-mail podia sair duas vezes: a frase de chegada do filtro e a
    frase rica do fluxo. A rota única exige: (1) exatamente um call-site de
    ``set_notify_hook`` fora de ``tests/`` (o boot, em ``main.py``); (2) o
    portão de delegação de ``core/email.py`` (``_notice_is_delegated``)
    definido E chamado — senão o aviso simples volta a falar por cima.
    """
    callers: list[str] = []
    for p in sorted(_ROOT.glob("*.py")) + sorted((_ROOT / "core").rglob("*.py")):
        if "__pycache__" in p.parts or p.suffix != ".py":
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"\.set_notify_hook\(", src):
            callers.append(p.name)
    email_tree = _tree("core/email.py")
    delegated_def = email_tree is not None and _find_func(
        email_tree, "_notice_is_delegated") is not None
    delegated_use = False
    if delegated_def and email_tree is not None:
        for sub in ast.walk(email_tree):
            if isinstance(sub, ast.Call) \
                    and _call_name(sub).endswith("_notice_is_delegated"):
                delegated_use = True
                break
    if len(callers) == 1 and callers[0] == "main.py" \
            and delegated_def and delegated_use:
        return OK, ""
    return (FALHA, f"produtores de aviso={callers or 'nenhum'}; "
            f"delegacao email.py definida={delegated_def} usada={delegated_use}")


def _sonda_mute_somente_microfone() -> tuple[str, str]:
    """Mute (estática): nenhum caminho de fala consulta a flag de mute da UI.

    Ordem do dono (07/11/2026): mute da interface É SOMENTE microfone. A sonda
    percorre o AST do ``main.py`` e vasculha os corpos das funções que produzem
    áudio (:func:`_TTS_FUNCS`) atrás de ``ui.muted``/``getattr(...,"muted")``.
    O portão do MODO-SILÊNCIO (chave ``"muted"`` do dict devolvido por
    ``_speech_route``) é outra coisa e não conta — o que é proibido aqui é o
    botão de mute calar a boca.
    """
    tree = _tree("main.py")
    if tree is None:
        return NV, "main.py ilegivel"
    guilty = []
    for fn_name in _TTS_FUNCS:
        fn = _find_func(tree, fn_name)
        if fn is None:
            guilty.append(f"{fn_name}(?)")
            continue
        if _muted_refs(fn):
            guilty.append(fn_name)
    if guilty:
        return FALHA, "mute da UI gateia caminho de fala: " + ",".join(guilty)
    return OK, ""


#: Funções que produzem/conduzem fala; nenhuma pode consultar o mute da UI.
_TTS_FUNCS = ("_say_now", "_say_blocking", "_play_audio", "_flows_speak",
              "_announce_otp", "_mail_flow_notice", "_live_progress_speech")


def _sonda_vault_nomes() -> tuple[str, str]:
    """Padrão de nomes do cofre (estática): validador + portão no caminho de escrita.

    A regra do dono (REGRA-2026-11-07) virou código em ``core/obsidian.py``.
    A sonda confere no AST: ``name_errors``/``check_name``/``build_name``/
    ``normalize_name`` definidos, as TRÊS políticas presentes, e — o elo que o
    dono cobra — ``_gate_name`` chamado dentro de ``write_note`` (caminho real
    de escrita, não um helper órfão).
    """
    tree = _tree("core/obsidian.py")
    if tree is None:
        return NV, "core/obsidian.py ilegivel"
    missing = [n for n in ("name_errors", "check_name", "build_name",
                           "normalize_name", "_gate_name", "write_note")
               if _find_func(tree, n) is None]
    if missing:
        return FALHA, "faltam no obsidian.py: " + ",".join(missing)
    policies: Any = None
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "NAME_POLICIES"
                for t in sub.targets):
            try:
                policies = ast.literal_eval(sub.value)
            except (ValueError, TypeError):
                policies = None
            break
    if not isinstance(policies, (tuple, list)) or len(policies) != 3:
        return FALHA, "as tres politicas de nome nao estao definidas"
    write = _find_func(tree, "write_note")
    if "_gate_name" not in " ".join(_calls_in(write)):  # type: ignore[arg-type]
        return FALHA, "write_note nao passa pelo _gate_name"
    return OK, ""


def _sonda_mic_reset_reopen() -> tuple[str, str]:
    """Ouvido surdo (estática): o reset do filtro de ruído roda no reopen/start.

    O fix do microfone que parava de ouvir exigia ``reset_noise_estimate``
    chamado no CAMINHO VIVO (``core/groq_stt.py``): na partida da stream e no
    reopen, com o gate re-semeando a estimativa. Teste chamando a rotina à mão
    não conta — a sonda só lê o módulo de produção.
    """
    tree = _tree("core/groq_stt.py")
    if tree is None:
        return NV, "core/groq_stt.py ilegivel"
    resetter = _find_func(tree, "_reset_noise_estimate")
    if resetter is None or not any(
            c.endswith("reset_noise_estimate") for c in _calls_in(resetter)):
        return FALHA, "metodo de reset nao delega ao noise_filter"
    src = _read("core/groq_stt.py")
    for token in ('_reset_noise_estimate("start")',
                  '_reset_noise_estimate("reopen")'):
        if token not in src:
            return FALHA, f"sem chamada de reset no caminho vivo: {token}"
    return OK, ""


def _sonda_whatsapp_paridade_gancho() -> tuple[str, str]:
    """Paridade WhatsApp (estática): o boot vivo audita a rota chamável de áudio.

    Dois buracos pagos pela mesma razão (07/11/2026): (a) o app sabia falar em
    arquivo, a ferramenta não — "manda um áudio" virava texto na conta errada;
    (b) action desconhecida caía no ``recent``, e a lista de conversas era lida
    como se o envio tivesse acontecido. A sonda confere no código de produção,
    sem ler ``tests/``:

      * ``main._start_whatsapp`` chama ``_whatsapp_parity_audit`` (o gancho está
        no BOOT, não só na fixture — o ``hook=ausente`` do UP-8);
      * ``_whatsapp_parity_audit`` escreve a linha de evento ``paridade de
        mecanismos`` que a sonda viva procura;
      * o plugin tem ``_do_send_voice`` ligando ``voice_note.synthesize`` ao
        ``whatsapp.send_media`` (UM motor, nenhuma segunda cópia de TTS);
      * ``run`` recusa action fora do catálogo ANTES de qualquer listagem
        (``canonical is None`` → ``_unknown_action``).
    """
    tree = _tree("main.py")
    if tree is None:
        return NV, "main.py ilegivel"
    start = _find_func(tree, "_start_whatsapp")
    audit = _find_func(tree, "_whatsapp_parity_audit")
    if start is None or audit is None:
        return FALHA, "falta _start_whatsapp ou _whatsapp_parity_audit no main.py"
    if "_whatsapp_parity_audit" not in " ".join(_calls_in(start)):
        return FALHA, "_start_whatsapp nao chama a auditoria de paridade"
    if "paridade de mecanismos" not in ast.unparse(audit):
        return FALHA, "auditoria nao grava a linha de evento da paridade"
    plugin = _tree("plugins/whatsapp_control.py")
    if plugin is None:
        return NV, "plugins/whatsapp_control.py ilegivel"
    sender = _find_func(plugin, "_do_send_voice")
    run = _find_func(plugin, "run")
    if sender is None or run is None:
        return FALHA, "sumiu _do_send_voice ou run() do whatsapp_control"
    calls = " ".join(_calls_in(sender))
    if "synthesize" not in calls or "send_media" not in calls:
        return FALHA, "send_voice nao usa o motor unico (voice_note.synthesize" \
                      " + whatsapp.send_media)"
    # O fallback morre num GUARDA, não numa string: o primeiro `if` do corpo
    # de `run` tem de ser `canonical is None` (comparação direta, sem `and`
    # disfarçando) e tem de retornar erro antes de qualquer listagem.
    guard = next((s for s in run.body if isinstance(s, ast.If)), None)
    test = getattr(guard, "test", None)
    refused = bool(guard and isinstance(test, ast.Compare)
                   and isinstance(test.ops[0], ast.Is)
                   and isinstance(test.comparators[0], ast.Constant)
                   and test.comparators[0].value is None
                   and any(isinstance(n, ast.Return) for n in guard.body)
                   and "_unknown_action" in " ".join(_calls_in(guard)))
    if not refused:
        return FALHA, "run() aceita action desconhecida sem erro (fallback vivo)"
    listing = [s for s in run.body
               if "_do_recent" in " ".join(_calls_in(s))]
    if listing and run.body.index(guard) > run.body.index(listing[0]):
        return FALHA, "run() lista conversas antes de recusar a action errada"
    return OK, ""


def _sonda_whatsapp_paridade_viva() -> tuple[str, str]:
    """Paridade WhatsApp (viva): o log DESTE boot registrou a auditoria com verde.

    Fonte no arquivo não basta — foi assim que o UP-8 viveu 214 vezes
    ``hook=ausente``. A prova é a linha de evento escrita pelo
    ``_whatsapp_parity_audit`` quando o processo subiu: sem linha, o mecanismo
    não está plugado no vivo; com ``send_voice=0``/``catalogo_fechado=0``, está
    plugado e quebrado. Log ilegível é ``nao_verificado`` (nunca verde).
    """
    lines, hits = _any_since(lambda ln: "paridade de mecanismos" in ln)
    if lines is None:
        return NV, "sem log de evento legivel neste boot"
    if not hits:
        return FALHA, "boot nao registrou a paridade do whatsapp"
    last = hits[-1]
    if "nao_verificado" in last:
        return NV, "auditoria de paridade falhou ao rodar neste boot"
    broken = [k for k in ("send_voice=0", "catalogo_fechado=0", "motor_unico=0")
              if k in last]
    if broken:
        return FALHA, "paridade registrada como quebrada: " + ",".join(broken)
    return OK, ""


def _sonda_skill_escopo() -> tuple[str, str]:
    """Escopo do pedido (estática): a SKILL existe e é referida no bloco por turno.

    A lei "entregue o artefato/tipo/canal/escopo pedidos — trocar é
    desobediência" mora em ``skills/escopo-do-pedido/SKILL.md`` e precisa
    chegar ao cérebro: a referência ao arquivo tem de estar no ``main.py``
    (bloco JUDGEMENT, o bloco compacto que volta a cada turno).
    """
    if not (_ROOT / "skills" / "escopo-do-pedido" / "SKILL.md").is_file():
        return FALHA, "skills/escopo-do-pedido/SKILL.md nao existe"
    src = _read("main.py")
    if not src:
        return NV, "main.py ilegivel"
    if "skills/escopo-do-pedido/SKILL.md" not in src:
        return FALHA, "referencia ausente do bloco compacto do turno (main.py)"
    return OK, ""


def _sonda_linkedin_config() -> tuple[str, str]:
    """LinkedIn (config): a regra ``linkedin_notification`` existe na config real.

    Sondagem DE CONFIG: só nomes e booleans — nunca valor de segredo, nunca
    e-mail, nunca conteúdo. A config ilegível é ``nao_verificado``; regra
    ausente na config lida é ``falha`` (o aviso do LinkedIn não tem porta).
    """
    getter = _CFG
    if getter is None:
        try:
            from memory.config_manager import get_plugin_config
        except Exception as exc:
            return NV, f"config indisponivel: {type(exc).__name__}"
        getter = lambda ns: get_plugin_config(ns)  # noqa: E731
    try:
        pc = getter("mail") or {}
    except Exception as exc:
        return NV, f"config ilegivel: {type(exc).__name__}"
    accounts = pc.get("accounts") if isinstance(pc, dict) else None
    if not isinstance(accounts, list):
        return FALHA, "sem contas de mail na config"
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        for rule in acct.get("filters") or []:
            if isinstance(rule, dict) and str(rule.get("tipo")) \
                    == "linkedin_notification" and rule.get("dispatch"):
                return OK, ""
    return FALHA, "nenhuma conta com filtro tipo=linkedin_notification dispatchavel"


def _sonda_persona_sem_numero() -> tuple[str, str]:
    """Persona sem número (estática): o cadastro manda e a rota de conta obedece.

    Regra do dono (09/11/2026): Aline e Arnaldo seguem ativos sem WhatsApp
    próprio; quem não tem ``direct_whatsapp`` declarado nunca recebe nem é
    roteada por conta, e não envia por conta que não seja a do assistente.
    A sonda lê SÓ código de produção (nenhum arquivo de ``tests/``):

      * ``core/personas.py`` tem o leitor do campo (``direct_whatsapp`` +
        ``own_account``), o ``whatsapp_account`` só é canônico com o booleano,
        e ``_resolve_whatsapp_account`` devolve vazio para vazio (vazio =
        nenhuma conta, nunca a principal — ordem 07/11/2026);
      * ``core/whatsapp_watch.addressed_persona`` filtra o fator
        ``conta_atribuida`` pelo leitor do cadastro (nada de comparar a conta
        direto com a chave crua);
      * ``core/whatsapp.send`` consulta ``persona_send_block`` antes de
        qualquer ``_A(account)`` — a rota de saída morre na porta, não no log.
    """
    store = _tree("core/personas.py")
    if store is None:
        return NV, "core/personas.py ilegivel"
    readers = ("direct_whatsapp", "own_account", "_normalize_direct_whatsapp")
    for name in readers:
        if _find_func(store, name) is None:
            return FALHA, f"falta {name}() em core/personas.py"
    upsert = _find_func(store, "upsert")
    if upsert is None:
        return FALHA, "sumiu upsert() de core/personas.py"
    up_calls = " ".join(_calls_in(upsert))
    if ("_normalize_direct_whatsapp" not in up_calls
            or "direct_whatsapp" not in ast.unparse(upsert)):
        return FALHA, "upsert nao le direct_whatsapp do cadastro"
    clean = _find_func(store, "_clean_persona")
    if clean is None or "direct_whatsapp" not in ast.unparse(clean):
        return FALHA, "leitura do cadastro nao normalize direct_whatsapp"
    resolve = _find_func(store, "_resolve_whatsapp_account")
    if resolve is None:
        return FALHA, "sumiu _resolve_whatsapp_account()"
    # Vazio = NENHUMA conta (ordem 07/11/2026): a função devolve string vazia
    # antes de qualquer comparação — nunca a conta principal por omissão.
    if not any(isinstance(node, ast.Constant) and node.value == ""
               for node in ast.walk(resolve)):
        return FALHA, "conta vazia nao devolve nenhuma conta (vazio != principal)"
    watch = _tree("core/whatsapp_watch.py")
    if watch is None:
        return NV, "core/whatsapp_watch.py ilegivel"
    addressed = _find_func(watch, "addressed_persona")
    helper = _find_func(watch, "_persona_own_account")
    if addressed is None or helper is None:
        return FALHA, "addressed_persona nao tem filtro de numero proprio"
    if "own_account" not in " ".join(_calls_in(helper)):
        return FALHA, "o filtro de conta nao usa o leitor do cadastro"
    if "_persona_own_account" not in " ".join(_calls_in(addressed)):
        return FALHA, "a rota de conta ainda compara a chave crua do cadastro"
    sender = _tree("core/whatsapp.py")
    if sender is None:
        return NV, "core/whatsapp.py ilegivel"
    send = _find_func(sender, "send")
    block = _find_func(sender, "persona_send_block")
    if send is None or block is None:
        return FALHA, "falta send() ou persona_send_block() no core/whatsapp.py"
    calls = " ".join(_calls_in(send))
    if "persona_send_block" not in calls:
        return FALHA, "a saida nao consulta o portao de persona sem numero"
    source = ast.unparse(send)
    if source.find("persona_send_block") > source.find("_A(account)"):
        return FALHA, "o portao roda depois de resolver a conta"
    if "own_account" not in " ".join(_calls_in(block)):
        return FALHA, "persona_send_block nao consulta direct_whatsapp/own_account"
    return OK, ""


for _a in (
    Assertion("UP-8.gancho-boot",
              "o boot real do main.py instala o gancho de progresso (task_mirror)",
              "estatica", _sonda_up8_gancho),
    Assertion("UP-8.vivo",
              "nenhuma linha nova hook=ausente; gancho instalado visto no log deste boot",
              "viva", _sonda_up8_viva),
    Assertion("UP-10.chamador",
              "execution_complete tem chamador em producao (FlowsRuntime._gate_worker)",
              "estatica", _sonda_up10_chamador),
    Assertion("aviso-email.gancho",
              "_start_mail instala o gancho de aviso do fluxo de e-mail",
              "estatica", _sonda_aviso_email_gancho),
    Assertion("linkedin.rota-unica",
              "um so produtor de aviso do LinkedIn, com delegacao no core/email.py",
              "estatica", _sonda_linkedin_rota_unica),
    Assertion("linkedin.filtro-config",
              "a config real tem filtro tipo=linkedin_notification com dispatch",
              "config", _sonda_linkedin_config),
    Assertion("mute.somente-microfone",
              "nenhuma funcao de fala consulta o mute da interface",
              "estatica", _sonda_mute_somente_microfone),
    Assertion("vault.padrao-nomes",
              "validador + portao de nome ativos no caminho de escrita do cofre",
              "estatica", _sonda_vault_nomes),
    Assertion("mic.reset-no-reopen",
              "o reset do estimador de ruido roda no start e no reopen do vivo",
              "estatica", _sonda_mic_reset_reopen),
    Assertion("skill.escopo-do-pedido",
              "SKILL.md do escopo existe e e referida no bloco compacto do turno",
              "estatica", _sonda_skill_escopo),
    Assertion("whatsapp.paridade-gancho",
              "boot vivo audita a rota chamavel de nota de voz e o catalogo fechado",
              "estatica", _sonda_whatsapp_paridade_gancho),
    Assertion("whatsapp.paridade-viva",
              "linha de evento da paridade vista no log deste boot, sem flag quebrada",
              "viva", _sonda_whatsapp_paridade_viva),
    Assertion("persona.sem-numero",
              "direct_whatsapp manda na conta: cadastro, rota de entrada e saida",
              "estatica", _sonda_persona_sem_numero),
):
    register(_a)


# ── execução ────────────────────────────────────────────────────────────────
def _run_one(a: Assertion) -> tuple[str, str]:
    """Uma sonda, sempre devolvendo (veredito, motivo); exceção vira prova de nada."""
    try:
        verdict, reason = a.probe()
    except Exception as exc:  # noqa: BLE001 - sonda louca não derruba a auditoria
        return NV, f"sonda explodiu: {type(exc).__name__}"
    if verdict not in VERDICTS:
        return NV, f"veredito invalido da sonda: {verdict!r:.40}"
    return verdict, str(reason or "")[:160]


def _sanitize(text: str) -> str:
    return " ".join(str(text or "").split())[:120]


def event_line(result: dict[str, Any]) -> str:
    """A linha estruturada do consolidado — só contagem e ids, nunca conteúdo."""
    return (f"event=dev_audit boot={result['boot']} "
            f"total={result['total']} ok={result['ok']} falha={result['falha']} "
            f"nao_verificado={result['nao_verificado']} "
            f"ids_falha=\"{','.join(result['ids_falha'])}\" "
            f"ids_nv=\"{','.join(result['ids_nv'])}\" "
            f"janela={result['janela']}")


def run_audit(*, force: bool = False, janela: str = "boot") -> dict[str, Any]:
    """Passada completa; uma por boot (cache), ``force=True`` para repetir.

    Nunca levanta: o pior resultado possível é um ``falha`` por asserção. A
    exceção do próprio runner (log quebrado, disco cheio) degrada a uma linha
    pelo canal de log e o dicionário ainda volta.
    """
    global _last
    with _lock:
        if _last is not None and not force:
            return _last
        verdicts: dict[str, tuple[str, str]] = {}
        for a in list(REGISTER):
            verdicts[a.id] = _run_one(a)
        counts = {v: sum(1 for x, _ in verdicts.values() if x == v)
                  for v in VERDICTS}
        result: dict[str, Any] = {
            "boot": boot_id(),
            "when": dt.datetime.now().isoformat(timespec="seconds"),
            "janela": janela,
            "total": len(verdicts),
            "ok": counts[OK], "falha": counts[FALHA],
            "nao_verificado": counts[NV],
            "ids_falha": [i for i, (v, _) in verdicts.items() if v == FALHA],
            "ids_nv": [i for i, (v, _) in verdicts.items() if v == NV],
            "verdicts": verdicts,
        }
        _last = result
    try:
        _LOG(event_line(result))
    except Exception:
        pass
    # Falha vira defeito no mecanismo que já existe no projeto (uma vez por
    # id por processo; force não reabre o mesmo defeito).
    by_id = {a.id: a for a in REGISTER}
    for aid in result["ids_falha"]:
        with _lock:
            if aid in _reported:
                continue
            _reported.add(aid)
        a = by_id.get(aid)
        motivo = result["verdicts"][aid][1]
        report = (f"[dev_audit] {aid}: a assercao \"{_sanitize(a.claims if a else aid)}\" "
                  f"falhou no vivo: {_sanitize(motivo)} "
                  f"(sonda {a.probe_type if a else '?'}, boot {result['boot']})")
        try:
            if _DEFECT_OPENER is not None:
                _DEFECT_OPENER(report)
            else:
                from core import owner_law
                owner_law.open_defect(report, intent="defeito_dev_audit",
                                      who="sistema")
        except Exception:
            pass  # sem defeito registrado não é motivo para gritar
    return result


# ── portão de fechamento (a segunda metade da ordem) ───────────────────────
def assert_ready_to_close(task_id: str) -> dict[str, Any]:
    """Re-sonda AGORA a(s) asserção(ões) daquele id. Nunca levanta.

    Retorna ``{"task_id", "known", "verdicts": {id: [veredito, motivo]}}``.
    Id sem asserção cadastrada devolve ``known=False`` — a auditoria não tem
    opinião sobre o que nunca afirmou. Não bloqueia nada: quem fecha é livre;
    o mecanismo existe para que o aviso fique REGISTRADO.
    """
    out: dict[str, Any] = {"task_id": str(task_id or ""), "known": False,
                           "verdicts": {}}
    for a in list(REGISTER):
        if a.id == str(task_id or "") or a.id.startswith(f"{task_id}."):
            out["known"] = True
            out["verdicts"][a.id] = list(_run_one(a))
    return out


def advise_close(task_id: str, status: str = "done") -> dict[str, Any]:
    """O AVISO que ``core/task_mirror.finish`` emite ao fechar um item.

    Só status de conclusão, só quando o id tem asserção e o veredito não é
    ok. A linha ``event=close_advisory`` carrega ids e motivos DE SONDA —
    nunca título, resumo ou fala de quem quer que seja.
    """
    info: dict[str, Any] = {"task_id": str(task_id or ""), "known": False,
                            "verdicts": {}}
    if str(status or "").strip().lower() not in ("done", "completed",
                                                 "concluido", "concluído"):
        return info
    try:
        info = assert_ready_to_close(task_id)
        for aid, (verdict, motivo) in info["verdicts"].items():
            if verdict == OK:
                continue
            _LOG(f"event=close_advisory task=\"{_sanitize(aid)}\" "
                 f"verdict={verdict} motivo=\"{_sanitize(motivo)}\" "
                 f"boot={boot_id()}")
    except Exception:
        pass  # o aviso não pode quebrar o fechamento de tarefa alheia
    return info


# ── (não) resumo: o JSON do resultado é para diagnóstico local ─────────────
def dump_result() -> str:
    """JSON do último resultado; nunca inclui fala — só vereditos e motivos de sonda."""
    with _lock:
        if _last is None:
            return "{}"
        return json.dumps({k: v for k, v in _last.items()},
                          ensure_ascii=False, default=str)
