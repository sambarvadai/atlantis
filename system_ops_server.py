from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None  # type: ignore[assignment]


# Load environment variables from a local .env file (if present).
# This is mainly used for MongoDB settings (MONGODB_URI, etc.).
load_dotenv()


app = FastAPI(
    title="Windows System Ops Server",
    description=(
        "FastAPI-based server exposing Windows system operations as HTTP tools. "
        "This is the core logic for your MCP `system_ops` server; "
        "you will still need to wire it into the official MCP HTTP/streamable_http protocol."
    ),
)


_mongo_client: Optional["MongoClient"] = None
_mongo_collection = None


def _init_mongo_if_needed() -> None:
    """
    Lazily initialize the MongoDB client/collection.

    Uses the following environment variables:
    - MONGODB_URI: full MongoDB Atlas connection string (required to enable logging).
    - MONGODB_DB: database name (default: "windows_troubleshooter").
    - MONGODB_SESSION_COLLECTION: collection name (default: "sessions").

    If any step fails (missing driver, bad URI, network issue), logging is silently disabled.
    """
    global _mongo_client, _mongo_collection

    if MongoClient is None:
        return
    if _mongo_client is not None and _mongo_collection is not None:
        return

    uri = os.getenv("MONGODB_URI")
    if not uri:
        return

    db_name = os.getenv("MONGODB_DB", "windows_troubleshooter")
    coll_name = os.getenv("MONGODB_SESSION_COLLECTION", "sessions")

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=2000)
        _mongo_client = client
        _mongo_collection = client[db_name][coll_name]
    except Exception:
        _mongo_client = None
        _mongo_collection = None


def log_session_to_mongo(document: dict[str, Any]) -> None:
    """
    Best-effort insert of a session document into MongoDB.

    This should never raise; failures are silently ignored so that diagnostics
    still work even if MongoDB is unreachable or not configured.
    """
    try:
        _init_mongo_if_needed()
        if _mongo_collection is None:
            return
        _mongo_collection.insert_one(document)
    except Exception:
        # Logging failures should not break the troubleshooting flow.
        return


def run_powershell(command: str) -> subprocess.CompletedProcess[str]:
    """Run a PowerShell command and return the completed process."""
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
    )


def is_command_available(command: str) -> bool:
    """Check if a given command is available on PATH using where.exe."""
    try:
        result = subprocess.run(
            ["where", command],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_system_overview_impl() -> dict:
    """Gather basic system information using PowerShell and return structured JSON."""
    # OS info
    os_cmd = (
        "Get-CimInstance Win32_OperatingSystem | "
        "Select-Object Caption, Version, OSArchitecture, LastBootUpTime, "
        "TotalVisibleMemorySize, FreePhysicalMemory | ConvertTo-Json -Depth 3"
    )
    os_proc = run_powershell(os_cmd)

    # CPU info
    cpu_cmd = (
        "Get-CimInstance Win32_Processor | "
        "Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed | "
        "ConvertTo-Json -Depth 3"
    )
    cpu_proc = run_powershell(cpu_cmd)

    # Disk info
    disk_cmd = (
        "Get-PSDrive -PSProvider FileSystem | "
        "Select-Object Name, Used, Free | ConvertTo-Json -Depth 3"
    )
    disk_proc = run_powershell(disk_cmd)

    errors: list[str] = []

    def safe_json_loads(raw: str, label: str) -> Optional[object]:
        if not raw:
            errors.append(f"{label} output empty")
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"Failed to parse {label} JSON")
            return None

    os_info = safe_json_loads(os_proc.stdout, "os")
    cpu_info = safe_json_loads(cpu_proc.stdout, "cpu")
    disk_info = safe_json_loads(disk_proc.stdout, "disk")

    success = len(errors) == 0

    return {
        "success": success,
        "os": os_info,
        "cpu": cpu_info,
        "disks": disk_info,
        "errors": errors,
        "raw": {
            "os_stdout": os_proc.stdout,
            "os_stderr": os_proc.stderr,
            "cpu_stdout": cpu_proc.stdout,
            "cpu_stderr": cpu_proc.stderr,
            "disk_stdout": disk_proc.stdout,
            "disk_stderr": disk_proc.stderr,
        },
    }


def safe_json_loads(raw: str, label: str, errors: list[str]) -> Optional[Any]:
    """Helper to parse JSON and record errors."""
    if not raw:
        errors.append(f"{label} output empty")
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        errors.append(f"Failed to parse {label} JSON")
        return None


