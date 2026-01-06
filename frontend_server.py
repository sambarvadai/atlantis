from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from main import app as mcp_app, run_agent


SYSTEM_OPS_BASE_URL = os.getenv("SYSTEM_OPS_BASE_URL", "http://127.0.0.1:8000")


frontend_app = FastAPI(
    title="Windows Troubleshooter Frontend",
    description=(
        "Simple web frontend that ties together the MCP Tavily+Gemini agent "
        "with the system_ops FastAPI backend and MongoDB session logging."
    ),
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
You are a Windows troubleshooting assistant that can:
- Use the `system_ops` server to inspect and act on this local Windows machine
  (system overview, processes, services, Defender, performance, installs, etc.).
- Use the `tavily` server to search the web for compatibility information,
  error codes, known issues, and step-by-step fixes.

User problem:
{problem}
{extra_feedback}

First, prefer `system_ops` tools to gather local context or run built-in diagnostics
when helpful (e.g., get_system_overview, diagnose_network_issue,
diagnose_performance, full_system_health_check, get_gpu_info for GPU/driver questions).
Then, use Tavily search to enrich your understanding and find best-practice fixes.

If the user asks about drivers (GPU, Wi-Fi, chipset, or device-specific drivers), you must:
1) Call appropriate `system_ops` tools to detect the local hardware and OS (e.g., get_system_overview, get_gpu_info, diagnose_network_issue).
2) Call Tavily using that hardware/OS information to find official or reputable driver download pages.
3) Include at least one direct driver download URL in the "notes" field so the user can click through.
4) When you have a reliable direct download URL for an installer or driver, prefer calling the `download_file` tool from the `system_ops` server (over PowerShell download commands) to save the file into the user's Downloads folder with a sensible filename. Then, in your JSON plan, describe what was downloaded and where in the "notes" field, and propose any follow-up install commands separately.

Finally, respond with a single JSON object ONLY (no extra text) in this exact shape:
{{
  "summary": "<one-line summary of the issue and fix>",
  "commands": [
    {{
      "text": "PowerShell or winget/choco command to help fix the issue",
      "reason": "why this command is needed",
      "origin": "agent",
      "reversible": false,
      "rollback_notes": "If reversible=true, explain briefly how to undo this command. If false, explain why it is risky or hard to undo."
    }}
  ],
  "notes": "<additional notes or manual steps for the user>"
}}

