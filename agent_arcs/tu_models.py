from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Sequence
from typing import Any
from urllib import error, request

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field


MAIN_MODEL = "gpt-5.4"
SUB_MODEL = "gpt-5.4-mini"
DEFAULT_REQUEST_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 3
RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_TOOL_DESCRIPTION_CHARS = 500


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Set {name} in .env.")
    return value


def tu_model_name(role: str) -> str:
    """Return the TU model name for a deep-agent role."""

    normalized = role.strip().lower().replace("-", "_")
    if normalized == "main":
        return MAIN_MODEL
    if normalized in {"sub", "auditor"}:
        return SUB_MODEL
    raise ValueError(f"Unsupported TU model role: {role}")


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _parse_json_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value


def _compact_text(text: Any, limit: int = MAX_TOOL_DESCRIPTION_CHARS) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."
    stripped = value.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    objects: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(stripped):
        while index < len(stripped) and stripped[index].isspace():
            index += 1
        if index >= len(stripped):
            break
        try:
            parsed, end = decoder.raw_decode(stripped, index)
        except json.JSONDecodeError:
            next_object = stripped.find("{", index + 1)
            if next_object == -1:
                break
            index = next_object
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
        index = end

    if objects:
        return objects

    single = _extract_json_object(stripped)
    return [single] if single else []