class EventLogsRequest(BaseModel):
    log_name: str = "Application"
    level: Optional[Literal["Error", "Warning", "Information"]] = None
    minutes: int = 60
    max_events: int = 50


def get_recent_event_logs_impl(payload: EventLogsRequest) -> dict:
    """
    Query Windows Event Logs for recent entries matching the filters.
    """
    errors: list[str] = []

    log_name = payload.log_name.replace("'", "''")
    level_filter = payload.level
    minutes = payload.minutes
    max_events = payload.max_events

    cmd_parts: list[str] = []
    cmd_parts.append(
        "$filter = @{ LogName='%s'; StartTime=(Get-Date).AddMinutes(-%d) };"
        % (log_name, minutes)
    )
    cmd_parts.append("Get-WinEvent -FilterHashtable $filter")
    if level_filter is not None:
        cmd_parts.append(
            f" | Where-Object {{ $_.LevelDisplayName -eq '{level_filter}' }}"
        )
    cmd_parts.append(
        " | Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message"
    )
    cmd_parts.append(" | Sort-Object TimeCreated -Descending")
    cmd_parts.append(f" | Select-Object -First {max_events}")
    cmd_parts.append(" | ConvertTo-Json -Depth 4")

    ps_cmd = "".join(cmd_parts)
    proc = run_powershell(ps_cmd)

    # If there is no output and no stderr, treat as "no events" rather than an error.
    if not proc.stdout.strip() and not proc.stderr.strip():
        events: Any = []
    else:
        events = safe_json_loads(proc.stdout, "event_logs", errors)

    success = len(errors) == 0

    return {
        "success": success,
        "events": events,
        "errors": errors,
        "raw": {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        },
    }


class NetworkDiagnoseResponse(BaseModel):
    success: bool
    probable_cause: str
    checks: dict
    log: str


def diagnose_network_issue_impl() -> NetworkDiagnoseResponse:
    """
    Run basic network diagnostics and infer a probable cause.
    """
    errors: list[str] = []
    log_parts: list[str] = []

    # Adapter info
    adapters_cmd = (
        "Get-NetAdapter | "
        "Select-Object Name, Status, LinkSpeed, MacAddress | "
        "ConvertTo-Json -Depth 4"
    )
    adapters_proc = run_powershell(adapters_cmd)

    # Ping a public IP
    ip_test_cmd = (
        "Test-NetConnection -ComputerName 8.8.8.8 -InformationLevel Detailed "
        "| ConvertTo-Json -Depth 4"
    )
    ip_test_proc = run_powershell(ip_test_cmd)

    # Ping a domain for DNS test
    dns_test_cmd = (
        "Test-NetConnection -ComputerName www.microsoft.com "
        "-InformationLevel Detailed | ConvertTo-Json -Depth 4"
    )
    dns_test_proc = run_powershell(dns_test_cmd)

    adapters = safe_json_loads(adapters_proc.stdout, "adapters", errors)
    ip_test = safe_json_loads(ip_test_proc.stdout, "ip_test", errors)
    dns_test = safe_json_loads(dns_test_proc.stdout, "dns_test", errors)

    checks: dict[str, Any] = {
        "adapters": adapters,
        "ip_test": ip_test,
        "dns_test": dns_test,
        "raw": {
            "adapters_stdout": adapters_proc.stdout,
            "adapters_stderr": adapters_proc.stderr,
            "ip_test_stdout": ip_test_proc.stdout,
            "ip_test_stderr": ip_test_proc.stderr,
            "dns_test_stdout": dns_test_proc.stdout,
            "dns_test_stderr": dns_test_proc.stderr,
        },
        "errors": errors,
    }

    # Heuristic for probable cause
    probable = "No obvious network issues detected from basic checks."

    try:
        adapter_list = adapters if isinstance(adapters, list) else [adapters] if adapters else []
        up_adapters = [
            a for a in adapter_list if a and str(a.get("Status", "")).lower() == "up"
        ]
        if not up_adapters:
            probable = "No active network adapters detected. Check Wi-Fi/Ethernet connections."
        else:
            # Test-NetConnection returns a PsObject; look for TcpTestSucceeded
            ip_ok = bool(ip_test and ip_test.get("TcpTestSucceeded"))
            dns_ok = bool(dns_test and dns_test.get("TcpTestSucceeded"))

            if not ip_ok and not dns_ok:
                probable = (
                    "Adapters are up but cannot reach the internet. "
                    "Possible WAN/router or ISP issue."
                )
            elif ip_ok and not dns_ok:
                probable = (
                    "Direct IP connectivity works but DNS lookup failed. "
                    "Likely a DNS configuration issue."
                )
    except Exception as exc:  # pragma: no cover - defensive
        log_parts.append(f"Heuristic failed: {exc!r}")

    if errors:
        log_parts.append("Some checks failed; see 'checks.errors' and 'checks.raw'.")

    return NetworkDiagnoseResponse(
        success=not errors,
        probable_cause=probable,
        checks=checks,
        log=" | ".join(log_parts),
    )


