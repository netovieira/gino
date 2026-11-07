"""core/obras.py — o registro de obras: a vault de uso exclusivo do assistente.

Ordem do dono, literal, 07/11/2026: "cria um vault (visível pra mim) mas de uso
exclusivo seu, nesse vault, você cria uma pasta pra cada projeto que você for
trabalhar, e nessa pasta do projeto, uma pasta com o nome/título da
implementação, e dentro dela, um md de status, que é uma lista de tudo que vai
sendo adicionado nessa pasta, com data de adição, início, conclusão, um arquivo
md com o prompt que o cérebro criou pra executar aquilo, um histórico também em
md, que registrar os commits, e um testes em md, com a lista dos novos testes
adicionados naquela tarefa. E um status, um progresso, um histórico e um de
testes geral na raiz do vault."

CUMPRIMENTO LITERAL (``skills/escopo-do-pedido/SKILL.md``): os nomes dos
arquivos são os que o dono deu — ``status.md``, ``prompt.md``,
``historico.md``, ``testes.md`` na pasta da implementação e ``status.md``,
``progresso.md``, ``historico.md``, ``testes.md`` na raiz. Nada é traduzido,
nada é padronizado. A REGRA de nomes do cofre do Obsidian
(``TIPO-AAAA-MM-DD-slug.md``, ``~/Jarvis``, ``skills/vault/SKILL.md``) NÃO vale
aqui: ela é do cofre do Jarvis, e estes nomes foram dados por extenso na ordem
de 07/11/2026. O único arquivo além dos pedidos é ``README.md`` da raiz — o
mínimo para a vault se explicar sozinha.

O que é gerado e o que é escrito:
  * os quatro arquivos da RAIZ são regenerados por ``atualizar_raiz()`` a
    partir do que existe por baixo — nunca se editam na mão;
  * os arquivos da pasta da implementação são escritos por este módulo
    (``registrar_implementacao``, ``gravar_prompt``, ``registrar_commits``,
    ``registrar_testes``, ``definir_status``), e o quadro "o que vai sendo
    adicionado nesta pasta" do ``status.md`` é refeito a cada escrita com a
    hora real do disco;
  * ``definir_status(impl, "concluída")`` só passa com ``historico.md``
    contendo commit e ``testes.md`` contendo teste: a lei de
    ``skills/obras/SKILL.md`` virou portão, como o portão de nomes do cofre.

SEGURANÇA DOS DADOS: todo texto livre que entra aqui passa por ``redigir()``.
Senha, token, chave de API, credencial de e-mail, bloco PEM, blob de base64/hex
e caminho de arquivo de segredo saem como ``[retido]``. Hash de commit é a
única string longa que não é redigida — é pública e é a evidência do histórico
(validada por ``_COMMIT_RE`` antes de encostar no disco).

O caminho mora em UMA chave só (``OBRAS_DIR``, sobrescrevível pelo env
``JARVIS_OBRAS_DIR``) para poder mudar depois sem dor. O módulo é puro: sem Qt,
sem ``main.py``, sem rede, e ``obras_dir()`` recusa caminho dentro do
repositório — a vault de obras nunca entra em git nenhum.
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

BASE_DIR = Path(__file__).resolve().parent.parent

#: A única chave de config do local da vault (ordem do dono: "visível pra mim").
OBRAS_DIR = Path.home() / "Gino-Obras"
_ENV_DIR = "JARVIS_OBRAS_DIR"

README_NOME = "README.md"
NOMES_IMPLEMENTACAO = ("status.md", "prompt.md", "historico.md", "testes.md")
NOMES_RAIZ = ("status.md", "progresso.md", "historico.md", "testes.md")

#: Conjunto fechado de status. Os cinco primeiros vêm da ordem do dono de
#: 07/11/2026 alinhada à lei de escopo (``skills/escopo-do-pedido/SKILL.md``);
#: o último é o marcador que o próprio dono mandou escrever no lugar de fechar,
#: quando a conclusão não foi provada no processo vivo.
STATUS_PERMITIDOS = (
    "em aberto",
    "em execução",
    "concluída",
    "não concluída: entreguei outra coisa",
    "bloqueada: aguardando dono",
    "pendente de validacao ao vivo",
)
STATUS_CONCLUIDA = "concluída"
STATUS_PENDENTE_VIVO = "pendente de validacao ao vivo"

REDACTED = "[retido]"
SEM_MARCA = "—"
_SEM_PROMPT = "(prompt ainda não registrado)"
FMT_HORA = "%Y-%m-%d %H:%M"

_GENERADO_POR = "core/obras.py (`atualizar_raiz()`)"

# ── redação: segredo nunca entra na vault ───────────────────────────────────

#: Caminhos de arquivo de segredo deste projeto (espelho do .gitignore).
_CAMINHOS_DE_SEGREDO = (
    r"config/api_keys\.json(?:[.\w-]*)?",
    r"config/linkedin\.json",
    r"config/mail(?:/[^\s\"',;)]*)?",
    r"config/certs(?:/[^\s\"',;)]*)?",
    r"config/whatsapp_web(?:/[^\s\"',;)]*)?",
    r"[^\s\"',;)]*credentials[^\s\"',;)]*\.json",
    r"[^\s\"',;)]*client_secret[^\s\"',;)]*\.json",
    r"(?:[\w.-]+/)*\.env(?:\.[\w-]+)?",
    r"[^\s\"',;)]*token[\w.-]*\.json",
)

# (chave)(separador)(aspas)(valor). Nomes compostos com "_" entram
# (``gemini_api_key``) — o ``\b`` junto do "_" não casa, por isso o prefixo
# ``[\w.-]*`` em vez de limite de palavra.
_RE_CHAVE_VALOR = re.compile(
    # (aspas da chave)(nome da chave)(aspas + separador + aspas)(valor)
    r"(?i)([\"']?)([\w.-]*(?:api[ _-]?key|access[ _-]?key|secret[\w.-]*"
    r"|token[\w.-]*|password|passwd|pwd|senha|chave|credential[\w.-]*"
    r"|authorization)[\w.-]*)([\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"',;)}\[\]]{6,})"
)
_RE_CHAVE_FORNECEDOR = re.compile(
    r"\b(?:AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{10,}"
    r"|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|ya29\.[A-Za-z0-9_.-]{10,}"
    r"|pk_(?:live|test)_[0-9a-zA-Z]{10,}|sk_(?:live|test)_[0-9a-zA-Z]{10,})"
)
_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
_RE_PEM = re.compile(
    r"(?ms)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
)
_RE_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{10,}={0,2}")
# Blob longo (32+) com dígito: chave solta, base64 de credencial, sha obscuro.
_RE_BLOB = re.compile(
    r"\b(?=[A-Za-z0-9+/]{32,}={0,2}\b)(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{32,}={0,2}"
)
_RE_CAMINHOS = re.compile(
    r"(?<![\w./-])(?:" + "|".join(_CAMINHOS_DE_SEGREDO) + r")(?![-\w.])")

_SECRET_RE: tuple[re.Pattern, ...] = (
    _RE_PEM,
    _RE_BEARER,
    _RE_CHAVE_VALOR,
    _RE_CHAVE_FORNECEDOR,
    _RE_JWT,
    _RE_BLOB,
    _RE_CAMINHOS,
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_RE_CAMPO = re.compile(r"^- \*\*(?P<campo>[^*]+?):\*\*[ \t]*(?P<valor>.*)$")
_RE_LINHA_COMMIT = re.compile(
    r"^- \[(?P<registro>[^\]]+)\] commit `(?P<hash>[0-9a-f]{7,40})`"
    r"(?: \((?P<data>[^)]*)\))?(?: — (?P<msg>.*))?$"
)
_RE_LINHA_TESTE = re.compile(
    r"^- \[(?P<registro>[^\]]+)\] teste `(?P<nome>[^`]+)`"
    r"(?: — cobre: (?P<cobre>.*))?$"
)

_LOCK = threading.RLock()


def redigir(texto: Any) -> str:
    """Troca segredo por ``[retido]`` antes de qualquer escrita na vault.

    Ordem importa: primeiro o bloco PEM (que engole o resto), depois os pares
    chave=valor, depois os formatos conhecidos de chave e, no fim, os blobs
    longos e os caminhos de arquivo de segredo. Falso positivo é aceitável
    (aparece ``[retido]``); vazamento não.

    Hash de commit NÃO passa por aqui: é público e é a evidência do histórico.
    """
    saida = str(texto if texto is not None else "")
    for pat in _SECRET_RE:
        if pat is _RE_CHAVE_VALOR:
            saida = pat.sub(
                lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", saida)
        else:
            saida = pat.sub(REDACTED, saida)
    return saida


def contem_segredo(texto: Any) -> bool:
    """True se ainda há padrão de segredo no texto (usado como travão)."""
    alvo = str(texto if texto is not None else "")
    return any(pat.search(alvo) for pat in _SECRET_RE)


def _norm(texto: Any) -> str:
    return " ".join(str(texto if texto is not None else "").split())


def _texto_seguro(texto: Any) -> str:
    return redigir(_norm(texto))


def _agora(ts: float | None = None) -> str:
    """Data e hora reais do sistema no formato visível AAAA-MM-DD HH:MM."""
    momento = datetime.now() if ts is None else datetime.fromtimestamp(ts)
    return momento.strftime(FMT_HORA)


def _slug(texto: Any, teto: int = 48) -> str:
    """Nome de pasta legível e estável: minúsculo, sem acento, sem espaço."""
    bruto = unicodedata.normalize("NFKD", str(texto if texto is not None else ""))
    ascii_ = "".join(c for c in bruto if not unicodedata.combining(c))
    limpo = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", ascii_.lower())).strip("-")
    if not limpo:
        raise ValueError(f"nome sem matéria para pasta: {texto!r}")
    if len(limpo) > teto:
        limpo = limpo[:teto].rstrip("-")
    return limpo


# ── caminho e disco ─────────────────────────────────────────────────────────

def obras_dir() -> Path:
    """O local da vault (o env ``JARVIS_OBRAS_DIR`` vence a constante)."""
    override = os.environ.get(_ENV_DIR, "").strip()
    caminho = Path(override).expanduser() if override else Path(OBRAS_DIR).expanduser()
    return _fora_do_repo(caminho)


def _fora_do_repo(caminho: Path) -> Path:
    """A vault nunca pode morar dentro de um repositório: registro não é git."""
    try:
        alvo = Path(os.path.abspath(caminho))
        alvo.relative_to(BASE_DIR)
    except ValueError:
        return caminho
    raise ValueError(
        f"vault de obras dentro do repositório ({caminho} em {BASE_DIR}): "
        "o registro de obras não é git e nunca pode ser versionado")


def _escrever(caminho: Path, texto: str) -> None:
    final = caminho.with_suffix(caminho.suffix + ".tmp")
    with _LOCK:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        final.write_text(texto, encoding="utf-8")
        os.replace(final, caminho)


def _ler(caminho: Path) -> str:
    try:
        return caminho.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _hora_do_arquivo(caminho: Path) -> str:
    return _agora(caminho.stat().st_mtime) if caminho.exists() else SEM_MARCA


# ── arquivos da implementação ───────────────────────────────────────────────

def _cabecalho(que: str, titulo: str, projeto: str, explicacao: str) -> str:
    return (f"# {que} — {titulo}\n\n"
            f"- **projeto:** {projeto}\n"
            f"- **implementação:** {titulo}\n"
            f"- **registrado:** {_agora()}\n\n"
            f"{explicacao}\n")


def _corpo_prompt(titulo: str, projeto: str, texto: str) -> str:
    cabeca = _cabecalho(
        "prompt", titulo, projeto,
        "Prompt real que o cérebro criou para executar isto. Texto livre passa "
        f"por redação na escrita: segredo sai ``{REDACTED}``.")
    conteudo = redigir(texto).strip() or _SEM_PROMPT
    return f"{cabeca}\n---\n\n{conteudo}\n"


def _status_md(titulo: str, projeto: str, campos: dict[str, str],
               arquivos: Sequence[str]) -> str:
    linhas = [f"# status — {titulo}", "",
              f"- **projeto:** {projeto}",
              f"- **implementação:** {titulo}",
              f"- **status:** {campos.get('status') or SEM_MARCA}",
              f"- **início:** {campos.get('início') or SEM_MARCA}",
              f"- **conclusão:** {campos.get('conclusão') or SEM_MARCA}",
              f"- **descrição:** {campos.get('descrição') or SEM_MARCA}",
              f"- **registrado:** {campos.get('registrado') or _agora()}",
              "", "## O que vai sendo adicionado nesta pasta", "",
              "| arquivo | adicionado em | início | conclusão |",
              "| --- | --- | --- | --- |"]
    for nome in arquivos:
        linhas.append(
            f"| {nome} | {campos.get('adicionado@' + nome) or SEM_MARCA} "
            f"| {campos.get('início') or SEM_MARCA} "
            f"| {campos.get('conclusão') or SEM_MARCA} |")
    return "\n".join(linhas) + "\n"


def _campos_do_status(pasta: Path) -> dict[str, str]:
    """Lê o cabeçalho do status.md (as datas dos arquivos vêm do disco)."""
    campos: dict[str, str] = {}
    for linha in _ler(pasta / "status.md").splitlines():
        m = _RE_CAMPO.match(linha)
        if m:
            valor = m.group("valor").strip()
            if valor and valor != SEM_MARCA:
                campos[m.group("campo")] = valor
    return campos


def _reescrever_status(pasta: Path, titulo: str, projeto: str,
                       campos: dict[str, str]) -> None:
    """Refaz o quadro de arquivos com a hora real de cada um no disco."""
    for nome in NOMES_IMPLEMENTACAO:
        campos["adicionado@" + nome] = _hora_do_arquivo(pasta / nome)
    arquivos = [n for n in NOMES_IMPLEMENTACAO if (pasta / n).exists()]
    campos.setdefault("registrado", _agora())
    _escrever(pasta / "status.md", _status_md(titulo, projeto, campos, arquivos))


def abrir_projeto(nome: str) -> Path:
    """Cria (ou reaproveita) a pasta do projeto na vault. Retorna o caminho."""
    if _norm(nome) == "":
        raise ValueError("nome do projeto vazio")
    pasta = obras_dir() / _slug(nome)
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def registrar_implementacao(projeto: str, titulo: str, *,
                            status: str = STATUS_PENDENTE_VIVO,
                            inicio: str | None = None,
                            conclusao: str | None = None,
                            descricao: str | None = None,
                            prompt: str | None = None,
                            atualizar: bool = True) -> Path:
    """Cria a pasta da implementação com os quatro .md, cabeçalho e data."""
    if _norm(titulo) == "":
        raise ValueError("título da implementação vazio")
    alvo = validar_status(status)
    nome_projeto = _norm(projeto)
    pasta = abrir_projeto(nome_projeto) / _slug(titulo)
    pasta.mkdir(parents=True, exist_ok=True)
    inicio_real = _norm(inicio) or _agora()
    campos = {
        "status": alvo,
        "início": inicio_real,
        "conclusão": _norm(conclusao) or SEM_MARCA,
        "descrição": _texto_seguro(descricao) or SEM_MARCA,
        "registrado": _agora(),
    }
    for nome in NOMES_IMPLEMENTACAO:
        arquivo = pasta / nome
        if arquivo.exists():
            continue
        if nome == "prompt.md":
            _escrever(arquivo, _corpo_prompt(titulo, nome_projeto, prompt or ""))
        elif nome == "historico.md":
            _escrever(arquivo, _cabecalho(
                "histórico", titulo, nome_projeto,
                "Commits desta implementação, com a data do registro."))
        elif nome == "testes.md":
            _escrever(arquivo, _cabecalho(
                "testes", titulo, nome_projeto,
                "Testes novos adicionados nesta tarefa, com o que cada um cobre."))
        else:
            _escrever(arquivo, _status_md(titulo, nome_projeto, campos,
                                          NOMES_IMPLEMENTACAO))
    _reescrever_status(pasta, titulo, nome_projeto, campos)
    if atualizar:
        atualizar_raiz()
    return pasta


def listar_implementacoes() -> list[Path]:
    """Toda implementação da vault, ordenada por projeto e por título."""
    raiz = obras_dir()
    if not raiz.is_dir():
        return []
    saida: list[Path] = []
    for projeto in sorted(p for p in raiz.iterdir()
                          if p.is_dir() and not p.name.startswith(".")):
        for pasta in sorted(p for p in projeto.iterdir()
                            if p.is_dir() and (pasta_ok(p))):
            saida.append(pasta)
    return saida


def pasta_ok(p: Path) -> bool:
    return (p / "status.md").exists()


def _implementacao(ref: Any) -> Path:
    """Resolve a referência em pasta de implementação.

    Aceita ``Path`` (absoluto ou relativo à vault), ``"projeto/título"`` ou o
    título sozinho, quando único na vault.
    """
    if isinstance(ref, Path):
        pasta = ref if ref.is_absolute() else obras_dir() / ref
        if pasta_ok(pasta):
            return pasta
        raise ValueError(f"implementação sem status.md: {pasta}")
    texto = str(ref if ref is not None else "").strip()
    if not texto:
        raise ValueError("referência de implementação vazia")
    if "/" in texto:
        projeto, titulo = textpartition(texto)
        pasta = obras_dir() / _slug(projeto) / _slug(titulo)
        if pasta_ok(pasta):
            return pasta
        raise ValueError(f"implementação não registrada: {texto}")
    achadas = [p for p in listar_implementacoes() if p.name == _slug(texto)]
    if len(achadas) == 1:
        return achadas[0]
    if not achadas:
        raise ValueError(f"implementação não encontrada na vault: {texto}")
    raise ValueError(
        f"título ambíguo ({texto}); passe 'projeto/{_slug(texto)}' — projetos: "
        + ", ".join(p.parent.name for p in achadas))


def textpartition(texto: str) -> tuple[str, str]:
    projeto, _, titulo = texto.partition("/")
    return projeto.strip(), titulo.strip()


def validar_status(status: str) -> str:
    """Status fora do conjunto fechado é rejeitado, com a lista na mensagem."""
    alvo = _norm(status)
    if alvo not in STATUS_PERMITIDOS:
        raise ValueError(
            f"status '{alvo}' não existe; o conjunto fechado é: "
            + ", ".join(STATUS_PERMITIDOS))
    return alvo


def gravar_prompt(implementacao: Any, texto_do_prompt: str) -> Path:
    """Escreve o prompt real em prompt.md, redigido e por extenso."""
    pasta = _implementacao(implementacao)
    campos = _campos_do_status(pasta)
    titulo = campos.get("implementação", pasta.name)
    projeto = campos.get("projeto", pasta.parent.name)
    _escrever(pasta / "prompt.md", _corpo_prompt(titulo, projeto, texto_do_prompt))
    _reescrever_status(pasta, titulo, projeto, campos)
    atualizar_raiz()
    return pasta / "prompt.md"


def registrar_commits(implementacao: Any, commits: Iterable[Any]) -> Path:
    """Acrescenta commits ao histórico, com a data do registro.

    Aceita hash solo (``"4de3a5f"``), ``(hash, mensagem)`` ou ``(hash, msg,
    data do commit)``, e o dict correspondente (``hash``/``msg``/``data``).
    Hash repetido não entra duas vezes.
    """
    pasta = _implementacao(implementacao)
    vistos = _commits_existentes(pasta)
    linhas: list[str] = []
    for item in commits:
        if isinstance(item, dict):
            hash_ = _norm(item.get("hash") or item.get("commit"))
            msg = item.get("msg") or item.get("mensagem") or ""
            data = _norm(item.get("data") or item.get("quando"))
        elif isinstance(item, (tuple, list)):
            hash_ = _norm(item[0]) if item else ""
            msg = item[1] if len(item) > 1 else ""
            data = _norm(item[2]) if len(item) > 2 else ""
        else:
            hash_, msg, data = _norm(item), "", ""
        hash_ = hash_.lower()
        if not _COMMIT_RE.match(hash_):
            raise ValueError(
                f"commit inválido {item!r}: hash hex de 7 a 40 caracteres, "
                "conferido em `git log`, nunca inventado")
        if hash_ in vistos:
            continue
        vistos.add(hash_)
        carimbo = f" ({_texto_seguro(data)})" if data else ""
        mensagem = _texto_seguro(msg)
        linhas.append(f"- [{_agora()}] commit `{hash_}`{carimbo}"
                      f"{' — ' + mensagem if mensagem else ''}")
    if linhas:
        _acrescentar(pasta, "historico.md", "histórico",
                     "Commits desta implementação, com a data do registro.", linhas)
        atualizar_raiz()
    return pasta / "historico.md"


def _commits_existentes(pasta: Path) -> set[str]:
    return {m.group("hash") for m in
            (_RE_LINHA_COMMIT.match(l) for l in _ler(pasta / "historico.md").splitlines())
            if m}


def registrar_testes(implementacao: Any, testes: Iterable[Any]) -> Path:
    """Acrescenta os testes novos, com o que cada um cobre em uma linha."""
    pasta = _implementacao(implementacao)
    vistos = _testes_existentes(pasta)
    linhas: list[str] = []
    for item in testes:
        if isinstance(item, dict):
            nome = _norm(item.get("nome") or item.get("teste"))
            cobre = item.get("cobre") or item.get("descricao") or ""
        elif isinstance(item, (tuple, list)):
            nome = _norm(item[0]) if item else ""
            cobre = item[1] if len(item) > 1 else ""
        else:
            nome, cobre = _norm(item), ""
        nome_seguro = _texto_seguro(nome)
        if not nome_seguro or nome_seguro == REDACTED:
            raise ValueError(f"teste sem nome utilizável: {item!r}")
        if nome_seguro in vistos:
            continue
        vistos.add(nome_seguro)
        cobre_seguro = _texto_seguro(cobre)
        linhas.append(f"- [{_agora()}] teste `{nome_seguro}`"
                      f"{' — cobre: ' + cobre_seguro if cobre_seguro else ''}")
    if linhas:
        _acrescentar(pasta, "testes.md", "testes",
                     "Testes novos adicionados nesta tarefa, com o que cada um cobre.",
                     linhas)
        atualizar_raiz()
    return pasta / "testes.md"


def _testes_existentes(pasta: Path) -> set[str]:
    return {m.group("nome") for m in
            (_RE_LINHA_TESTE.match(l) for l in _ler(pasta / "testes.md").splitlines())
            if m}


def _acrescentar(pasta: Path, nome: str, que: str, explicacao: str,
                 linhas: Sequence[str]) -> None:
    """Anexa linhas datadas sob o cabeçalho, sem reescrever o que já está."""
    campos = _campos_do_status(pasta)
    titulo = campos.get("implementação", pasta.name)
    projeto = campos.get("projeto", pasta.parent.name)
    atual = _ler(pasta / nome)
    base = (atual.rstrip("\n") if atual else
            _cabecalho(que, titulo, projeto, explicacao).rstrip("\n"))
    _escrever(pasta / nome, base + "\n" + "\n".join(linhas) + "\n")
    _reescrever_status(pasta, titulo, projeto, campos)


def definir_status(implementacao: Any, status: str, inicio: str | None = None,
                   conclusao: str | None = None) -> Path:
    """Muda o status. "concluída" exige commit no histórico e teste registrado."""
    pasta = _implementacao(implementacao)
    alvo = validar_status(status)
    if alvo == STATUS_CONCLUIDA:
        faltam = []
        if not _commits_existentes(pasta):
            faltam.append("historico.md sem commit")
        if not _testes_existentes(pasta):
            faltam.append("testes.md sem teste")
        if faltam:
            raise ValueError(
                "não dá para marcar como concluída: " + "; ".join(faltam)
                + " — skills/obras/SKILL.md")
    campos = _campos_do_status(pasta)
    campos["status"] = alvo
    if inicio is not None:
        campos["início"] = _norm(inicio) or SEM_MARCA
    if conclusao is not None:
        campos["conclusão"] = _norm(conclusao) or SEM_MARCA
    elif alvo == STATUS_CONCLUIDA:
        campos["conclusão"] = _agora()
    elif alvo != STATUS_CONCLUIDA and campos.get("conclusão"):
        campos["conclusão"] = SEM_MARCA
    _reescrever_status(pasta, campos.get("implementação", pasta.name),
                       campos.get("projeto", pasta.parent.name), campos)
    atualizar_raiz()
    return pasta / "status.md"


# ── a raiz: gerada a partir do que existe por baixo ─────────────────────────

def _le_commits(pasta: Path) -> list[dict]:
    saida = []
    for linha in _ler(pasta / "historico.md").splitlines():
        m = _RE_LINHA_COMMIT.match(linha)
        if m:
            d = dict(m.groupdict())
            d.update(_projeto_e_titulo(pasta))
            saida.append(d)
    return saida


def _le_testes(pasta: Path) -> list[dict]:
    saida = []
    for linha in _ler(pasta / "testes.md").splitlines():
        m = _RE_LINHA_TESTE.match(linha)
        if m:
            d = dict(m.groupdict())
            d.update(_projeto_e_titulo(pasta))
            saida.append(d)
    return saida


def _projeto_e_titulo(pasta: Path) -> dict[str, str]:
    campos = _campos_do_status(pasta)
    return {"projeto": campos.get("projeto", pasta.parent.name),
            "projeto_slug": pasta.parent.name,
            "titulo": campos.get("implementação", pasta.name),
            "slug": pasta.name}


def _marca_gerada(agora: str) -> str:
    return (f"Gerado por {_GENERADO_POR} em {agora}.\n\n"
            "**Não edite este arquivo na mão**: ele é espelho das pastas e é "
            "refeito inteiro na próxima escrita. Quem editar aqui perde o "
            "texto na regeneração — a fonte da verdade são os arquivos de "
            "cada implementação.\n")


def _gerar_status_raiz(implementacoes: Sequence[Path], agora: str) -> str:
    por_projeto: dict[str, list[Path]] = {}
    for pasta in implementacoes:
        por_projeto.setdefault(pasta.parent.name, []).append(pasta)
    linhas = ["# status geral — obras do Gino", "", _marca_gerada(agora), "",
              f"- **projetos:** {len(por_projeto)}",
              f"- **implementações:** {len(implementacoes)}", "",
              "## Por projeto"]
    for projeto, pastas in sorted(por_projeto.items()):
        linhas += ["", f"### {projeto}", ""]
        for pasta in pastas:
            c = _campos_do_status(pasta)
            linhas.append(
                f"- `{pasta.name}` — {c.get('implementação', pasta.name)} | "
                f"status: {c.get('status', SEM_MARCA)} | "
                f"início: {c.get('início', SEM_MARCA)} | "
                f"conclusão: {c.get('conclusão', SEM_MARCA)} | "
                f"commits: {len(_le_commits(pasta))} | "
                f"testes: {len(_le_testes(pasta))}")
    return "\n".join(linhas) + "\n"


def _gerar_progresso_raiz(implementacoes: Sequence[Path], agora: str) -> str:
    total = len(implementacoes)
    contagem: dict[str, int] = {}
    for pasta in implementacoes:
        alvo = _campos_do_status(pasta).get("status", SEM_MARCA)
        contagem[alvo] = contagem.get(alvo, 0) + 1
    feitos = contagem.get(STATUS_CONCLUIDA, 0)
    pacto = round(100 * feitos / total) if total else 0
    linhas = ["# progresso geral — obras do Gino", "", _marca_gerada(agora), "",
              f"- **implementações:** {total}",
              f"- **concluída:** {feitos} ({pacto}%)", "",
              "## Por status", ""]
    for status in STATUS_PERMITIDOS:
        if contagem.get(status):
            linhas.append(f"- {status}: {contagem[status]}")
    for extra in sorted(set(contagem) - set(STATUS_PERMITIDOS)):
        linhas.append(f"- {extra}: {contagem[extra]}")
    pendentes = [p for p in implementacoes
                 if _campos_do_status(p).get("status") != STATUS_CONCLUIDA]
    linhas += ["", "## Andamento — o que ainda não fechou", ""]
    if not pendentes:
        linhas.append("- nada pendente")
    for pasta in pendentes:
        c = _campos_do_status(pasta)
        linhas.append(
            f"- {c.get('projeto', pasta.parent.name)}/{c.get('implementação', pasta.name)}"
            f" — {c.get('status', SEM_MARCA)} (início {c.get('início', SEM_MARCA)})")
    commits = sum(len(_le_commits(p)) for p in implementacoes)
    testes = sum(len(_le_testes(p)) for p in implementacoes)
    com_prompt = sum(1 for p in implementacoes
                     if _SEM_PROMPT not in _ler(p / "prompt.md"))
    linhas += ["", "## Matéria registrada", "",
               f"- commits no histórico: {commits}",
               f"- testes listados: {testes}",
               f"- prompts com conteúdo: {com_prompt} de {total}"]
    return "\n".join(linhas) + "\n"


def _gerar_historico_raiz(implementacoes: Sequence[Path], agora: str) -> str:
    linhas = ["# histórico geral — obras do Gino", "", _marca_gerada(agora), "",
              "Todo commit registrado em cada implementação, com a data do "
              "registro e a data do próprio commit quando informada.", ""]
    vistos: set[tuple[str, str]] = set()
    registros: list[tuple[str, str]] = []
    for pasta in implementacoes:
        for d in _le_commits(pasta):
            chave = (d["hash"], d["slug"])
            if chave in vistos:
                continue
            vistos.add(chave)
            carimbo = f" ({d['data']})" if d.get("data") else ""
            msg = f" — {d['msg']}" if d.get("msg") else ""
            registros.append(
                (d["registro"],
                 f"- [{d['registro']}] {d['projeto']}/{d['slug']} — commit "
                 f"`{d['hash']}`{carimbo}{msg}"))
    for _, linha in sorted(registros, key=lambda item: (item[0], item[1])):
        linhas.append(linha)
    if len(registros) == 0:
        linhas.append("- nenhum commit registrado")
    return "\n".join(linhas) + "\n"


def _gerar_testes_raiz(implementacoes: Sequence[Path], agora: str) -> str:
    linhas = ["# testes gerais — obras do Gino", "", _marca_gerada(agora), "",
              "Cada teste novo registrado na sua implementação, com o que cobre.", ""]
    total = 0
    for pasta in implementacoes:
        achados = _le_testes(pasta)
        if not achados:
            continue
        total += len(achados)
        c = _campos_do_status(pasta)
        linhas += ["", f"## {c.get('projeto', pasta.parent.name)} / "
                       f"{c.get('implementação', pasta.name)}", ""]
        for d in achados:
            cobre = f" — cobre: {d['cobre']}" if d.get("cobre") else ""
            linhas.append(f"- [{d['registro']}] `{d['nome']}`{cobre}")
    if total == 0:
        linhas.append("- nenhum teste registrado")
    return "\n".join(linhas) + "\n"


_LEI_DO_REGISTRO = """# Obras — a vault de registro do assistente

