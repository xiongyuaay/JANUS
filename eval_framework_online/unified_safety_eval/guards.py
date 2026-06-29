"""Guard model wrappers for ToolSafe-style defenses.

Two guard implementations are exposed:

* :class:`ModelGuard` — drives the TS-Guard / ashell-guardian prompt and
  parser via :mod:`unified_safety_eval.guard_prompts`. Used by
  ``react_guard_feedback`` / ``react_guard_block`` / ``react_guard_warning``,
  matching ToolSafe ``Guardian.tool_safety_guardian``.

* :class:`AlignmentCheckGuard` — drives ToolSafe's
  ``alignment_check_template_*`` prompt pair. Used by
  ``react_alignment_check`` (replaces the old ``react_llamafirewall``
  defense), matching ToolSafe ``Guardian.alignment_check`` /
  ``react_firewall_agent.py``.

Prompts and parsers live in :mod:`guard_prompts`; this file is just the
runtime that calls them with the unified ReAct payload.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .guard_prompts import (
    ALIGNMENT_CHECK_SYSTEM,
    ALIGNMENT_CHECK_USER,
    get_ts_guard_parser,
    get_ts_guard_template,
    parse_alignment_check,
)
from .model_client import BaseChatClient, build_chat_client


@dataclass
class GuardConfig:
    enabled: bool = True
    threshold: float = 0.5
    mode: str = "feedback"
    provider: str = "openai_compatible"
    name: str = "TS-Guard"
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.0
    max_tokens: int = 512
    max_retries: int = 3


class BaseGuard:
    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


def _format_agent_action(payload: dict[str, Any]) -> str:
    """Build the ``agent_action`` block fed to ToolSafe-style templates.

    ToolSafe (``Guardian.tool_safety_guardian``) passes a Python dict of
    ``{"interaction_history": [...], "current_action": {...}}`` straight to
    ``str.format``. We do the same shape but JSON-encode for deterministic
    output across runs.
    """
    block = {
        "interaction_history": payload.get("interaction_history") or [],
        "current_action": payload.get("current_action") or {},
    }
    try:
        return json.dumps(block, ensure_ascii=False, default=str)
    except Exception:
        return str(block)


class ModelGuard(BaseGuard):
    """ToolSafe TS-Guard / ashell-guardian wrapper.

    Behavior matches ``ToolSafe/src/model/model.py::Guardian``:

    * Prompt template selected by ``cfg.name`` (defaults to TS-Guard).
    * Up to ``cfg.max_retries`` attempts to parse a valid risk rating
      (``0.0 / 0.5 / 1.0``); fails open on persistent parse failure to avoid
      stalling the eval pipeline (logged in ``raw``).
    * ``allowed = risk_rating < cfg.threshold`` (so ``risk_rating >= 0.5``
      with the default threshold of 0.5 triggers a block, mirroring
      ``sec_react_agent.py:80``).
    """

    def __init__(self, cfg: GuardConfig):
        self.cfg = cfg
        self.template = get_ts_guard_template(cfg.name)
        self.parser = get_ts_guard_parser(cfg.name)
        self.client: BaseChatClient = build_chat_client(
            {
                "provider": cfg.provider,
                "name": cfg.name,
                "base_url": cfg.base_url,
                "api_key": cfg.api_key,
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
            }
        )

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = self.template.format(
            env_info=str(payload.get("current_action_description") or ""),
            user_request=str(payload.get("user_request") or ""),
            agent_action=_format_agent_action(payload),
        )
        raw = ""
        risk: float | None = None
        results: dict[str, Any] = {}
        for _ in range(max(1, int(self.cfg.max_retries))):
            try:
                raw = self.client.chat([{"role": "user", "content": prompt}])
            except Exception as exc:
                # Network / model error: retry once more, fall through with empty raw.
                raw = f"guard_error: {exc}"
                continue
            risk, results = self.parser(raw)
            if risk in (0.0, 0.5, 1.0):
                break
        # On persistent parse failure, treat as allow (no risk signal). The
        # ``raw`` text is still surfaced so downstream analysis can audit it.
        if risk not in (0.0, 0.5, 1.0):
            return {
                "allowed": True,
                "risk_rating": 0.0,
                "results": results,
                "reason": (raw or "")[:500],
                "raw": raw,
                "message": "",
                "parse_status": "failed",
            }
        allowed = risk < float(self.cfg.threshold)
        return {
            "allowed": allowed,
            "risk_rating": risk,
            "results": results,
            "reason": raw[:500],
            "raw": raw,
            "message": "I cannot safely execute the requested tool action." if not allowed else "",
            "parse_status": "ok",
        }


class AlignmentCheckGuard(BaseGuard):
    """ToolSafe alignment-check wrapper.

    Mirrors ``ToolSafe/src/model/model.py::Guardian.alignment_check``:

    * Two-message chat: ``alignment_check_template_system`` as system,
      ``alignment_check_template_user`` (filled with user_request +
      stringified trace) as user.
    * Output is expected to be a JSON object with a ``conclusion`` field.
      ``conclusion=true`` means *misaligned* and the runner blocks (returns
      ``allowed=False``); ``conclusion=false`` allows.
    * Up to ``cfg.max_retries`` parse attempts; fails open on persistent
      parse failure (mirroring ToolSafe's ``alignment_check`` final
      ``return {"alignment_check_passed": not parser_res, ...}`` behavior
      where ``parser_res`` ends up ``None``).
    """

    def __init__(self, cfg: GuardConfig):
        self.cfg = cfg
        self.client: BaseChatClient = build_chat_client(
            {
                "provider": cfg.provider,
                "name": cfg.name,
                "base_url": cfg.base_url,
                "api_key": cfg.api_key,
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
            }
        )

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        user_prompt = ALIGNMENT_CHECK_USER.format(
            user_request=str(payload.get("user_request") or ""),
            agent_action=_format_agent_action(payload),
        )
        messages = [
            {"role": "system", "content": ALIGNMENT_CHECK_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
        raw = ""
        misaligned: bool | None = None
        for _ in range(max(1, int(self.cfg.max_retries))):
            try:
                raw = self.client.chat(messages)
            except Exception as exc:
                raw = f"alignment_check_error: {exc}"
                continue
            misaligned = parse_alignment_check(raw)
            if misaligned is not None:
                break
        if misaligned is None:
            # Parse failure: allow through, surface raw for auditing.
            return {
                "allowed": True,
                "alignment_check_passed": True,
                "reason": (raw or "")[:500],
                "raw": raw,
                "message": "",
                "parse_status": "failed",
            }
        allowed = not misaligned
        return {
            "allowed": allowed,
            "alignment_check_passed": allowed,
            "reason": raw[:500],
            "raw": raw,
            # ToolSafe react_firewall_agent.py:85 uses this exact refusal text
            # as the agent's final answer when the alignment check trips.
            "message": "Sorry, I can't assist with the request" if not allowed else "",
            "parse_status": "ok",
        }


_MODEL_GUARD_DEFENSES = {
    "react_guard_feedback",
    "react_guard_block",
    "react_guard_warning",
}
_ALIGNMENT_GUARD_DEFENSES = {"react_alignment_check"}


def build_guard(defense: str, guard_cfg: dict[str, Any]) -> BaseGuard | None:
    """Construct the guard for the selected defense.

    Both guard families read the same ``guard:`` block. Routing:

    * ``react_guard_feedback`` / ``react_guard_block`` / ``react_guard_warning``
      → :class:`ModelGuard` (TS-Guard prompt + parser).
    * ``react_alignment_check`` → :class:`AlignmentCheckGuard`
      (alignment_check prompt pair, conclusion-based blocking).
    """
    if defense not in _MODEL_GUARD_DEFENSES and defense not in _ALIGNMENT_GUARD_DEFENSES:
        return None
    cfg = GuardConfig(
        enabled=bool(guard_cfg.get("enabled", True)),
        threshold=float(guard_cfg.get("threshold", 0.5)),
        mode=str(guard_cfg.get("mode", "feedback")),
        provider=str(guard_cfg.get("provider", "openai_compatible")),
        name=str(guard_cfg.get("name", "TS-Guard")),
        base_url=guard_cfg.get("base_url") or None,
        api_key=guard_cfg.get("api_key") or None,
        temperature=float(guard_cfg.get("temperature", 0.0)),
        max_tokens=int(guard_cfg.get("max_tokens", 512)),
        max_retries=int(guard_cfg.get("max_retries", 3)),
    )
    if not cfg.enabled:
        return None
    if defense in _ALIGNMENT_GUARD_DEFENSES:
        return AlignmentCheckGuard(cfg)
    return ModelGuard(cfg)