Rules:
- For each command, set "reversible" to true ONLY if there is a clear, practical way to undo its effects; otherwise set it to false.
- If no commands are needed or you cannot safely propose any commands, return "commands": [] and explain in "notes".
- The \"text\" field for each command MUST be a command that can run directly in a Windows PowerShell terminal (e.g., PowerShell, winget, or choco). Do NOT return Python, C#, or HTTP API client code, and do NOT use function call syntax like default_api.run_defender_scan(...). Convert such calls into the underlying PowerShell or CLI commands instead. For file downloads, prefer using the `download_file` tool as described above rather than emitting raw PowerShell download commands.
- Do NOT wrap the JSON in backticks or add any commentary outside the JSON.
"""

    try:
        async with mcp_app.run() as agent_app:
            result_str = await run_agent(
                agent_name="windows_troubleshooter",
                prompt=prompt,
                app_ctx=agent_app.context,
            )
    except Exception as exc:
        # If the agent or MCP layer fails entirely, return a well-formed
        # fallback plan so the frontend can surface a clear error.
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
    """
    Serve a minimal single-page frontend for interactive troubleshooting.
    """
    html = """
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>Windows Troubleshooter</title>
    <style>
      body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 0; background: #f3f4f6; }
      .page { max-width: 900px; margin: 0 auto; padding: 16px; min-height: 100vh; display: flex; flex-direction: column; }
      textarea { width: 100%; min-height: 80px; }
      button { margin-top: 8px; padding: 6px 12px; }
      .section { margin-top: 20px; }
      .commands { margin-top: 10px; }
      .command-item { margin-bottom: 8px; border: 1px solid #ccc; padding: 6px; border-radius: 4px; }
      pre { background: #f6f6f6; padding: 8px; border-radius: 4px; max-height: 240px; overflow: auto; }
      .small { font-size: 0.85rem; color: #555; }
      .chat-container { border: 1px solid #ddd; border-radius: 12px; padding: 10px 12px; max-height: 420px; overflow-y: auto; background: #ffffff; }
      .chat-message { margin-bottom: 10px; display: flex; }
      .chat-message.user { justify-content: flex-end; }
      .chat-message.assistant { justify-content: flex-start; }
      .chat-bubble { max-width: 75%; padding: 8px 10px; border-radius: 12px; }
      .chat-bubble.user { background: #2563eb; color: #ffffff; border-bottom-right-radius: 2px; }
      .chat-bubble.assistant { background: #e5e7eb; color: #111827; border-bottom-left-radius: 2px; }
      .chat-role { font-weight: 600; display: block; margin-bottom: 2px; font-size: 0.8rem; opacity: 0.8; }
      .footer { margin-top: auto; padding-top: 16px; }
    </style>
  </head>
  <body>
    <div class="page">
      <h1>Windows Troubleshooter</h1>
      <div id="health-warning" class="small" style="display:none; color:#b91c1c; margin-bottom:6px;"></div>
      <p class="small">
        Describe your Windows issue in plain language and I’ll propose a plan with steps and safe commands.
        Type <code>help</code> or <code>commands</code> to see examples of what you can ask.
      </p>

      <div class="section">
        <div class="chat-container" id="chat-log"></div>
        <div id="status" class="small" style="display:none; margin-top:6px;"></div>
      </div>

      <div class="section">
        <label for="problem">Message:</label><br />
        <textarea id="problem" placeholder="Describe your Windows issue or ask what to do next..."></textarea>
        <br />
        <label for="feedback" class="small">Optional refinement for this turn:</label><br />
        <textarea id="feedback" placeholder="e.g. Prefer winget over choco; don't run full Defender scans"></textarea>
        <br />
        <button id="get-plan">Send</button>
      </div>

      <div class="section" id="session-info" style="display:none;">
        <strong>Session ID:</strong> <span id="session-id"></span>
      </div>

      <div class="section" id="plan-section" style="display:none;">
        <h2>Plan</h2>
        <p><strong>Summary:</strong> <span id="plan-summary"></span></p>
        <p><strong>Notes:</strong></p>
        <pre id="plan-notes"></pre>

        <div class="commands">
          <h3>Proposed commands</h3>
          <div id="commands-container"></div>
          <button id="run-commands">Run selected commands</button>
        </div>
      </div>

      <div class="section" id="result-section" style="display:none;">
        <h2>Execution result</h2>
        <pre id="exec-result"></pre>
      </div>

      <div class="footer small">
        <span>Tip: Treat this like a chat. Ask a question, review the plan, then choose which commands to run.</span>
      </div>
    </div>

    <script>
      let sessionId = null;
      let currentPlan = null;
      let chatHistory = [];

      function linkify(text) {
        const urlRegex = /(https?:\/\/[^\s]+)/g;
        return text.replace(urlRegex, function(url) {
          const safeUrl = url.replace(/"/g, "%22");
          return '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' + url + "</a>";
        });
      }

      function appendChatMessage(role, text) {
        const log = document.getElementById("chat-log");
        const div = document.createElement("div");
        div.className = "chat-message " + (role === "user" ? "user" : "assistant");

        const bubble = document.createElement("div");
        bubble.className = "chat-bubble " + (role === "user" ? "user" : "assistant");

        const who = document.createElement("span");
        who.className = "chat-role";
        who.textContent = role === "user" ? "You" : "Assistant";

        const body = document.createElement("div");
        if (role === "assistant") {
          body.innerHTML = linkify(text);
        } else {
          body.textContent = text;
        }

        bubble.appendChild(who);
        bubble.appendChild(body);
        div.appendChild(bubble);
        log.appendChild(div);
        log.scrollTop = log.scrollHeight;

        // Track history so the agent can see prior context
        chatHistory.push({ role, text });
      }

      function setStatus(text) {
        const el = document.getElementById("status");
        if (text) {
          el.textContent = text;
          el.style.display = "block";
        } else {
          el.textContent = "";
          el.style.display = "none";
        }
      }

      async function checkBackendHealth() {
        try {
          const resp = await fetch("/api/system_ops/health", { method: "GET" });
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          const data = await resp.json();
          if (!data.ok) {
            const msg = "system_ops backend reported it is unavailable. Please ensure system_ops_server is running.";
            const el = document.getElementById("health-warning");
            el.textContent = msg;
            el.style.display = "block";
          }
        } catch (err) {
          const msg = "Unable to reach system_ops backend. Make sure it is running on 127.0.0.1:8000.";
          const el = document.getElementById("health-warning");
          el.textContent = msg;
          el.style.display = "block";
        }
      }

      async function ensureSession() {
        if (sessionId) return sessionId;
        try {
          const resp = await fetch("/api/session", { method: "POST" });
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          const data = await resp.json();
          if (!data.session_id) {
            throw new Error("Malformed session response");
          }
          sessionId = data.session_id;
          document.getElementById("session-id").textContent = sessionId;
          document.getElementById("session-info").style.display = "block";
          return sessionId;
        } catch (err) {
          appendChatMessage(
            "assistant",
            "Unable to create a troubleshooting session. Please refresh the page and try again."
          );
          throw err;
        }
      }

      document.getElementById("get-plan").addEventListener("click", async () => {
        const problem = document.getElementById("problem").value.trim();
        const feedback = document.getElementById("feedback").value.trim();
        if (!problem) {
          alert("Please describe your Windows issue first.");
          return;
        }

        const norm = problem.toLowerCase().trim();
        if (
          norm === "help" ||
          norm === "/help" ||
          norm === "commands" ||
          norm.indexOf("list commands") !== -1 ||
          norm.indexOf("what can you do") !== -1 ||
          norm.indexOf("what can i ask") !== -1
        ) {
          appendChatMessage("user", problem);
          const helpText = [
            "Here are some example things you can ask:",
            "- Show my system specs or a system health overview.",
            "- Diagnose my network or slow Wi-Fi.",
            "- Check why my PC is slow (disk space, top CPU/RAM processes).",
            "- Run a quick Defender scan or scan a specific folder.",
            "- Install or uninstall apps (e.g., Chrome, VS Code, VLC).",
            "- List running processes or restart a Windows service.",
            "- Troubleshoot a specific error code (e.g., a game crash or 0xC0000005).",
            "",
            "I will propose a plan and one or more commands. You can review and choose which commands to run."
          ].join("\\n");
          appendChatMessage("assistant", helpText);
          return;
        }

        try {
          await ensureSession();
        } catch {
          // Session creation already surfaced a user-friendly message.
          return;
        }
        appendChatMessage("user", problem);
        setStatus("Processing request (planning)...");
        document.getElementById("get-plan").disabled = true;
        document.getElementById("run-commands").disabled = true;

        try {
          const resp = await fetch(`/api/session/${encodeURIComponent(sessionId)}/plan`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ problem, feedback: feedback || null, history: chatHistory })
          });
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          const data = await resp.json();
          if (!data || typeof data.plan !== "object") {
            throw new Error("Malformed plan response");
          }

          currentPlan = data.plan || {};
          const summary = currentPlan.summary || "";
          const notes = currentPlan.notes || "";
          appendChatMessage("assistant", summary || "(No summary provided)");
          if (notes) {
            appendChatMessage("assistant", "Notes: " + notes);
          }

          document.getElementById("plan-summary").textContent = summary;
          document.getElementById("plan-notes").textContent = notes || "(No additional notes.)";
          document.getElementById("plan-section").style.display = "block";

          const container = document.getElementById("commands-container");
          container.innerHTML = "";
          const cmds = Array.isArray(currentPlan.commands) ? currentPlan.commands : [];
          if (!cmds.length) {
            container.textContent = "No commands proposed.";
          } else {
            cmds.forEach((cmd, idx) => {
              const div = document.createElement("div");
              div.className = "command-item";
              const checkbox = document.createElement("input");
              checkbox.type = "checkbox";
              checkbox.id = "cmd-" + idx;
              checkbox.checked = true;
              const label = document.createElement("label");
              label.htmlFor = checkbox.id;
              label.innerHTML = `<code>${cmd.text || ""}</code><br />
                <span class="small">Reason: ${cmd.reason || ""}</span><br />
                <span class="small">Reversible: ${
                  cmd.reversible === true ? "yes" : cmd.reversible === false ? "no" : "unknown"
                }</span><br />
                <span class="small">Rollback notes: ${cmd.rollback_notes || ""}</span>`;
              div.appendChild(checkbox);
              div.appendChild(label);
              container.appendChild(div);
            });
          }
        } catch (err) {
          appendChatMessage(
            "assistant",
            "I couldn't generate a troubleshooting plan right now. Please try again, and check that the MCP agent is running."
          );
          document.getElementById("plan-section").style.display = "none";
        } finally {
          setStatus("");
          document.getElementById("get-plan").disabled = false;
          document.getElementById("run-commands").disabled = false;
        }
      });

      document.getElementById("run-commands").addEventListener("click", async () => {
        if (!currentPlan) {
          alert("No plan available yet.");
          return;
        }
        if (!sessionId) {
          alert("Session not initialized.");
          return;
        }

        const problem = document.getElementById("problem").value.trim();
        const cmds = currentPlan.commands || [];
        const selected = [];
        cmds.forEach((cmd, idx) => {
          const cb = document.getElementById("cmd-" + idx);
          if (cb && cb.checked) {
            selected.push({
              text: cmd.text || "",
              reason: cmd.reason || null,
              origin: cmd.origin || "agent"
            });
          }
        });

        if (!selected.length) {
          alert("No commands selected.");
          return;
        }

        const payload = {
          problem: problem,
          summary: currentPlan.summary || "",
          notes: currentPlan.notes || "",
          commands: selected
        };

        appendChatMessage("assistant", "Executing selected commands via system_ops...");
        setStatus("Processing request (executing commands)...");
        document.getElementById("get-plan").disabled = true;
        document.getElementById("run-commands").disabled = true;

        try {
          const resp = await fetch(`/api/session/${encodeURIComponent(sessionId)}/execute`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
          });
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          const data = await resp.json();
          document.getElementById("exec-result").textContent = JSON.stringify(data, null, 2);
          document.getElementById("result-section").style.display = "block";
          const total = (data.results && data.results.length) || selected.length;
          const ok = (data.results || []).filter(r => r.exit_code === 0 || r.exit_code === null).length;
          if (data.success) {
            appendChatMessage(
              "assistant",
              `Finished executing commands (${ok}/${total} succeeded). See details below.`
            );
          } else {
            appendChatMessage(
              "assistant",
              `Some commands failed (${ok}/${total} succeeded). Check the execution result below for details.`
            );
          }
        } catch (err) {
          const summary = {
            success: false,
            error: "Failed to execute commands via system_ops."
          };
          document.getElementById("exec-result").textContent = JSON.stringify(summary, null, 2);
          document.getElementById("result-section").style.display = "block";
          appendChatMessage(
            "assistant",
            "I couldn't execute the selected commands. Please make sure the system_ops backend is running and try again."
          );
        } finally {
          setStatus("");
          document.getElementById("get-plan").disabled = false;
          document.getElementById("run-commands").disabled = false;
        }
      });

      // Kick off a lightweight backend health check once the page is loaded.
      checkBackendHealth();
    </script>
  </body>
</html>
    """
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