class InstallAppRequest(BaseModel):
    name: Optional[str] = None
    package_id: str
    manager: Literal["auto", "winget", "choco"] = "auto"
    allow_manager_install: bool = False
    confirmed: bool = False


class InstallAppResponse(BaseModel):
    success: bool
    manager_used: Optional[Literal["winget", "choco"]] = None
    manager_installed: bool = False
    stdout: str
    stderr: str
    reboot_required: Optional[bool] = None
    log: str


class UninstallAppRequest(BaseModel):
    name: Optional[str] = None
    package_id: str
    manager: Literal["auto", "winget", "choco"] = "auto"
    confirmed: bool = False


class UninstallAppResponse(BaseModel):
    success: bool
    manager_used: Optional[Literal["winget", "choco"]] = None
    stdout: str
    stderr: str
    log: str


class DefenderScanRequest(BaseModel):
    scan_type: Literal["quick", "full", "path"] = "quick"
    path: Optional[str] = None
    confirmed: bool = False


class DefenderScanResponse(BaseModel):
    success: bool
    scan_type: Literal["quick", "full", "path"]
    path: Optional[str] = None
    stdout: str
    stderr: str
    log: str


class ListProcessesResponse(BaseModel):
    success: bool
    processes: Any
    raw: dict
    log: str


class KillProcessRequest(BaseModel):
    pid: Optional[int] = None
    name: Optional[str] = None
    confirmed: bool = False


class KillProcessResponse(BaseModel):
    success: bool
    stdout: str
    stderr: str
    log: str


class ListServicesResponse(BaseModel):
    success: bool
    services: Any
    raw: dict
    log: str


class ControlServiceRequest(BaseModel):
    name: str
    action: Literal["start", "stop", "restart"]
    confirmed: bool = False


class ControlServiceResponse(BaseModel):
    success: bool
    stdout: str
    stderr: str
    log: str


class DiagnosePerformanceResponse(BaseModel):
    success: bool
    low_disk_drives: Any
    top_cpu: Any
    top_memory: Any
    raw: dict
    log: str


class GPUInfoResponse(BaseModel):
    success: bool
    gpus: Any
    raw: dict
    log: str


class FullHealthCheckResponse(BaseModel):
    success: bool
    summary: str
    system_overview: dict
    network: NetworkDiagnoseResponse
    recent_errors: dict
    log: str


class CommandSpec(BaseModel):
    text: str
    reason: Optional[str] = None
    origin: Literal["agent", "user"] = "agent"


class ExecuteCommandsRequest(BaseModel):
    commands: list[CommandSpec]
    confirmed: bool = False
    # Optional logical session identifier so callers can correlate
    # multiple execute calls belonging to the same troubleshooting run.
    session_id: Optional[str] = None
    # Optional free-form metadata about the troubleshooting session,
    # such as the original problem description or agent notes.
    session_metadata: Optional[dict[str, Any]] = None


class PerCommandResult(BaseModel):
    command: str
    origin: Literal["agent", "user"]
    executed: bool
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class ExecuteCommandsResponse(BaseModel):
    success: bool
    results: list[PerCommandResult]
    log: str


def ensure_choco(allow_install: bool) -> tuple[bool, bool, str]:
    """
    Ensure Chocolatey is installed.

    Returns (available, installed_now, log).
    If allow_install is False and choco is missing, no installation is attempted.
    """
    if is_command_available("choco"):
        return True, False, "Chocolatey already available on PATH."

    if not allow_install:
        return False, False, (
            "Chocolatey is not installed, and automatic installation was not allowed."
        )

    # Official Chocolatey installation bootstrap script.
    install_script = (
        "Set-ExecutionPolicy Bypass -Scope Process -Force; "
        "$env:chocolateyUseWindowsCompression='true'; "
        "[System.Net.ServicePointManager]::SecurityProtocol = "
        "[System.Net.ServicePointManager]::SecurityProtocol -bor 3072; "
        "iex ((New-Object System.Net.WebClient).DownloadString("
        "'https://community.chocolatey.org/install.ps1'))"
    )
    proc = run_powershell(install_script)
    if proc.returncode != 0:
        log = (
            "Chocolatey installation failed. "
            f"stdout: {proc.stdout} stderr: {proc.stderr}"
        )
        return False, False, log

    # Re-check availability
    available = is_command_available("choco")
    log = (
        "Chocolatey installation attempted. "
        f"stdout: {proc.stdout} stderr: {proc.stderr}"
    )
    return available, True, log


