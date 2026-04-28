from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from main import app as mcp_app, run_agent
from rag import delete_session, get_all_sessions, retrieve_similar, store_session


SYSTEM_OPS_BASE_URL = os.getenv("SYSTEM_OPS_BASE_URL", "http://127.0.0.1:8000")

_agent_ctx = None

# Per-session SSE event queues. Keyed by session_id.
_session_events: Dict[str, asyncio.Queue] = {}

# Stores the last plan + problem per session so the feedback endpoint can save them.
_session_plans: Dict[str, Dict] = {}
_session_problems: Dict[str, str] = {}


def _get_queue(session_id: str) -> asyncio.Queue:
    if session_id not in _session_events:
        _session_events[session_id] = asyncio.Queue()
    return _session_events[session_id]


async def _emit(session_id: str, event: str, data: str = "") -> None:
    await _get_queue(session_id).put({"event": event, "data": data})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent_ctx
    async with mcp_app.run() as agent_app:
        _agent_ctx = agent_app.context
        yield
    _agent_ctx = None


frontend_app = FastAPI(
    title="Windows Troubleshooter Frontend",
    description=(
        "Simple web frontend that ties together the MCP Tavily+Gemini agent "
        "with the system_ops FastAPI backend and MongoDB session logging."
    ),
    lifespan=lifespan,
)


