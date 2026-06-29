"""Guard that delegates safety judgement to a vLLM-hosted model reached via
an OpenAI-compatible `/v1/chat/completions` endpoint using the `openai` SDK.

The prompt template and response parser are fully externalised so this module
does not assume any particular guard model. A typical config drives the
behaviour end-to-end. Default prompt text lives in `./prompts/*.txt` so it
can be edited without touching Python.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..schema import Step, Trajectory
from .base import Guard, Verdict
from .prompts import (
    DEFAULT_USER_TEMPLATE,
    JUDGE_USER_WITH_PREDICTED_SUMMARY_TEMPLATE,
    JUDGE_USER_WITH_TRUTH_SUMMARY_TEMPLATE,
    PREDICT_FUTURE_SUMMARY_TEMPLATE,
)

logger = logging.getLogger(__name__)

__all__ = [
    "VLLMGuard",
    "DEFAULT_USER_TEMPLATE",
    "flatten_history_to_chat",
    "fit_messages_to_char_budget",
    "future_summary_cache_key",
    "load_future_summary_cache",
    "make_future_summary_cache_key",
    "MAX_TOTAL_INPUT_CHARS",
]

# Hardcoded universal cap on the total input-content char length across all
# messages. Sized so that a ~32k-context judge model (smallest common case)
# still has room for its chat-template wrapper and output budget after
# wrapping. Tune this one constant if you change model class.
MAX_TOTAL_INPUT_CHARS = 50000


def flatten_history_to_chat(
    history: Sequence[Step],
    *,
    max_content_chars: Optional[int] = None,
) -> list[dict[str, str]]:
    """Convert a trajectory into a strict user/assistant-alternating messages
    list for classifier-style guards (Llama-Guard, Qwen3Guard, ...).

    Rules:
      - `system` steps are dropped (classifier chat templates reject system).
      - `tool` steps are folded into the nearest assistant turn as
        `[tool_result: ...]`.
      - Assistant `tool_calls` are serialised into the assistant's content
        as `[tool_call: name({args_json})]`.
      - Consecutive same-role messages are merged so roles strictly alternate.
      - When `max_content_chars` is set, each message content is mid-truncated
        via `_truncate_middle`.
    """
    out: list[dict[str, str]] = []
    for s in history:
        if s.role == "system":
            continue
        content = s.content or ""
        if s.tool_calls:
            tc_text = "\n".join(
                f"[tool_call: {tc.name}({json.dumps(tc.arguments, ensure_ascii=False)})]"
                for tc in s.tool_calls
            )
            content = (content + "\n" + tc_text).strip() if content else tc_text
        if s.role == "tool":
            content = f"[tool_result: {content}]"
            role = "assistant"
        elif s.role in ("user", "assistant"):
            role = s.role
        else:
            continue
        if max_content_chars is not None:
            content = _truncate_middle(content, max_content_chars)
        if out and out[-1]["role"] == role:
            out[-1]["content"] = (out[-1]["content"] + "\n\n" + content).strip()
        else:
            out.append({"role": role, "content": content})
    return out


def _truncate_middle(text: str, max_chars: int) -> str:
    """Keep head + tail of a long string, replace the middle with a marker."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker_tmpl = "\n... [truncated {n} chars] ...\n"
    # Rough estimate of marker length after formatting; leave room for it.
    marker_slack = len(marker_tmpl.format(n=len(text)))
    keep = max(0, max_chars - marker_slack)
    head = keep // 2
    tail = keep - head
    return text[:head] + marker_tmpl.format(n=len(text) - keep) + (text[-tail:] if tail else "")


def fit_messages_to_char_budget(
    messages: list[dict[str, str]],
    budget: int = MAX_TOTAL_INPUT_CHARS,
) -> list[dict[str, str]]:
    """If the total content char length across all messages exceeds `budget`,
    proportionally mid-truncate each message's content until the total fits.
    Deterministic, model-agnostic, no tokenizer required. Iterates up to 3
    times with a 5% cushion to absorb per-message truncation-marker overhead
    and the 200-char floor on small messages."""
    if not messages:
        return messages
    msgs = [dict(m) for m in messages]
    for _ in range(3):
        total = sum(len(m.get("content", "") or "") for m in msgs)
        if total <= budget:
            return msgs
        scale = (budget / total) * 0.95
        for m in msgs:
            c = m.get("content") or ""
            target = max(200, int(len(c) * scale))
            if target < len(c):
                m["content"] = _truncate_middle(c, target)
    return msgs


