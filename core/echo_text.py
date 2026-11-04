"""Turno fantasma: a transcrição é a nossa própria fala voltando pelo microfone.

Por que existe
--------------
O cancelador de eco do PipeWire nem sempre dá conta. Quando ele falha (o caso
real de 03/11/2026: captura presa em 32 kHz contra um filtro de 48 kHz, 250 mil
erros acumulados), o TTS sai pelo alto-falante, entra pelo microfone e o
transcritor o devolve como se fosse o dono. O assistente então responde a uma
frase que ele mesmo falou — ou pior, reclama que "não conseguiu entender" e
entra em laço, porque a reclamação vira eco e gera a próxima.

O que este módulo faz
---------------------
Guarda as últimas falas realmente audíveis do assistente (texto + instante, com
estimativa de duração) e responde a duas perguntas baratas:

* ``matches(texto)`` — este texto transcrito é, claramente, uma das minhas
  falas recentes (inclusive com palavra corrompida pelo transcritor, como
  "roteador" virando "vanteador")?
* ``spoke_during(início, fim)`` — eu estava falando (ou acabara de falar)
  enquanto este segmento de microfone era capturado? É o caso em que o
  transcritor volta vazio porque o VAD interno descarta o eco.
* ``quoted_repeat(texto)`` — a fala é, quase à letra, um trecho que eu CITEI
  para o dono repetir ("Diga 'cadastra minha voz'")? Isso não é eco: o eco
  devolve a frase inteira, com o "diga" e o resto.

Não usa modelo, não usa numpy e não usa rede: normaliza texto, compara tokens e
4-gramas de caracteres, e no pior caso chama ``difflib`` em frases curtas. A
conta fica abaixo de 5 ms para oito falas guardadas (ver tests/test_echo_text).
"""

from __future__ import annotations

import re
import unicodedata

# Depois disto a fala guardada já não pode explicar um eco: a cauda acústica
# morre em segundos, não em minutos.
_JANELA_S = 45.0

# Restos de eco aparecem por alguns segundos depois do fim do áudio: o buffer
# do alto-falante, o monitor e a própria sala somam isso.
_CAUDA_S = 4.0

_MIN_CHARS = 12        # menos que isso não é frase: pode ser "sim", "pode"
_MIN_TOKENS = 2
_GUARDADAS = 8
_CHARS_POR_S = 13.0    # ritmo aproximado do TTS em português
_DURACAO_MAX = 25.0

# Primeira frase que quase ninguém fala e que aparece em 90% dos falsos
# positivos quando o dono responde logo depois ("sim", "não", "pode", "certo",
# "beleza"): não são eco e nunca podem ser engolidas.
_STOP = frozenset("""
a o as os um uma uns umas de do da dos das em no na nos nas por para pra pro
com sem que e ou mas se eu tu voce vc ele ela nos eles elas me te lhe nos
ja nao sim pode certo beleza ok ta esta estou foi ser seu sua meu minha isso
este esta esse essa aqui ali la entao como quando porque pois tambem muito
""".split())

_SENT_RE = re.compile(r"[.!?;:—\n]+")
_NAO_ALFA = re.compile(r"[^\w\s]", re.UNICODE)
_ESPACOS = re.compile(r"\s+")

# Trechos que o assistente CITOU para o dono repetir. O texto guardado é o
# semântico (o que foi mandado ao TTS), então as aspas chegam inteiras mesmo
# quando o sintetizador não as pronuncia.
_ASPAS = re.compile(r"[“”\"«»‘’']([^“”\"«»‘’'\n]{2,120})[“”\"«»‘’']")

# Uma citação é uma instrução curta, não um parágrafo: acima disto o trecho é
# conteúdo citado (título, termo técnico) e repeti-lo não autoriza nada.
_CITACAO_MAX = 60
_CITACAO_MIN = 12
_CITACAO_RATIO = 0.90


