"""Guard adapter for Qwen3Guard-Gen served via a vLLM OpenAI-compatible endpoint.

Qwen3Guard-Gen is a three-level safety classifier (Safe / Controversial /
Unsafe) with a fixed chat template. The template auto-switches between
user-query evaluation and assistant-response evaluation based on the last
message's role. Like `LlamaGuardVLLMGuard`, this class does NOT wrap the
trajectory in a custom judge prompt.

Output format expected:
    Safety: {Safe|Unsafe|Controversial}
    Categories: <comma list | None>
    Refusal: {Yes|No}          # only present when last message is assistant
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional, Sequence

from ..schema import Step
from .base import Verdict
from .vllm_guard import VLLMGuard, fit_messages_to_char_budget, flatten_history_to_chat

logger = logging.getLogger(__name__)


_SAFETY_RE = re.compile(r"Safety\s*:\s*([A-Za-z]+)", re.IGNORECASE)
_CATEGORIES_RE = re.compile(r"Categories\s*:\s*([^\n]+)", re.IGNORECASE)
_REFUSAL_RE = re.compile(r"Refusal\s*:\s*([A-Za-z]+)", re.IGNORECASE)

_LABEL_MAP = {
    "safe": "safe",
    "unsafe": "unsafe",
    "controversial": "potential_unsafe",
}


def parse_qwen3guard_response(text: str) -> Verdict:
    src = text or ""
    m = _SAFETY_RE.search(src)
    if not m:
        return Verdict(
            flagged=False,
            reason=f"parse_error: no Safety line in {src[:120]!r}",
            raw={"response_text": text},
        )
    label = _LABEL_MAP.get(m.group(1).lower())
    if label is None:
        return Verdict(
            flagged=False,
            reason=f"parse_error: unknown Safety value {m.group(1)!r}",
            raw={"response_text": text},
        )

    cats_m = _CATEGORIES_RE.search(src)
    refusal_m = _REFUSAL_RE.search(src)
    categories = cats_m.group(1).strip() if cats_m else None
    refusal = refusal_m.group(1).lower() if refusal_m else None

    reason_parts: list[str] = []
    if categories and categories.lower() != "none":
        reason_parts.append(f"categories={categories}")
    if refusal:
        reason_parts.append(f"refusal={refusal}")

    score = {"safe": 0.0, "potential_unsafe": 0.5, "unsafe": 1.0}[label]
    return Verdict(
        flagged=(label == "unsafe"),
        score=score,
        reason="; ".join(reason_parts) or None,
        label=label,  # type: ignore[arg-type]
        raw={
            "response_text": text,
            "categories": categories,
            "refusal": refusal,
        },
    )


class Qwen3GuardVLLMGuard(VLLMGuard):
    """Send trajectory as role-preserving messages to Qwen3Guard-Gen and parse
    its canonical `Safety: .../Categories: .../Refusal: ...` output."""

    name = "qwen3guard"

    def __init__(
        self,
        base_url: str,
        model_name: str,
        *,
        api_key: Optional[str] = None,
        max_tokens: int = 64,
        temperature: float = 0.0,
        timeout: float = 60.0,
        extra_body: Optional[dict[str, Any]] = None,
        max_input_chars: Optional[int] = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            extra_body=extra_body,
            max_input_chars=max_input_chars,
        )

    def judge(self, history: Sequence[Step], meta: dict[str, Any]) -> Verdict:
        messages = flatten_history_to_chat(history)
        if not messages:
            return Verdict(flagged=False, reason="no messages")
        messages = fit_messages_to_char_budget(messages, budget=self.max_input_chars)
        try:
            text = self._request(messages)
        except Exception as exc:  # noqa: BLE001 — guard must not crash replay
            logger.warning("qwen3guard request failed: %s", exc)
            return Verdict(flagged=False, reason=f"request_error: {exc}")
        return parse_qwen3guard_response(text)
