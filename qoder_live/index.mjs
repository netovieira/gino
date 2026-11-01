#!/usr/bin/env node
/**
 * JARVIS Qoder Live sidecar.
 *
 * One process, one persistent Qoder daemon/session. Python writes JSON lines to
 * stdin and reads JSON lines from stdout. stderr is for logs only.
 *
 *   -> {"type":"warmup"}
 *   -> {"type":"ask","id":"...","text":"...","speaker":"Anthero"}
 *   -> {"type":"interrupt"}
 *   -> {"type":"close"}
 *
 *   <- {"type":"ready","model":"Qwen3.8-Flash","warmup_ms":1234}
 *   <- {"type":"text","id":"...","text":"...","delta":true}
 *   <- {"type":"tool","id":"...","name":"mcp__jarvis__jarvis_call","input":{}}
 *   <- {"type":"done","id":"...","text":"..."}
 *   <- {"type":"error","id":"...","error":"..."}
 *   <- {"type":"closed"}
 *
 * Environment:
 *   JARVIS_BRIDGE_URL=http://127.0.0.1:8011
 *   JARVIS_BRIDGE_TOKEN=<bearer token>
 *   JARVIS_MODEL=Qwen3.8-Flash        (free allowlist only)
 *   JARVIS_CWD=/home/anthero/jarvis
 *   JARVIS_SYSTEM_PROMPT_FILE=...      (optional rendered prompt)
 *   JARVIS_ASSISTANT_NAME=Gino
 *   JARVIS_RESUME_SESSION=<session id> (continue the same chat after a restart)
 */
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import {
  createSdkMcpServer,
  qodercliAuth,
  query,
  tool,
} from "@qoder-ai/qoder-agent-sdk";
import { z } from "zod";

const ALLOWED_MODELS = new Set(["Qwen3.8-Flash", "DeepSeek-Flash"]);
const DEFAULT_MODEL = "Qwen3.8-Flash";
const HEAVY_MODEL = "DeepSeek-Flash";   // subagents: 1M window, deeper reasoning
const BRIDGE_URL = (process.env.JARVIS_BRIDGE_URL || "http://127.0.0.1:8011").replace(/\/+$/, "");
const BRIDGE_TOKEN = String(process.env.JARVIS_BRIDGE_TOKEN || "").trim();
const CWD = process.env.JARVIS_CWD || "/home/anthero/jarvis";
const ASSISTANT_NAME = process.env.JARVIS_ASSISTANT_NAME || "Gino";
const RESUME_SESSION = String(process.env.JARVIS_RESUME_SESSION || "").trim();
const MODEL = ALLOWED_MODELS.has(String(process.env.JARVIS_MODEL || "").trim())
  ? String(process.env.JARVIS_MODEL).trim()
  : DEFAULT_MODEL;
// Runtime policy skills. Technical docs in the same folder are deliberately
// not loaded into the live prompt. The contents are read at every start, so an
// edit to a skill applies on the next session without touching this process.
const RUNTIME_SKILLS = [
  "conversation", "execution", "actions", "memory", "voice",
];
const DESTRUCTIVE = /(?:\b(?:reboot|shutdown|poweroff|halt|suspend|hibernate)\b|systemctl\s+(?:reboot|poweroff|restart|suspend|hibernate)\b|loginctl\s+(?:lock|unlock)-session|pmset\s+(?:sleepnow|displaysleepnow)|LockWorkStation|rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r|dd\s+if=|mkfs(?:\.|\s)|chmod\s+777|>\s*\/etc\/|git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f)/i;
// Anything that changes shared or external state goes through a background
// agent and the owner's authorization. The live session never runs it inline.
const FORBIDDEN_BASH = /(?:\bgit\s+push\b|\bgh\s+(?:pr|release|repo)\b|\bnpm\s+publish\b|\bdocker\s+push\b|\bscp\b|\brsync\b|--force\b|--no-verify\b)/i;
const READONLY_BASH = /^\s*(?:git\s+(?:status|log|diff|show|branch|remote\s+-v)|ls|pwd|date|whoami|echo|cat\s+[^|;>]*|grep\b|rg\b|find\b|head\b|tail\b|wc\b|file\b)\b/i;