def ensure_winget(allow_install: bool) -> tuple[bool, bool, str]:
    """
    Ensure winget is available.

    On most modern Windows systems, winget is preinstalled via the Microsoft Store
    App Installer. Automated installation is non-trivial and may require user
    interaction, so this helper currently does NOT attempt an automatic install.

    Returns (available, installed_now, log).
    """
    if is_command_available("winget"):
        return True, False, "winget already available on PATH."

    if not allow_install:
        return False, False, (
            "winget is not installed, and automatic installation is not supported. "
            "Please install the 'App Installer' from the Microsoft Store manually."
        )

    # Placeholder: avoid trying to script winget installation automatically.
    return False, False, (
        "Automatic winget installation is not implemented. "
        "Install 'App Installer' from the Microsoft Store to get winget."
    )


def install_with_winget(package_id: str) -> tuple[bool, str, str]:
    command = (
        f'winget install --id "{package_id}" '
        "--accept-package-agreements --accept-source-agreements"
    )
    proc = run_powershell(command)
    return proc.returncode == 0, proc.stdout, proc.stderr


def install_with_choco(package_id: str) -> tuple[bool, str, str]:
    command = f'choco install "{package_id}" -y'
    proc = run_powershell(command)
    return proc.returncode == 0, proc.stdout, proc.stderr


def uninstall_with_winget(package_id: str) -> tuple[bool, str, str]:
    command = (
        f'winget uninstall --id "{package_id}" '
        "--accept-package-agreements --accept-source-agreements"
    )
    proc = run_powershell(command)
    return proc.returncode == 0, proc.stdout, proc.stderr


def uninstall_with_choco(package_id: str) -> tuple[bool, str, str]:
    command = f'choco uninstall \"{package_id}\" -y'
    proc = run_powershell(command)
    return proc.returncode == 0, proc.stdout, proc.stderr


def install_app_impl(payload: InstallAppRequest) -> InstallAppResponse:
    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail="Installation not confirmed. Set confirmed=true to proceed.",
        )

    log_parts: list[str] = []
    winget_available = is_command_available("winget")
    choco_available = is_command_available("choco")
    manager_installed_now = False
    manager_used: Optional[Literal["winget", "choco"]] = None

    # Decide which manager to use
    if payload.manager == "winget":
        available, installed_now, log = ensure_winget(payload.allow_manager_install)
        log_parts.append(log)
        winget_available = available
        manager_installed_now = manager_installed_now or installed_now
        if not winget_available:
            raise HTTPException(
                status_code=400,
                detail=(
                    "winget is not available and could not be installed. "
                    "See log for details."
                ),
            )
        manager_used = "winget"
    elif payload.manager == "choco":
        available, installed_now, log = ensure_choco(payload.allow_manager_install)
        log_parts.append(log)
        choco_available = available
        manager_installed_now = manager_installed_now or installed_now
        if not choco_available:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Chocolatey is not available and could not be installed. "
                    "See log for details."
                ),
            )
        manager_used = "choco"
    else:  # auto
        if winget_available:
            manager_used = "winget"
            log_parts.append("Auto-selected winget (already available).")
        elif choco_available:
            manager_used = "choco"
            log_parts.append("Auto-selected Chocolatey (already available).")
        else:
            # Try to install Chocolatey automatically if allowed
            if payload.allow_manager_install:
                available, installed_now, log = ensure_choco(True)
                log_parts.append(log)
                choco_available = available
                manager_installed_now = manager_installed_now or installed_now
                if choco_available:
                    manager_used = "choco"
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "No package manager available and Chocolatey installation "
                            "failed. See log for details."
                        ),
                    )
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "No package manager available. Set allow_manager_install=true "
                        "to permit installing Chocolatey automatically."
                    ),
                )

    success = False
    stdout = ""
    stderr = ""

    if manager_used == "winget":
        success, stdout, stderr = install_with_winget(payload.package_id)
        log_parts.append(f"winget install returned success={success}.")
    elif manager_used == "choco":
        success, stdout, stderr = install_with_choco(payload.package_id)
        log_parts.append(f"Chocolatey install returned success={success}.")
    else:
        raise HTTPException(
            status_code=500,
            detail="No package manager selected for installation.",
        )

    # Basic heuristic for reboot requirement
    reboot_required = (
        "restart" in (stdout + stderr).lower()
        or "reboot" in (stdout + stderr).lower()
    )

    return InstallAppResponse(
        success=success,
        manager_used=manager_used,
        manager_installed=manager_installed_now,
        stdout=stdout,
        stderr=stderr,
        reboot_required=reboot_required,
        log=" | ".join(log_parts),
    )


