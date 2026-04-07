"""MCP and local tools for the agent."""

import asyncio
from datetime import datetime
import logging

from langchain_core.tools import BaseTool, tool
from langchain_mcp_adapters.client import MultiServerMCPClient


@tool
async def get_current_datetime() -> str:
    """Get the current date and time.

    Returns:
        A string with the current date and time.
    """
    now = datetime.now()
    return f"Current date: {now.strftime('%Y-%m-%d')}, Current time: {now.strftime('%H:%M:%S')}"



"""MCP service configuration.

Uses langchain-mcp-adapters to connect to:
- DDGS MCP via stdio transport
"""

logger = logging.getLogger(__name__)

_mcp_client: MultiServerMCPClient | None = None
_mcp_tools: tuple[BaseTool, ...] | None = None
_mcp_init_lock = asyncio.Lock()
_local_tools: tuple[BaseTool, ...] = (get_current_datetime,)


async def initialize_mcp() -> list[BaseTool]:
    """Connect to configured MCP servers and load tools."""
    global _mcp_client, _mcp_tools

    if _mcp_tools is not None:
        return list(_mcp_tools)

    async with _mcp_init_lock:
        if _mcp_tools is not None:
            return list(_mcp_tools)

        logger.info("Connecting to DDGS MCP server...")

        _mcp_client = MultiServerMCPClient(
            {
                "ddgs": {
                    "command": "ddgs",
                    "args": ["mcp"],
                    "transport": "stdio",
                },
            }
        )

        try:
            tools = await _mcp_client.get_tools()
            _mcp_tools = tuple(tools)
            tool_names = [t.name for t in _mcp_tools]
            logger.info(f"MCP tools loaded: {tool_names}")
        except Exception as e:
            logger.error(f"Failed to load MCP tools: {e}", exc_info=True)
            _mcp_client = None
            _mcp_tools = tuple()

    return list(_mcp_tools)


def get_local_tools() -> list[BaseTool]:
    """Get local tools that do not require MCP startup."""
    return [*_local_tools]


async def get_mcp_tools() -> list[BaseTool]:
    """Get MCP-backed tools only."""
    return await initialize_mcp()
