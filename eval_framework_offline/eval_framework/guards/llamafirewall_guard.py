"""LlamaFirewall adapter.

Wraps `LlamaFirewall.scan(current_message, trace=history_so_far)` and maps
the canonical `Step` role to the appropriate LlamaFirewall `Message` subclass.

Scanner composition is controlled via constructor `scanners` arg (forwarded to
`LlamaFirewall(...)`) or `usecase` for the prebuilt CHAT_BOT/CODING_ASSISTANT
configurations.

The dependency on `llamafirewall` is imported lazily so the rest of the
framework (loaders, runner, stub guard) remains importable in environments
that don't have the package installed.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Optional, Sequence

from ..schema import Step
from .base import Guard, Verdict

logger = logging.getLogger(__name__)


def _build_messages(history: Sequence[Step]) -> list[Any]:
    from llamafirewall import (  # lazy
        AssistantMessage,
        SystemMessage,
        ToolMessage,
        UserMessage,
    )

    msgs: list[Any] = []
    for s in history:
        if s.role == "system":
            msgs.append(SystemMessage(content=s.content))
        elif s.role == "user":
            msgs.append(UserMessage(content=s.content))
        elif s.role == "assistant":
            tool_calls = None
            if s.tool_calls:
                tool_calls = [
                    {"name": tc.name, "arguments": tc.arguments, "id": tc.call_id}
                    for tc in s.tool_calls
                ]
            msgs.append(AssistantMessage(content=s.content, tool_calls=tool_calls))
        elif s.role == "tool":
            msgs.append(ToolMessage(content=s.content))
        else:
            logger.debug("llamafirewall: dropping step %d with role %r", s.index, s.role)
    return msgs


class LlamaFirewallGuard(Guard):
    name = "llamafirewall"

    def __init__(
        self,
        scanners: Optional[dict[Any, list[Any]]] = None,
        *,
        usecase: Optional[str] = None,
    ) -> None:
        """Instantiate the underlying LlamaFirewall.

        Exactly one of `scanners` or `usecase` should be provided. If neither
        is given, LlamaFirewall's default scanner config is used.

        `scanners` may be passed in user-facing string form, e.g.
        `{"user": ["prompt_guard"], "tool": ["prompt_guard"]}`. These get
        translated to the LlamaFirewall enum types at construction time.
        """
        from llamafirewall import LlamaFirewall, Role, ScannerType, UseCase  # lazy

        if scanners is not None and usecase is not None:
            raise ValueError("Provide only one of `scanners` or `usecase`.")

        if usecase is not None:
            self._lf = LlamaFirewall.from_usecase(UseCase[usecase.upper()])
        elif scanners is not None:
            resolved: dict[Any, list[Any]] = {}
            for role_key, types in scanners.items():
                role_enum = Role[role_key.upper()] if isinstance(role_key, str) else role_key
                resolved_types = [
                    ScannerType[t.upper()] if isinstance(t, str) else t for t in types
                ]
                resolved[role_enum] = resolved_types
            self._lf = LlamaFirewall(scanners=resolved)
        else:
            self._lf = LlamaFirewall()

    def judge(self, history: Sequence[Step], meta: dict[str, Any]) -> Verdict:
        from llamafirewall import ScanDecision  # lazy

        messages = _build_messages(history)
        if not messages:
            return Verdict(flagged=False, reason="empty_history")
        current = messages[-1]
        trace = messages[:-1]
        try:
            result = self._lf.scan(current, trace=trace)
        except Exception as exc:  # noqa: BLE001 — guard must not crash replay
            logger.warning("llamafirewall scan failed: %s", exc)
            return Verdict(flagged=False, reason=f"scan_error: {exc}")

        flagged = result.decision in {
            ScanDecision.BLOCK,
            ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED,
        }
        return Verdict(
            flagged=flagged,
            score=float(result.score),
            reason=result.reason,
            raw={
                "decision": result.decision.value,
                "status": result.status.value,
                "score": result.score,
                "reason": result.reason,
            },
        )
