from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
else:
    BaseTool = Any

DEFAULT_DDGS_ARGS = ["mcp"]
MCP_CONNECT_RETRIES = 3
MCP_CONNECT_INITIAL_DELAY_SECONDS = 2.0


def _format_mcp_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _default_ddgs_command() -> str:
    candidate = Path(sys.executable).with_name("ddgs")
    if candidate.exists():
        return str(candidate)
    return "ddgs"


_TOOL_GUIDANCE: dict[str, str] = {
    "search_text": (
        "\n\nWhen to use: broad topic discovery, finding specific facts/entities/data, "
        "locating primary sources, exploring terminology, and initial landscape mapping. "
        "This is the default discovery tool for most research needs."
        "\nWhen NOT to use: you already have the exact URL and need its full content (use extract_content "
        "instead), you need recent news specifically (use search_news), you need book/academic references "
        "specifically (use search_books), or you are searching for images/video (forbidden in this workflow)."
        "\nSearch strategy: start with short, broad queries (2-4 words) to map the landscape, then narrow "
        "with specific terms, entities, dates, or constraints. Do not start with overly long specific queries."
        "\nExamples:"
        "\n  Good: search_text(query='CRISPR gene therapy') → maps landscape, shows major players and terms"
        "\n  Good: search_text(query='CRISPR sickle cell 2024 clinical trial') → narrows after landscape known"
        "\n  Bad: search_text(query='comprehensive systematic review of all CRISPR gene therapy clinical trials for sickle cell disease published between 2020 and 2025') → too long, too specific first"
        "\nEdge cases: if results are empty, shorten the query or try synonyms. If results are all SEO farms, "
        "add site: qualifiers or switch to search_news/search_books for authoritative sources."
    ),
    "search_news": (
        "\n\nWhen to use: recent events, time-sensitive claims, current developments, press releases, "
        "regulatory announcements, and verifying whether something is current or outdated."
        "\nWhen NOT to use: historical facts, academic research, static reference material, or when "
        "search_text would return better authoritative sources. Do not use for image/video search."
        "\nExamples:"
        "\n  Good: search_news(query='EU AI Act enforcement') → finds current regulatory developments"
        "\n  Good: search_news(query='FDA drug approval 2025') → finds recent approvals"
        "\n  Bad: search_news(query='history of quantum mechanics') → not time-sensitive, use search_text or search_books"
        "\nEdge cases: news results may overlap with search_text. If search_text already found recent coverage, "
        "prefer those sources unless you need specifically dated press coverage."
    ),
    "search_books": (
        "\n\nWhen to use: academic/literary references, in-depth treatment of topics, textbook-level "
        "explanations, and finding authoritative long-form sources."
        "\nWhen NOT to use: current events, recent developments, quick factual lookups, or when "
        "search_text returns sufficient results. Do not use for image/video search."
        "\nExamples:"
        "\n  Good: search_books(query='reinforcement learning theory') → finds textbooks and monographs"
        "\n  Good: search_books(query='international trade law WTO') → finds authoritative legal references"
        "\n  Bad: search_books(query='stock market today') → use search_news for current data"
        "\nEdge cases: book results may be older. For topics needing recent publications, combine with "
        "search_text or search_news to find papers and preprints."
    ),
    "extract_content": (
        "\n\nWhen to use: inspecting the full text of a specific URL you already have, verifying whether "
        "a cited source actually supports a claim, extracting specific data/tables/numbers from a known page, "
        "and reading deep content that snippets cannot convey."
        "\nWhen NOT to use: discovering new URLs or topics (use search_text/search_news/search_books first), "
        "when a search snippet already answers your question, or when you need to find sources rather than "
        "read one. Do not use for image/video extraction."
        "\nEfficiency: extract only high-value pages, not every search result. A page is worth extracting "
        "when it likely contains primary data, official statements, detailed specifications, or evidence "
        "for a specific claim."
        "\nExamples:"
        "\n  Good: extract_content(url='https://who.int/publications/2024-malaria-report') → reads full official report"
        "\n  Good: extract_content(url='https://arxiv.org/abs/2401.12345') → reads paper abstract/intro for claims"
        "\n  Bad: extract_content(url='https://en.wikipedia.org/wiki/Quantum_computing') → search snippet suffices for overview"
        "\nEdge cases: if extraction returns an error or paywall, note it in your handoff, try search_text with "
        "site: prefix for cached/indexed content, or search for alternative sources covering the same data. "
        "Do not retry the same failing URL."
    ),
}

