from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from main import app as mcp_app, run_agent


SYSTEM_OPS_BASE_URL = os.getenv("SYSTEM_OPS_BASE_URL", "http://127.0.0.1:8000")

_agent_ctx = None


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

    prompt = f"""
User problem: {problem}
{extra_feedback}
For driver questions (GPU, Wi-Fi, chipset, etc.):
1) Use `system_ops` tools to detect the exact hardware model and OS.
2) Use Tavily with that info to find official driver download pages.
3) Include at least one direct download URL in "notes".
4) Prefer the `download_file` tool to save installers to the Downloads folder, then describe the download in "notes".

Respond with a single JSON object ONLY (no extra text, no backticks):
{{
  "summary": "<one-line summary of the issue and fix>",
  "commands": [
    {{
      "text": "PowerShell, winget, or choco command",
      "reason": "why this command is needed",
      "origin": "agent",
      "reversible": false,
      "rollback_notes": "how to undo if reversible=true, or why it is risky if false"
    }}
  ],
  "notes": "<additional notes or manual steps>"
}}

Rules:
- Commands must run directly in a Windows PowerShell terminal. No Python, C#, or HTTP API syntax.
- Set "reversible": true only if there is a clear, practical way to undo the effect.
- If no commands are needed, return "commands": [] and explain in "notes".
"""

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
    /* command block */
    .cmd-block { margin-top: 10px; border-radius: 10px; overflow: hidden; border: 1px solid #313244; background: var(--code-bg); }
    .cmd-header { display: flex; align-items: center; justify-content: space-between; padding: 7px 12px; background: #181825; gap: 8px; }
    .cmd-reason { font-size: .78rem; color: #a6adc8; flex: 1; }
    .cmd-badges { display: flex; gap: 4px; align-items: center; flex-shrink: 0; }
    .rev-badge { font-size: .68rem; padding: 2px 7px; border-radius: 99px; }
    .rev-badge.yes { background: #1e3a2f; color: #4ade80; }
    .rev-badge.no  { background: #3b1f1f; color: #f87171; }
    .cmd-row { display: flex; align-items: center; padding: 10px 12px; gap: 12px; }
    .cmd-code { flex: 1; font-family: "Cascadia Code","Fira Code",Consolas,monospace; font-size: .83rem; color: var(--code-text); white-space: pre-wrap; word-break: break-all; }
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

    function linkify(t) {
      return t.replace(/(https?:\\/\\/[^\\s<>"]+)/g, u =>
        '<a href="' + u.replace(/"/g, "%22") + '" target="_blank" rel="noopener">' + u + "</a>");
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
      const t = document.createElement("div"); t.className = "typing";
      t.innerHTML = '<div class="dot"></div><div class="dot"></div><div class="dot"></div>';
      row.appendChild(icon); row.appendChild(t); messagesEl.appendChild(row); scroll();
    }
    function removeTyping() { const el = document.getElementById("typing"); if (el) el.remove(); }

    function addAssistantBubble(plan) {
      const row = document.createElement("div"); row.className = "msg assistant";
      const icon = document.createElement("div"); icon.className = "msg-icon"; icon.textContent = "A";
      const bubble = document.createElement("div"); bubble.className = "bubble";

      const summary = (plan.summary || "").trim();
      const notes   = (plan.notes   || "").trim();

      if (summary) {
        const p = document.createElement("p"); p.innerHTML = linkify(summary); bubble.appendChild(p);
      }
      if (notes && notes !== summary) {
        const p = document.createElement("p");
        p.style.cssText = "margin-top:6px;font-size:.875rem;color:#374151;";
        p.innerHTML = linkify(notes); bubble.appendChild(p);
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
        const code = document.createElement("span"); code.className = "cmd-code"; code.textContent = cmd.text || "";
        const output = document.createElement("div"); output.className = "cmd-output";
        const btn = document.createElement("button"); btn.className = "run-btn";
        btn.innerHTML = "&#9654; Run";
        btn.addEventListener("click", () => execCommand(btn, cmd, output));
        cmdRow.appendChild(code); cmdRow.appendChild(btn);

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
      try {
        await ensureSession();
        const resp = await fetch("/api/session/" + encodeURIComponent(sessionId) + "/plan", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ problem: text, history: chatHistory })
        });
        const data = await resp.json();
        removeTyping();
        addAssistantBubble(data.plan || { summary: "No plan returned.", commands: [] });
      } catch {
        removeTyping();
        addAssistantBubble({ summary: "Something went wrong \u2014 please try again.", commands: [] });
      } finally {
        busy = false; sendBtn.disabled = false; inputEl.focus();
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


@frontend_app.post("/api/session/{session_id}/plan")
async def api_plan(session_id: str, req: PlanRequest) -> JSONResponse:
    """
    Generate a troubleshooting plan for the given problem (and optional feedback/history)
    using the windows_troubleshooter agent (Gemini + system_ops + Tavily).
    """
    plan = await get_troubleshooting_plan(
        problem=req.problem,
        feedback=req.feedback,
        history=req.history,
    )
    return JSONResponse(
        {
            "session_id": session_id,
            "plan": plan,
        }
    )


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