Ordem do dono, 07/11/2026: um vault **visível para ele** e **de uso exclusivo
do assistente**, com uma pasta por projeto, uma pasta por implementação e,
dentro dela, um md de status, um md com o prompt que o cérebro criou, um md de
histórico (commits) e um md de testes. Na raiz, um status, um progresso, um
histórico e um testes gerais.

## Estrutura (os nomes são os do dono, não traduzidos)

```
<Gino-Obras>/
├── README.md              (este arquivo)
├── status.md              (geral — gerado)
├── progresso.md           (geral — gerado)
├── historico.md           (geral — gerado)
├── testes.md              (geral — gerado)
└── <projeto>/<implementação>/
    ├── status.md          (o que vai sendo adicionado, com data de adição,
                            início e conclusão)
    ├── prompt.md          (o prompt real que gerou a execução)
    ├── historico.md       (os commits, com data)
    └── testes.md          (os testes novos, com o que cada um cobre)
```

Os nomes vieram por extenso na ordem do dono. A REGRA `TIPO-AAAA-MM-DD-slug.md`
do cofre do Obsidian (`~/Jarvis`) **não** se aplica a esta vault: ela vale para
o cofre do Jarvis, e aqui o dono deu os nomes um por um.

## As duas obrigações que fazem o registro existir

