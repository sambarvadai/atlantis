from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import webbrowser
from typing import List, Tuple


def open_session_window(log_path: str) -> None:
    """
    Open a dedicated PowerShell console window that tails the Atlantis session
    log in real time. The window stays open until the user closes it.
    """
    abs_path = os.path.abspath(log_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    if not os.path.exists(abs_path):
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(f"=== Atlantis Session Log — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n\n")

    safe = abs_path.replace("'", "''")
    ps_cmd = (
        f"$Host.UI.RawUI.WindowTitle = 'Atlantis — Session Commands'; "
        f"Get-Content -Wait -Path '{safe}'"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NoExit", "-Command", ps_cmd],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def find_free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """
    Find an available TCP port on the given host, preferring the provided port
    when possible.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            s.bind((host, 0))
            return s.getsockname()[1]


def update_mcp_config_system_ops_url(port: int) -> None:
    """
    Update the system_ops MCP server URL in mcp_agent.config.yaml to point to
    the dynamically selected port.
    """
    config_path = os.path.join(os.path.dirname(__file__), "mcp_agent.config.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        print("start_all: Warning: could not read mcp_agent.config.yaml to update system_ops URL.")
        return

    new_url = f'url: "http://localhost:{port}/mcp"'
    updated, count = re.subn(
        r'url:\s*"?http://localhost:\d+/mcp"?', new_url, content, count=1
    )
    if count == 0:
        print(
            "start_all: Warning: did not find system_ops url in mcp_agent.config.yaml; "
            "windows_troubleshooter may still point at the wrong MCP port."
        )
        return

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(updated)
    except OSError:
        print("start_all: Warning: could not write updated mcp_agent.config.yaml.")


def start_process(cmd: List[str]) -> subprocess.Popen:
    """
    Start a subprocess for the given command.

    Uses the current Python interpreter to run uvicorn so that the
    already-configured virtual environment and dependencies are reused.
    """
    print(f"Starting: {' '.join(cmd)}")
    return subprocess.Popen(cmd)


def main() -> None:
    """
    Convenience script to start all local servers and open the frontend:

    - system_ops HTTP backend (FastAPI) on 127.0.0.1:8000
    - system_ops MCP server (streamable_http) on 127.0.0.1:8001
    - frontend_server (chat UI) on 127.0.0.1:9001

    Run this after dependencies are installed (e.g., `uv sync`), using either:
      - python start_all.py      (inside an activated virtualenv), or
      - uv run python start_all.py
    """
    procs: List[Tuple[List[str], subprocess.Popen]] = []

    # Pick ports for system_ops, its MCP wrapper, and the frontend dynamically
    # to avoid conflicts and make them available to the frontend / MCP agent.
    system_ops_port = find_free_port(8000)
    system_ops_mcp_port = find_free_port(8001)
    frontend_port = find_free_port(9001)

    os.environ["SYSTEM_OPS_PORT"] = str(system_ops_port)
    os.environ["SYSTEM_OPS_BASE_URL"] = f"http://127.0.0.1:{system_ops_port}"
    os.environ["FRONTEND_INTERNAL_URL"] = f"http://127.0.0.1:{frontend_port}"

    # Ensure mcp_agent.config.yaml points system_ops at the chosen MCP port.
    update_mcp_config_system_ops_url(system_ops_mcp_port)

    commands: List[List[str]] = [
        [
            sys.executable,
            "-m",
            "uvicorn",
            "system_ops_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(system_ops_port),
        ],
        [
            sys.executable,
            "-m",
            "uvicorn",
            "system_ops_mcp_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(system_ops_mcp_port),
        ],
        [
            sys.executable,
            "-m",
            "uvicorn",
            "frontend_server:frontend_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
        ],
    ]

    try:
        # Start all servers with small delays so logs are readable
        for cmd in commands:
            proc = start_process(cmd)
            procs.append((cmd, proc))
            # Give each process a brief moment to fail fast if misconfigured
            time.sleep(1.0)
            if proc.poll() is not None and proc.returncode not in (0, None):
                print(
                    f"Process {' '.join(cmd)} exited early with code {proc.returncode}. "
                    "Check its logs above and fix any issues before retrying."
                )
                raise RuntimeError("One of the servers failed to start.")

        # Give servers a moment to bind, then open the session window and browser
        time.sleep(2.0)
        open_session_window("logs/atlantis_session.log")
        try:
            url = f"http://127.0.0.1:{frontend_port}/"
            webbrowser.open(url)
            print(f"Opened frontend at {url}")
        except Exception:
            print(f"Frontend running at http://127.0.0.1:{frontend_port}/ (open in your browser).")

        print("All servers started. Press Ctrl+C to stop.")
        while True:
            # If any server dies unexpectedly, notify the user and exit the loop
            for cmd, proc in procs:
                code = proc.poll()
                if code not in (None, 0):
                    print(
                        f"Process {' '.join(cmd)} exited with code {code}. "
                        "Stopping remaining servers."
                    )
                    raise RuntimeError("A server process exited unexpectedly.")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping servers...")
    except RuntimeError as exc:
        print(f"\nstart_all: {exc}")
    finally:
        for _cmd, p in procs:
            if p.poll() is None:
                p.terminate()
        for _cmd, p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    main()
