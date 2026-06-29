from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLMConfig:
    """LLM settings shared across worker agents."""

    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_tokens: int = 32000
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0


@dataclass
class PipelineConfig:
    """Execution settings for the compiled-prompt pipeline."""

    max_iterations: int = 3
    auto_approve: bool = False
    parallel_execution: bool = True
