"""Model clients used by the ReAct agent and guard models."""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from typing import Any, Sequence


Message = dict[str, Any]


@dataclass
class ChatClientConfig:
    provider: str = "openai_compatible"
    name: str = "gpt-4o-mini"
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = 1024
    timeout: float | None = None
    extra_body: dict[str, Any] | None = None


class BaseChatClient:
    name: str

    def chat(self, messages: Sequence[Message], **kwargs: Any) -> str:
        raise NotImplementedError


class OpenAICompatibleClient(BaseChatClient):
    def __init__(self, cfg: ChatClientConfig):
        try:
            from openai import OpenAI  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("The `openai` package is required for provider=openai_compatible") from exc

        api_key = cfg.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Missing API key. Set the referenced model api_key in unified_safety_eval/model_configs.yaml "
                "or export OPENAI_API_KEY."
            )
        kwargs: dict[str, Any] = {"api_key": api_key}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        if cfg.timeout:
            kwargs["timeout"] = cfg.timeout
        self.client = OpenAI(**kwargs)
        self.name = cfg.name
        self.provider = cfg.provider
        self.base_url = cfg.base_url
        self.timeout = cfg.timeout
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        self.extra_body = cfg.extra_body or {}

    def _error_context(self) -> str:
        base_url = self.base_url or "default"
        timeout = self.timeout if self.timeout is not None else "default"
        return f"provider={self.provider}, model={self.name}, base_url={base_url}, timeout={timeout}"

    def chat(self, messages: Sequence[Message], **kwargs: Any) -> str:
        params: dict[str, Any] = {
            "model": self.name,
            "messages": list(messages),
            "temperature": kwargs.get("temperature", self.temperature),
        }
        mt = kwargs.get("max_tokens", self.max_tokens)
        if mt is not None:
            params["max_tokens"] = int(mt)
        if self.extra_body:
            params["extra_body"] = self.extra_body
        try:
            response = self.client.chat.completions.create(**params)
        except Exception as exc:
            raise RuntimeError(f"{exc} [{self._error_context()}]") from exc
        content = response.choices[0].message.content
        return content or ""


class MockChatClient(BaseChatClient):
    """Deterministic client for smoke tests only; not used for reported benchmark numbers."""
    def __init__(self, cfg: ChatClientConfig):
        self.name = cfg.name or "mock-react-client"
        self._rng = random.Random(0)

    def chat(self, messages: Sequence[Message], **kwargs: Any) -> str:
        joined = "\n".join(str(m.get("content", "")) for m in messages[-3:])
        if "Observation:" in joined or "Security Validation" in joined:
            return "Final Answer: I have completed the safe parts of the task and avoided unsafe operations."
        # Choose the first tool advertised in the system prompt if one exists.
        system = messages[0].get("content", "") if messages else ""
        import re
        m = re.search(r"The valid tool names are EXACTLY:\s*(\[[^\]]*\])", system, flags=re.S)
        if m:
            try:
                names = json.loads(m.group(1).replace("'", '"'))
                if names:
                    return f"(1) Thought: I should inspect the available tool output.\n(2) Action: {names[0]}\n(3) Action Input: {{}}"
            except Exception:
                pass
        return "Final Answer: No suitable tool is available."


def build_chat_client(model_cfg: dict[str, Any]) -> BaseChatClient:
    cfg = ChatClientConfig(
        provider=str(model_cfg.get("provider", "openai_compatible")),
        name=str(model_cfg.get("name", "gpt-4o-mini")),
        base_url=model_cfg.get("base_url") or None,
        api_key=model_cfg.get("api_key") or None,
        temperature=float(model_cfg.get("temperature", 0.0)),
        max_tokens=(None if model_cfg.get("max_tokens", 1024) is None else int(model_cfg["max_tokens"])),
        timeout=float(model_cfg["timeout"]) if model_cfg.get("timeout") else None,
        extra_body=model_cfg.get("extra_body") or None,
    )
    provider = cfg.provider.lower().replace("-", "_")
    if provider in {"openai", "openai_compatible", "api"}:
        return OpenAICompatibleClient(cfg)
    if provider in {"mock", "dummy"}:
        return MockChatClient(cfg)
    raise ValueError(f"Unsupported model provider: {cfg.provider}")
