from __future__ import annotations

import asyncio
import sys
from typing import NoReturn

from main import app, run_agent
from mcp_agent.core.context import Context as AppContext  # just for type hints


async def main_async(prompt: str) -> None:
    # Reuse the existing MCPApp (`app`) and the `run_agent` tool from main.py
    async with app.run() as agent_app:
        result = await run_agent(
            agent_name="tavily_helper",
            prompt=prompt,
            app_ctx=agent_app.context,
        )
        print("\n=== Tavily helper result ===\n")
        print(result)


def main() -> NoReturn:
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    else:
        prompt = (
            "Troubleshoot: Valorant crashes with error code 0xC0000005 on "
            "Windows 11. Explain likely causes and step-by-step fixes."
        )
    asyncio.run(main_async(prompt))
    raise SystemExit(0)


if __name__ == "__main__":
    main()

