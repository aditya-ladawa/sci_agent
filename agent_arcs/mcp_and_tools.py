from __future__ import annotations

import asyncio
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
MCP_CONNECT_RETRIES = 3
MCP_CONNECT_INITIAL_DELAY_SECONDS = 2.0


def _format_mcp_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _augment_tool_description(tool: BaseTool) -> BaseTool:
    description = getattr(tool, "description", "") or ""
    guidance = (
        "\n\nReliability guidance for agents: if this Tavily Internet Search MCP tool "
        "returns an error, empty result, inaccessible page, or low-quality output, preserve "
        "the exact error/result in your notes, use think_tool, then retry with a changed "
        "query/source strategy or mark the evidence gap explicitly. Do not infer missing facts."
    )
    updated_description = description + guidance
    if hasattr(tool, "model_copy"):
        return tool.model_copy(update={"description": updated_description})
    try:
        tool.description = updated_description
    except Exception:
        return tool
    return tool


def _augment_tavily_tools(tools: list[BaseTool]) -> list[BaseTool]:
    return [_augment_tool_description(tool) for tool in tools]


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
    """Load Tavily Internet Search MCP tools with the adapter's default stateless async sessions."""

    client = build_tavily_mcp_client(config)
    try:
        return _augment_tavily_tools(await client.get_tools())
    except Exception as error:
        raise RuntimeError(
            "Failed to load Tavily Internet Search MCP tools via stateless session: "
            + _format_mcp_error(error)
        ) from error


@asynccontextmanager
async def tavily_mcp_tools(
    config: TavilyMCPConfig | None = None,
) -> AsyncIterator[list[BaseTool]]:
    """Yield Tavily Internet Search MCP tools bound to one persistent async MCP session."""

    resolved_config = config or TavilyMCPConfig.from_env()
    client = build_tavily_mcp_client(resolved_config)

    session_context: Any | None = None
    session: Any | None = None
    tools: list[BaseTool] | None = None
    last_error: Exception | None = None

    for attempt in range(1, MCP_CONNECT_RETRIES + 1):
        try:
            session_context = client.session("tavily")
            session = await session_context.__aenter__()
            tools = _augment_tavily_tools(await load_mcp_tools(session))
            break
        except Exception as error:
            last_error = error
            if session_context is not None:
                try:
                    await session_context.__aexit__(type(error), error, error.__traceback__)
                except Exception:
                    pass
            session_context = None
            session = None
            if attempt < MCP_CONNECT_RETRIES:
                await asyncio.sleep(MCP_CONNECT_INITIAL_DELAY_SECONDS * attempt)

    if tools is None or session_context is None or session is None:
        detail = _format_mcp_error(last_error) if last_error else "unknown error"
        raise RuntimeError(
            f"Failed to establish Tavily Internet Search MCP session after {MCP_CONNECT_RETRIES} attempts: {detail}"
        )

    try:
        yield tools
    finally:
        await session_context.__aexit__(None, None, None)


__all__ = [
    "DEFAULT_PARAMETERS",
    "MCP_CONNECT_INITIAL_DELAY_SECONDS",
    "MCP_CONNECT_RETRIES",
    "TAVILY_REMOTE_MCP_URL",
    "TavilyMCPConfig",
    "build_tavily_mcp_client",
    "get_tavily_mcp_tools",
    "tavily_mcp_tools",
]