def uninstall_app_impl(payload: UninstallAppRequest) -> UninstallAppResponse:
    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail="Uninstall not confirmed. Set confirmed=true to proceed.",
        )

    log_parts: list[str] = []
    winget_available = is_command_available("winget")
    choco_available = is_command_available("choco")
    manager_used: Optional[Literal["winget", "choco"]] = None

    # Decide which manager to use
    if payload.manager == "winget":
        if not winget_available:
            raise HTTPException(
                status_code=400,
                detail=(
                    "winget is not available on this system. "
                    "Please install the 'App Installer' from the Microsoft Store."
                ),
            )
        manager_used = "winget"
    elif payload.manager == "choco":
        if not choco_available:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Chocolatey is not available on this system. "
                    "Install Chocolatey manually or choose manager='winget'."
                ),
            )
        manager_used = "choco"
    else:  # auto
        if winget_available:
            manager_used = "winget"
            log_parts.append("Auto-selected winget (already available) for uninstall.")
        elif choco_available:
            manager_used = "choco"
            log_parts.append("Auto-selected Chocolatey (already available) for uninstall.")
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No package manager available for uninstall. "
                    "Install winget (App Installer) or Chocolatey first."
                ),
            )

    success = False
    stdout = ""
    stderr = ""

    if manager_used == "winget":
        success, stdout, stderr = uninstall_with_winget(payload.package_id)
        log_parts.append(f"winget uninstall returned success={success}.")
    elif manager_used == "choco":
        success, stdout, stderr = uninstall_with_choco(payload.package_id)
        log_parts.append(f"Chocolatey uninstall returned success={success}.")
    else:
        raise HTTPException(
            status_code=500,
            detail="No package manager selected for uninstall.",
        )

    return UninstallAppResponse(
        success=success,
        manager_used=manager_used,
        stdout=stdout,
        stderr=stderr,
        log=" | ".join(log_parts),
    )


def run_defender_scan_impl(payload: DefenderScanRequest) -> DefenderScanResponse:
    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail="Defender scan not confirmed. Set confirmed=true to proceed.",
        )

    log_parts: list[str] = []

    if payload.scan_type == "quick":
        cmd = "Start-MpScan -ScanType QuickScan"
    elif payload.scan_type == "full":
        cmd = "Start-MpScan -ScanType FullScan"
    else:
        if not payload.path:
            raise HTTPException(
                status_code=400,
                detail="Path scan requested but no path provided.",
            )
        # Basic escaping of double quotes
        scan_path = payload.path.replace('"', '\"')
        cmd = f'Start-MpScan -ScanPath "{scan_path}"'

    proc = run_powershell(cmd)

    success = proc.returncode == 0
    log_parts.append(
        f"Defender scan command executed with returncode={proc.returncode}."
    )

    return DefenderScanResponse(
        success=success,
        scan_type=payload.scan_type,
        path=payload.path,
        stdout=proc.stdout,
        stderr=proc.stderr,
        log=" | ".join(log_parts),
    )


def list_processes_impl() -> ListProcessesResponse:
    cmd = (
        "Get-Process | Select-Object Name, Id, CPU, WorkingSet, StartTime "
        "-ErrorAction SilentlyContinue | ConvertTo-Json -Depth 4"
    )
    proc = run_powershell(cmd)
    errors: list[str] = []
    processes = safe_json_loads(proc.stdout, "processes", errors)
    log_parts: list[str] = []
    if errors:
        log_parts.extend(errors)
    return ListProcessesResponse(
        success=proc.returncode == 0,
        processes=processes,
        raw={"stdout": proc.stdout, "stderr": proc.stderr, "errors": errors},
        log=" | ".join(log_parts),
    )