1. **Toda implementação nova abre pasta aqui**, com os quatro arquivos, e os
   mantém durante o trabalho. A lei está em `skills/obras/SKILL.md` (repositório
   `~/jarvis`).
2. **Concluída só se `historico.md` tiver commit e `testes.md` tiver teste.** O
   portão é código: `core.obras.definir_status` levanta `ValueError` sem a
   evidência. Conclusão não provada no processo vivo não é concluída: é
   `pendente de validacao ao vivo`.

## Os quatro arquivos da raiz são gerados

`status.md`, `progresso.md`, `historico.md` e `testes.md` da raiz saem de
`core.obras.atualizar_raiz()`, que lê o que existe por baixo. **Nunca edite a
mão**: a próxima escrita reescreve por cima. Quem precisa de uma nota solta
escreve no cofre do Jarvis (`~/Jarvis`), não aqui.

## Redação de segredo

Registro é escrito por máquina a partir de prompts e commits reais. Nenhum
arquivo daqui leva senha, token, chave de API, credencial de e-mail, bloco
PEM, blob de base64 ou caminho de arquivo de segredo: `core.obras.redigir()`
troca tudo por `[retido]` antes de encostar no disco. Hash de commit é a única
string longa que passa — é público e é a evidência.

## Onde mora e como muda de lugar

