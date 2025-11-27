# Troubleshooter MCP Agent - v 0.1

| Version | Features |
|--------|----------|
| 0.1    |    Basic windows operations, Tavily search integration for bug fixes. |

This project is a Windows troubleshooting agent that combines:

- A local **system operations server** (`system_ops_server.py`) that exposes safe, structured tools for inspecting and acting on a Windows machine.
- An **MCP app** (`main.py` with `mcp-agent`) that runs a `windows_troubleshooter` agent using **Gemini** and **Tavily**.
- A **chat-style web frontend** (`frontend_server.py`) where users describe issues, review a plan, approve commands, and see execution results.
- **MongoDB-backed session logging** so every approved command run is auditable by `session_id`.

The architecture is designed to keep all real system changes **local and controlled**:
LLM + web search propose commands, but nothing runs without explicit user approval.

## Requirements

- Windows 10/11 (PowerShell and Windows tools like Defender, `Get-Process`, `Get-Service`).
- Python 3.13
- [`uv`](https://github.com/astral-sh/uv) for dependency management.
- Optional: `winget` and/or Chocolatey for app install/uninstall helpers.
- Optional: MongoDB Atlas (or any MongoDB) for session logging.

## Installation

Clone this repository and install dependencies using `uv`:

```bash
uv sync
```

This reads `pyproject.toml` and creates/updates the virtual environment with:

- `mcp-agent[google]`
- `fastapi`, `uvicorn`
- `pymongo` (MongoDB)
- `python-dotenv` (for `.env`)

## Required configuration

### 1. Gemini / Google API key

The MCP app uses Gemini via `GoogleAugmentedLLM`. You can configure the API key either:

**Option A: `mcp_agent.secrets.yaml`**

Edit `mcp_agent.secrets.yaml` and set:

```yaml
google:
  api_key: "<YOUR_GOOGLE_API_KEY>"
```

**Option B: environment variable**

Set `GOOGLE_API_KEY` in your shell before running anything (PowerShell example):

```powershell
$env:GOOGLE_API_KEY = "<YOUR_GOOGLE_API_KEY>"
```

If neither is set, any flows requiring the `windows_troubleshooter` agent will fail.

### 2. Tavily API key

Tavily is used for web troubleshooting (error codes, driver pages, known issues). The Tavily MCP server is configured in `mcp_agent.config.yaml` under `mcp.servers.tavily`.

Update the URL to include **your own** Tavily API key:

```yaml
mcp:
  servers:
    tavily:
      transport: stdio
      command: npx
      args:
        - -y
        - mcp-remote
        - "https://mcp.tavily.com/mcp/?tavilyApiKey=<YOUR_TAVILY_API_KEY>"
```

Without a valid Tavily key, the agent can still use local `system_ops` tools, but web-powered troubleshooting will fail.

### 3. MongoDB (optional, for session logging)

MongoDB is used only for **optional session logging** by `system_ops_server.py`. If you skip this, the troubleshooting flow still works; it just won’t persist sessions to Mongo.

You can configure MongoDB via `.env` (recommended) or environment variables.

**Option A: `.env` file in project root**

Create a file named `.env` alongside `system_ops_server.py`, based on `.env.example`:

```dotenv
MONGODB_URI=mongodb+srv://user:password@cluster0.example.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB=windows_troubleshooter
MONGODB_SESSION_COLLECTION=sessions
```

`system_ops_server.py` uses `python-dotenv` to load this automatically on startup.

**Option B: environment variables**

Set them in your shell (PowerShell example):

```powershell
$env:MONGODB_URI = "mongodb+srv://user:password@cluster0.example.mongodb.net/?retryWrites=true&w=majority"
$env:MONGODB_DB = "windows_troubleshooter"
$env:MONGODB_SESSION_COLLECTION = "sessions"
```

Only `MONGODB_URI` is required; the others default to `windows_troubleshooter` and `sessions`.
If `MONGODB_URI` is missing or invalid, Mongo logging is silently disabled and the APIs still work.

## Running the project

### One-command launcher (recommended)

After `uv sync` and setting your keys/env vars, you can start everything with:

```bash
uv run python start_all.py
```

This will:

- Start the system operations HTTP backend:
  - `system_ops_server:app` on `127.0.0.1:8000`
- Start the MCP `system_ops` server (streamable_http):
  - `system_ops_mcp_server:app` on `127.0.0.1:8001`
- Start the chat frontend:
  - `frontend_server:frontend_app` on `127.0.0.1:9001`
- Attempt to open `http://127.0.0.1:9001/` in your default browser.

Press `Ctrl+C` in the terminal to stop all servers.

### Manual startup (if you prefer separate terminals)

Terminal 1 – system_ops HTTP backend:

```bash
uv run uvicorn system_ops_server:app --host 127.0.0.1 --port 8000
```

Terminal 2 – system_ops MCP server:

```bash
uv run uvicorn system_ops_mcp_server:app --host 127.0.0.1 --port 8001
```

Ensure `mcp_agent.config.yaml` has:

```yaml
mcp:
  servers:
    system_ops:
      transport: streamable_http
      url: "http://localhost:8001/mcp"
      http_timeout_seconds: 30
      read_timeout_seconds: 120
```

Terminal 3 – chat frontend:

```bash
uv run uvicorn frontend_server:frontend_app --host 127.0.0.1 --port 9001
```

Then open:

```text
http://127.0.0.1:9001/
```

## How it works (high level)

- **Chat UI (browser)**  
  - You describe a Windows issue or ask a question.  
  - The UI sends your message to the frontend server and shows a chat-like transcript.

- **Frontend server (`frontend_server.py`)**  
  - For each message, it calls the `windows_troubleshooter` agent via the MCP app (`main.py` + `mcp-agent`).  
  - Receives a structured plan: `{ summary, commands, notes }`.  
  - Renders the summary/notes in the chat and shows proposed commands with checkboxes.
  - When you click “Run selected commands”, it calls `/tools/execute_powershell_commands` on `system_ops` with `confirmed=true` and a `session_id`.

- **MCP app (`main.py`, `mcp_agent.config.yaml`)**  
  - Defines agents in `mcp_agent.config.yaml`, including `windows_troubleshooter`.  
  - `windows_troubleshooter` can use tools from:
    - `system_ops` MCP server (local diagnostics and actions).  
    - `tavily` MCP server (web search).  
  - Gemini chooses which tools to call (e.g., `get_system_overview`, `diagnose_network_issue`, `get_gpu_info` + Tavily search) and returns a JSON plan.

- **system_ops HTTP backend (`system_ops_server.py`)**  
  - Implements Windows tools via PowerShell:
    - System info / health: `get_system_overview`, `diagnose_performance`, `full_system_health_check`.  
    - Network: `diagnose_network_issue`.  
    - Event logs: `get_recent_event_logs`.  
    - Security: `run_defender_scan`.  
    - Processes/services: `list_processes`, `kill_process`, `list_services`, `control_service`.  
    - Apps: `install_app`, `uninstall_app`.  
    - GPU: `get_gpu_info` (basic video controller info).  
    - Generic: `execute_powershell_commands` (confirmed-only, fully logged).
  - When `/tools/execute_powershell_commands` runs, it logs each execution to MongoDB (if configured) keyed by `session_id`.

- **system_ops MCP server (`system_ops_mcp_server.py`)**  
  - Wraps the same Python implementations as MCP tools over `streamable_http` at `/mcp`.  
  - Lets the `windows_troubleshooter` agent call `system_ops` helpers via MCP instead of raw HTTP.

## Environment & safety notes

- Designed for **Windows 10/11**; many tools rely on PowerShell and Windows Defender.  
- `run_defender_scan` and some commands may require running `uvicorn` in an elevated PowerShell (Run as Administrator).  
- `install_app` / `uninstall_app` rely on `winget` or Chocolatey where available.  
- The agent never runs arbitrary commands by itself:
  - It only **proposes** commands.  
  - The user selects which commands to run.  
  - `execute_powershell_commands` checks `confirmed=true` and logs every execution.

## Tech stack

- **Language:** Python 3.13  
- **Web framework:** FastAPI + Uvicorn  
- **Agent framework:** `mcp-agent` (MCPApp, AgentSpec, workflows)  
- **LLM:** Google Gemini via `GoogleAugmentedLLM`  
- **MCP:** `system_ops` via `streamable_http`, Tavily via remote MCP (`mcp-remote`)  
- **Windows ops:** PowerShell + WMI (`Get-CimInstance`, `Get-Process`, `Get-Service`, `Start-MpScan`, `winget`/Chocolatey)  
- **Data & config:** MongoDB Atlas (`pymongo`), `python-dotenv` for `.env`  

## Summary for reviewers

- Run `uv sync`.  
- Set your **Google API key** and **Tavily API key**.  
- (Optionally) set `MONGODB_URI` in `.env` for session logging.  
- Start everything with `uv run python start_all.py`.  
- Open `http://127.0.0.1:9001/`, describe a Windows issue, review the plan, and choose which commands to run.  

All secrets (LLM keys, Tavily key, Mongo URI) are expected to be provided locally and should **not** be committed with real values.

Link to Demo: https://drive.google.com/file/d/1OXKLV2hztSKGuc6rolAoLuyiz4VjTwMf/view?usp=sharing