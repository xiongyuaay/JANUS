from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

try:
    from openai import AsyncOpenAI, OpenAI
except ImportError:  # pragma: no cover - optional at runtime
    AsyncOpenAI = None  # type: ignore[assignment]
    OpenAI = None  # type: ignore[assignment]

from multi_turn.config.llm import LLMConfig
from multi_turn.modules.log import Log


class BaseAgent(ABC):
    """Shared LLM wrapper for worker agents."""

    def __init__(self, config: LLMConfig, system_prompt: str, name: str):
        self.config = config
        self.system_prompt = system_prompt
        self.name = name
        if OpenAI is None or AsyncOpenAI is None:
            raise ImportError("The 'openai' package is required to use compiled_prompt workers.")
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self.async_client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key)
        Log.info(f"Agent initialized (model: {config.model})", self.name)

    def call_llm(self, user_message: str, temperature: Optional[float] = None) -> str:
        """Run a synchronous LLM call with the agent's system prompt."""

        Log.info(f"Calling LLM... (prompt length: {len(user_message)} chars)", self.name)
        max_attempts = max(1, self.config.max_retries + 1)
        for attempt in range(1, max_attempts + 1):
            start_time = time.time()
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=temperature or self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                content = response.choices[0].message.content or ""
                elapsed = time.time() - start_time
                Log.success(f"LLM response received (time: {elapsed:.2f}s, length: {len(content)} chars)", self.name)
                return content
            except Exception as exc:
                elapsed = time.time() - start_time
                if attempt == max_attempts:
                    Log.error(f"LLM request failed after {attempt} attempts (time: {elapsed:.2f}s): {exc}", self.name)
                    raise
                delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                Log.warning(
                    f"LLM request failed on attempt {attempt}/{max_attempts} (time: {elapsed:.2f}s): {exc}. "
                    f"Retrying in {delay:.1f}s",
                    self.name,
                )
                time.sleep(delay)

    async def call_llm_async(self, user_message: str, temperature: Optional[float] = None) -> str:
        """Run an asynchronous LLM call with the agent's system prompt."""

        Log.info(f"Calling LLM asynchronously... (prompt length: {len(user_message)} chars)", self.name)
        max_attempts = max(1, self.config.max_retries + 1)
        for attempt in range(1, max_attempts + 1):
            start_time = time.time()
            try:
                response = await self.async_client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=temperature or self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                content = response.choices[0].message.content or ""
                elapsed = time.time() - start_time
                Log.success(f"LLM async response received (time: {elapsed:.2f}s, length: {len(content)} chars)", self.name)
                return content
            except Exception as exc:
                elapsed = time.time() - start_time
                if attempt == max_attempts:
                    Log.error(f"LLM async request failed after {attempt} attempts (time: {elapsed:.2f}s): {exc}", self.name)
                    raise
                delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                Log.warning(
                    f"LLM async request failed on attempt {attempt}/{max_attempts} (time: {elapsed:.2f}s): {exc}. "
                    f"Retrying in {delay:.1f}s",
                    self.name,
                )
                await asyncio.sleep(delay)

    def dump_raw_response(self, stage: str, response: str) -> None:
        """Persist raw LLM output for debugging when downstream parsing fails."""

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_stage = stage.lower().replace(" ", "_")
        filename = f"{stamp}_{self.name}_{safe_stage}_raw.txt"
        Log.dump_text(filename, response, self.name)

    @abstractmethod
    def run(self, input_data: Any) -> dict:
        """Execute the agent synchronously."""

    @abstractmethod
    async def run_async(self, input_data: Any) -> dict:
        """Execute the agent asynchronously."""
