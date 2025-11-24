from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

import mcp.types as mcp_types
from mcp.server import Server as MCPServer
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.responses import PlainTextResponse

import system_ops_server as ops


#
# MCP Server definition for `system_ops`
#

mcp_server = MCPServer(
    name="system_ops",
    version="0.1.0",
    instructions=(
        "Windows system operations tools. "
        "Use these tools to inspect and act on the local Windows machine: "
        "system overview, event logs, network diagnostics, performance checks, "
        "Defender scans, process/service control, app installs, and controlled "
        "PowerShell command execution."
    ),
)


def _schema_from_model(model_cls: type[ops.BaseModel]) -> Dict[str, Any]:  # type: ignore[type-arg]
    """
    Helper to convert a Pydantic model into a JSON Schema suitable for MCP Tool.inputSchema.
    """
    return model_cls.model_json_schema()  # type: ignore[no-any-return]


@mcp_server.list_tools()
async def list_tools() -> List[mcp_types.Tool]:
    """
    Advertise system_ops tools to MCP clients.
    """
    tools: List[mcp_types.Tool] = []

    # Simple, no-input tools
    empty_input: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    tools.append(
        mcp_types.Tool(
            name="get_system_overview",
            description="Get OS, CPU, and disk info for this Windows machine.",
            inputSchema=empty_input,
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="diagnose_network_issue",
            description="Run basic network diagnostics and infer a probable cause.",
            inputSchema=empty_input,
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="diagnose_performance",
            description=(
                "Diagnose disk and performance issues: low disk space and top CPU/RAM consumers."
            ),
            inputSchema=empty_input,
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="get_gpu_info",
            description="Get GPU / video controller info (similar to dxdiag display tab).",
            inputSchema=empty_input,
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="full_system_health_check",
            description=(
                "Run a one-shot health check combining system overview, network diagnostics, "
                "and recent application error logs."
            ),
            inputSchema=empty_input,
            outputSchema={"type": "object"},
        )
    )

    # Tools with structured input
    tools.append(
        mcp_types.Tool(
            name="get_recent_event_logs",
            description=(
                "Fetch recent Windows Event Log entries for a given log, severity, and time window."
            ),
            inputSchema=_schema_from_model(ops.EventLogsRequest),
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="install_app",
            description=(
                "Install an application using winget or Chocolatey. "
                "Requires appropriate confirmation flags for safety."
            ),
            inputSchema=_schema_from_model(ops.InstallAppRequest),
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="uninstall_app",
            description=(
                "Uninstall an application using winget or Chocolatey. "
                "Requires confirmed=true for safety."
            ),
            inputSchema=_schema_from_model(ops.UninstallAppRequest),
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="run_defender_scan",
            description="Trigger a Windows Defender scan (quick, full, or path).",
            inputSchema=_schema_from_model(ops.DefenderScanRequest),
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="list_processes",
            description="List running processes with basic CPU and memory information.",
            inputSchema=empty_input,
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="kill_process",
            description="Terminate a process by PID or name (requires confirmed=true).",
            inputSchema=_schema_from_model(ops.KillProcessRequest),
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="list_services",
            description="List Windows services and their status.",
            inputSchema=empty_input,
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="control_service",
            description="Start, stop, or restart a Windows service (requires confirmed=true).",
            inputSchema=_schema_from_model(ops.ControlServiceRequest),
            outputSchema={"type": "object"},
        )
    )

    tools.append(
        mcp_types.Tool(
            name="execute_powershell_commands",
            description=(
                "Execute one or more PowerShell commands on this machine, only when confirmed=true. "
                "Designed for explicit, human-approved fixes; all executions are logged."
            ),
            inputSchema=_schema_from_model(ops.ExecuteCommandsRequest),
            outputSchema={"type": "object"},
        )
    )

    return tools