Uma chave só: `core/obras.py` → `OBRAS_DIR` (hoje `~/Gino-Obras`), com o env
`JARVIS_OBRAS_DIR` por cima para teste. O módulo recusa caminho dentro do
repositório: esta vault **não é git** e nunca é versionada.
"""


def _garantir_readme(raiz: Path) -> None:
    """README da raiz só quando falta: a vault se explica sozinha ao dono.

    Não é regenerado em cima de um README que o dono escreveu à mão.
    """
    alvo = raiz / README_NOME
    if not alvo.exists():
        alvo.write_text(_LEI_DO_REGISTRO, encoding="utf-8")


def atualizar_raiz() -> dict[str, int]:
    """Regenera os quatro .md gerais a partir do que existe por baixo.

    A raiz nunca é lida para se autogerar: ela é espelho das pastas. Retorna
    as contagens (projetos, implementacoes, commits, testes).
    """
    raiz = obras_dir()
    raiz.mkdir(parents=True, exist_ok=True)
    implementacoes = listar_implementacoes()
    agora = _agora()
    _escrever(raiz / "status.md", _gerar_status_raiz(implementacoes, agora))
    _escrever(raiz / "progresso.md", _gerar_progresso_raiz(implementacoes, agora))
    _escrever(raiz / "historico.md", _gerar_historico_raiz(implementacoes, agora))
    _escrever(raiz / "testes.md", _gerar_testes_raiz(implementacoes, agora))
    _garantir_readme(raiz)
    return {
        "projetos": len({p.parent.name for p in implementacoes}),
        "implementacoes": len(implementacoes),
        "commits": sum(len(_le_commits(p)) for p in implementacoes),
        "testes": sum(len(_le_testes(p)) for p in implementacoes),
    }