def normalize(text: str) -> str:
    """Minúsculas, sem acento e sem pontuação — o que o eco preserva.

    O transcritor erra acento e pontuação o tempo todo; erra palavra inteira
    com menos frequência. Normalizar primeiro deixa a comparação olhar só para
    o que sobrevive a esses dois erros.
    """
    t = unicodedata.normalize("NFD", str(text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _NAO_ALFA.sub(" ", t)
    return _ESPACOS.sub(" ", t).strip()


def _tokens(texto: str) -> set[str]:
    return {w for w in texto.split() if len(w) >= 3 and w not in _STOP}


def _gramas(texto: str, n: int = 4) -> set[str]:
    if len(texto) <= n:
        return {texto} if texto else set()
    return {texto[i:i + n] for i in range(len(texto) - n + 1)}


def _dice(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def _frases(referencia: str) -> list[str]:
    return [p for p in _SENT_RE.split(str(referencia or "")) if p.strip()]


def similarity(transcricao: str, referencia: str) -> float:
    """Quanto desta transcrição já foi dita por nós — 0.0 a 1.0.

    Compara com cada frase da referência, não com o bloco inteiro: um eco pode
    ser só o pedaço que voltou pelo microfone, e uma fala longa diluiria a
    semelhança até não acusar nada.
    """
    t = normalize(transcricao)
    if len(t) < _MIN_CHARS:
        return 0.0
    tg = _gramas(t)
    tt = _tokens(t)
    melhor = 0.0
    # A linha inteira e as frases dela: um eco pode voltar como a linha toda
    # (o caso comum) ou como o pedaço que o segmentador separou.
    for frase in [referencia] + _frases(referencia):
        r = normalize(frase)
        if len(r) < 10:
            continue
        # A fala do dono é tipicamente mais longa que uma frase nossa. Se não
        # for, a semelhança vale; se for muito maior, um pedaço em comum não é
        # eco de nada.
        if len(t) > 2 * len(r) + 20:
            continue
        nota = _dice(tg, _gramas(r))
        if len(tt) >= _MIN_TOKENS:
            rt = _tokens(r)
            if rt:
                nota = max(nota, len(tt & rt) / len(tt | rt))
        # Trecho contíguo: pega o eco com uma palavra corrompida no meio, em
        # que os 4-gramas da frase inteira caem mas o resto continua igual.
        if len(t) >= 18 and len(r) >= 18:
            from difflib import SequenceMatcher
            m = SequenceMatcher(None, t, r, autojunk=False).find_longest_match(
                0, len(t), 0, len(r))
            nota = max(nota, 0.9 * m.size / len(t))
        melhor = max(melhor, nota)
    return melhor


class EchoTextGuard:
    """As últimas falas do assistente, para reconhecer o eco delas.

    Barato o suficiente para o caminho da transcrição (thread do STT) e
    tolerante a falha: qualquer exceção interna vira "não é eco", nunca um
    turno do dono perdido por causa do guarda.
    """

    LIMIAR = 0.62

    def __init__(self, janela_s: float = _JANELA_S, guardadas: int = _GUARDADAS,
                 limiar: float = LIMIAR):
        self.janela_s = float(janela_s)
        self.guardadas = int(guardadas)
        self.limiar = float(limiar)
        self._falas: list[tuple[str, float, float]] = []

    # ── o que falamos ────────────────────────────────────────────────────
    def note(self, texto: str, when: float | None = None) -> None:
        """Registra uma fala que vai para o alto-falante (texto, semântica)."""
        if not texto:
            return
        try:
            import time as _time
            agora = _time.monotonic() if when is None else float(when)
            dur = min(_DURACAO_MAX, len(str(texto)) / _CHARS_POR_S + 0.6)
            self._falas.append((str(texto), agora, agora + dur))
            del self._falas[:-self.guardadas]
        except Exception:
            pass

    def reset(self) -> None:
        self._falas.clear()

    # ── perguntas baratas ────────────────────────────────────────────────
    def matches(self, texto: str, when: float | None = None) -> bool:
        """A transcrição é claramente eco de uma fala recente nossa?"""
        try:
            if not self._falas:
                return False
            import time as _time
            agora = _time.monotonic() if when is None else float(when)
            for ref, inicio, fim in reversed(self._falas):
                if agora - fim > self.janela_s:
                    continue
                if agora < inicio - 1.0:
                    continue
                if similarity(texto, ref) >= self.limiar:
                    return True
        except Exception:
            return False
        return False

    def quoted_repeat(self, texto: str, when: float | None = None) -> bool:
        """A fala é, quase à letra, um trecho que nós citamos para repetir?

        Quando a resposta foi "diga 'cadastra minha voz'", a fala seguinte do
        dono é exatamente o que está entre aspas — e o eco de verdade não é:
        ele devolve a frase inteira, com o "diga" e o resto. Por isso a regra
        exige quase igualdade com a citação (o transcritor ainda pode errar uma
        letra) e fala curta; uma passagem longa citada nunca vira isenção.

        Não muda ``matches``: o chamador decide o que fazer com a diferença.
        """
        try:
            if not self._falas:
                return False
            t = normalize(texto)
            if len(t) < _CITACAO_MIN or len(t) > _CITACAO_MAX:
                return False
            import time as _time
            from difflib import SequenceMatcher
            agora = _time.monotonic() if when is None else float(when)
            for ref, inicio, fim in reversed(self._falas):
                if agora - fim > self.janela_s:
                    continue
                if agora < inicio - 1.0:
                    continue
                for trecho in _ASPAS.findall(str(ref)):
                    c = normalize(trecho)
                    if len(c) < _CITACAO_MIN:
                        continue
                    # Mesma extensão: um texto longo que só CONTÉM a citação
                    # não é a citação (e é justamente o eco a ser descartado).
                    if not (0.8 <= len(t) / len(c) <= 1.25):
                        continue
                    if SequenceMatcher(None, t, c, autojunk=False).ratio() \
                            >= _CITACAO_RATIO:
                        return True
        except Exception:
            return False
        return False

    def spoke_during(self, inicio: float, fim: float,
                     pad: float = _CAUDA_S) -> bool:
        """Eu estava falando (ou tinha acabado) nesta janela de captura?"""
        try:
            for _ref, a, b in self._falas:
                if b + pad >= inicio and a <= fim:
                    return True
        except Exception:
            return False
        return False

    def last_spoke_at(self) -> float:
        try:
            return self._falas[-1][2] if self._falas else 0.0
        except Exception:
            return 0.0
