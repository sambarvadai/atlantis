from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = "http://127.0.0.1:8000"


def call(
    method: str, path: str, body: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = ""
        return {
            "success": False,
            "http_status": e.code,
            "error": f"HTTPError for {method} {path}: {e}",
            "detail": detail,
        }
    except URLError as e:
        return {
            "success": False,
            "error": f"URLError for {method} {path}: {e}",
        }


def print_section(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def summarize_get_system_overview() -> None:
    print_section("GET /tools/get_system_overview")
    res = call("GET", "/tools/get_system_overview")
    print("success:", res.get("success"))
    os_info = res.get("os") or {}
    cpu_info = res.get("cpu") or {}
    disks = res.get("disks") or []
    print("OS:", os_info.get("Caption"), os_info.get("Version"))
    print("CPU:", cpu_info.get("Name"))
    print("Disks count:", len(disks) if isinstance(disks, list) else 1)


def summarize_get_recent_event_logs() -> None:
    print_section("POST /tools/get_recent_event_logs")
    body = {
        "log_name": "Application",
        "level": "Error",
        "minutes": 60,
        "max_events": 20,
    }
    res = call("POST", "/tools/get_recent_event_logs", body)
    print("success:", res.get("success"))
    events = res.get("events") or []
    if isinstance(events, dict):
        count = 1
    else:
        count = len(events)
    print("events returned:", count)


def summarize_diagnose_network_issue() -> None:
    print_section("GET /tools/diagnose_network_issue")
    res = call("GET", "/tools/diagnose_network_issue")
    print("success:", res.get("success"))
    print("probable_cause:", res.get("probable_cause"))


def summarize_list_processes() -> None:
    print_section("GET /tools/list_processes")
    res = call("GET", "/tools/list_processes")
    print("success:", res.get("success"))
    processes = res.get("processes") or []
    if isinstance(processes, dict):
        count = 1
    else:
        count = len(processes)
    print("processes returned:", count)


def summarize_list_services() -> None:
    print_section("GET /tools/list_services")
    res = call("GET", "/tools/list_services")
    print("success:", res.get("success"))
    services = res.get("services") or []
    if isinstance(services, dict):
        count = 1
    else:
        count = len(services)
    print("services returned:", count)


def summarize_diagnose_performance() -> None:
    print_section("GET /tools/diagnose_performance")
    res = call("GET", "/tools/diagnose_performance")
    print("success:", res.get("success"))
    low_disk = res.get("low_disk_drives") or []
    top_cpu = res.get("top_cpu") or []
    top_mem = res.get("top_memory") or []
    print("low_disk_drives:", len(low_disk) if isinstance(low_disk, list) else 1)
    print("top_cpu entries:", len(top_cpu) if isinstance(top_cpu, list) else 1)
    print("top_memory entries:", len(top_mem) if isinstance(top_mem, list) else 1)


def summarize_full_system_health_check() -> None:
    print_section("GET /tools/full_system_health_check")
    res = call("GET", "/tools/full_system_health_check")
    print("success:", res.get("success"))
    print("summary:", res.get("summary"))
    recent = res.get("recent_errors") or {}
    events = recent.get("events") or []
    if isinstance(events, dict):
        count = 1
    else:
        count = len(events)
    print("recent error events:", count)


def main() -> None:
    print("Testing system_ops server at", BASE_URL)
    print("Make sure uvicorn is running, e.g.:")
    print("  uv run uvicorn system_ops_server:app --host 127.0.0.1 --port 8000")
    time.sleep(0.5)

    summarize_get_system_overview()
    summarize_get_recent_event_logs()
    summarize_diagnose_network_issue()
    summarize_list_processes()
    summarize_list_services()
    summarize_diagnose_performance()
    summarize_full_system_health_check()

    print("\nAll test calls completed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)

