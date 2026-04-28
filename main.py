"""
Welcome to mcp-agent! We believe MCP is all you need to build and deploy agents.
This is a canonical getting-started example that covers everything you need to know to get started.

We will cover:
  - Hello world agent: Setting up a basic Agent that uses the fetch and filesystem MCP servers to do cool stuff.
  - @app.tool and @app.async_tool decorators to expose your agents as long-running tools on an MCP server.
  - Advanced MCP features: Notifications, sampling, and elicitation

You can run this example locally using "uv run main.py", and also deploy it as an MCP server using "mcp-agent deploy".

Let's get started!
"""

from __future__ import annotations

import asyncio
from typing import Optional

from mcp_agent.app import MCPApp
from mcp_agent.agents.agent import Agent
from mcp_agent.agents.agent_spec import AgentSpec
from mcp_agent.core.context import Context as AppContext
from mcp_agent.workflows.factory import create_agent

# We are using the OpenAI augmented LLM for this example but you can swap with others (e.g. AnthropicAugmentedLLM)
from mcp_agent.workflows.llm.augmented_llm_google import GoogleAugmentedLLM

# Create the MCPApp, the root of mcp-agent.
app = MCPApp(
    name="hello_world",
    description="Hello world mcp-agent application",
    # settings= <specify programmatically if needed; by default, configuration is read from mcp_agent.config.yaml/mcp_agent.secrets.yaml>
)


# Hello world agent: an Agent using MCP servers + LLM
@app.tool()
async def finder_agent(request: str, app_ctx: Optional[AppContext] = None) -> str:
    """
    Run an Agent with access to MCP servers (fetch + filesystem) to handle the input request.

    Notes:
    - @app.tool:
      - runs the function as a long-running workflow tool when deployed as an MCP server
      - no-op when running this locally as a script
    - app_ctx:
      - MCPApp Context (configuration, logger, upstream session, etc.)
    """

    logger = app_ctx.app.logger
    # Logger requests are forwarded as notifications/message to the client over MCP.
    logger.info(f"finder_tool called with request: {request}")

    agent = Agent(
        name="finder",
        instruction=(
            "You are a helpful assistant. Use MCP servers to fetch and read files,"
            " then answer the request concisely."
        ),
        server_names=["fetch", "filesystem"],
        context=app_ctx,
    )

    async with agent:
        llm = await agent.attach_llm(GoogleAugmentedLLM)
        result = await llm.generate_str(message=request)

        # Best-effort logging of LLM token usage / cost for observability.
        try:
            usage = await agent.get_token_usage()
            cost = await agent.get_token_cost()
            logger.info(
                "LLM token usage for finder_agent",
                data={
                    "usage": usage,
                    "estimated_cost_usd": cost,
                },
            )
        except Exception as exc:  # pragma: no cover - observability only
            logger.warning(
                "Failed to record LLM token usage for finder_agent",
                data={"error": str(exc)},
            )

        return result


# Run a configured agent by name (defined in mcp_agent.config.yaml)
@app.async_tool(name="run_agent_async")
async def run_agent(
    agent_name: str = "web_helper",
    prompt: str = "Please summarize the first paragraph of https://modelcontextprotocol.io/docs/getting-started/intro",
    app_ctx: Optional[AppContext] = None,
) -> str:
    """
    Load an agent defined in mcp_agent.config.yaml by name and run it.

    Notes:
    - @app.async_tool:
      - async version of @app.tool -- returns a workflow ID back (can be used with workflows-get_status tool)
      - runs the function as a long-running workflow tool when deployed as an MCP server
      - no-op when running this locally as a script
    """

    logger = app_ctx.app.logger

    try:
        agent_definitions = app.config.agents.definitions or []
    except AttributeError:
        agent_definitions = []

    agent_spec: AgentSpec | None = next(
        (d for d in agent_definitions if d.name == agent_name), None
    )

    if agent_spec is None:
        logger.error("Agent not found", data={"name": agent_name})
        return f"agent '{agent_name}' not found"

    logger.info(
        "Agent found in spec",
        data={"name": agent_name, "instruction": agent_spec.instruction},
    )

    agent = create_agent(agent_spec, context=app_ctx)

    async with agent:
        llm = await agent.attach_llm(GoogleAugmentedLLM)
        result = await llm.generate_str(message=prompt)

        # Best-effort logging of LLM token usage / cost for observability.
        try:
            usage = await agent.get_token_usage()
            cost = await agent.get_token_cost()
            logger.info(
                "LLM token usage for run_agent",
                data={
                    "agent_name": agent_name,
                    "usage": usage,
                    "estimated_cost_usd": cost,
                },
            )
        except Exception as exc:  # pragma: no cover - observability only
            logger.warning(
                "Failed to record LLM token usage for run_agent",
                data={"error": str(exc), "agent_name": agent_name},
            )

        return result


async def main():
    async with app.run() as agent_app:
        # Run the agent
        readme_summary = await finder_agent(
            request="Please summarize the README.md file in this directory.",
            app_ctx=agent_app.context,
        )
        print("README.md file summary:")
        print(readme_summary)

        webpage_summary = await run_agent(
            agent_name="web_helper",
            prompt="Please summarize the first few paragraphs of https://modelcontextprotocol.io/docs/getting-started/intro.",
            app_ctx=agent_app.context,
        )
        print("Webpage summary:")
        print(webpage_summary)

        # UNCOMMENT to run this MCPApp as an MCP server
        #########################################################
        # Create the MCP server that exposes both workflows and agent configurations,
        # optionally using custom FastMCP settings
        from mcp_agent.server.app_server import create_mcp_server_for_app
        mcp_server = create_mcp_server_for_app(agent_app)

        # Run the server
        await mcp_server.run_sse_async()


if __name__ == "__main__":
    asyncio.run(main())

# When you're ready to deploy this MCPApp as a remote SSE server, run:
# > uv run mcp-agent deploy "hello_world" --no-auth
#
# Congrats! You made it to the end of the getting-started example!
# There is a lot more that mcp-agent can do, and we hope you'll explore the rest of the documentation.
# Check out other examples in the mcp-agent repo:
# https://github.com/lastmile-ai/mcp-agent/tree/main/examples
# and read the docs (or ask an mcp-agent to do it for you):
# https://docs.mcp-agent.com/
#
# Happy mcp-agenting!