_SHARED_DDGS_GUIDANCE = (
    "\n\nThis DDGS MCP tool is part of Dux Distributed Global Search. "
    "This workflow is text-only; do not use image search, include images, embed Markdown images, "
    "collect visual assets, or use direct image URLs as report content. "
    "If a tool returns an error, empty result, inaccessible page, or low-quality output, preserve "
    "the exact error/result in your notes, then retry with changed query terms, source type, or angle. "
    "Do not infer missing facts from failed searches."
)


def _augment_tool_description(tool: BaseTool) -> BaseTool:
    tool_name = getattr(tool, "name", "") or ""
    description = getattr(tool, "description", "") or ""
    per_tool = _TOOL_GUIDANCE.get(tool_name, "")
    guidance = per_tool + _SHARED_DDGS_GUIDANCE
    updated_description = description + guidance
    if hasattr(tool, "model_copy"):
        return tool.model_copy(update={"description": updated_description})
    try:
        tool.description = updated_description
    except Exception:
        return tool
    return tool


def _augment_ddgs_tools(tools: list[BaseTool]) -> list[BaseTool]:
    return [_augment_tool_description(tool) for tool in tools]


@dataclass(slots=True, frozen=True)
class DDGSMCPConfig:
    """Configuration for DDGS's local stdio MCP server."""

    command: str = "ddgs"
    args: list[str] | None = None
    proxy: str | None = None

    @classmethod
    def from_env(cls) -> "DDGSMCPConfig":
        proxy = os.getenv("DDGS_PROXY")
        args = list(DEFAULT_DDGS_ARGS)
        if proxy:
            args.extend(["-pr", proxy])
        return cls(
            command=os.getenv("DDGS_MCP_COMMAND", _default_ddgs_command()),
            args=args,
            proxy=proxy,
        )

    def validate(self) -> None:
        if not self.command:
            raise ValueError("Set DDGS_MCP_COMMAND or install ddgs[mcp] so `ddgs mcp` is available.")

    def connection(self) -> dict[str, dict[str, Any]]:
        self.validate()
        return {
            "ddgs": {
                "transport": "stdio",
                "command": self.command,
                "args": self.args or list(DEFAULT_DDGS_ARGS),
            }
        }


def build_ddgs_mcp_client(
    config: DDGSMCPConfig | None = None,
) -> MultiServerMCPClient:
    resolved_config = config or DDGSMCPConfig.from_env()
    return MultiServerMCPClient(resolved_config.connection())


async def get_ddgs_mcp_tools(
    config: DDGSMCPConfig | None = None,
) -> list[BaseTool]:
    """Load DDGS MCP tools with the adapter's default stateless async sessions."""

    client = build_ddgs_mcp_client(config)
    try:
        return _augment_ddgs_tools(await client.get_tools())
    except Exception as error:
        raise RuntimeError(
            "Failed to load DDGS MCP tools via stateless session: " + _format_mcp_error(error)
        ) from error


@asynccontextmanager
async def ddgs_mcp_tools(
    config: DDGSMCPConfig | None = None,
) -> AsyncIterator[list[BaseTool]]:
    """Yield DDGS MCP tools bound to one persistent async MCP stdio session."""

    resolved_config = config or DDGSMCPConfig.from_env()
    client = build_ddgs_mcp_client(resolved_config)

    session_context: Any | None = None
    session: Any | None = None
    tools: list[BaseTool] | None = None
    last_error: Exception | None = None

    for attempt in range(1, MCP_CONNECT_RETRIES + 1):
        try:
            session_context = client.session("ddgs")
            session = await session_context.__aenter__()
            tools = _augment_ddgs_tools(await load_mcp_tools(session))
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
        raise RuntimeError(f"Failed to establish DDGS MCP session after {MCP_CONNECT_RETRIES} attempts: {detail}")

    try:
        yield tools
    finally:
        await session_context.__aexit__(None, None, None)