let currentId = null;
let busy = false;
let sawDelta = false;
let abortingId = null;
let abortTimer = null;
let turnTools = 0;
let turnHadTool = false;
let finalCandidate = "";
let sharedActionUntil = 0;   // explicit owner window for a git push
const SHARED_ACTION_WINDOW_MS = 10 * 60 * 1000;
const taskDescriptions = new Map();
const MAX_TOOLS_PER_TURN = 10;
const queue = [];
let chatQuery = null;
let channel = null;
let closing = false;

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function log(message) {
  process.stderr.write(`[QoderLive] ${message}\n`);
}

function userMessage(text, priority = "next", context = "") {
  const body = context
    ? `[CONTEXTO INTERNO — metadado, nunca leia em voz alta]\n${context}\n[/CONTEXTO INTERNO]\n\n${text}`
    : text;
  return {
    type: "user",
    message: { role: "user", content: [{ type: "text", text: body }] },
    parent_tool_use_id: null,
    priority,
  };
}

function createChannel() {
  const items = [];
  let resolveNext = null;
  let ended = false;
  return {
    push(msg) {
      if (resolveNext) {
        const r = resolveNext;
        resolveNext = null;
        r({ value: msg, done: false });
      } else {
        items.push(msg);
      }
    },
    end() {
      ended = true;
      if (resolveNext) {
        const r = resolveNext;
        resolveNext = null;
        r({ value: undefined, done: true });
      }
    },
    [Symbol.asyncIterator]() {
      return {
        next() {
          if (items.length) return Promise.resolve({ value: items.shift(), done: false });
          if (ended) return Promise.resolve({ value: undefined, done: true });
          return new Promise((resolve) => { resolveNext = resolve; });
        },
      };
    },
  };
}

async function bridge(pathname, body = null, method = "POST") {
  if (!BRIDGE_TOKEN) throw new Error("JARVIS_BRIDGE_TOKEN is not set");
  const res = await fetch(`${BRIDGE_URL}${pathname}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${BRIDGE_TOKEN}`,
    },
    body: body === null ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(120_000),
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    const detail = typeof data === "object" ? JSON.stringify(data) : String(data);
    throw new Error(`${pathname} HTTP ${res.status}: ${detail.slice(0, 400)}`);
  }
  return data;
}

function toolResult(value, isError = false) {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? null);
  return { content: [{ type: "text", text }], isError };
}