def kill_process_impl(payload: KillProcessRequest) -> KillProcessResponse:
    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail="Process kill not confirmed. Set confirmed=true to proceed.",
        )
    if payload.pid is None and not payload.name:
        raise HTTPException(
            status_code=400,
            detail="Either pid or name must be provided.",
        )

    if payload.pid is not None:
        cmd = f"Stop-Process -Id {payload.pid} -Force"
    else:
        # Basic escaping: wrap in single quotes and escape existing ones
        name = payload.name.replace("'", "''")  # type: ignore[union-attr]
        cmd = f"Get-Process -Name '{name}' -ErrorAction SilentlyContinue | Stop-Process -Force"

    proc = run_powershell(cmd)
    log = f"Kill process command executed with returncode={proc.returncode}."
    return KillProcessResponse(
        success=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        log=log,
    )


def list_services_impl() -> ListServicesResponse:
    cmd = (
        "Get-Service | Select-Object Name, DisplayName, Status "
        "| ConvertTo-Json -Depth 4"
    )
    proc = run_powershell(cmd)
    errors: list[str] = []
    services = safe_json_loads(proc.stdout, "services", errors)
    log_parts: list[str] = []
    if errors:
        log_parts.extend(errors)
    return ListServicesResponse(
        success=proc.returncode == 0,
        services=services,
        raw={"stdout": proc.stdout, "stderr": proc.stderr, "errors": errors},
        log=" | ".join(log_parts),
    )


def control_service_impl(payload: ControlServiceRequest) -> ControlServiceResponse:
    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail="Service control not confirmed. Set confirmed=true to proceed.",
        )

    name = payload.name.replace("'", "''")
    if payload.action == "start":
        cmd = f"Start-Service -Name '{name}'"
    elif payload.action == "stop":
        cmd = f"Stop-Service -Name '{name}'"
    else:
        cmd = f"Restart-Service -Name '{name}'"

    proc = run_powershell(cmd)
    log = (
        f"Service '{payload.name}' action '{payload.action}' executed with "
        f"returncode={proc.returncode}."
    )
    return ControlServiceResponse(
        success=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        log=log,
    )


def diagnose_performance_impl() -> DiagnosePerformanceResponse:
    errors: list[str] = []
    log_parts: list[str] = []

    # Disk usage
    disk_cmd = (
        "Get-PSDrive -PSProvider FileSystem | "
        "Select-Object Name, Used, Free | ConvertTo-Json -Depth 3"
    )
    disk_proc = run_powershell(disk_cmd)
    disks = safe_json_loads(disk_proc.stdout, "disks", errors)

    low_disk_drives: list[dict[str, Any]] = []
    try:
        disk_list = disks if isinstance(disks, list) else [disks] if disks else []
        for d in disk_list:
            if not d:
                continue
            used = d.get("Used") or 0
            free = d.get("Free") or 0
            total = used + free
            free_ratio = (free / total) if total else 0
            if total and free_ratio < 0.1:  # less than 10% free
                low_disk_drives.append(
                    {
                        "name": d.get("Name"),
                        "used": used,
                        "free": free,
                        "free_ratio": free_ratio,
                    }
                )
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"disk heuristic failed: {exc!r}")

    # Top CPU processes
    top_cpu_cmd = (
        "Get-Process | Sort-Object CPU -Descending | "
        "Select-Object -First 10 Name, Id, CPU, WorkingSet "
        "| ConvertTo-Json -Depth 4"
    )
    top_cpu_proc = run_powershell(top_cpu_cmd)
    top_cpu = safe_json_loads(top_cpu_proc.stdout, "top_cpu", errors)

    # Top memory processes
    top_mem_cmd = (
        "Get-Process | Sort-Object WorkingSet -Descending | "
        "Select-Object -First 10 Name, Id, CPU, WorkingSet "
        "| ConvertTo-Json -Depth 4"
    )
    top_mem_proc = run_powershell(top_mem_cmd)
    top_memory = safe_json_loads(top_mem_proc.stdout, "top_memory", errors)

    if errors:
        log_parts.extend(errors)

    raw = {
        "disks_stdout": disk_proc.stdout,
        "disks_stderr": disk_proc.stderr,
        "top_cpu_stdout": top_cpu_proc.stdout,
        "top_cpu_stderr": top_cpu_proc.stderr,
        "top_memory_stdout": top_mem_proc.stdout,
        "top_memory_stderr": top_mem_proc.stderr,
        "errors": errors,
    }

    return DiagnosePerformanceResponse(
        success=len(errors) == 0,
        low_disk_drives=low_disk_drives,
        top_cpu=top_cpu,
        top_memory=top_memory,
        raw=raw,
        log=" | ".join(log_parts),
    )


