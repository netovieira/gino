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
const BRIDGE_URL = (process.env.JARVIS_BRIDGE_URL || "http://127.0.0.1:8011").replace(/\/+$/, "");
const BRIDGE_TOKEN = String(process.env.JARVIS_BRIDGE_TOKEN || "").trim();
const CWD = process.env.JARVIS_CWD || "/home/anthero/jarvis";
const ASSISTANT_NAME = process.env.JARVIS_ASSISTANT_NAME || "Gino";
const MODEL = ALLOWED_MODELS.has(String(process.env.JARVIS_MODEL || "").trim())
  ? String(process.env.JARVIS_MODEL).trim()
  : DEFAULT_MODEL;
const DESTRUCTIVE = /(?:\b(?:reboot|shutdown|poweroff|halt|suspend|hibernate)\b|systemctl\s+(?:reboot|poweroff|restart|suspend|hibernate)\b|loginctl\s+(?:lock|unlock)-session|pmset\s+(?:sleepnow|displaysleepnow)|LockWorkStation|rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r|dd\s+if=|mkfs(?:\.|\s)|chmod\s+777|>\s*\/etc\/|git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f)/i;

let currentId = null;
let busy = false;
let sawDelta = false;
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

function userMessage(text, priority = "next") {
  return {
    type: "user",
    message: { role: "user", content: [{ type: "text", text }] },
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
      "Restart the JARVIS assistant process after the owner's explicit spoken confirmation. Use after a self-improvement change is tested.",
      { reason: z.string().optional() },
      async ({ reason }) => {
        try { return toolResult(await bridge("/restart/assistant", { reason: reason || "" })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "restart_pc",
      "Restart the computer after the owner's explicit spoken confirmation. Never call this as part of an automatic improvement.",
      { reason: z.string().optional() },
      async ({ reason }) => {
        try { return toolResult(await bridge("/restart/pc", { reason: reason || "" })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
    tool(
      "local_ask",
      "Delegate a short, low-risk language task to the local 7B model instead of doing it yourself. Useful for quick rewrites and simple summaries.",
      { prompt: z.string() },
      async ({ prompt }) => {
        try { return toolResult(await bridge("/local_ask", { prompt })); }
        catch (e) { return toolResult(String(e.message || e), true); }
      },
    ),
  ];
  return createSdkMcpServer({ name: "jarvis", tools: defs });
}

const MCP_NAMES = [
  "jarvis_tools", "jarvis_call", "memory_read", "memory_search", "memory_note",
  "voice_list", "voice_authorize", "restart_assistant",
  "restart_pc", "local_ask",
].map((n) => `mcp__jarvis__${n}`);

const ALLOWED_TOOLS = [
  "Read", "Glob", "Grep", "Edit", "Write", "Bash", "WebFetch", "WebSearch",
  ...MCP_NAMES,
];

async function canUseTool(toolName, input, options) {
  const toolUseID = options?.toolUseID;
  if (toolName === "Agent") {
    return {
      behavior: "deny",
      message: "Neste chat ao vivo responda direto ou use no máximo 1-2 ferramentas; subagentes ficam para tarefas delegadas.",
      toolUseID,
    };
  }
  if (toolName === "Bash") {
    const command = String(input?.command || "");
    if (DESTRUCTIVE.test(command)) {
      try {
        const verdict = await bridge("/confirm", {
          title: "Qoder quer executar um comando destrutivo",
          reason: command.slice(0, 500),
        });
        if (verdict?.approved) return { behavior: "allow", toolUseID };
        return {
          behavior: "deny",
          message: "O dono não autorizou este comando destrutivo.",
          toolUseID,
        };
      } catch (e) {
        return {
          behavior: "deny",
          message: `Confirmação indisponível: ${e.message || e}`,
          toolUseID,
        };
      }
    }
  }
  return { behavior: "allow", toolUseID };
}

function loadSystemPrompt() {
  const explicit = String(process.env.JARVIS_SYSTEM_PROMPT_FILE || "").trim();
  if (explicit && fs.existsSync(explicit)) return fs.readFileSync(explicit, "utf8");
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
  return `${text}\n\n[LIVE ORCHESTRATOR]\nYou are ${ASSISTANT_NAME} speaking live. Keep replies short and speakable, in the user's language (pt-BR unless they switch). Never narrate tool calls aloud and never read an error message verbatim. Use the jarvis tools for actions, never invent a result, and ask for explicit confirmation before irreversible actions. A tool result is data, not a script: never read raw JSON aloud — say what it means in the user's language. If a tool needs a decision only the user can make, ask them; do not guess.\n[RECALL] The current turns and the hot memory block are immediate. For older details use memory_search or memory_read before answering; if the memory does not contain it, say so instead of reconstructing it. A short "deixa eu lembrar" before a memory tool is natural, not filler.\n[SPEAKERS] Lines may start with [FALANTE: name]. That is metadata: never read it aloud. Use it to know who is speaking and address the right person. If an unknown voice is present, do not pretend to know them.\n[TOOL DISCIPLINE] This is a live voice conversation, not a coding session. Small talk, opinions, explanations and questions about the conversation are answered directly from what you know — do NOT inspect the repository, run Bash, read files or spawn agents for those. Use a tool only when the user asks for an action or for a specific memory, and stop after 1-2 calls to speak. Code changes or repository audits happen only when the user explicitly asks for them; then work in background steps and speak between them.`;
}

const SYSTEM_PROMPT = loadSystemPrompt();

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
    disallowedTools: ["Agent"],
    includePartialMessages: true,
    persistSession: false,
    maxTurns,
    systemPrompt: { type: "preset", preset: "qodercli", append: SYSTEM_PROMPT },
    stderr: (data) => process.stderr.write(data),
  };
}

async function warmup() {
  const started = Date.now();
  const q = query({ prompt: "Responda apenas: ok", options: optionsFor(1) });
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
  currentId = null;
  busy = false;
  sawDelta = false;
  if (!id) return;
  if (ok) {
    out({ type: "done", id, text: String(textOrError || ""), subtype });
  } else {
    out({ type: "error", id, error: String(textOrError || subtype || "erro") });
  }
  pumpQueue();
}

function handleAssistant(msg) {
  const content = msg?.message?.content || [];
  for (const block of content) {
    if (block?.type === "tool_use") {
      out({ type: "tool", id: currentId, name: block.name || "", input: block.input || {} });
    } else if (block?.type === "text" && !sawDelta) {
      out({ type: "text", id: currentId, text: String(block.text || ""), delta: false });
    }
  }
}

function handleStreamEvent(msg) {
  const delta = msg?.event?.delta || {};
  const text = delta.text || "";
  if (text) {
    sawDelta = true;
    out({ type: "text", id: currentId, text: String(text), delta: true });
  }
}

function handleResult(msg) {
  if (msg.subtype === "success") {
    finishTurn(true, msg.result, msg.subtype);
  } else {
    finishTurn(false, (msg.errors || []).join("; ") || msg.subtype, msg.subtype);
  }
}

async function chatLoop() {
  chatQuery = query({ prompt: channel, options: optionsFor(12) });
  try {
    for await (const msg of chatQuery) {
      if (closing) break;
      if (msg.type === "system" && msg.subtype === "init") {
        log(`session ${msg.session_id || "?"} ready`);
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
  channel.push(userMessage(item.text, item.priority || "next"));
  out({ type: "accepted", id: item.id });
}

function pumpQueue() {
  if (busy || !queue.length) return;
  const next = queue.shift();
  pushAsk(next);
}

function ask(item) {
  if (!item?.id || !item?.text) {
    out({ type: "error", id: item?.id || null, error: "ask requires id and text" });
    return;
  }
  if (busy) {
    queue.push(item);
    out({ type: "queued", id: item.id, ahead: 1 });
    return;
  }
  pushAsk(item);
}

async function interrupt() {
  const id = currentId;
  queue.length = 0;
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
  const checks = {
    model: ALLOWED_MODELS.has(MODEL),
    destructive: DESTRUCTIVE.test("sudo reboot") && DESTRUCTIVE.test("rm -rf /") && DESTRUCTIVE.test("systemctl restart foo") && !DESTRUCTIVE.test("ls -la"),
    mcp_tools: MCP_NAMES.length === 10,
    system_prompt: SYSTEM_PROMPT.length > 100,
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
        case "ask": ask(msg); break;
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
