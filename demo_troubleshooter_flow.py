from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from main import app, run_agent


SYSTEM_OPS_BASE_URL = "http://127.0.0.1:8000"


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


async def get_troubleshooting_plan(
    agent_ctx, problem: str, feedback: Optional[str] = None
) -> Dict[str, Any]:
    """
    Ask the tavily_helper agent to produce a JSON troubleshooting plan
    with proposed PowerShell commands.
    """
    extra_feedback = (
        f"\nThe user rejected the previous plan because: {feedback}\n"
        if feedback
        else ""
    )

    prompt = f"""
You are a Windows troubleshooting assistant using Tavily search.

User problem:
{problem}
{extra_feedback}

Use Tavily to research likely causes and fixes for this problem.

Then respond with a single JSON object ONLY (no extra text) in this exact shape:
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
- The "text" field for each command MUST be a command that can run directly in a Windows PowerShell terminal (e.g., PowerShell, winget, or choco). Do NOT return Python, C#, or HTTP API client code, and do NOT use function call syntax like default_api.run_defender_scan(...). Convert such calls into the underlying PowerShell or CLI commands instead.
- Do NOT wrap the JSON in backticks or add any commentary outside the JSON.
"""

    result_str = await run_agent(
        agent_name="tavily_helper",
        prompt=prompt,
        app_ctx=agent_ctx,
    )

    # Some models wrap JSON in Markdown fences (``` or ```json). Strip them if present.
    raw = result_str.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        # Drop first fence line (``` or ```json)
        if lines:
            lines = lines[1:]
        # Drop trailing fence line if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        plan = json.loads(raw)
        if not isinstance(plan, dict):
            raise ValueError("Top-level JSON is not an object")
        return plan
    except Exception:
        # If parsing fails, fall back to a minimal structure so we can at least show the raw text.
        return {
            "summary": "Failed to parse JSON plan from agent.",
            "commands": [],
            "notes": result_str,
        }


def show_plan(plan: Dict[str, Any]) -> None:
    summary = plan.get("summary") or ""
    commands = plan.get("commands") or []
    notes = plan.get("notes") or ""

    print("\n=== Troubleshooting Plan ===")
    if summary:
        print("Summary:", summary)
    print()

    if commands:
        print("Proposed commands:")
        for idx, cmd in enumerate(commands, start=1):
            text = cmd.get("text") or ""
            reason = cmd.get("reason") or ""
            reversible = cmd.get("reversible")
            rollback_notes = cmd.get("rollback_notes") or ""
            print(f"  [{idx}] {text}")
            if reason:
                print(f"       Reason: {reason}")
            if reversible is True:
                print("       Reversible: yes (agent believes this can be undone)")
            elif reversible is False:
                print("       Reversible: no (no automatic rollback available)")
            if rollback_notes:
                print(f"       Rollback notes: {rollback_notes}")
    else:
        print("No commands proposed.")

    if notes:
        print("\nNotes:")
        print(notes)


async def main_async() -> None:
    if len(sys.argv) > 1:
        problem = " ".join(sys.argv[1:])
    else:
        problem = input("Describe your Windows issue in natural language: ").strip()
        if not problem:
            print("No problem description provided. Exiting.")
            return

    print("Starting troubleshooting flow using Tavily + system_ops...")
    print("Make sure system_ops is running, e.g.:")
    print("  uv run uvicorn system_ops_server:app --host 127.0.0.1 --port 8000")

    feedback: Optional[str] = None
    session_id = str(uuid.uuid4())

    async with app.run() as agent_app:
        for iteration in range(2):  # up to 2 refinement rounds
            plan = await get_troubleshooting_plan(agent_app.context, problem, feedback)
            show_plan(plan)

            commands = plan.get("commands") or []
            if not commands:
                print("\nThe agent did not propose any commands to run.")
                refine = input(
                    "Do you want to refine the search or specify where/how to look for commands? [y/N]: "
                ).strip().lower()
                if refine == "y":
                    feedback = input(
                        "Describe what should change (e.g., 'look for PowerShell commands on StackOverflow', "
                        "'prefer winget over choco', or any other guidance): "
                    ).strip()
                    if not feedback:
                        print("No additional guidance provided. Stopping without executing any commands.")
                        break
                    # Loop will continue with updated feedback
                    continue
                else:
                    print("Stopping without executing any commands.")
                    break

            choice = input(
                "\nDo you want to run all proposed commands? [y/N]: "
            ).strip().lower()
            if choice == "y":
                # Prepare commands for the execute_powershell_commands API
                exec_commands: List[Dict[str, Any]] = []
                for cmd in commands:
                    exec_commands.append(
                        {
                            "text": cmd.get("text") or "",
                            "reason": cmd.get("reason"),
                            "origin": cmd.get("origin") or "agent",
                        }
                    )

                session_metadata: Dict[str, Any] = {
                    "problem": problem,
                    "summary": plan.get("summary"),
                    "notes": plan.get("notes"),
                    "commands_count": len(exec_commands),
                }

                print("\nExecuting approved commands via system_ops...")
                exec_result = call_execute_powershell_commands(
                    exec_commands,
                    session_id=session_id,
                    session_metadata=session_metadata,
                )

                print("\n=== Execution Result ===")
                print(json.dumps(exec_result, indent=2))
                break
            else:
                # Ask for feedback and refine the plan
                feedback = input(
                    "Got it. What is wrong with this plan, or what should be changed? "
                    "(Press Enter to stop refining): "
                ).strip()
                if not feedback:
                    print("Stopping without executing any commands.")
                    break


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")


if __name__ == "__main__":
    main()