def get_gpu_info_impl() -> GPUInfoResponse:
    """
    Gather basic GPU / video controller information using WMI.

    This is similar in spirit to parts of dxdiag (display tab) but uses
    Win32_VideoController to return structured JSON with Name, DriverVersion,
    AdapterRAM, and PNPDeviceID.
    """
    cmd = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name, DriverVersion, AdapterRAM, PNPDeviceID | "
        "ConvertTo-Json -Depth 4"
    )
    proc = run_powershell(cmd)

    errors: list[str] = []
    gpus = safe_json_loads(proc.stdout, "gpus", errors)

    log_parts: list[str] = []
    if errors:
        log_parts.extend(errors)

    return GPUInfoResponse(
        success=proc.returncode == 0 and not errors,
        gpus=gpus,
        raw={
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "errors": errors,
        },
        log=" | ".join(log_parts),
    )


def full_system_health_check_impl() -> FullHealthCheckResponse:
    log_parts: list[str] = []

    # System overview
    system_overview = get_system_overview_impl()
    if system_overview.get("errors"):
        log_parts.append("System overview reported errors.")

    # Network
    network = diagnose_network_issue_impl()
    if not network.success:
        log_parts.append("Network diagnostics reported errors.")

    # Recent error logs (last 60 minutes, Application errors)
    recent_req = EventLogsRequest(
        log_name="Application", level="Error", minutes=60, max_events=50
    )
    recent_errors = get_recent_event_logs_impl(recent_req)
    if recent_errors.get("errors"):
        log_parts.append("Event log retrieval reported errors.")

    summary = "Basic health checks completed."
    if network.probable_cause:
        summary = f"{summary} Network: {network.probable_cause}"

    success = (
        not system_overview.get("errors")
        and network.success
        and not recent_errors.get("errors")
    )

    return FullHealthCheckResponse(
        success=success,
        summary=summary,
        system_overview=system_overview,
        network=network,
        recent_errors=recent_errors,
        log=" | ".join(log_parts),
    )


