"""Model client: a thin HTTP wrapper over the OpenAI chat-completions shape."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app import env as _env  # noqa: F401  -- ensures .env is loaded before from_env()

PROVIDER_DEFAULTS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai",
               "gemini-3.1-flash-lite", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-20b", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct", "OPENROUTER_API_KEY"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "TOGETHER_API_KEY"),
    "local": ("http://localhost:11434/v1", "llama3.3", "LOCAL_API_KEY"),
}


class LLMUnavailable(RuntimeError):
    """No usable model configuration, or the provider could not serve us."""


class ToolCallRejected(LLMUnavailable):
    """The provider refused the model's tool call arguments (schema mismatch)."""


class RateLimited(LLMUnavailable):
    """Provider returned 429. Carries the suggested wait in seconds."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


_RETRY_HINT = re.compile(r"try again in ([\d.]+)\s*s", re.I)

# Providers that reject the ["string", "null"] union the toolbox emits.
_SINGLE_TYPE_SCHEMAS = {"gemini"}


def _collapse_type_unions(node: Any) -> Any:
    """Rewrite {"type": ["string", "null"]} to {"type": "string"}, recursively."""
    if isinstance(node, dict):
        out = {k: _collapse_type_unions(v) for k, v in node.items()}
        t = out.get("type")
        if isinstance(t, list):
            concrete = [x for x in t if x != "null"]
            out["type"] = concrete[0] if concrete else "string"
            out["nullable"] = True
        return out
    if isinstance(node, list):
        return [_collapse_type_unions(x) for x in node]
    return node


def _retry_after_seconds(resp: "httpx.Response") -> float:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = _RETRY_HINT.search(resp.text or "")
    return float(match.group(1)) if match else 5.0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    # The provider's own tool-call object, kept whole: some providers require
    # opaque fields (e.g. Gemini's thought_signature) echoed back on replay.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantTurn:
    # None when the turn only requests tools and carries no prose.
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    # The provider's own message object, replayed verbatim.
    raw_message: dict[str, Any] = field(default_factory=dict)

    def replay_message(self) -> dict:
        """The assistant turn in the shape the API expects it echoed back."""
        if self.raw_message:
            # Output-only fields some providers add and reject on input.
            drop = {"annotations", "audio", "refusal", "reasoning_content"}
            replay = {k: v for k, v in self.raw_message.items()
                      if k not in drop and v is not None}
            replay.setdefault("role", "assistant")
            replay.setdefault("content", self.content or "")
            if self.tool_calls and "tool_calls" not in replay:
                replay["tool_calls"] = [tc.raw for tc in self.tool_calls if tc.raw]
            return replay

        return {
            "role": "assistant",
            "content": self.content or "",
            "tool_calls": [
                tc.raw or {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.raw_arguments or "{}"},
                }
                for tc in self.tool_calls
            ],
        }

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class LLMConfig:
    provider: str = "groq"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.0  # deterministic routing; the engines own the numbers
    max_tokens: int = 1600
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        provider = os.getenv("LLM_PROVIDER", "groq").lower()
        base, model, key_var = PROVIDER_DEFAULTS.get(
            provider, PROVIDER_DEFAULTS["groq"]
        )
        return cls(
            provider=provider,
            base_url=os.getenv("LLM_BASE_URL", base).rstrip("/"),
            model=os.getenv("LLM_MODEL", model),
            api_key=os.getenv("LLM_API_KEY") or os.getenv(key_var, ""),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1600")),
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key) or self.provider == "local"


def _parse_arguments(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        # Some open models emit single quotes or trailing commas.
        try:
            import ast

            parsed = ast.literal_eval(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return {}


class LLMClient:
    MAX_RETRIES = 2
    MAX_WAIT = 12.0  # seconds; longer suggested waits fall through to salvage

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()
        # Cleared permanently if the provider rejects parallel_tool_calls.
        self._parallel_ok = True

    @property
    def describe(self) -> str:
        return f"{self.config.provider}:{self.config.model}"

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> AssistantTurn:
        """Send one request, retrying briefly on rate limits."""
        last: LLMUnavailable | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                return self._chat_once(messages, tools)
            except RateLimited as exc:
                last = exc
                if attempt == self.MAX_RETRIES or exc.retry_after > self.MAX_WAIT:
                    raise LLMUnavailable(
                        f"{self.config.provider} rate limit reached and the suggested "
                        f"wait is {exc.retry_after:.0f}s. Either wait a moment and "
                        f"retry, or switch LLM_MODEL to a model with more headroom."
                    ) from exc
                time.sleep(max(1.0, exc.retry_after) + 0.5)
        raise last or LLMUnavailable("request failed")

    def _chat_once(self, messages: list[dict], tools: list[dict] | None = None) -> AssistantTurn:
        if not self.config.configured:
            raise LLMUnavailable(
                f"No API key for provider {self.config.provider!r}. Set LLM_API_KEY "
                "(or the provider's own key variable) and optionally LLM_PROVIDER "
                "and LLM_MODEL."
            )

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            if self.config.provider in _SINGLE_TYPE_SCHEMAS:
                tools = _collapse_type_unions(tools)
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            if self._parallel_ok:
                payload["parallel_tool_calls"] = True

        try:
            resp = httpx.post(
                f"{self.config.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.config.timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"could not reach {self.config.base_url}: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimited(
                f"{self.config.provider} rate limited: {resp.text[:200]}",
                _retry_after_seconds(resp),
            )
        if resp.status_code >= 400:
            body_text = resp.text or ""
            if self._parallel_ok and "parallel_tool_calls" in body_text:
                # The provider does not know this field; drop it for good.
                self._parallel_ok = False
                return self._chat_once(messages, tools)
            if "tool_use_failed" in body_text or "tool call validation" in body_text:
                raise ToolCallRejected(
                    "the model produced a tool call the provider rejected as invalid"
                )
            raise LLMUnavailable(
                f"{self.config.provider} returned {resp.status_code}: {body_text[:400]}"
            )

        body = resp.json()
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or ""
            calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{len(calls)}",
                    name=fn.get("name") or "",
                    arguments=_parse_arguments(raw),
                    raw_arguments=raw,
                    raw=tc if isinstance(tc, dict) else {},
                )
            )

        return AssistantTurn(
            content=message.get("content"),
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "stop",
            usage=body.get("usage") or {},
            model=body.get("model") or self.config.model,
            raw_message=message if isinstance(message, dict) else {},
        )
