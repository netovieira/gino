"""Limites de simultaneidade do quadro de personas (ordem do dono, 09/11/2026).

"Posso ter infinitas personas, mas não posso pedir coisas a mais de 4 por vez,
e cada persona não pode executar mais de 4 tasks por vez" — duas contas, dois
tetos, ambos ajustáveis na interface (⚙ → PLUGIN SETTINGS → PERSONAS, ver
``plugins/persona_limits.py``):

``max_concurrent_personas``
    Quantas personas podem estar trabalhando ao mesmo tempo. Pedir a quinta é
    recusado com o motivo na resposta, nunca enfileirado em segredo.
``max_tasks_per_persona``
    Quantas tasks uma mesma persona pode estar executando ao mesmo tempo.

Nada aqui decide trabalho nem abre thread: este módulo só conta. Quem executa
(abridor de sessão no sidecar, ``core.persona_ops.wiring``) pede a senha antes
e devolve no fim. Contas vivas morrem com o processo; o que precisa sobreviver
é o parâmetro, e ele mora em ``plugin_config.personas`` — lido pelo ``memory.
config_manager``, que é o mesmo arquivo que a UI grava.

Teto de personas não existe mais: ``core.personas.MAX_PERSONAS`` foi retirado
por ordem do dono. O que existe é este teto de *concorrência*.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

NAMESPACE = "personas"
KEY_CONCURRENT = "max_concurrent_personas"
KEY_TASKS = "max_tasks_per_persona"

DEFAULT_MAX_CONCURRENT_PERSONAS = 4
DEFAULT_MAX_TASKS_PER_PERSONA = 4
MIN_LIMIT = 1
MAX_LIMIT = 64


class LimitReached(RuntimeError):
    """O teto foi batido: a mensagem é falável e diz qual teto."""


def _clamp(value, default: int) -> int:
    """Um inteiro de limite, sempre utilizável: nada de zero, nada de texto."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if number < MIN_LIMIT:
        return MIN_LIMIT
    if number > MAX_LIMIT:
        return MAX_LIMIT
    return number


def _stored(namespace: str = NAMESPACE) -> dict:
    try:
        from memory import config_manager
        cfg = config_manager.get_plugin_config(namespace)
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def limit(key: str, default: int, source: dict | None = None) -> int:
    """Um limite do config, com teto de segurança aplicado."""
    return _clamp((source if source is not None else _stored()).get(key),
                  default)


def max_concurrent_personas(source: dict | None = None) -> int:
    return limit(KEY_CONCURRENT, DEFAULT_MAX_CONCURRENT_PERSONAS, source)


def max_tasks_per_persona(source: dict | None = None) -> int:
    return limit(KEY_TASKS, DEFAULT_MAX_TASKS_PER_PERSONA, source)


def read_limits(source: dict | None = None) -> dict[str, int]:
    """Os dois parâmetros, prontos para a UI e para a resposta da ferramenta."""
    return {
        "max_concurrent_personas": max_concurrent_personas(source),
        "max_tasks_per_persona": max_tasks_per_persona(source),
    }


def save_limits(concurrent: Any = None, per_persona: Any = None) -> dict:
    """Grava os parâmetros no config (a UI chama isto; ninguém edita JSON)."""
    values: dict[str, int] = {}
    if concurrent is not None:
        values[KEY_CONCURRENT] = _clamp(concurrent,
                                        DEFAULT_MAX_CONCURRENT_PERSONAS)
    if per_persona is not None:
        values[KEY_TASKS] = _clamp(per_persona, DEFAULT_MAX_TASKS_PER_PERSONA)
    if not values:
        return read_limits()
    try:
        from memory import config_manager
        config_manager.save_plugin_config(NAMESPACE, values)
    except Exception as exc:
        raise LimitReached(f"não foi possível salvar os limites: {exc}") from exc
    return read_limits()


def _slug(persona: Any) -> str:
    """A chave da contagem: aceita o registro, o id ou o nome."""
    if isinstance(persona, dict):
        return str(persona.get("id") or persona.get("name") or "").strip()
    return str(persona or "").strip()