def execute_commands_impl(payload: ExecuteCommandsRequest) -> ExecuteCommandsResponse:
    """
    Execute one or more PowerShell commands, ONLY if confirmed is true.

    This is a generic wrapper around PowerShell intended to be used with
    explicit, human-in-the-loop consent. It does not try to be smart about
    which commands are "good" or "bad" beyond that.
    """
    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail=(
                "Command execution not confirmed. Set confirmed=true after the user "
                "has explicitly approved the commands."
            ),
        )

    results: list[PerCommandResult] = []

    log_lines: list[str] = []
    timestamp = datetime.utcnow().isoformat() + "Z"
    session_id = payload.session_id or str(uuid4())

    for spec in payload.commands:
        proc = run_powershell(spec.text)
        result = PerCommandResult(
            command=spec.text,
            origin=spec.origin,
            executed=True,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        results.append(result)
        log_lines.append(
            f"{timestamp} session_id={session_id} origin={spec.origin} "
            f"exit_code={proc.returncode} command={spec.text!r}"
        )

    # Append to a simple log file for auditing (best-effort; ignore failures)
    try:
        with open("logs/system_ops_commands.log", "a", encoding="utf-8") as f:
            for line in log_lines:
                f.write(line + "\n")
    except Exception:
        pass

    overall_success = all(
        r.exit_code == 0 or r.exit_code is None for r in results
    )

    # Best-effort MongoDB session logging
    session_doc: dict[str, Any] = {
        "session_id": session_id,
        "created_at": timestamp,
        "overall_success": overall_success,
        "commands": [
            {
                "command": r.command,
                "origin": r.origin,
                "executed": r.executed,
                "exit_code": r.exit_code,
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
            for r in results
        ],
        "log": "; ".join(log_lines),
    }
    if payload.session_metadata:
        session_doc["session_metadata"] = payload.session_metadata

    log_session_to_mongo(session_doc)

    return ExecuteCommandsResponse(
        success=overall_success,
        results=results,
        log="; ".join(log_lines),
    )


@app.get("/tools/get_system_overview")
def get_system_overview() -> dict:
    """
    HTTP tool: return basic system overview.

    This is the underlying implementation that your MCP `system_ops` server will call.
    """
    return get_system_overview_impl()


@app.get("/tools/get_gpu_info", response_model=GPUInfoResponse)
def get_gpu_info() -> GPUInfoResponse:
    """
    HTTP tool: return GPU / video controller information (similar to dxdiag display info).
    """
    return get_gpu_info_impl()


@app.post("/tools/get_recent_event_logs")
def get_recent_event_logs(request: EventLogsRequest) -> dict:
    """
    HTTP tool: fetch recent Windows Event Logs entries matching the filters.
    """
    return get_recent_event_logs_impl(request)


@app.get("/tools/diagnose_network_issue", response_model=NetworkDiagnoseResponse)
def diagnose_network_issue() -> NetworkDiagnoseResponse:
    """
    HTTP tool: run a basic network diagnostic and infer a probable cause.
    """
    return diagnose_network_issue_impl()


@app.post("/tools/install_app", response_model=InstallAppResponse)
def install_app(request: InstallAppRequest) -> InstallAppResponse:
    """
    HTTP tool: install an application using winget or Chocolatey.

    This endpoint is designed to be wrapped by MCP tooling. It enforces a `confirmed`
    flag to avoid accidental installations.
    """
    return install_app_impl(request)


@app.post("/tools/uninstall_app", response_model=UninstallAppResponse)
def uninstall_app(request: UninstallAppRequest) -> UninstallAppResponse:
    """
    HTTP tool: uninstall an application using winget or Chocolatey.

    This endpoint is designed to be wrapped by MCP tooling. It enforces a `confirmed`
    flag to avoid accidental uninstalls.
    """
    return uninstall_app_impl(request)


@app.post("/tools/run_defender_scan", response_model=DefenderScanResponse)
def run_defender_scan(request: DefenderScanRequest) -> DefenderScanResponse:
    """
    HTTP tool: trigger a Windows Defender scan (quick, full, or path).
    """
    return run_defender_scan_impl(request)


@app.get("/tools/list_processes", response_model=ListProcessesResponse)
def list_processes() -> ListProcessesResponse:
    """
    HTTP tool: list running processes with basic CPU and memory info.
    """
    return list_processes_impl()


@app.post("/tools/kill_process", response_model=KillProcessResponse)
def kill_process(request: KillProcessRequest) -> KillProcessResponse:
    """
    HTTP tool: terminate a process by PID or name (requires confirmed=true).
    """
    return kill_process_impl(request)


@app.get("/tools/list_services", response_model=ListServicesResponse)
def list_services() -> ListServicesResponse:
    """
    HTTP tool: list Windows services and their status.
    """
    return list_services_impl()


@app.post("/tools/control_service", response_model=ControlServiceResponse)
def control_service(request: ControlServiceRequest) -> ControlServiceResponse:
    """
    HTTP tool: start/stop/restart a Windows service (requires confirmed=true).
    """
    return control_service_impl(request)


@app.get("/tools/diagnose_performance", response_model=DiagnosePerformanceResponse)
def diagnose_performance() -> DiagnosePerformanceResponse:
    """
    HTTP tool: diagnose disk and performance issues (low disk, top CPU/RAM).
    """
    return diagnose_performance_impl()


@app.get(
    "/tools/full_system_health_check", response_model=FullHealthCheckResponse
)
def full_system_health_check() -> FullHealthCheckResponse:
    """
    HTTP tool: run a one-shot health check combining overview, network, and recent logs.
    """
    return full_system_health_check_impl()


@app.post(
    "/tools/execute_powershell_commands", response_model=ExecuteCommandsResponse
)
def execute_powershell_commands(
    request: ExecuteCommandsRequest,
) -> ExecuteCommandsResponse:
    """
    HTTP tool: execute one or more PowerShell commands, ONLY when confirmed=true.

    - This is a generic wrapper and will execute exactly the commands provided.
    - It is the responsibility of the caller (agent/UI) to:
      - Show commands and explanations to the user.
      - Ask for explicit approval.
      - Set confirmed=true only after the user consents.
    - All executions are appended to logs/system_ops_commands.log for auditing.
    """
    return execute_commands_impl(request)


# Note:
# - This file focuses on the FastAPI app and Windows operations logic.
# - To use it as a true MCP `streamable_http` server, you will need to:
#   - Add the appropriate MCP HTTP/SSE wiring according to the official Python MCP
#     library documentation, so that MCP tool calls are routed to these endpoints.
#   - Run the app with uvicorn, for example:
#       uv run uvicorn system_ops_server:app --host 127.0.0.1 --port 8000
#   - Point `mcp_agent.config.yaml` `system_ops` URL to the correct path exposed for MCP.
