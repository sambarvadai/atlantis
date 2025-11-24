from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from typing import List


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
    procs: List[subprocess.Popen] = []

    commands = [
        [
            sys.executable,
            "-m",
            "uvicorn",
            "system_ops_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        [
            sys.executable,
            "-m",
            "uvicorn",
            "system_ops_mcp_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
        ],
        [
            sys.executable,
            "-m",
            "uvicorn",
            "frontend_server:frontend_app",
            "--host",
            "127.0.0.1",
            "--port",
            "9001",
        ],
    ]

    try:
        # Start all servers with small delays so logs are readable
        for cmd in commands:
            procs.append(start_process(cmd))
            time.sleep(1.0)

        # Give servers a moment to bind, then open the browser
        time.sleep(2.0)
        try:
            webbrowser.open("http://127.0.0.1:9001/")
            print("Opened frontend at http://127.0.0.1:9001/")
        except Exception:
            print("Frontend running at http://127.0.0.1:9001/ (open in your browser).")

        print("All servers started. Press Ctrl+C to stop.")
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping servers...")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    main()