def call_execute_powershell_commands(
    commands: List[Dict[str, Any]],
    session_id: Optional[str] = None,
    session_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Call the /tools/execute_powershell_commands endpoint with confirmed=true,
    optionally including a logical session id and metadata for MongoDB logging.
    """
    url = f"{SYSTEM_OPS_BASE_URL}/tools/execute_powershell_commands"
    body: Dict[str, Any] = {
        "commands": commands,
        "confirmed": True,
    }
    if session_id is not None:
        body["session_id"] = session_id
    if session_metadata is not None:
        body["session_metadata"] = session_metadata

    data = json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    req = Request(url, data=data, headers=headers, method="POST")

    try:
        with urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        return {
            "success": False,
            "error": f"HTTPError from system_ops: {e}",
            "detail": detail,
        }
    except URLError as e:
        return {
            "success": False,
            "error": f"URLError connecting to system_ops: {e}",
        }


class PlanRequest(BaseModel):
    problem: str
    feedback: Optional[str] = None
    history: Optional[List[Dict[str, str]]] = None


async def get_troubleshooting_plan(
    problem: str,
    feedback: Optional[str],
    history: Optional[List[Dict[str, str]]] = None,
    prior_sessions: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Ask the windows_troubleshooter agent to produce a JSON troubleshooting plan
    that can combine local system_ops helpers with Tavily web search, and then
    propose PowerShell commands when appropriate.
    """
    extra_feedback = (
        f"\nThe user rejected or refined the previous plan because: {feedback}\n"
        if feedback
        else ""
    )

    if prior_sessions:
        prior_block = "Similar issues resolved before (use as a starting point if relevant):\n"
        for i, s in enumerate(prior_sessions, 1):
            prior_block += f"{i}. Problem: \"{s['problem']}\"\n   Fix: {s['summary']}"
            if s["commands"]:
                prior_block += f"\n   Commands used: {s['commands']}"
            prior_block += "\n"
        prior_block += "\n"
    else:
        prior_block = ""

    prompt = f"""User problem: {problem}
{extra_feedback}{prior_block}For hardware (GPU/Wi-Fi/chipset), detect the model via system_ops first, then search Tavily for official drivers and include download URLs in "notes".

Reply with ONLY a JSON object — no markdown, no extra text:
{{
  "summary": "one-line summary of the issue and fix",
  "commands": [
    {{
      "text": "PowerShell or winget or choco command",
      "reason": "why this command is needed",
      "origin": "agent",
      "reversible": false,
      "rollback_notes": "how to undo, or why it is risky"
    }}
  ],
  "notes": "additional steps or download links"
}}

Rules:
- "text" must be a raw PowerShell command a user can paste into a terminal (e.g. "Start-MpScan -ScanType QuickScan"). Never use Python, API calls, or MCP tool syntax.
- Set reversible to true only if clearly undoable.
- Use commands [] if none needed."""

    if _agent_ctx is None:
        return {
            "summary": "Failed to generate troubleshooting plan.",
            "commands": [],
            "notes": "Agent context not initialized. Please restart the server.",
        }
    try:
        result_str = await run_agent(
            agent_name="windows_troubleshooter",
            prompt=prompt,
            app_ctx=_agent_ctx,
        )
    except Exception as exc:
        return {
            "summary": "Failed to generate troubleshooting plan.",
            "commands": [],
            "notes": f"Error while calling windows_troubleshooter agent: {exc}",
        }

    raw = result_str.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    # Try direct parse first
    try:
        plan = json.loads(raw)
        if isinstance(plan, dict):
            return plan
    except Exception:
        pass

    # Heuristic: extract the first top-level JSON object from the text
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = raw[start : end + 1]
        try:
            plan = json.loads(candidate)
            if isinstance(plan, dict):
                return plan
        except Exception:
            pass

    # Fallback: return a minimal structure and include the raw text for debugging
    return {
        "summary": "Failed to parse troubleshooting plan from agent.",
        "commands": [],
        "notes": result_str,
    }


@frontend_app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Atlantis \u2014 Windows Troubleshooter</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #f9fafb; --surface: #ffffff; --border: #e5e7eb;
      --text: #111827; --muted: #6b7280;
      --user-bg: #2563eb; --assist-bg: #f3f4f6;
      --code-bg: #1e1e2e; --code-text: #cdd6f4;
      --accent: #2563eb;
    }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      background: var(--bg); color: var(--text);
      height: 100dvh; display: flex; flex-direction: column; overflow: hidden;
    }
    .header {
      background: #1e1e2e; color: #cdd6f4;
      padding: 12px 20px; display: flex; align-items: center; gap: 10px;
      flex-shrink: 0; border-bottom: 1px solid #313244;
    }
    .header h1 { font-size: 1rem; font-weight: 600; letter-spacing: .02em; }
    .header .pill {
      font-size: .7rem; background: #313244; color: #89b4fa;
      padding: 2px 8px; border-radius: 99px;
    }
    .health-warn {
      background: #7f1d1d; color: #fca5a5;
      padding: 6px 20px; font-size: .8rem; flex-shrink: 0; display: none;
    }
    .chat-area { flex: 1; overflow-y: auto; padding: 24px 0 8px; scroll-behavior: smooth; }
    .messages {
      max-width: 780px; margin: 0 auto; padding: 0 16px;
      display: flex; flex-direction: column; gap: 16px;
    }
    .msg { display: flex; gap: 10px; }
    .msg.user { justify-content: flex-end; }
    .bubble { max-width: 82%; padding: 10px 14px; border-radius: 16px; line-height: 1.55; font-size: .925rem; }
    .msg.user .bubble { background: var(--user-bg); color: #fff; border-bottom-right-radius: 4px; max-width: 70%; }
    .msg.assistant .bubble { background: var(--assist-bg); color: var(--text); border-bottom-left-radius: 4px; max-width: 90%; }
    .msg-icon {
      width: 28px; height: 28px; border-radius: 50%;
      background: #1e1e2e; color: #89b4fa;
      display: flex; align-items: center; justify-content: center;
      font-size: .72rem; font-weight: 700; flex-shrink: 0; margin-top: 2px;
    }
    .bubble a { color: #3b82f6; }
    .bubble p + p { margin-top: 6px; }
    .bubble ol, .bubble ul { margin: 6px 0 6px 20px; padding: 0; }
    .bubble li { margin-bottom: 6px; line-height: 1.55; }
    .bubble li p { margin: 0; }
    /* command block */
    .cmd-block { margin-top: 10px; border-radius: 10px; overflow: hidden; border: 1px solid #313244; background: var(--code-bg); }
    .cmd-header { display: flex; align-items: center; justify-content: space-between; padding: 7px 12px; background: #181825; gap: 8px; }
    .cmd-reason { font-size: .78rem; color: #a6adc8; flex: 1; }
    .cmd-badges { display: flex; gap: 4px; align-items: center; flex-shrink: 0; }
    .rev-badge { font-size: .68rem; padding: 2px 7px; border-radius: 99px; }
    .rev-badge.yes { background: #1e3a2f; color: #4ade80; }
    .rev-badge.no  { background: #3b1f1f; color: #f87171; }
    .cmd-row { display: flex; align-items: center; padding: 10px 12px; gap: 12px; }
    .cmd-edit {
      flex: 1; font-family: "Cascadia Code","Fira Code",Consolas,monospace;
      font-size: .83rem; color: var(--code-text);
      background: transparent; border: none; border-radius: 4px;
      padding: 0; resize: none; outline: none; overflow: hidden;
      white-space: pre-wrap; word-break: break-all;
      line-height: 1.5; cursor: default; min-height: 20px;
    }
    .cmd-edit.editing { background: #16162a; border: 1px solid #3b3b6b; padding: 4px 6px; cursor: text; }
    .edit-btn {
      flex-shrink: 0; background: transparent; border: 1px solid #3b3b5e;
      color: #6b7280; border-radius: 5px; padding: 5px 10px;
      font-size: .75rem; cursor: pointer; white-space: nowrap;
      transition: background .15s, color .15s, border-color .15s;
    }
    .edit-btn:hover { background: #1e1e3a; color: #a6adc8; border-color: #5b5b8e; }
    .edit-btn.active { border-color: #4ade80; color: #4ade80; }
    .run-btn {
      flex-shrink: 0; background: #16a34a; color: #fff; border: none; border-radius: 6px;
      padding: 6px 13px; font-size: .8rem; cursor: pointer;
      display: flex; align-items: center; gap: 5px;
      transition: background .15s, opacity .15s; white-space: nowrap;
    }
    .run-btn:hover:not(:disabled) { background: #15803d; }
    .run-btn:disabled { opacity: .6; cursor: not-allowed; }
    .run-btn.running { background: #1d4ed8; }
    .run-btn.ok  { background: #15803d; }
    .run-btn.err { background: #b91c1c; }
    .cmd-output {
      padding: 8px 12px; background: #11111b;
      font-family: Consolas, monospace; font-size: .78rem;
      white-space: pre-wrap; word-break: break-all;
      max-height: 220px; overflow-y: auto;
      border-top: 1px solid #313244; display: none;
    }
    .cmd-output.show { display: block; }
    .cmd-output.ok  { color: #4ade80; }
    .cmd-output.err { color: #f87171; }
    /* typing */
    .typing { display: flex; gap: 4px; padding: 10px 14px; background: var(--assist-bg); border-radius: 16px; border-bottom-left-radius: 4px; width: fit-content; }
    .step-label { font-size: .72rem; color: var(--muted); margin-top: 3px; font-style: italic; }
    #scan-bar { background:#1e1e2e; color:#89b4fa; font-size:.78rem; padding:6px 20px; display:flex; align-items:center; gap:8px; flex-shrink:0; border-bottom:1px solid #313244; }
    #scan-bar.done { color:#4ade80; }
    .spin { display:inline-block; animation:spin .9s linear infinite; }
    @keyframes spin { to { transform:rotate(360deg); } }
    .prior-card {
      margin-left: 38px; max-width: 88%;
      background: #0f1b2d; border: 1px solid #1e3a5f;
      border-left: 3px solid #2563eb; border-radius: 10px;
      padding: 10px 14px; font-size: .82rem;
    }
    .prior-card-hdr {
      color: #60a5fa; font-size: .72rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: .05em;
      margin-bottom: 8px;
    }
    .prior-item + .prior-item { border-top: 1px solid #1e293b; margin-top: 8px; padding-top: 8px; }
    .prior-prob { color: #94a3b8; font-size: .78rem; margin-bottom: 3px; }
    .prior-sum  { color: #cbd5e1; margin-bottom: 4px; line-height: 1.4; }
    .prior-cmds {
      font-family: Consolas, monospace; font-size: .74rem; color: #5eead4;
      background: #0a1120; padding: 5px 8px; border-radius: 6px;
      white-space: pre-wrap; word-break: break-all;
    }
    .feedback-row { display: flex; align-items: center; gap: 8px; padding: 2px 0 8px 38px; }
    .fb-label { font-size: .72rem; color: var(--muted); }
    .fb-btn { font-size: .72rem; padding: 3px 12px; border-radius: 99px; border: 1px solid; cursor: pointer; background: transparent; transition: background .15s; }
    .fb-btn.fb-yes { color: #4ade80; border-color: #4ade80; }
    .fb-btn.fb-yes:hover { background: #1e3a2f; }
    .fb-btn.fb-no { color: #9ca3af; border-color: #6b7280; }
    .fb-btn.fb-no:hover { background: #1f2937; }
    .dot { width: 6px; height: 6px; border-radius: 50%; background: #9ca3af; animation: pop 1.2s infinite; }
    .dot:nth-child(2) { animation-delay: .2s; }
    .dot:nth-child(3) { animation-delay: .4s; }
    @keyframes pop { 0%,80%,100%{transform:translateY(0);opacity:.4;} 40%{transform:translateY(-6px);opacity:1;} }
    /* input */
    .input-bar { flex-shrink: 0; padding: 10px 16px 12px; background: var(--surface); border-top: 1px solid var(--border); }
    .input-inner { max-width: 780px; margin: 0 auto; display: flex; gap: 8px; align-items: flex-end; }
    .input-inner textarea {
      flex: 1; border: 1px solid var(--border); border-radius: 12px;
      padding: 10px 14px; font-size: .925rem; font-family: inherit;
      resize: none; max-height: 160px; min-height: 44px; line-height: 1.45;
      outline: none; transition: border-color .15s;
    }
    .input-inner textarea:focus { border-color: var(--accent); }
    .send-btn {
      flex-shrink: 0; width: 40px; height: 40px; border-radius: 10px;
      background: var(--accent); color: #fff; border: none; cursor: pointer;
      display: flex; align-items: center; justify-content: center; transition: background .15s;
    }
    .send-btn:hover:not(:disabled) { background: #1d4ed8; }
    .send-btn:disabled { background: #9ca3af; cursor: not-allowed; }
    .send-btn svg { width: 16px; height: 16px; fill: currentColor; }
    .input-hint { font-size: .72rem; color: var(--muted); margin-top: 5px; text-align: center; }
  </style>
</head>
<body>
  <div class="header">
    <h1>&#128736; Atlantis</h1>
    <span class="pill">Windows Troubleshooter</span>
  </div>
  <div class="health-warn" id="health-warn"></div>

  <div class="chat-area" id="chat-area">
    <div class="messages" id="messages">
      <div class="msg assistant">
        <div class="msg-icon">A</div>
        <div class="bubble">
          Hey! Describe your Windows issue and I&#8217;ll diagnose it and suggest fixes.
          Hit &#9654; <strong>Run</strong> on any command to execute it directly.<br /><br />
          <span style="color:#6b7280;font-size:.85rem">Try: &#8220;my Wi-Fi keeps dropping&#8221; &middot; &#8220;PC is really slow&#8221; &middot; &#8220;install VS Code&#8221;</span>
        </div>
      </div>
    </div>
  </div>

  <div class="input-bar">
    <div class="input-inner">
      <textarea id="input" placeholder="Describe your Windows issue&#8230;" rows="1"></textarea>
      <button class="send-btn" id="send-btn" title="Send (Enter)">
        <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
      </button>
    </div>
    <div class="input-hint">Enter to send &middot; Shift+Enter for new line</div>
  </div>

  <script>
    let sessionId = null;
    let chatHistory = [];
    let busy = false;

    const messagesEl = document.getElementById("messages");
    const chatArea   = document.getElementById("chat-area");
    const inputEl    = document.getElementById("input");
    const sendBtn    = document.getElementById("send-btn");

    inputEl.addEventListener("input", () => {
      inputEl.style.height = "auto";
      inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
    });
    inputEl.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!busy) send(); }
    });
    sendBtn.addEventListener("click", () => { if (!busy) send(); });

    function scroll() { chatArea.scrollTop = chatArea.scrollHeight; }

    function formatInline(t) {
      // Bold: **text**
      t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      // Links
      t = t.replace(/(https?:\\/\\/[^\\s<>"]+)/g, u =>
        '<a href="' + u.replace(/"/g, "%22") + '" target="_blank" rel="noopener">' + u + "</a>");
      return t;
    }

    function renderMarkdown(text) {
      if (!text) return "";
      const lines = text.split("\\n");
      const parts = [];
      let inOl = false, inUl = false;

      const closeList = () => {
        if (inOl) { parts.push("</ol>"); inOl = false; }
        if (inUl) { parts.push("</ul>"); inUl = false; }
      };

      for (const line of lines) {
        const t = line.trim();
        const olMatch = t.match(/^(\\d+)\\. (.+)$/);
        const ulMatch = t.match(/^[-*] (.+)$/);

        if (olMatch) {
          if (inUl) { parts.push("</ul>"); inUl = false; }
          if (!inOl) { parts.push("<ol>"); inOl = true; }
          parts.push("<li>" + formatInline(olMatch[2]) + "</li>");
        } else if (ulMatch) {
          if (inOl) { parts.push("</ol>"); inOl = false; }
          if (!inUl) { parts.push("<ul>"); inUl = true; }
          parts.push("<li>" + formatInline(ulMatch[1]) + "</li>");
        } else {
          closeList();
          if (t) parts.push("<p>" + formatInline(t) + "</p>");
        }
      }
      closeList();
      return parts.join("");
    }

    let _scanPoll = null;
    function startScanStatusPolling(scanType) {
      if (_scanPoll) return;
      const bar = document.createElement("div"); bar.id = "scan-bar";
      bar.innerHTML = '<span class="spin">&#8635;</span> Antivirus ' + scanType + ' scan in progress…';
      document.querySelector(".header").insertAdjacentElement("afterend", bar);
      _scanPoll = setInterval(async () => {
        try {
          const r = await fetch("/api/scan-status");
          const d = await r.json();
          const s = d.status || {};
          const running = scanType === "quick" ? s.QuickScanInProgress : s.FullScanInProgress;
          if (!running) {
            clearInterval(_scanPoll); _scanPoll = null;
            bar.className = "done";
            bar.innerHTML = '✓ Antivirus ' + scanType + ' scan complete';
            setTimeout(() => bar.remove(), 4000);
          }
        } catch {}
      }, 6000);
    }

    function addFeedbackRow(sid) {
      const row = document.createElement("div"); row.className = "feedback-row"; row.id = "fb-" + sid;
      const lbl = document.createElement("span"); lbl.className = "fb-label"; lbl.textContent = "Did this fix it?";
      const yes = document.createElement("button"); yes.className = "fb-btn fb-yes"; yes.textContent = "✓ Fixed it";
      const no  = document.createElement("button"); no.className  = "fb-btn fb-no";  no.textContent  = "✗ Didn’t work";
      yes.addEventListener("click", () => sendFeedback(sid, true,  row));
      no.addEventListener ("click", () => sendFeedback(sid, false, row));
      row.appendChild(lbl); row.appendChild(yes); row.appendChild(no);
      messagesEl.appendChild(row); scroll();
    }

    async function sendFeedback(sid, worked, row) {
      try {
        await fetch("/api/session/" + encodeURIComponent(sid) + "/feedback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ worked })
        });
      } catch {}
      row.innerHTML = worked
        ? '<span class="fb-label" style="color:#4ade80;">✓ Saved as a confirmed fix — will inform future answers</span>'
        : '<span class="fb-label">– Noted, not saved</span>';
    }

    function addPriorSessionsCard(sessions) {
      if (!sessions || sessions.length === 0) return;
      const card = document.createElement("div"); card.className = "prior-card";
      const hdr = document.createElement("div"); hdr.className = "prior-card-hdr";
      hdr.textContent = "💡 Similar issue resolved before";
      card.appendChild(hdr);
      sessions.forEach(s => {
        const item = document.createElement("div"); item.className = "prior-item";
        if (s.problem) {
          const p = document.createElement("div"); p.className = "prior-prob";
          p.textContent = "📌 " + s.problem; item.appendChild(p);
        }
        if (s.summary) {
          const p = document.createElement("div"); p.className = "prior-sum";
          p.textContent = s.summary; item.appendChild(p);
        }
        if (s.commands) {
          const p = document.createElement("div"); p.className = "prior-cmds";
          p.textContent = s.commands; item.appendChild(p);
        }
        card.appendChild(item);
      });
      const typing = document.getElementById("typing");
      if (typing) messagesEl.insertBefore(card, typing);
      else messagesEl.appendChild(card);
      scroll();
    }

    function addUserBubble(text) {
      const row = document.createElement("div"); row.className = "msg user";
      const b = document.createElement("div"); b.className = "bubble"; b.textContent = text;
      row.appendChild(b); messagesEl.appendChild(row); scroll();
      chatHistory.push({ role: "user", text });
    }

    function addTyping() {
      const row = document.createElement("div"); row.className = "msg assistant"; row.id = "typing";
      const icon = document.createElement("div"); icon.className = "msg-icon"; icon.textContent = "A";
      const wrap = document.createElement("div"); wrap.style.cssText = "display:flex;flex-direction:column;gap:3px;";
      const t = document.createElement("div"); t.className = "typing";
      t.innerHTML = '<div class="dot"></div><div class="dot"></div><div class="dot"></div>';
      const lbl = document.createElement("div"); lbl.className = "step-label"; lbl.id = "typing-step"; lbl.textContent = "Analyzing…";
      wrap.appendChild(t); wrap.appendChild(lbl);
      row.appendChild(icon); row.appendChild(wrap); messagesEl.appendChild(row); scroll();
    }
    function updateTypingStep(text) {
      const el = document.getElementById("typing-step");
      if (el) el.textContent = text;
    }
    function removeTyping() { const el = document.getElementById("typing"); if (el) el.remove(); }

    function addAssistantBubble(plan) {
      const row = document.createElement("div"); row.className = "msg assistant";
      const icon = document.createElement("div"); icon.className = "msg-icon"; icon.textContent = "A";
      const bubble = document.createElement("div"); bubble.className = "bubble";

      const summary = (plan.summary || "").trim();
      const notes   = (plan.notes   || "").trim();

      if (summary) {
        const div = document.createElement("div"); div.innerHTML = renderMarkdown(summary); bubble.appendChild(div);
      }
      if (notes && notes !== summary) {
        const div = document.createElement("div");
        div.style.cssText = "margin-top:6px;font-size:.875rem;color:#374151;";
        div.innerHTML = renderMarkdown(notes); bubble.appendChild(div);
      }

      const cmds = Array.isArray(plan.commands) ? plan.commands : [];
      cmds.forEach(cmd => {
        const block = document.createElement("div"); block.className = "cmd-block";

        const hdr = document.createElement("div"); hdr.className = "cmd-header";
        const reason = document.createElement("span"); reason.className = "cmd-reason";
        reason.textContent = cmd.reason || "";
        const badges = document.createElement("div"); badges.className = "cmd-badges";
        const badge = document.createElement("span");
        badge.className = "rev-badge " + (cmd.reversible === true ? "yes" : "no");
        badge.textContent = cmd.reversible === true ? "reversible" : "irreversible";
        badges.appendChild(badge); hdr.appendChild(reason); hdr.appendChild(badges);

        const cmdRow = document.createElement("div"); cmdRow.className = "cmd-row";
        const ta = document.createElement("textarea"); ta.className = "cmd-edit";
        ta.value = cmd.text || ""; ta.readOnly = true; ta.spellcheck = false;
        const autoH = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; };
        ta.addEventListener("input", autoH);
        requestAnimationFrame(autoH);
        const editBtn = document.createElement("button"); editBtn.className = "edit-btn"; editBtn.textContent = "✏ Edit";
        editBtn.addEventListener("click", () => {
          if (ta.readOnly) {
            ta.readOnly = false; ta.classList.add("editing");
            editBtn.textContent = "✓ Done"; editBtn.classList.add("active");
            ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
          } else {
            ta.readOnly = true; ta.classList.remove("editing");
            editBtn.textContent = "✏ Edit"; editBtn.classList.remove("active");
            autoH();
          }
        });
        const output = document.createElement("div"); output.className = "cmd-output";
        const btn = document.createElement("button"); btn.className = "run-btn";
        btn.innerHTML = "&#9654; Run";
        btn.addEventListener("click", () => execCommand(btn, { ...cmd, text: ta.value.trim() }, output));
        cmdRow.appendChild(ta); cmdRow.appendChild(editBtn); cmdRow.appendChild(btn);

        block.appendChild(hdr); block.appendChild(cmdRow); block.appendChild(output);
        bubble.appendChild(block);
      });

      row.appendChild(icon); row.appendChild(bubble); messagesEl.appendChild(row); scroll();
      chatHistory.push({ role: "assistant", text: summary });
    }

    async function execCommand(btn, cmd, outputEl) {
      if (!sessionId) return;
      btn.disabled = true; btn.className = "run-btn running"; btn.textContent = "Running\u2026";
      outputEl.className = "cmd-output show"; outputEl.textContent = "Running\u2026";
      try {
        const resp = await fetch("/api/session/" + encodeURIComponent(sessionId) + "/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            problem: (chatHistory.find(m => m.role === "user") || {}).text || "",
            commands: [{ text: cmd.text, reason: cmd.reason || null, origin: cmd.origin || "agent" }]
          })
        });
        const data = await resp.json();
        const r = data.results && data.results[0];
        const ok = r ? (r.exit_code === 0 || r.exit_code === null) : !!data.success;
        const out = r ? ((r.stdout || "") + (r.stderr ? "\\n" + r.stderr : "")).trim()
                      : JSON.stringify(data, null, 2);
        btn.className = "run-btn " + (ok ? "ok" : "err");
        btn.innerHTML = ok ? "&#10003; Done" : "&#10007; Failed";
        outputEl.className = "cmd-output show " + (ok ? "ok" : "err");
        outputEl.textContent = out || (ok ? "Command completed successfully." : "Command failed with no output.");
      } catch (err) {
        btn.className = "run-btn err"; btn.innerHTML = "&#10007; Error";
        outputEl.className = "cmd-output show err";
        outputEl.textContent = "Request failed: " + err.message;
      }
      scroll();
    }

    async function ensureSession() {
      if (sessionId) return;
      const r = await fetch("/api/session", { method: "POST" });
      const d = await r.json();
      sessionId = d.session_id;
    }

    async function send() {
      const text = inputEl.value.trim();
      if (!text) return;
      inputEl.value = ""; inputEl.style.height = "auto";
      busy = true; sendBtn.disabled = true;
      addUserBubble(text); addTyping();
      let es = null;
      const done = () => { busy = false; sendBtn.disabled = false; inputEl.focus(); };
      try {
        await ensureSession();
        es = new EventSource("/api/session/" + encodeURIComponent(sessionId) + "/events");
        es.addEventListener("step", e => {
          updateTypingStep(e.data);
          const lower = e.data.toLowerCase();
          if (lower.includes("antivirus") && lower.includes("progress")) startScanStatusPolling("quick");
          if (lower.includes("antivirus") && lower.includes("full")) startScanStatusPolling("full");
        });
        es.addEventListener("prior_sessions", e => {
          let sessions;
          try { sessions = JSON.parse(e.data); } catch { return; }
          addPriorSessionsCard(sessions);
        });
        es.addEventListener("done", e => {
          es.close();
          removeTyping();
          let plan;
          try { plan = JSON.parse(e.data); } catch { plan = { summary: "No plan returned.", commands: [] }; }
          addAssistantBubble(plan);
          if (Array.isArray(plan.commands) && plan.commands.length > 0) addFeedbackRow(sessionId);
          done();
        });
        es.addEventListener("timeout", () => {
          es.close(); removeTyping();
          addAssistantBubble({ summary: "Request timed out \u2014 please try again.", commands: [] });
          done();
        });
        es.onerror = () => {
          es.close(); removeTyping();
          addAssistantBubble({ summary: "Something went wrong \u2014 please try again.", commands: [] });
          done();
        };
        await fetch("/api/session/" + encodeURIComponent(sessionId) + "/plan", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ problem: text, history: chatHistory })
        });
      } catch {
        if (es) es.close();
        removeTyping();
        addAssistantBubble({ summary: "Something went wrong \u2014 please try again.", commands: [] });
        done();
      }
    }

    (async () => {
      try {
        const r = await fetch("/api/system_ops/health");
        const d = await r.json();
        if (!d.ok) throw new Error();
      } catch {
        const el = document.getElementById("health-warn");
        el.textContent = "\u26a0 system_ops backend unreachable \u2014 make sure system_ops_server is running on port 8000.";
        el.style.display = "block";
      }
    })();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)




@frontend_app.post("/api/session")
async def create_session() -> JSONResponse:
    """
    Create a new logical troubleshooting session and return its ID.
    """
    session_id = str(uuid.uuid4())
    return JSONResponse({"session_id": session_id})


class ExecuteRequest(BaseModel):
    problem: str
    summary: Optional[str] = None
    notes: Optional[str] = None
    commands: List[Dict[str, Any]]


async def _run_plan_task(
    session_id: str,
    problem: str,
    feedback: Optional[str],
    history: Optional[List[Dict[str, str]]],
) -> None:
    await _emit(session_id, "step", "Checking past sessions…")
    prior = retrieve_similar(problem)
    if prior:
        await _emit(session_id, "prior_sessions", json.dumps(prior))
    await _emit(session_id, "step", "Analyzing your problem…")
    plan = await get_troubleshooting_plan(problem, feedback, history, prior_sessions=prior)
    _session_plans[session_id] = plan
    _session_problems[session_id] = problem
    await _emit(session_id, "done", json.dumps(plan))
    _session_events.pop(session_id, None)


@frontend_app.get("/api/session/{session_id}/events")
async def session_events(session_id: str) -> StreamingResponse:
    """SSE stream that delivers real-time step updates and the final plan."""
    q = _get_queue(session_id)

    async def generator():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=180.0)
                except asyncio.TimeoutError:
                    yield "event: timeout\ndata: {}\n\n"
                    break
                event = item.get("event", "message")
                data = item.get("data", "")
                yield f"event: {event}\ndata: {data}\n\n"
                if event == "done":
                    break
        finally:
            _session_events.pop(session_id, None)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ToolEventPayload(BaseModel):
    tool: str
    step: str


@frontend_app.post("/internal/tool-event")
async def internal_tool_event(payload: ToolEventPayload) -> JSONResponse:
    """Receives tool-call notifications from the MCP server and fans them to active sessions."""
    for q in list(_session_events.values()):
        await q.put({"event": "step", "data": payload.step})
    return JSONResponse({"ok": True})


class FeedbackRequest(BaseModel):
    worked: bool


@frontend_app.post("/api/session/{session_id}/feedback")
async def api_feedback(session_id: str, req: FeedbackRequest) -> JSONResponse:
    """If the user confirms the fix worked, embed and store the session in ChromaDB."""
    if req.worked:
        plan = _session_plans.get(session_id)
        problem = _session_problems.get(session_id)
        if plan and problem:
            store_session(
                session_id=session_id,
                problem=problem,
                summary=plan.get("summary", ""),
                commands=plan.get("commands", []),
                notes=plan.get("notes", ""),
            )
    _session_plans.pop(session_id, None)
    _session_problems.pop(session_id, None)
    return JSONResponse({"ok": True})


@frontend_app.post("/api/session/{session_id}/plan")
async def api_plan(session_id: str, req: PlanRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """Start the troubleshooting plan as a background task; progress is streamed via SSE."""
    background_tasks.add_task(_run_plan_task, session_id, req.problem, req.feedback, req.history)
    return JSONResponse({"session_id": session_id, "status": "planning"})


@frontend_app.post("/api/session/{session_id}/execute")
async def api_execute(session_id: str, req: ExecuteRequest) -> JSONResponse:
    """
    Execute selected commands via system_ops, logging under the given session_id.
    """
    session_metadata: Dict[str, Any] = {
        "problem": req.problem,
        "summary": req.summary,
        "notes": req.notes,
        "commands_count": len(req.commands),
    }
    exec_result = call_execute_powershell_commands(
        commands=req.commands,
        session_id=session_id,
        session_metadata=session_metadata,
    )
    return JSONResponse(exec_result)


@frontend_app.get("/api/rag/sessions")
async def rag_list() -> JSONResponse:
    """Return all confirmed fixes stored in ChromaDB."""
    return JSONResponse({"sessions": get_all_sessions(), "count": len(get_all_sessions())})


@frontend_app.delete("/api/rag/sessions/{session_id}")
async def rag_delete(session_id: str) -> JSONResponse:
    """Remove a specific session from the RAG store."""
    ok = delete_session(session_id)
    return JSONResponse({"ok": ok})


@frontend_app.get("/api/scan-status")
async def api_scan_status() -> JSONResponse:
    """Proxy to system_ops: returns current Defender scan in-progress status."""
    url = f"{SYSTEM_OPS_BASE_URL}/tools/get_defender_status"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=15) as resp:
            return JSONResponse(json.loads(resp.read().decode("utf-8")))
    except Exception as exc:
        return JSONResponse({"success": False, "error": str(exc)})


@frontend_app.get("/api/system_ops/health")
async def system_ops_health() -> JSONResponse:
    """
    Lightweight proxy to check whether the system_ops backend is reachable.
    """
    url = f"{SYSTEM_OPS_BASE_URL}/health"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return JSONResponse({"ok": False, "error": "Empty health response from system_ops."})
            try:
                data = json.loads(raw)
            except Exception:
                return JSONResponse({"ok": False, "error": "Malformed health response from system_ops."})
            ok = bool(data.get("ok", True))
            return JSONResponse({"ok": ok, "raw": data})
    except HTTPError as e:
        return JSONResponse(
            {"ok": False, "error": f"HTTPError contacting system_ops health: {e}"},
            status_code=502,
        )
    except URLError as e:
        return JSONResponse(
            {"ok": False, "error": f"URLError contacting system_ops health: {e}"},
            status_code=502,
        )


async def main() -> None:
    """
    Convenience entry point if you want to run this with `python frontend_server.py`.
    For normal usage, prefer `uv run uvicorn frontend_server:frontend_app --host 127.0.0.1 --port 9001`.
    """
    import uvicorn

    config = uvicorn.Config(
        "frontend_server:frontend_app",
        host="127.0.0.1",
        port=9001,
        reload=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
