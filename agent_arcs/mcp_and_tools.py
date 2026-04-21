from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
else:
    BaseTool = Any

TAVILY_REMOTE_MCP_URL = "https://mcp.tavily.com/mcp"
DEFAULT_PARAMETERS = {
    "search_depth": "advanced",
    "max_results": 10,
}


@dataclass(slots=True, frozen=True)
class TavilyMCPConfig:
    """Configuration for Tavily's remote MCP server over async HTTP."""

    api_key: str | None = None
    url: str = TAVILY_REMOTE_MCP_URL
    default_parameters: dict[str, Any] | None = None

    @classmethod
    def from_env(cls) -> "TavilyMCPConfig":
        return cls(
            api_key=os.getenv("TAVILY_API_KEY"),
            url=os.getenv("TAVILY_MCP_URL", TAVILY_REMOTE_MCP_URL),
            default_parameters=DEFAULT_PARAMETERS,
        )

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self.default_parameters:
            headers["DEFAULT_PARAMETERS"] = json.dumps(
                self.default_parameters,
                separators=(",", ":"),
            )

        return headers

    def validate(self) -> None:
        if not self.url:
            raise ValueError("Set TAVILY_MCP_URL.")
        if not self.api_key:
            raise ValueError("Set TAVILY_API_KEY.")

    def connection(self) -> dict[str, dict[str, Any]]:
        self.validate()

        server: dict[str, Any] = {
            "transport": "http",
            "url": self.url,
        }
        headers = self.headers()
        if headers:
            server["headers"] = headers

        return {"tavily": server}


def build_tavily_mcp_client(
    config: TavilyMCPConfig | None = None,
) -> MultiServerMCPClient:
    resolved_config = config or TavilyMCPConfig.from_env()
    return MultiServerMCPClient(resolved_config.connection())


async def get_tavily_mcp_tools(
    config: TavilyMCPConfig | None = None,
) -> list[BaseTool]:
    """Load Tavily tools with the adapter's default stateless async sessions."""

    client = build_tavily_mcp_client(config)
    return await client.get_tools()


@asynccontextmanager
async def tavily_mcp_tools(
    config: TavilyMCPConfig | None = None,
) -> AsyncIterator[list[BaseTool]]:
    """Yield Tavily tools bound to one persistent async MCP session."""

    resolved_config = config or TavilyMCPConfig.from_env()
    client = build_tavily_mcp_client(resolved_config)

    async with client.session("tavily") as session:
        tools = await load_mcp_tools(session)
        yield tools


__all__ = [
    "DEFAULT_PARAMETERS",
    "TAVILY_REMOTE_MCP_URL",
    "TavilyMCPConfig",
    "build_tavily_mcp_client",
    "get_tavily_mcp_tools",
    "tavily_mcp_tools",
]