@dataclass
class PersonaSlots:
    """Contas vivas de personas pedindo e de tasks rodando."""

    limits_source: Callable[[], dict] = read_limits
    log: Callable[[str], None] = print
    _requests: dict[str, int] = field(default_factory=dict, repr=False)
    _tasks: dict[str, set] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock,
                                  repr=False)

    # ── tetos ───────────────────────────────────────────────────────────────
    def limits(self) -> dict[str, int]:
        try:
            values = self.limits_source()
        except Exception:
            values = {}
        return read_limits(values if isinstance(values, dict) else {})

    # ── pedidos (uma persona trabalhando) ───────────────────────────────────
    def active_personas(self) -> list[str]:
        with self._lock:
            return sorted(slug for slug, count in self._requests.items()
                          if count > 0)

    def can_request(self, persona: Any) -> tuple[bool, str]:
        slug = _slug(persona)
        if not slug:
            return False, "persona sem nome"
        caps = self.limits()
        with self._lock:
            if slug in self._requests and self._requests[slug] > 0:
                return True, ""
            if len(self.active_personas_locked()) >= caps[
                    "max_concurrent_personas"]:
                return False, (
                    f"no teto de {caps['max_concurrent_personas']} personas "
                    f"ao mesmo tempo")
        return True, ""

    def request(self, persona: Any) -> str:
        """Pede a senha de uma persona; levanta :class:`LimitReached` se não dá."""
        slug = _slug(persona)
        ok, why = self.can_request(slug)
        if not ok:
            raise LimitReached(f"{slug or 'persona'}: {why}")
        with self._lock:
            self._requests[slug] = self._requests.get(slug, 0) + 1
        return slug

    def release_request(self, persona: Any) -> None:
        slug = _slug(persona)
        with self._lock:
            left = self._requests.get(slug, 0) - 1
            if left > 0:
                self._requests[slug] = left
            else:
                self._requests.pop(slug, None)

    # ── tasks dentro de uma persona ─────────────────────────────────────────
    def task_count(self, persona: Any) -> int:
        slug = _slug(persona)
        with self._lock:
            return len(self._tasks.get(slug, ()))

    def can_task(self, persona: Any, task_id: Any = None) -> tuple[bool, str]:
        slug = _slug(persona)
        if not slug:
            return False, "persona sem nome"
        caps = self.limits()
        task = str(task_id if task_id is not None else _next_id(slug))
        with self._lock:
            running = self._tasks.get(slug, set())
            if task in running:
                return True, ""
            if len(running) >= caps["max_tasks_per_persona"]:
                return False, (
                    f"{slug} já tem {len(running)} das "
                    f"{caps['max_tasks_per_persona']} tasks permitidas")
        return True, ""

    def start_task(self, persona: Any, task_id: Any = None) -> str:
        """Abre uma task da persona; o teto por persona decide."""
        slug = _slug(persona)
        task = str(task_id if task_id is not None else _next_id(slug))
        ok, why = self.can_task(slug, task)
        if not ok:
            raise LimitReached(why)
        with self._lock:
            self._tasks.setdefault(slug, set()).add(task)
            self._requests[slug] = max(self._requests.get(slug, 0), 1)
        return task

    def end_task(self, persona: Any, task_id: Any) -> None:
        slug = _slug(persona)
        task = str(task_id or "")
        with self._lock:
            running = self._tasks.get(slug)
            if not running:
                return
            running.discard(task)
            if not running:
                self._tasks.pop(slug, None)

    # ── leitura ─────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        caps = self.limits()
        with self._lock:
            working = self.active_personas_locked()
            tasks = {slug: sorted(items) for slug, items in self._tasks.items()
                     if items}
        return {
            "max_concurrent_personas": caps["max_concurrent_personas"],
            "max_tasks_per_persona": caps["max_tasks_per_persona"],
            "personas_working": len(working),
            "personas": working,
            "tasks": tasks,
        }

    def reset(self) -> None:
        """Zera as contas (fim de turno anormal, e os testes)."""
        with self._lock:
            self._requests.clear()
            self._tasks.clear()

    def active_personas_locked(self) -> list[str]:
        return sorted(slug for slug, count in self._requests.items()
                      if count > 0)


_COUNTER = 0


def _next_id(slug: str) -> int:
    """Identificador de task quando o chamador não trouxe um."""
    global _COUNTER
    _COUNTER += 1
    return _COUNTER


#: As contas vivas do processo. Um segundo orquestrador não existe: quem
#: quiser saber quem está trabalhando pergunta a este objeto.
SLOTS = PersonaSlots()


def slots() -> PersonaSlots:
    return SLOTS


def request(persona: Any) -> str:
    return SLOTS.request(persona)


def release(persona: Any) -> None:
    SLOTS.release_request(persona)


def start_task(persona: Any, task_id: Any = None) -> str:
    return SLOTS.start_task(persona, task_id)


def end_task(persona: Any, task_id: Any) -> None:
    SLOTS.end_task(persona, task_id)


def working(personas: Iterable[Any] | None = None) -> list[str]:
    """As personas com pedido aberto agora (a lista serve para conferência)."""
    if personas is not None:
        return [slug for slug in (_slug(item) for item in personas)
                if slug in SLOTS.active_personas()]
    return SLOTS.active_personas()