# Previous Tavily MCP implementation kept inactive for quick restoration.
#
# import json
#
# TAVILY_REMOTE_MCP_URL = "https://mcp.tavily.com/mcp"
# DEFAULT_PARAMETERS = {
#     "search_depth": "basic",
#     "max_results": 10,
# }
#
# @dataclass(slots=True, frozen=True)
# class TavilyMCPConfig:
#     """Configuration for Tavily's remote MCP server over async HTTP."""
#
#     api_key: str | None = None
#     url: str = TAVILY_REMOTE_MCP_URL
#     default_parameters: dict[str, Any] | None = None
#
#     @classmethod
#     def from_env(cls) -> "TavilyMCPConfig":
#         return cls(
#             api_key=os.getenv("TAVILY_API_KEY"),
#             url=os.getenv("TAVILY_MCP_URL", TAVILY_REMOTE_MCP_URL),
#             default_parameters=DEFAULT_PARAMETERS,
#         )
#
#     def headers(self) -> dict[str, str]:
#         headers: dict[str, str] = {}
#
#         if self.api_key:
#             headers["Authorization"] = f"Bearer {self.api_key}"
#
#         if self.default_parameters:
#             headers["DEFAULT_PARAMETERS"] = json.dumps(
#                 self.default_parameters,
#                 separators=(",", ":"),
#             )
#
#         return headers
#
#     def validate(self) -> None:
#         if not self.url:
#             raise ValueError("Set TAVILY_MCP_URL.")
#         if not self.api_key:
#             raise ValueError("Set TAVILY_API_KEY.")
#
#     def connection(self) -> dict[str, dict[str, Any]]:
#         self.validate()
#
#         server: dict[str, Any] = {
#             "transport": "http",
#             "url": self.url,
#         }
#         headers = self.headers()
#         if headers:
#             server["headers"] = headers
#
#         return {"tavily": server}
#
#
# def build_tavily_mcp_client(
#     config: TavilyMCPConfig | None = None,
# ) -> MultiServerMCPClient:
#     resolved_config = config or TavilyMCPConfig.from_env()
#     return MultiServerMCPClient(resolved_config.connection())
#
#
# async def get_tavily_mcp_tools(
#     config: TavilyMCPConfig | None = None,
# ) -> list[BaseTool]:
#     """Load Tavily Internet Search MCP tools with the adapter's default stateless async sessions."""
#
#     client = build_tavily_mcp_client(config)
#     try:
#         return _augment_tavily_tools(await client.get_tools())
#     except Exception as error:
#         raise RuntimeError(
#             "Failed to load Tavily Internet Search MCP tools via stateless session: "
#             + _format_mcp_error(error)
#         ) from error
#
#
# @asynccontextmanager
# async def tavily_mcp_tools(
#     config: TavilyMCPConfig | None = None,
# ) -> AsyncIterator[list[BaseTool]]:
#     """Yield Tavily Internet Search MCP tools bound to one persistent async MCP session."""
#
#     resolved_config = config or TavilyMCPConfig.from_env()
#     client = build_tavily_mcp_client(resolved_config)
#
#     session_context: Any | None = None
#     session: Any | None = None
#     tools: list[BaseTool] | None = None
#     last_error: Exception | None = None
#
#     for attempt in range(1, MCP_CONNECT_RETRIES + 1):
#         try:
#             session_context = client.session("tavily")
#             session = await session_context.__aenter__()
#             tools = _augment_tavily_tools(await load_mcp_tools(session))
#             break
#         except Exception as error:
#             last_error = error
#             if session_context is not None:
#                 try:
#                     await session_context.__aexit__(type(error), error, error.__traceback__)
#                 except Exception:
#                     pass
#             session_context = None
#             session = None
#             if attempt < MCP_CONNECT_RETRIES:
#                 await asyncio.sleep(MCP_CONNECT_INITIAL_DELAY_SECONDS * attempt)
#
#     if tools is None or session_context is None or session is None:
#         detail = _format_mcp_error(last_error) if last_error else "unknown error"
#         raise RuntimeError(
#             f"Failed to establish Tavily Internet Search MCP session after {MCP_CONNECT_RETRIES} attempts: {detail}"
#         )
#
#     try:
#         yield tools
#     finally:
#         await session_context.__aexit__(None, None, None)


__all__ = [
    "DDGSMCPConfig",
    "DEFAULT_DDGS_ARGS",
    "MCP_CONNECT_INITIAL_DELAY_SECONDS",
    "MCP_CONNECT_RETRIES",
    "build_ddgs_mcp_client",
    "ddgs_mcp_tools",
    "get_ddgs_mcp_tools",
]
