# Admin-Focused Action Items

- Define two log agents: a non-admin **Log Reader Agent** and an **Admin Log Agent** that is only invoked for elevated checks.
- Tag backend tools as `normal` vs `admin_required` and wire them into the two agents via `mcp_agent.config.yaml`.
- Implement event-log–centric analysis tools (Application/System logs) for the non-admin agent.
- Implement Security/Defender/other admin-only log analysis tools for the Admin agent, with clear "requires elevation" handling.
- Add a `locate_app_logs` helper that combines Tavily search hints with local filesystem probing, and marks paths that need admin.
- Update the troubleshooting workflow so the main `windows_troubleshooter` agent proposes explicit plan steps when the Admin Log Agent is needed.
- Design a redaction layer (`redact_log_text`) that strips obvious identifiers and secrets (emails, usernames, IPs, hostnames, tokens, PII-like patterns) before any log snippets are sent to Gemini.
- Add a configurable `LOG_SHARING_MODE` (e.g., `off`, `summary_only`, `limited_snippets`) so orgs can restrict how much log content ever leaves the machine.
- Default the system to a "summary-only" mode where Gemini sees aggregated stats and anonymized samples, with raw log sharing as an explicit, opt-in setting.
- Capture a future-phase task to support "LLM-visible vs local-only tools" and/or on-device inference once there is capacity to run local models.
- Add search grounding: use Tavily (or another MCP search provider) to propose likely log locations and external error references, then verify candidates via local filesystem checks before trusting results.

## 2026-01-05 – Troubleshooter UX & infra

- Hardened frontend error handling and status messages for planning and command execution; added a backend health proxy endpoint and warning banner.
- Added timeouts and safer failure handling to `run_powershell`, plus a `/health` endpoint and `logging_enabled` flag in `system_ops_server.py`.
- Made `system_ops_server`, `system_ops_mcp_server`, and `frontend_server` ports dynamic in `start_all.py`, updating `SYSTEM_OPS_BASE_URL` and `mcp_agent.config.yaml` automatically.
- Extended `system_ops` with a Python-based `download_file` tool (HTTP endpoint + MCP tool) to download installers/drivers without PowerShell.
- Updated the `windows_troubleshooter` prompt so agents prefer the `download_file` backend tool for driver/app downloads and clarified Tavily + system_ops usage.
- Investigated Gemini 429 errors and confirmed free-tier quotas (20 requests/day and input-token caps) as the cause; left a path open to swap to other LLM providers if needed.