def _step_to_dict(s: Step, *, content_char_cap: Optional[int] = None) -> dict[str, Any]:
    content = s.content or ""
    if content_char_cap is not None:
        content = _truncate_middle(content, content_char_cap)
    item: dict[str, Any] = {"index": s.index, "role": s.role, "content": content}
    if s.tool_calls:
        item["tool_calls"] = [
            {"name": tc.name, "arguments": tc.arguments, "id": tc.call_id}
            for tc in s.tool_calls
        ]
    if s.tool_name:
        item["tool_name"] = s.tool_name
    if s.tool_call_id:
        item["tool_call_id"] = s.tool_call_id
    return item


def _default_format_history(history: Sequence[Step]) -> str:
    payload = [_step_to_dict(s) for s in history]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def make_future_summary_cache_key(
    benchmark: str,
    model: Optional[str],
    split: str,
    trajectory_id: str,
    step_index: int,
) -> str:
    return json.dumps(
        [benchmark, model or "", split, trajectory_id, int(step_index)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def future_summary_cache_key(meta: dict[str, Any]) -> str:
    return make_future_summary_cache_key(
        str(meta.get("benchmark") or ""),
        meta.get("model"),
        str(meta.get("split") or ""),
        str(meta.get("trajectory_id") or ""),
        int(meta.get("step_index")),
    )


def load_future_summary_cache(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, dict):
        raise ValueError(f"future summary cache must contain a records object: {path}")
    out: dict[str, str] = {}
    for key, value in records.items():
        if isinstance(value, str):
            out[str(key)] = value
        elif isinstance(value, dict) and isinstance(value.get("summary"), str):
            out[str(key)] = value["summary"]
        else:
            raise ValueError(f"invalid future summary cache record for key {key!r}")
    return out


def _extract_instruction(history: Sequence[Step]) -> str:
    for s in history:
        if s.role == "user" and (s.content or "").strip():
            return s.content
    return ""


def _observed_tail_steps(history: Sequence[Step]) -> list[Step]:
    """Everything after the first user instruction."""
    first_user_seen = False
    tail: list[Step] = []
    for s in history:
        if not first_user_seen and s.role == "user" and (s.content or "").strip():
            first_user_seen = True
            continue
        tail.append(s)
    return tail


def _format_observed(
    history: Sequence[Step],
    *,
    max_chars: Optional[int] = None,
) -> str:
    """Serialize the observed trajectory as JSON. When `max_chars` is set, keep
    only as many tail steps as fit, prepend a `{"truncated_steps": N}` marker,
    and cap any single oversized step's `content`.
    """
    steps = _observed_tail_steps(history)
    if not steps:
        return "(no steps yet)"
    if max_chars is None:
        return _default_format_history(steps)

    # First pass: cap each step's content so one huge tool reply can't blow past
    # the total budget on its own. We allocate ~30% of the total budget to any
    # single step's content as a soft cap (applied only if it helps).
    per_step_cap = max(512, max_chars // 3)
    serialized: list[str] = []
    for s in steps:
        d = _step_to_dict(s, content_char_cap=per_step_cap)
        serialized.append(json.dumps(d, ensure_ascii=False, indent=2))

    total = sum(len(x) for x in serialized) + 2 + 2 * max(0, len(serialized) - 1)  # brackets + separators
    if total <= max_chars:
        return "[\n" + ",\n".join(serialized) + "\n]"

    # Drop from the head until it fits, reserving room for a truncation marker.
    marker_budget = 80  # conservative upper bound for the marker entry
    kept_rev: list[str] = []
    running = 2  # "[\n...\n]"
    for item in reversed(serialized):
        cost = len(item) + 2  # ",\n" separator or trailing newline
        if running + cost + marker_budget > max_chars and kept_rev:
            break
        kept_rev.append(item)
        running += cost
    kept = list(reversed(kept_rev))
    dropped = len(serialized) - len(kept)
    if dropped <= 0:
        return "[\n" + ",\n".join(serialized) + "\n]"
    marker = json.dumps({"truncated_steps": dropped}, ensure_ascii=False, indent=2)
    return "[\n" + marker + (",\n" + ",\n".join(kept) if kept else "") + "\n]"


_VALID_LABELS = {"safe", "potential_unsafe", "unsafe"}
_LABEL_ALIASES = {
    "benign": "safe",
    "ok": "safe",
    "allow": "safe",
    "suspicious": "potential_unsafe",
    "uncertain": "potential_unsafe",
    "caution": "potential_unsafe",
    "warn": "potential_unsafe",
    "potentially_unsafe": "potential_unsafe",
    "potentialunsafe": "potential_unsafe",
    "potential-unsafe": "potential_unsafe",
    "controversial": "potential_unsafe",
    "malicious": "unsafe",
    "harmful": "unsafe",
    "block": "unsafe",
    "deny": "unsafe",
}


def _normalise_label(raw: str) -> Optional[str]:
    key = (raw or "").strip().lower().replace(" ", "_").strip("`'\"")
    if key in _VALID_LABELS:
        return key
    return _LABEL_ALIASES.get(key)


_BOXED_RE = re.compile(r"\\boxed\{\s*([A-Za-z_\- ]+?)\s*\}")
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def _default_parse(text: str) -> Verdict:
    """Parse `<think>...</think>` + `\\boxed{label}` output.

    Falls back to scanning for a bare label token if `\\boxed{}` is missing.
    """
    think_match = _THINK_RE.search(text or "")
    reason = think_match.group(1).strip() if think_match else None

    label: Optional[str] = None
    for m in _BOXED_RE.finditer(text or ""):
        cand = _normalise_label(m.group(1))
        if cand is not None:
            label = cand  # last boxed wins, in case the model emits multiple

    if label is None:
        tail = (text or "").split("</think>")[-1]
        for token in re.findall(r"[A-Za-z_\-]+", tail):
            cand = _normalise_label(token)
            if cand is not None:
                label = cand
                break

    if label is None:
        return Verdict(
            flagged=False,
            reason=f"parse_error: no label in {text[:160]!r}",
            raw={"response_text": text},
        )

    score = {"safe": 0.0, "potential_unsafe": 0.5, "unsafe": 1.0}[label]
    return Verdict(
        flagged=(label == "unsafe"),
        score=score,
        reason=reason,
        label=label,  # type: ignore[arg-type]
        raw={"response_text": text},
    )


class VLLMGuard(Guard):
    name = "vllm"

    def __init__(
        self,
        base_url: str,
        model_name: str,
        *,
        api_key: Optional[str] = None,
        user_prompt_template: str = DEFAULT_USER_TEMPLATE,
        format_history: Callable[[Sequence[Step]], str] = _default_format_history,
        parse_response: Callable[[str], Verdict] = _default_parse,
        max_tokens: int = 256,
        temperature: float = 0.0,
        timeout: float = 60.0,
        extra_body: Optional[dict[str, Any]] = None,
        max_input_chars: Optional[int] = None,
        future_summary_mode: Optional[str] = None,
        future_summary_cache: Optional[dict[str, str]] = None,
        predicted_summary_prompt_template: str = PREDICT_FUTURE_SUMMARY_TEMPLATE,
        future_summary_judge_template: Optional[str] = None,
        future_summary_mismatch: bool = False,
        summary_max_tokens: int = 256,
        summary_temperature: Optional[float] = None,
    ) -> None:
        if future_summary_mode not in (None, "predicted", "truth"):
            raise ValueError("future_summary_mode must be one of: predicted, truth")
        if future_summary_mismatch and future_summary_mode != "truth":
            raise ValueError("future_summary_mismatch requires truth future-summary mode")
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.user_prompt_template = user_prompt_template
        self.format_history = format_history
        self.parse_response = parse_response
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.extra_body = extra_body or {}
        self.max_input_chars = max_input_chars if max_input_chars is not None else MAX_TOTAL_INPUT_CHARS
        self.future_summary_mode = future_summary_mode
        self.future_summary_cache = future_summary_cache or {}
        self.future_summary_mismatch = future_summary_mismatch
        self.future_summary_keys = sorted(self.future_summary_cache)
        self.predicted_summary_prompt_template = predicted_summary_prompt_template
        if future_summary_judge_template is not None:
            self.future_summary_judge_template = future_summary_judge_template
        elif future_summary_mode == "predicted":
            self.future_summary_judge_template = JUDGE_USER_WITH_PREDICTED_SUMMARY_TEMPLATE
        elif future_summary_mode == "truth":
            self.future_summary_judge_template = JUDGE_USER_WITH_TRUTH_SUMMARY_TEMPLATE
        else:
            self.future_summary_judge_template = None
        self.summary_max_tokens = summary_max_tokens
        self.summary_temperature = temperature if summary_temperature is None else summary_temperature
        self._client_lock = threading.Lock()
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            from openai import OpenAI  # local import so module is importable without the dep

            base_url = self.base_url if self.base_url.rstrip("/").endswith("/v1") else f"{self.base_url}/v1"
            self._client = OpenAI(
                base_url=base_url,
                api_key=self.api_key or "EMPTY",
                timeout=self.timeout,
            )
            return self._client

    def _request(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        client = self._get_client()
        resp = client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
            temperature=self.temperature if temperature is None else temperature,
            extra_body=self.extra_body or None,
        )
        return resp.choices[0].message.content or ""


    def _render_template(
        self,
        template: str,
        history: Sequence[Step],
        *,
        future_summary: Optional[str] = None,
    ) -> str:
        instruction = _extract_instruction(history)
        observed = _format_observed(history)
        # Use str.replace so literal braces in the template (e.g. `\boxed{label}`)
        # don't trip `str.format`.
        return (
            template
            .replace("{instruction}", instruction)
            .replace("{tpast}", observed)
            .replace("{future_summary}", future_summary or "")
            .replace("{history_json}", self.format_history(history))
        )

    def _render_user_prompt(
        self,
        history: Sequence[Step],
        *,
        future_summary: Optional[str] = None,
    ) -> str:
        template = self.user_prompt_template
        if future_summary is not None and self.future_summary_judge_template is not None:
            template = self.future_summary_judge_template
        return self._render_template(template, history, future_summary=future_summary)

    def _render_predicted_summary_prompt(self, history: Sequence[Step]) -> str:
        return self._render_template(self.predicted_summary_prompt_template, history)

    def _predict_future_summary(self, history: Sequence[Step]) -> str:
        user_prompt = self._render_predicted_summary_prompt(history)
        messages = [{"role": "user", "content": user_prompt}]
        messages = fit_messages_to_char_budget(messages, budget=self.max_input_chars)
        try:
            return self._request(
                messages,
                max_tokens=self.summary_max_tokens,
                temperature=self.summary_temperature,
            ).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("vllm future summary request failed: %s", exc)
            return f"(summary_request_error: {exc})"

    def _truth_future_summary(self, meta: dict[str, Any]) -> str:
        key = future_summary_cache_key(meta)
        if self.future_summary_mismatch:
            candidates = [candidate for candidate in self.future_summary_keys if candidate != key]
            if not candidates:
                raise KeyError("future summary cache has no mismatched records")
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            sampled_key = candidates[int(digest, 16) % len(candidates)]
            return self.future_summary_cache[sampled_key]
        if key not in self.future_summary_cache:
            raise KeyError(f"future summary cache missing key: {key}")
        return self.future_summary_cache[key]

    def _resolve_future_summary(
        self,
        history: Sequence[Step],
        meta: dict[str, Any],
    ) -> Optional[str]:
        if self.future_summary_mode == "predicted":
            return self._predict_future_summary(history)
        if self.future_summary_mode == "truth":
            return self._truth_future_summary(meta)
        return None

    def validate_trajectories(self, trajectories: Sequence[Trajectory]) -> None:
        if self.future_summary_mode != "truth":
            return
        missing: list[str] = []
        for traj in trajectories:
            for step in traj.steps:
                key = make_future_summary_cache_key(
                    traj.benchmark,
                    traj.model,
                    traj.split,
                    traj.id,
                    step.index,
                )
                if key not in self.future_summary_cache:
                    missing.append(key)
                    if len(missing) >= 10:
                        break
            if len(missing) >= 10:
                break
        if missing:
            raise ValueError(
                "future summary cache is missing records for this eval run; "
                f"first missing keys: {missing}"
            )

    def judge(self, history: Sequence[Step], meta: dict[str, Any]) -> Verdict:
        future_summary = self._resolve_future_summary(history, meta)
        user_prompt = self._render_user_prompt(history, future_summary=future_summary)
        messages = [{"role": "user", "content": user_prompt}]
        messages = fit_messages_to_char_budget(messages, budget=self.max_input_chars)
        try:
            text = self._request(messages)
        except Exception as exc:  # noqa: BLE001 — guard must not crash replay
            logger.warning("vllm guard request failed: %s", exc)
            return Verdict(flagged=False, reason=f"request_error: {exc}")
        verdict = self.parse_response(text)
        if not verdict.raw:
            verdict.raw = {"response_text": text}
        if future_summary is not None:
            verdict.raw.setdefault("future_summary_mode", self.future_summary_mode)
            verdict.raw.setdefault("future_summary", future_summary)
            if self.future_summary_mismatch:
                verdict.raw.setdefault("future_summary_mismatch", True)
        return verdict