function makeJarvisServer() {
  const defs = [
    tool(
      "jarvis_tools",
      "List the JARVIS actions available through the tool bridge. Use before calling an action whose name you are unsure about.",
      {},
      async () => {
        try { return toolResult(await bridge("/tools", null, "GET")); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "jarvis_call",
      "Run a JARVIS action by name through the tool bridge. This is the general path for music, apps, files, windows, messages, reminders and system status. High-risk actions may ask the user for explicit confirmation.",
      { name: z.string().describe("Action name from jarvis_tools"), args: z.record(z.string(), z.any()).optional().describe("Action parameters") },
      async ({ name, args }) => {
        try { return toolResult(await bridge("/call", { name, args: args || {} })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "memory_read",
      "Read a markdown file from the conversation memory (for example state.md, hot.md, topics/x.md).",
      { path: z.string().describe("Relative path under the memory chat store") },
      async ({ path: p }) => {
        try { return toolResult(await bridge("/memory/read", { path: p })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "memory_search",
      "Search the conversation memory for a word or subject.",
      { query: z.string(), limit: z.number().int().positive().optional() },
      async ({ query: q, limit }) => {
        try { return toolResult(await bridge("/memory/search", { query: q, limit })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "memory_note",
      "Write a short note to the conversation memory. Use silently for decisions, open threads and facts worth keeping.",
      { text: z.string(), kind: z.string().optional() },
      async ({ text, kind }) => {
        try { return toolResult(await bridge("/memory/note", { text, kind })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "voice_list",
      "List the enrolled voices and whether each is authorized to talk to the assistant.",
      {},
      async () => {
        try { return toolResult(await bridge("/voice/list", {})); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "voice_authorize",
      "Authorize an already enrolled voice to receive answers.",
      { name: z.string() },
      async ({ name }) => {
        try { return toolResult(await bridge("/voice/authorize", { name })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "restart_assistant",
      "Ask the owner to restart the JARVIS assistant after a tested change. This only PARKS the request: the owner must then say 'confirmo reiniciar' out loud. Tell them the exact phrase and stop. Never call this again to confirm, and never claim it restarted.",
      { reason: z.string().optional() },
      async ({ reason }) => {
        try { return toolResult(await bridge("/restart/assistant", { reason: reason || "" })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "restart_pc",
      "Ask the owner to restart the computer. This only PARKS the request: the owner must then say the exact confirmation phrase out loud. Never call this as part of an automatic improvement, and never try to confirm for them.",
      { reason: z.string().optional() },
      async ({ reason }) => {
        try { return toolResult(await bridge("/restart/pc", { reason: reason || "" })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
  ];
  return createSdkMcpServer({ name: "jarvis", tools: defs });
}

const MCP_NAMES = [
  "jarvis_tools", "jarvis_call", "memory_read", "memory_search", "memory_note",
  "voice_list", "voice_authorize", "restart_assistant", "restart_pc",
].map((n) => `mcp__jarvis__${n}`);

const ALLOWED_TOOLS = [
  "Read", "Glob", "Grep", "Edit", "Write", "Bash", "WebFetch", "WebSearch",
  "Agent", "TaskStop", "TaskList", "TaskGet",
  ...MCP_NAMES,
];

async function canUseTool(toolName, input, options) {
  const toolUseID = options?.toolUseID;
  const inAgent = Boolean(options?.agentID);
  if (toolName === "Agent") {
    // The live conversation is the orchestrator. Every execution runs as a
    // background subagent so the chat never waits for it and the main session
    // keeps the whole conversation in context.
    if (input?.run_in_background === true) {
      return { behavior: "allow", toolUseID };
    }
    return {
      behavior: "deny",
      message: "Sempre chame Agent com run_in_background=true. A conversa ao vivo nunca espera uma execução.",
      toolUseID,
    };
  }
  if (toolName === "Bash") {
    const command = String(input?.command || "").trim();
    if (/\bgit\s+push\b/i.test(command)) {
      // Pushing is visible to others. Only a background agent may do it, and
      // only inside the window opened by the owner explicitly asking to push.
      if (inAgent && Date.now() < sharedActionUntil) {
        return { behavior: "allow", toolUseID };
      }
      return {
        behavior: "deny",
        message: "Push não autorizado. Só um Agent em segundo plano pode fazê-lo, e apenas depois de o dono pedir explicitamente para subir o repositório.",
        toolUseID,
      };
    }
    if (DESTRUCTIVE.test(command) || FORBIDDEN_BASH.test(command)) {
      return {
        behavior: "deny",
        message: "Comando destrutivo negado. Use uma ferramenta própria, com autorização falada do dono.",
        toolUseID,
      };
    }
    if (!inAgent && !READONLY_BASH.test(command)) {
      return {
        behavior: "deny",
        message: "O chat ao vivo só roda consultas rápidas. Para qualquer execução (push, edição, instalação, serviço), chame Agent com run_in_background=true e continue conversando.",
        toolUseID,
      };
    }
  }
  if (toolName === "Edit" || toolName === "Write" || toolName === "NotebookEdit") {
    if (!inAgent) {
      return {
        behavior: "deny",
        message: "Edição de arquivo é execução: delegue a um Agent em segundo plano (run_in_background=true) e continue a conversa.",
        toolUseID,
      };
    }
    return { behavior: "allow", toolUseID };
  }
  if (inAgent) return { behavior: "allow", toolUseID };
  // One turn must not become a twenty-command investigation while the user is
  // waiting in a live conversation. The budget resets on every new ask.
  if (turnTools >= MAX_TOOLS_PER_TURN) return denyBudget(toolUseID);
  turnTools += 1;
  return { behavior: "allow", toolUseID };
}

function denyBudget(toolUseID) {
  return {
    behavior: "deny",
    message: "Você já usou o limite de ferramentas deste turno. Pare de investigar e responda agora com o que já tem.",
    toolUseID,
  };
}

function orchestrationPrompt() {
  return `[LIVE ORCHESTRATOR]\nVocê é ${ASSISTANT_NAME} na mesma conversa contínua desde o início do app. Nunca trate um turno como uma conversa nova: o histórico inteiro desta sessão é seu contexto. Fale curto e falável, em pt-BR, a menos que o dono mude de idioma. Nunca narre chamadas de ferramenta em voz alta, nunca leia JSON ou erro cru, e nunca invente um resultado. Use as ferramentas jarvis para ações rápidas (música, apps, arquivos, memória, status) e nunca finja que fez algo sem o resultado da ferramenta.\n[EXECUÇÃO] Toda execução — editar arquivos, testar, instalar, rodar comandos longos, investigar código, git — acontece em um subagente em segundo plano: chame Agent com run_in_background=true, subagent_type="executor". A conversa NUNCA espera a execução. Depois de delegar, diga em UMA frase curta o que está rodando e pare de falar; não faça o usuário esperar e não repita atualizações de andamento. O resultado chega sozinho nesta mesma sessão quando terminar; aí sim você comenta em uma ou duas frases. Nunca use Bash/Edit/Write no chat ao vivo para execuções: o gancho de permissão bloqueia, e tentar só atrasa a resposta.\n[STATUS] Atualizações automáticas de andamento não são suas e não viram fala do usuário. Não responda a elas; só o dono fala com você. Se uma tarefa terminar, comente o resultado na próxima vez que fizer sentido, sem transformar a conversa num relatório.\n[CONTEXTO INTERNO] Blocos [CONTEXTO INTERNO ...] são metadados do sistema (pausa, fonte da mensagem, canal). Nunca os leia em voz alta nem os trate como fala do dono; use-os para saber se é continuação, novo assunto ou WhatsApp.\n[RECALL] O índice com níveis e os últimos turnos vêm no prompt do sistema a cada início. Use o nível 0 (esta sessão) para o que acabou de ser dito; níveis 1-2 (hot/estado) para as últimas horas; memory_search/memory_read para horas, dias e tópicos antigos; e diga que não lembra quando o registro não existir, em vez de reconstruir. Um "deixa eu lembrar" antes de uma busca de memória é natural.\n[SKILLS] As skills ativas abaixo são a política do dono. Siga-as como regras, não como sugestões: elas explicam quando usar cada ferramenta, como pedir confirmação e o que nunca fazer.\n[FALANTES] Linhas podem começar com [FALANTE: nome]. Isso é metadado: nunca leia o rótulo em voz alta. Use para saber com quem está falando; se a voz for desconhecida, não finja conhecê-la.\n[CONFIRMAÇÕES] Ações irreversíveis (reiniciar, desligar, suspender, enviar mensagem, publicar) só acontecem depois de uma confirmação explícita do dono NA CONVERSA. Peça a frase exata, espere a resposta dele e só então chame a ferramenta. Você nunca confirma por ele nem chama uma ação de confirmação sozinho. Reinício e energia só são confirmados pela fala do dono, nunca pelo modelo: use a ferramenta, diga a frase exata e pare.\n[LÍNGUA] Toda frase em português do Brasil. Se escapar palavra em outro idioma, reescreva a frase em português antes de terminar.`;
}

function loadSkills() {
  const dir = path.join(CWD, "skills");
  const parts = [];
  for (const name of RUNTIME_SKILLS) {
    const p = path.join(dir, name, "SKILL.md");
    if (!fs.existsSync(p)) {
      log(`skill ausente: ${p}`);
      continue;
    }
    let text = fs.readFileSync(p, "utf8");
    // Strip the YAML frontmatter: the prompt only needs the policy body.
    text = text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "").trim();
    if (text) parts.push(`### SKILL: ${name}\n${text}`);
  }
  return parts.join("\n\n");
}

function loadSystemPrompt() {
  const explicit = String(process.env.JARVIS_SYSTEM_PROMPT_FILE || "").trim();
  let base;
  if (explicit && fs.existsSync(explicit)) {
    base = fs.readFileSync(explicit, "utf8").trimEnd();
  } else {
    const parts = [];
    for (const rel of ["core/prompt.txt", "core/persona.md"]) {
      const p = path.join(CWD, rel);
      if (fs.existsSync(p)) parts.push(fs.readFileSync(p, "utf8"));
    }
    let text = parts.join("\n\n");
    text = text
      .replaceAll("{assistant_name}", ASSISTANT_NAME)
      .replaceAll("{platform}", process.platform)
      .replaceAll("{capabilities}", "Use the jarvis MCP tools for actions; use jarvis_tools to discover names.")
      .replaceAll("{limits}", "If no tool covers a request, say so; never claim an action without a tool result.");
    base = text;
  }
  const skills = loadSkills();
  return [base, orchestrationPrompt(), skills ? `# SKILLS ATIVAS (política do dono)\n${skills}` : ""]
    .filter(Boolean).join("\n\n");
}

const SYSTEM_PROMPT = loadSystemPrompt();

// The executor is a background subagent, not a separate CLI process. It is
// spawned by the main session, inherits the task description written from the
// live conversation, and reports back into the same session when it finishes.
const EXECUTOR_AGENT = {
  description: "Executa mudanças e investigações pesadas no projeto (editar, testar, instalar, rodar comandos, git) sem travar a conversa.",
  prompt: "Você é o executor do Gino. Receba a tarefa descrita pela conversa e execute até o fim: leia o código antes de mudar, rode os testes relevantes, verifique o resultado e não invente sucesso. Não peça confirmação para o que o dono já pediu na conversa; peça somente se a tarefa mudar o escopo ou for irreversível. Responda em português com um resumo curto do que mudou e da verificação. Nunca faça push sem o dono ter pedido explicitamente.",
  model: HEAVY_MODEL,
  tools: ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "WebFetch", "WebSearch", "Agent"],
  permissionMode: "acceptEdits",
  background: true,
  maxTurns: 80,
};

function optionsFor(maxTurns) {
  return {
    auth: qodercliAuth(),
    model: MODEL,
    cwd: CWD,
    settingSources: [],
    permissionMode: "default",
    canUseTool,
    mcpServers: { jarvis: makeJarvisServer() },
    allowedTools: ALLOWED_TOOLS,
    agents: { executor: EXECUTOR_AGENT },
    includePartialMessages: true,
    persistSession: true,
    resume: RESUME_SESSION || undefined,
    maxTurns,
    systemPrompt: { type: "preset", preset: "qodercli", append: SYSTEM_PROMPT },
    stderr: (data) => process.stderr.write(data),
  };
}

async function warmup() {
  const started = Date.now();
  const q = query({
    prompt: "Responda apenas: ok",
    // The warm-up is about loading the daemon/model. It must not create a
    // resumable session that could later collide with the real chat.
    options: { ...optionsFor(1), persistSession: false, resume: undefined },
  });
  try {
    for await (const msg of q) {
      if (msg.type === "result" && msg.subtype !== "success") {
        const detail = (msg.errors || []).join("; ") || msg.subtype;
        throw new Error(detail);
      }
    }
  } finally {
    await q.close?.();
  }
  return Date.now() - started;
}

function finishTurn(ok, textOrError, subtype = "") {
  const id = currentId;
  if (abortingId && id === abortingId) {
    // The turn was preempted by a new spoken turn; its abort result must not
    // be attributed to the new turn nor spoken as an error.
    abortingId = null;
    if (abortTimer) { clearTimeout(abortTimer); abortTimer = null; }
    currentId = null;
    busy = false;
    sawDelta = false;
    out({ type: "interrupted", id });
    pumpQueue();
    return;
  }
  currentId = null;
  busy = false;
  sawDelta = false;
  if (!id) return;
  if (ok) {
    const answer = String(textOrError || finalCandidate || "");
    out({ type: "done", id, text: answer, subtype });
  } else {
    out({ type: "error", id, error: String(textOrError || subtype || "erro") });
  }
  pumpQueue();
}

function handleAssistant(msg) {
  if (msg?.parent_tool_use_id) return;   // subagent chatter never reaches TTS
  const content = msg?.message?.content || [];
  const hasTool = content.some((block) => block?.type === "tool_use");
  if (hasTool) turnHadTool = true;
  for (const block of content) {
    if (block?.type === "tool_use") {
      out({ type: "tool", id: currentId, name: block.name || "", input: block.input || {} });
    } else if (block?.type === "text" && !hasTool) {
      const text = String(block.text || "");
      if (turnHadTool) {
        // After a tool, this is likely the final answer composed across
        // messages: keep the last one for the result, never speak plans.
        finalCandidate = text;
      } else {
        out({ type: "text", id: currentId, text, delta: false });
      }
    }
  }
}

function handleStreamEvent(_msg) {
  // Partial text is deliberately NOT spoken: a model that narrates "deixa eu
  // ver o log" before every tool would say its plan to the user. Full
  // assistant messages are filtered by handleAssistant; tool-using turns speak
  // only the final result.
}

function handleResult(msg) {
  if (msg.subtype === "success") {
    finishTurn(true, msg.result, msg.subtype);
  } else {
    finishTurn(false, (msg.errors || []).join("; ") || msg.subtype, msg.subtype);
  }
}

async function chatLoop() {
  // No maxTurns on the live session: the process is meant to keep ONE
  // conversation alive for the whole app run. Runaway tool chains are bounded
  // by [TOOL DISCIPLINE], the Agent denial and the Python-side watchdog.
  chatQuery = query({ prompt: channel, options: optionsFor(undefined) });
  try {
    for await (const msg of chatQuery) {
      if (closing) break;
      if (msg.type === "system" && msg.subtype === "init") {
        log(`session ${msg.session_id || "?"} ready`);
        if (msg.session_id) out({ type: "session", id: msg.session_id });
      } else if (msg.type === "system" && msg.subtype === "task_started") {
        taskDescriptions.set(msg.task_id, msg.description || "");
        out({ type: "task", phase: "started", id: msg.task_id,
              description: msg.description || "" });
        // A background subagent never holds the conversation. The main session
        // keeps running; the task notification arrives in this same session
        // when it finishes, so the context is never lost.
      } else if (msg.type === "system" && msg.subtype === "task_notification") {
        const description = taskDescriptions.get(msg.task_id) || "";
        taskDescriptions.delete(msg.task_id);
        out({ type: "task", phase: "done", id: msg.task_id,
              status: msg.status || "", description,
              summary: msg.summary || "" });
      } else if (msg.type === "stream_event") {
        handleStreamEvent(msg);
      } else if (msg.type === "assistant") {
        handleAssistant(msg);
      } else if (msg.type === "result") {
        handleResult(msg);
      }
    }
  } catch (e) {
    log(`chat loop failed: ${e?.message || e}`);
    out({ type: "error", id: currentId, error: String(e?.message || e) });
  } finally {
    if (!closing) {
      out({ type: "closed" });
      process.exitCode = 1;
    }
  }
}

function pushAsk(item) {
  currentId = item.id;
  busy = true;
  sawDelta = false;
  turnTools = 0;
  turnHadTool = false;
  finalCandidate = "";
  const text = String(item.text || "");
  // The owner's own words open a short window for the background agent to
  // push. The model cannot open it by talking about a push.
  if (/\b(?:push|sub(?:ir|a|am)|envi(?:ar|e)|public(?:ar|a))\b/i.test(text)
      && /\b(?:git|github|reposit[oó]rio|repo|origin|tudo)\b/i.test(text)) {
    sharedActionUntil = Date.now() + SHARED_ACTION_WINDOW_MS;
    log("owner window: git push allowed from a background agent");
  }
  channel.push(userMessage(item.text, item.priority || "next",
                           item.context || ""));
  out({ type: "accepted", id: item.id });
}

function pumpQueue() {
  if (busy || !queue.length) return;
  const next = queue.shift();
  pushAsk(next);
}

async function ask(item) {
  if (!item?.id || !item?.text) {
    out({ type: "error", id: item?.id || null, error: "ask requires id and text" });
    return;
  }
  if (busy) {
    if (item.priority === "now") {
      // A new spoken turn preempts the previous one; background agents keep
      // running. The new turn waits for the aborted result so it is not
      // mistaken for the old one.
      queue.unshift({ ...item, priority: "next" });
      if (!abortingId) {
        abortingId = currentId;
        try { await chatQuery?.interrupt?.(); } catch (e) { log(`preempt: ${e?.message || e}`); }
        abortTimer = setTimeout(() => {
          if (!abortingId) return;
          const id = abortingId;
          abortingId = null;
          abortTimer = null;
          currentId = null;
          busy = false;
          sawDelta = false;
          out({ type: "interrupted", id });
          pumpQueue();
        }, 1500);
      }
      return;
    }
    queue.push(item);
    out({ type: "queued", id: item.id, ahead: 1 });
    return;
  }
  pushAsk(item);
}

async function interrupt() {
  const id = currentId;
  queue.length = 0;
  abortingId = null;
  if (abortTimer) { clearTimeout(abortTimer); abortTimer = null; }
  try { await chatQuery?.interrupt?.(); } catch (e) { log(`interrupt: ${e?.message || e}`); }
  if (id) {
    currentId = null;
    busy = false;
    sawDelta = false;
    out({ type: "interrupted", id });
  }
}

async function closeAll() {
  if (closing) return;
  closing = true;
  try { channel?.end(); } catch {}
  try { await chatQuery?.close?.(); } catch {}
  out({ type: "closed" });
  setTimeout(() => process.exit(0), 50);
}

function selftest() {
  const skills = RUNTIME_SKILLS.map((name) => path.join(CWD, "skills", name, "SKILL.md"));
  const checks = {
    model: ALLOWED_MODELS.has(MODEL),
    destructive: DESTRUCTIVE.test("sudo reboot") && DESTRUCTIVE.test("rm -rf /") && DESTRUCTIVE.test("systemctl restart foo") && !DESTRUCTIVE.test("ls -la"),
    mcp_tools: MCP_NAMES.length === 9,
    system_prompt: SYSTEM_PROMPT.length > 100,
    skills: skills.every((p) => fs.existsSync(p)),
    background_agent: EXECUTOR_AGENT.background === true && EXECUTOR_AGENT.model === HEAVY_MODEL,
    internal_context: userMessage("x", "next", "ctx").message.content[0].text.includes("[CONTEXTO INTERNO"),
    bridge_url: BRIDGE_URL.startsWith("http"),
  };
  const ok = Object.values(checks).every(Boolean);
  process.stdout.write(JSON.stringify({ ok, checks, model: MODEL }) + "\n");
  process.exit(ok ? 0 : 1);
}

async function main() {
  if (process.argv.includes("--selftest")) return selftest();
  if (!BRIDGE_TOKEN) log("JARVIS_BRIDGE_TOKEN is not set; tool calls will fail");
  channel = createChannel();
  const warm = await warmup();
  out({ type: "ready", model: MODEL, warmup_ms: warm });
  log(`ready with ${MODEL} after ${warm}ms`);
  chatLoop().catch((e) => log(`chat loop crashed: ${e?.message || e}`));

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on("line", async (line) => {
    line = line.trim();
    if (!line) return;
    let msg;
    try { msg = JSON.parse(line); }
    catch { log(`bad json: ${line.slice(0, 120)}`); return; }
    try {
      switch (msg.type) {
        case "warmup":
          out({ type: "ready", model: MODEL, warmup_ms: 0 });
          break;
        case "ask": await ask(msg); break;
        case "interrupt": await interrupt(); break;
        case "close": await closeAll(); break;
        default: log(`unknown message: ${msg.type}`);
      }
    } catch (e) {
      out({ type: "error", id: msg.id || null, error: String(e?.message || e) });
    }
  });
  rl.on("close", () => closeAll());
  process.on("SIGTERM", () => closeAll());
  process.on("SIGINT", () => closeAll());
}

main().catch((e) => {
  log(`fatal: ${e?.stack || e}`);
  out({ type: "error", error: String(e?.message || e) });
  process.exit(1);
});
