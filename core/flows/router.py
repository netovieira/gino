"""Roteador determinístico (<5 ms) e fallback remoto declarado.

Portado da PoC (``/tmp/gino-poc-arquitetura/poc.py``). O roteador NÃO executa
nada: devolve uma :class:`Decision` com ferramentas/consultas preparadas. Quem
despacha é o orquestrador; quem executa é o executor.

Decisões do dono embutidas:

* o fallback é SEMPRE remoto; este módulo nem oferece um classificador local;
* ``trust != "owner"`` bloqueia a rota (lei c): texto de terceiro é dado, não
  instrução.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

TRUST_OWNER = "owner"
TRUST_UNTRUSTED = "untrusted"


def norm(text: str) -> str:
    """Minúsculas, sem acento e com espaços colapsados (mesmo da PoC)."""
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", value).strip()


# ── vocabulário ─────────────────────────────────────────────────────────────

APPS = {
    "whatsapp": "WhatsApp", "spotify": "Spotify", "chrome": "Chrome",
    "vscode": "VS Code", "codigo": "VS Code", "telegram": "Telegram",
    "discord": "Discord", "steam": "Steam", "youtube": "YouTube",
    "calculadora": "Calculadora", "terminal": "Terminal", "vlc": "VLC",
    "obs": "OBS", "navegador": "navegador",
}

CANCEL_RE = re.compile(
    r"\b(melhor nao|deixa quieto|deixa pra la|esquece|esquece isso|cancela|"
    r"nao faz nada|para com isso)\b")
GREET_RE = re.compile(
    r"^(e ai|oi|ola|bom dia|boa tarde|boa noite|tudo bem|tudo bom|obrigado|"
    r"valeu)\b")
AMBIG_RE = re.compile(
    r"\b(aquele negocio|da um jeito|fulano|sei la|negocio la|aquilo la|"
    r"aquela parada)\b")
MUSIC_RE = re.compile(r"\b(musica|som|faixa|playlist|spotify|vlc|tocar|toca|play)\b")
MUSIC_NOUN_RE = re.compile(r"\b(musica|som|faixa|playlist)\b")
PLAY_VERB_RE = re.compile(r"\b(toca|tocar|play|dale)\b")
POE_VERB_RE = re.compile(r"\b(poe|coloca|bota)\b")
OPEN_VERB_RE = re.compile(r"\b(abre|abrir|inicia|roda|executa|lanca)\b")
CONFIRM_RE = re.compile(
    r"\b(reinicia|reiniciar|desliga|desligar|apaga|apagar|envia|enviar|manda|"
    r"mandar|desinstala)\b")
TIME_RE = re.compile(r"\b(que horas|horas sao|que dia|data de hoje|dia e hoje)\b")
CONTEXT_RE = re.compile(r"\b(aquela musica|o que eu pedi|qual era|ontem)\b")
TASK_RE = re.compile(
    r"\b(abre|abrir|toca|tocar|poe|coloca|bota|pausa|para|baixa|abaixa|"
    r"aumenta|sobe|muda|reinicia|desliga|envia|manda|lembra|agenda|play)\b")

# ── conversa curta na boca (frente local, cérebro só para ação/raciocínio) ───
#
# Decisão do dono (03/11/2026): o cérebro remoto leva ~16 s para a primeira
# frase; conversa curta (saudação, "como você está", bate-papo sobre o próprio
# Gino, eco do que foi dito agora) sai da boca local em ~2 s. O roteador é
# conservador por construção: a rota só fecha para a boca quando a fala CASA
# num padrão explícito de conversa E não casa em nenhum excluidor. Dúvida,
# número, ferramenta, cálculo, memória antiga ou fato do mundo vão ao cérebro
# exatamente como hoje. A boca jamais afirma resultado de ação: o guard da
# fase "brief" continua sendo o portão final da frase (lei a).
SHORT_CHAT_MAX_CHARS = 140
CHAT_GREET_RE = re.compile(
    r"\b(oi|opa|eai|e ai|eae|bom dia|boa tarde|boa noite|tudo bem|tudo bom|"
    r"tudo certo|beleza|fala ai|acorda|esta ai|vc esta ai|oi gino|gino oi)\b")
CHAT_BYE_RE = re.compile(
    r"\b(tchau|adeus|ate amanha|ate logo|ate mais|falou|boa noite|bom descanso)\b")
CHAT_THANKS_RE = re.compile(r"\b(obrigado|obrigada|valeu|brigado|parabens)\b")
CHAT_YOU_RE = re.compile(
    r"\b(como (voce|vc|si) esta|como voce ta|como esta voce|voce esta bem|"
    r"voce ta bem|tudo bem com (voce|vc)|como foi seu dia|como esta seu dia|"
    r"quem (e|eh) voce|o que (voce|vc) (e|eh)|voce (e|eh) (um|uma) (robo|"
    r"programa|ia|inteligencia)|voce e real|voce e vivo|voce esta feliz|"
    r"voce esta triste|voce esta cansado|voce dorme|voce sonha|voce me ama|"
    r"voce gosta de mim|quem te criou|qual (e|eh) seu nome|o que voce faz|"
    r"voce trabalha|como voce se sente|o que voce acha de voce)\b")
CHAT_ECHO_RE = re.compile(
    r"\b(repete|repita|pode repetir|o que eu (falei|disse|pedi agora)|"
    r"o que voce (disse|falou|acabou de falar)|o que foi dito|"
    r"como assim|que historia e essa|"
    r"resume o que (eu )?(falei|disse|voce disse)|do que a gente estava falando)\b")
# Desabafo contra o próprio Gino e vocativo puro: evidência do activity log
# de 05/11/2026 (43 turnos ``type=turn``): xingamento sem verbo de ação,
# "voce ta quebrando" e "Gino." sozinho eram conversa pura e mesmo assim
# o veredito era 0 rota-boca. Estes padrões fecham só esses casos — verbo
# de ação, confirmação, número e excluidores continuam no cérebro (a
# checagem deles vem antes, na mesma ordem conservadora de sempre).
CHAT_VENT_RE = re.compile(
    r"\b(vai se foder|vai tomar no cu|caralho|porra|merda|filho da puta|"
    r"otario|otaria|imbecil|idiota|burro|burra|mentiroso|mentirosa|"
    r"mentira|inutil|lerdo|sem nocao|sem vergonha)\b")
CHAT_FAULT_RE = re.compile(
    r"\bvoce (ta|esta|ficou|anda) (muito |meio |)?(quebrando|estragando|"
    r"atrapalhando|atrasado|parado|perdido|ignorando|mentindo|errando)\b")
CHAT_LOCAL_RES = (CHAT_GREET_RE, CHAT_BYE_RE, CHAT_THANKS_RE, CHAT_YOU_RE,
                  CHAT_ECHO_RE)
# Excluidores: qualquer um destes manda o turno para o cérebro, sem discussão.
CHAT_EXCLUDE_RE = re.compile(
    r"\b(quando|onde|por que|porque|como (faco|consigo|funciona|abro|ponho)|"
    r"o que (e|eh|sao)\s|quem (e|eh)\s+(?!voce)|quanto|custa|calcul|soma|"
    r"subtra|multiplica|divid|media |por cento|clima|tempo (hoje|amanha)|"
    r"preco|cotacao|noticia|jogo|placar|google|busca|pesquis|traduz|site|url|"
    r"link|arquivo|pasta|codigo|programa|instala|atualiza|configura|abre o|"
    r"lembrete|lembra|memoria|ontem|anteontem|semana passada|mes passado|"
    r"ano passado|email|e-mail|whatsapp|spotify|youtube|chrome|terminal|"
    r"volume|musica|som\b|film|filme|confirma\w*)\b")


def short_chat_reason(text: str, decision: "Decision | None" = None,
                      *, name: str = "gino") -> str:
    """Motivo da fala sair da boca como RESPOSTA SUPERFICIAL imediata, ou ''.

    Desde 05/11/2026 (fluxo do dono, passos 2/3) fechar esta rota NÃO desliga
    o cérebro: a frase da boca vira o ack de menos de 1 s e o raciocínio roda
    junto. Conservador por construção: só fecham rota as falas que casam num
    padrão explícito de conversa pessoal E não casam em nenhum excluidor. Sem
    decisão de regra (fallback, ferramenta, consulta, cancelamento, bloqueio)
    o turno vai ao raciocínio como hoje.
    """
    n = norm(text)
    if not n or len(n) > SHORT_CHAT_MAX_CHARS:
        return ""
    if re.search(r"\d", n):
        return ""
    addr = norm(name)
    if addr and re.fullmatch(
            r"(?:{})(?:[\s.,!?;:]+{})*[\s.,!?;:]*".format(
                re.escape(addr), re.escape(addr)), n):
        # Vocativo puro ("Gino.") — chamar sem assunto é conversa.
        return "chamou"
    if decision is not None:
        if (decision.actions or decision.lookups or decision.cancel
                or decision.blocked or decision.needs_fallback):
            return ""
        if decision.rule not in ("saudacao", "livre"):
            return ""
        if decision.rule == "saudacao" and not CHAT_EXCLUDE_RE.search(n):
            return "saudacao"
    else:
        if not GREET_RE.search(n):
            return ""
    if any(r.search(n) for r in (TASK_RE, CONFIRM_RE, MUSIC_RE, TIME_RE,
                                 CONTEXT_RE, AMBIG_RE)):
        return ""
    if CHAT_EXCLUDE_RE.search(n):
        return ""
    if CHAT_YOU_RE.search(n):
        return "bate-papo"
    if CHAT_ECHO_RE.search(n):
        return "eco-recente"
    if CHAT_BYE_RE.search(n):
        return "despedida"
    if CHAT_THANKS_RE.search(n):
        return "agradecimento"
    if CHAT_VENT_RE.search(n) or CHAT_FAULT_RE.search(n):
        return "desabafo"
    if CHAT_GREET_RE.search(n):
        return "saudacao"
    return ""


@dataclass
class Action:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    requires_confirm: bool = False
    kind: str = "tool"
    label: str = ""

    def sig(self) -> str:
        return self.tool + ":" + json.dumps(
            self.args, sort_keys=True, ensure_ascii=False)


@dataclass
class Lookup:
    kind: str
    args: dict[str, Any] = field(default_factory=dict)

    def sig(self) -> str:
        return "lookup:" + self.kind + ":" + json.dumps(self.args, sort_keys=True)


@dataclass
class Decision:
    intent: str = "unknown"
    rule: str = ""
    confidence: float = 0.0
    actions: list[Action] = field(default_factory=list)
    lookups: list[Lookup] = field(default_factory=list)
    cancel: bool = False
    needs_fallback: bool = False
    brief_label: str = ""
    target: str = ""
    draft: str = ""
    blocked: bool = False
    trust: str = TRUST_OWNER

    @property
    def route(self) -> str:
        return self.rule or ("fallback" if self.needs_fallback else "desconhecida")


class Router:
    """Decisão determinística; o fallback só é aplicado pelo orquestrador."""

    def __init__(self) -> None:
        self.rule_decisions = 0
        self.fallback_decisions = 0
        self.fallback_calls = 0
        self.fallback_last: dict[str, Any] = {}

    def route(self, text: str, trust: str = TRUST_OWNER) -> Decision:
        if str(trust or TRUST_OWNER) != TRUST_OWNER:
            # Lei (c): evento de monitor nunca vira pedido.
            return Decision(intent="monitor_proposal", rule="untrusted",
                            confidence=1.0, blocked=True, trust=str(trust))
        return self.parse(text)

    def parse(self, text: str) -> Decision:
        n = norm(text)
        d = Decision()
        if not n:
            d.needs_fallback = True
            d.rule = "vazio"
            return d

        if CANCEL_RE.search(n):
            d.intent, d.rule, d.confidence, d.cancel = (
                "cancel", "cancelamento", 0.96, True)
            d.brief_label = "cancelar"
            d.target = "nada"
            d.draft = "Deixa quieto, então."
            self.rule_decisions += 1
            return d

        if (GREET_RE.search(n) and not CONFIRM_RE.search(n)
                and not TASK_RE.search(n)):
            d.intent, d.rule, d.confidence = "smalltalk", "saudacao", 0.93
            d.brief_label = "conversa"
            d.target = "o dono"
            d.draft = "Tudo em ordem por aqui, criador."
            self.rule_decisions += 1
            return d

        clauses = [c.strip() for c in re.split(r"\s+e\s+|,\s*", n) if c.strip()]
        if not clauses:
            clauses = [n]

        if CONFIRM_RE.search(n):
            d.intent, d.rule, d.confidence = "confirm_required", "irreversivel", 0.97
            d.actions.append(Action(
                "restart_assistant", {"action": "assistant"},
                requires_confirm=True, label="reiniciar o assistente"))
            d.brief_label = "pedir confirmação para reiniciar o assistente"
            d.target = "o assistente"
            d.draft = "Reiniciar é irreversível: não faço isso sem a tua voz."
            self.rule_decisions += 1
            return d

        for clause in clauses:
            action = self._parse_clause(clause, text)
            if action and action.sig() not in {a.sig() for a in d.actions}:
                d.actions.append(action)

        if not d.actions:
            if TIME_RE.search(n):
                d.lookups.append(Lookup("time"))
                d.rule, d.intent, d.confidence = "hora", "lookup_time", 0.94
            elif CONTEXT_RE.search(n) and not AMBIG_RE.search(n):
                # UC-1: a consulta era feita com a palavra fixa "musica",
                # qualquer que fosse a pergunta — por isso o índice devolvia
                # lixo e o cérebro parava de chamar. A busca agora pede o que
                # o dono realmente disse.
                d.lookups.append(Lookup("memory", {"query": n[:160]}))
                d.rule, d.intent, d.confidence = "contexto", "lookup_memory", 0.80
            elif AMBIG_RE.search(n):
                d.needs_fallback = True
                d.rule = "ambiguo_sem_regra"
            else:
                # Rota livre: fala solta do dono (conversa, opinião, assunto
                # novo). Não é ferramenta nem ambiguidade: a boca local fala o
                # brief com o contexto e o cérebro responde em seguida. O
                # fallback remoto continua existindo só para o caso ambíguo.
                d.intent, d.rule, d.confidence = (
                    "conversa", "livre", 0.55)
                d.brief_label = "conversar"
                d.target = "o dono"
                d.draft = "Certo, deixa eu te responder, criador."

        if d.actions or d.lookups:
            self.rule_decisions += 1
            if not d.lookups and any(
                    a.tool == "media_control" and a.args.get("action") == "play"
                    for a in d.actions):
                d.lookups.append(Lookup("system"))
            if not d.rule:
                short = {"media_control": "musica", "open_app": "app",
                         "reminder": "lembrete", "restart_assistant": "confirma"}
                d.rule = (short.get(d.actions[0].tool, "acao")
                          if d.actions else "consulta")
            if not d.intent or d.intent == "unknown":
                d.intent = "tool" if d.actions else "lookup"
            first = d.actions[0] if d.actions else None
            if first:
                d.brief_label = first.label or "executar o pedido"
                d.target = self._target_of(first)
                d.draft = self._draft_for(first)
            else:
                kind = d.lookups[0].kind
                d.brief_label = "consultar" if kind == "time" else "procurar no histórico"
                d.target = "a hora" if kind == "time" else "o pedido de ontem"
                d.draft = ("Conferindo a hora."
                           if kind == "time"
                           else "Deixa eu procurar o que você pediu.")
        return d

    def _parse_clause(self, clause: str, full: str) -> Action | None:
        app = next((APPS[k] for k in APPS
                    if re.search(rf"\b{k}\b", clause)), "")
        if CONFIRM_RE.search(clause):
            return Action("restart_assistant", {"action": "assistant"},
                          requires_confirm=True,
                          label="reiniciar o assistente")
        if re.search(r"\b(me lembra|lembrar|lembrete|agenda|agendar)\b", clause):
            date_iso, time_hm, when_label = self._reminder_when(clause)
            target = self._reminder_target(clause)
            return Action("reminder",
                          {"message": target, "date": date_iso, "time": time_hm},
                          label=f"anotar o lembrete para {when_label}")
        if (re.search(r"\b(abaixa|baixa|diminui|reduz)\b", clause)
                and re.search(r"\b(som|volume)\b", clause)):
            return Action("media_control", {"action": "volume", "value": "down"},
                          label="abaixar o volume")
        if (re.search(r"\b(aumenta|sobe)\b", clause)
                and re.search(r"\b(som|volume)\b", clause)):
            return Action("media_control", {"action": "volume", "value": "up"},
                          label="aumentar o volume")
        if re.search(r"\b(mudo|muta|silencia)\b", clause):
            return Action("media_control", {"action": "volume", "value": "mute"},
                          label="silenciar")
        if (re.search(r"\b(pausa|pausar|para|parar)\b", clause)
                and MUSIC_RE.search(clause)):
            return Action("media_control", {"action": "pause"},
                          label="pausar a música")
        if ((PLAY_VERB_RE.search(clause)
             or (POE_VERB_RE.search(clause) and MUSIC_NOUN_RE.search(clause)))
                and MUSIC_RE.search(clause)):
            query = self._music_query(full)
            args: dict[str, Any] = {"action": "play"}
            if query:
                args["query"] = query
            return Action("media_control", args,
                          label=("tocar " + query) if query else "tocar música")
        if OPEN_VERB_RE.search(clause) and app:
            return Action("open_app", {"app_name": app},
                          label=f"abrir o {app}")
        if POE_VERB_RE.search(clause) and app and not MUSIC_NOUN_RE.search(clause):
            return Action("open_app", {"app_name": app},
                          label=f"abrir o {app}")
        return None

    @staticmethod
    def _music_query(text: str) -> str:
        n = norm(text)
        m = re.search(
            r"musica\s+(?:do|da|de)\s+([a-z0-9 ]+?)"
            r"(?:\s+(?:ai|agora|pra mim|por favor))?$", n)
        if m:
            return m.group(1).strip().title()
        m = re.search(
            r"(?:toca|tocar|play)\s+([a-z0-9 ]+?)"
            r"(?:\s+(?:ai|agora|pra mim|por favor))?$", n)
        if m and m.group(1) not in ("musica", "som"):
            return m.group(1).strip().title()
        return ""

    @staticmethod
    def _reminder_when(clause: str) -> tuple[str, str, str]:
        today = dt.date.today()
        day = today + dt.timedelta(days=1) if "amanha" in clause else today
        label = "amanhã" if "amanha" in clause else "hoje"
        m = re.search(r"\bas\s*(\d{1,2})(?::(\d{2}))?h?", clause)
        time_hm = ""
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2) or 0)
            time_hm = f"{hh:02d}:{mm:02d}"
            label += f" às {hh:02d}h" + (m.group(2) or "")
        return day.isoformat(), time_hm, label

    @staticmethod
    def _reminder_target(clause: str) -> str:
        m = re.search(
            r"(?:me lembra|lembrar|lembrete)\s+(?:de\s+)?(.+?)"
            r"(?:\s+(?:amanha|hoje|as\s*\d)|$)", clause)
        return (m.group(1).strip() if m else "o compromisso")

    @staticmethod
    def _target_of(action: Action) -> str:
        if action.tool == "media_control":
            return str(action.args.get("query") or action.args.get("action")
                       or "a música")
        return str(action.args.get("app_name") or action.args.get("message") or "")

    @staticmethod
    def _draft_for(action: Action) -> str:
        if action.tool == "media_control" and action.args.get("action") == "play":
            return f"Vou pôr {action.args.get('query') or 'música'}, criador."
        if action.tool == "media_control" and action.args.get("action") == "volume":
            return "Ajustando o volume."
        if action.tool == "media_control" and action.args.get("action") == "pause":
            return "Pausando a música."
        if action.tool == "open_app":
            return f"Abrindo o {action.args.get('app_name')}."
        if action.tool == "reminder":
            return "Anotando o lembrete."
        return "Certo, deixa comigo."

    def apply_fallback(self, decision: Decision, payload: dict[str, Any] | None,
                       *, source: str = "remote") -> Decision:
        """Aplica a classificação vinda do modelo remoto.

        O roteador só aceita ``source="remote"``. Qualquer tentativa de usar o
        7b local cai em esclarecimento, nunca em ação.
        """
        payload = payload or {}
        if str(source or "") != "remote":
            decision.intent, decision.rule = "clarify", "fallback_local_recusado"
            decision.brief_label, decision.target = "pedir esclarecimento", "?"
            decision.draft = "Certo, deixa eu entender o que você quer."
            return decision

        self.fallback_decisions += 1
        self.fallback_last = dict(payload)
        intent = str(payload.get("intent") or "desconhecida")
        try:
            confidence = float(payload.get("confianca") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        target = str(payload.get("alvo") or "").strip()

        if confidence >= 0.60 and intent == "musica":
            decision.intent, decision.rule, decision.confidence = (
                "tool", "fallback_ia", confidence)
            args: dict[str, Any] = {"action": "play"}
            if target:
                args["query"] = target
            decision.actions.append(Action(
                "media_control", args,
                label=f"tocar {target or 'música'}"))
            decision.brief_label = decision.actions[0].label
            decision.target = target or "a música"
            decision.draft = f"Vou pôr {target or 'música'}, criador."
        elif confidence >= 0.60 and intent == "hora":
            decision.intent, decision.rule, decision.confidence = (
                "lookup", "fallback_ia", confidence)
            decision.lookups.append(Lookup("time"))
            decision.brief_label, decision.target = "consultar a hora", "a hora"
            decision.draft = "Conferindo a hora."
        elif confidence >= 0.60 and intent == "irreversivel":
            decision.intent, decision.rule, decision.confidence = (
                "confirm_required", "fallback_ia", confidence)
            decision.actions.append(Action(
                "restart_assistant", {"action": "assistant"},
                requires_confirm=True, label="reiniciar o assistente"))
            decision.brief_label = "pedir confirmação para reiniciar"
            decision.target = "o assistente"
            decision.draft = "Reiniciar é irreversível: não faço sem a tua voz."
        else:
            decision.intent, decision.rule, decision.confidence = (
                "clarify", "fallback_ia_sem_confianca", confidence)
            decision.brief_label, decision.target = "pedir esclarecimento", "?"
            decision.draft = "Certo, deixa eu entender o que você quer."
        decision.needs_fallback = False
        return decision


FallbackFn = Callable[[str, int], dict[str, Any] | None]


def classify_remote(text: str, timeout_ms: int = 450) -> dict[str, Any] | None:
    """Hook do fallback remoto.

    A PoC classificou com o 7b local por falta de outra chamada; o dono vetou
    local. A integração ao vivo injeta aqui o classificador do RACIOCÍNIO
    (Qoder). Sem hook, devolve ``None`` e o orquestrador pede esclarecimento —
    nunca cai em ação local.
    """
    return None
