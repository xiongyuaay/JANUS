"""Guard adapter for Llama-Guard-3 served via a vLLM OpenAI-compatible endpoint.

Llama-Guard-3 is a binary safety classifier with a fixed chat template. Unlike
the generic `VLLMGuard`, this class does NOT wrap the trajectory in a custom
judge prompt — it sends the conversation as a user/assistant-alternating
messages list and lets Llama-Guard's own template steer the classification.

Output format expected:
    safe
or
    unsafe
    S2,S4
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from ..schema import Step
from .base import Verdict
from .vllm_guard import VLLMGuard, fit_messages_to_char_budget, flatten_history_to_chat

logger = logging.getLogger(__name__)

# Llama-Guard-3 taxonomy (S14 only present in the Python variant).
_LLAMAGUARD_CATEGORIES: dict[str, str] = {
    "S1": "Violent Crimes",
    "S2": "Non-Violent Crimes",
    "S3": "Sex-Related Crimes",
    "S4": "Child Sexual Exploitation",
    "S5": "Defamation",
    "S6": "Specialized Advice",
    "S7": "Privacy",
    "S8": "Intellectual Property",
    "S9": "Indiscriminate Weapons",
    "S10": "Hate",
    "S11": "Suicide & Self-Harm",
    "S12": "Sexual Content",
    "S13": "Elections",
    "S14": "Code Interpreter Abuse",
}


def parse_llamaguard_response(text: str) -> Verdict:
    raw = (text or "").strip()
    if not raw:
        return Verdict(flagged=False, reason="parse_error: empty response", raw={"response_text": text})
    lines = raw.splitlines()
    first = lines[0].strip().lower()
    if first == "safe":
        return Verdict(flagged=False, score=0.0, label="safe", raw={"response_text": text})
    if first == "unsafe":
        cats: list[str] = []
        if len(lines) > 1:
            cats = [c.strip().upper() for c in lines[1].split(",") if c.strip()]
        reason = ", ".join(_LLAMAGUARD_CATEGORIES.get(c, c) for c in cats) or None
        return Verdict(
            flagged=True,
            score=1.0,
            label="unsafe",
            reason=reason,
            raw={"response_text": text, "categories": cats},
        )
    return Verdict(
        flagged=False,
        reason=f"parse_error: unexpected first line {first!r}",
        raw={"response_text": text},
    )


class LlamaGuardVLLMGuard(VLLMGuard):
    """Send trajectory as alternating user/assistant messages to Llama-Guard-3
    and parse its canonical `safe` / `unsafe\\nS2,S4` output."""

    name = "llamaguard"

    def __init__(
        self,
        base_url: str,
        model_name: str,
        *,
        api_key: Optional[str] = None,
        max_tokens: int = 32,
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
            logger.warning("llamaguard request failed: %s", exc)
            return Verdict(flagged=False, reason=f"request_error: {exc}")
        return parse_llamaguard_response(text)