@mcp_server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dispatch MCP tool calls to the underlying system_ops implementations.
    """

    # Helper to normalize pydantic models to plain dicts
    def _to_dict(value: Any) -> Dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump()  # type: ignore[no-any-return]
        if isinstance(value, dict):
            return value
        # Fallback: wrap as-is
        return {"result": value}

    if name == "get_system_overview":
        return _to_dict(ops.get_system_overview_impl())

    if name == "get_recent_event_logs":
        payload = ops.EventLogsRequest(**arguments)
        return _to_dict(ops.get_recent_event_logs_impl(payload))

    if name == "diagnose_network_issue":
        return _to_dict(ops.diagnose_network_issue_impl())

    if name == "diagnose_performance":
        return _to_dict(ops.diagnose_performance_impl())

    if name == "get_gpu_info":
        return _to_dict(ops.get_gpu_info_impl())

    if name == "full_system_health_check":
        return _to_dict(ops.full_system_health_check_impl())

    if name == "install_app":
        payload = ops.InstallAppRequest(**arguments)
        return _to_dict(ops.install_app_impl(payload))

    if name == "uninstall_app":
        payload = ops.UninstallAppRequest(**arguments)
        return _to_dict(ops.uninstall_app_impl(payload))

    if name == "run_defender_scan":
        payload = ops.DefenderScanRequest(**arguments)
        return _to_dict(ops.run_defender_scan_impl(payload))

    if name == "list_processes":
        return _to_dict(ops.list_processes_impl())

    if name == "kill_process":
        payload = ops.KillProcessRequest(**arguments)
        return _to_dict(ops.kill_process_impl(payload))

    if name == "list_services":
        return _to_dict(ops.list_services_impl())

    if name == "control_service":
        payload = ops.ControlServiceRequest(**arguments)
        return _to_dict(ops.control_service_impl(payload))

    if name == "execute_powershell_commands":
        payload = ops.ExecuteCommandsRequest(**arguments)
        result = ops.execute_commands_impl(payload)
        return _to_dict(result)

    # Unknown tool name
    return {"error": f"Unknown tool name: {name}"}


#
# ASGI app using StreamableHTTPServerTransport
#

_transport = StreamableHTTPServerTransport(
    mcp_session_id=None,
    is_json_response_enabled=False,
)
_server_task: asyncio.Task | None = None


async def _ensure_mcp_server_running() -> None:
    """
    Ensure the MCP Server.run loop is running in the background.
    """
    global _server_task
    if _server_task is not None and not _server_task.done():
        return

    async def _runner() -> None:
        init_options = mcp_server.create_initialization_options(
            NotificationOptions(
                prompts_changed=False,
                resources_changed=False,
                tools_changed=True,
            )
        )
        async with _transport.connect() as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                init_options,
                raise_exceptions=False,
                stateless=False,
            )

    _server_task = asyncio.create_task(_runner())

    # Wait briefly for the transport to finish connect() and set up streams
    # before handling requests, to avoid "No read stream writer available".
    for _ in range(50):
        # Accessing private attributes is not ideal, but acceptable here for
        # a simple hackathon-grade MCP wrapper.
        if getattr(_transport, "_read_stream_writer", None) is not None:
            break
        await asyncio.sleep(0.02)


async def app(scope, receive, send) -> None:
    """
    Minimal ASGI application that exposes the MCP streamable HTTP server.

    This is intended to be run with uvicorn, e.g.:
      uv run uvicorn system_ops_mcp_server:app --host 127.0.0.1 --port 8001

    Configure `mcp_agent.config.yaml` `system_ops` server as:
      transport: streamable_http
      url: "http://localhost:8001/mcp"
    """
    if scope["type"] != "http":
        response = PlainTextResponse("Not Found", status_code=404)
        await response(scope, receive, send)
        return

    path = scope.get("path", "")
    if path not in ("/mcp", "/mcp/"):
        response = PlainTextResponse("Not Found", status_code=404)
        await response(scope, receive, send)
        return

    await _ensure_mcp_server_running()
    await _transport.handle_request(scope, receive, send)