class TUChatModel(BaseChatModel):
    """LangChain chat model for the documented TU KI-Toolbox chat endpoint."""

    model_name: str
    api_key: str
    base_url: str
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    temperature: float = 0.0
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | None = None
    parallel_tool_calls: bool = True

    @property
    def _llm_type(self) -> str:
        return "tu_ki_toolbox_chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "base_url": self.base_url,
            "has_bound_tools": bool(self.bound_tools),
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        system_parts = [
            _message_text(message)
            for message in messages
            if isinstance(message, SystemMessage)
        ]
        if self.bound_tools:
            system_parts.append(self._tool_instruction())

        prompt = "\n\n".join(
            self._format_message(message)
            for message in messages
            if not isinstance(message, SystemMessage)
        ).strip()
        if not prompt:
            prompt = "Reply with exactly: ok"

        payload = {
            "thread": None,
            "prompt": prompt,
            "model": self.model_name,
            "customInstructions": "\n\n".join(system_parts),
            "hideCustomInstructions": True,
        }
        response_payload = self._post(payload)
        text = str(response_payload.get("response") or response_payload.get("content") or "")
        if stop:
            for marker in stop:
                text = text.split(marker, 1)[0]

        usage_metadata = None
        if "promptTokens" in response_payload or "responseTokens" in response_payload or "totalTokens" in response_payload:
            usage_metadata = {
                "input_tokens": int(response_payload.get("promptTokens") or 0),
                "output_tokens": int(response_payload.get("responseTokens") or 0),
                "total_tokens": int(response_payload.get("totalTokens") or 0),
            }

        tool_calls = self._parse_tool_calls(text) if self.bound_tools else []
        message = AIMessage(
            content="" if tool_calls else text,
            response_metadata={"tu_response": response_payload},
            usage_metadata=usage_metadata,
            tool_calls=tool_calls,
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "TUChatModel":
        converted = [convert_to_openai_tool(tool) for tool in tools]
        return self.model_copy(
            update={
                "bound_tools": converted,
                "tool_choice": tool_choice,
                "parallel_tool_calls": bool(kwargs.get("parallel_tool_calls", True)),
            }
        )

    def _format_message(self, message: BaseMessage) -> str:
        text = _message_text(message)
        if isinstance(message, HumanMessage):
            return f"<user>\n{text}\n</user>"
        if isinstance(message, ToolMessage):
            name_attr = f' name="{message.name}"' if message.name else ""
            return (
                f'<tool_result{name_attr} tool_call_id="{message.tool_call_id}" '
                f'status="{message.status}">\n{text}\n</tool_result>'
            )
        if isinstance(message, AIMessage):
            parts = []
            if text:
                parts.append(f"<assistant>\n{text}\n</assistant>")
            if message.tool_calls:
                parts.append(
                    "<assistant_tool_calls>\n"
                    + json.dumps(message.tool_calls, ensure_ascii=False)
                    + "\n</assistant_tool_calls>"
                )
            return "\n".join(parts) if parts else "<assistant />"
        return f"<{message.type}>\n{text}\n</{message.type}>"

    def _tool_instruction(self) -> str:
        tool_specs = []
        for tool in self.bound_tools:
            function = tool.get("function", {})
            tool_specs.append(
                {
                    "name": function.get("name"),
                    "description": _compact_text(function.get("description")),
                    "parameters": function.get("parameters", {}),
                }
            )
        tool_choice_instruction = ""
        if self.tool_choice == "any":
            tool_choice_instruction = " You must call at least one available tool."
        elif self.tool_choice:
            tool_choice_instruction = f" You must call the tool named {self.tool_choice!r}."

        parallel_instruction = (
            "Put all independent tool calls in the same tool_calls array."
            if self.parallel_tool_calls
            else "Call at most one tool at a time."
        )
        return "\n".join(
            [
                "Tool-use protocol:",
                "- If the next step needs a tool, reply with exactly one JSON object and no prose.",
                '- Required JSON shape: {"tool_calls":[{"name":"tool_name","args":{}}]}',
                "- Tool args must be valid JSON objects matching the tool schema.",
                f"- {parallel_instruction}",
                "- After tool results appear as <tool_result ...>, use them to decide the next step.",
                "- If no tool is needed, answer normally and do not include tool-call JSON." + tool_choice_instruction,
                "Available tools JSON:",
                json.dumps(tool_specs, ensure_ascii=False),
            ]
        )

    def _parse_tool_calls(self, text: str) -> list[dict[str, Any]]:
        valid_tool_names = {
            str(tool.get("function", {}).get("name"))
            for tool in self.bound_tools
            if tool.get("function", {}).get("name")
        }
        tool_calls = []
        seen_write_todos = False
        seen_calls: set[tuple[str, str]] = set()
        for parsed in _extract_json_objects(text):
            raw_tool_calls = parsed.get("tool_calls")
            if raw_tool_calls is None and "tool_call" in parsed:
                raw_tool_calls = [parsed["tool_call"]]
            if raw_tool_calls is None and "name" in parsed:
                raw_tool_calls = [parsed]
            if not isinstance(raw_tool_calls, list):
                continue
            for raw_call in raw_tool_calls:
                if not isinstance(raw_call, dict):
                    continue
                function_call = raw_call.get("function")
                if isinstance(function_call, dict):
                    name = function_call.get("name")
                    args = function_call.get("arguments", raw_call.get("args", {}))
                else:
                    name = raw_call.get("name")
                    args = raw_call.get("args", raw_call.get("arguments", {}))
                args = _parse_json_maybe(args)
                if name not in valid_tool_names or not isinstance(args, dict):
                    continue
                if name == "write_todos":
                    if seen_write_todos:
                        continue
                    seen_write_todos = True
                dedupe_key = (str(name), json.dumps(args, sort_keys=True, ensure_ascii=False))
                if dedupe_key in seen_calls:
                    continue
                seen_calls.add(dedupe_key)
                tool_calls.append(
                    {
                        "name": name,
                        "args": args,
                        "id": str(raw_call.get("id") or f"call_{uuid.uuid4().hex}"),
                    }
                )
                if not self.parallel_tool_calls:
                    return tool_calls
        return tool_calls

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        attempts = max(1, self.max_retries)
        for attempt in range(attempts):
            req = request.Request(
                self.base_url,
                data=data,
                headers={
                    "accept": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with request.urlopen(req, timeout=self.request_timeout) as resp:
                    raw_text = resp.read().decode("utf-8", errors="replace")
                return self._parse_response(raw_text)
            except error.HTTPError as exc:
                raw_text = exc.read().decode("utf-8", errors="replace")
                message = f"TU chat request failed with HTTP {exc.code}: {raw_text[:500]}"
                if exc.code not in RETRYABLE_HTTP_STATUS or attempt == attempts - 1:
                    raise ValueError(message) from exc
                last_error = ValueError(message)
            except (TimeoutError, error.URLError) as exc:
                if attempt == attempts - 1:
                    raise ValueError(f"TU chat request failed after timeout/network error: {exc}") from exc
                last_error = exc
            time.sleep(min(2**attempt, 20))
        raise ValueError(f"TU chat request failed: {last_error}")

    def _parse_response(self, raw_text: str) -> dict[str, Any]:
        final_payload: dict[str, Any] | None = None
        chunks: list[str] = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                continue
            parsed = json.loads(line)
            if parsed.get("type") == "chunk":
                chunks.append(str(parsed.get("content") or ""))
            if parsed.get("type") == "done":
                final_payload = parsed
        if final_payload is None:
            if chunks:
                return {"type": "done", "response": "".join(chunks)}
            raise ValueError(f"TU chat response did not contain a done event: {raw_text[:500]}")
        if not final_payload.get("response") and chunks:
            final_payload["response"] = "".join(chunks)
        return final_payload


def build_tu_chat_model(
    *,
    role: str,
    temperature: float,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> TUChatModel:
    """Build a chat client for TU-provided KI-Toolbox models."""

    return TUChatModel(
        model_name=tu_model_name(role),
        api_key=_required_env("TUB_API_KEY"),
        base_url=_required_env("TUB_BASE_URL"),
        temperature=temperature,
        max_retries=max_retries,
        request_timeout=timeout,
    )


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_REQUEST_TIMEOUT",
    "MAIN_MODEL",
    "SUB_MODEL",
    "TUChatModel",
    "build_tu_chat_model",
    "tu_model_name",
]
