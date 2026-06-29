from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Optional

from jinja2 import Template


_TPAST_HEADER_PATTERN = r"\n\s*(?:observed\s+trajectory\s+prefix|tpast)\s*:"

_INSTRUCTION_PATTERNS = [
    re.compile(
        rf"(?:^|\n)\s*instruction\s*:\s*(.*?)(?:{_TPAST_HEADER_PATTERN}|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        rf"(?:^|\n)\s*question\s*:\s*(.*?)(?:{_TPAST_HEADER_PATTERN}|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
]

_TPAST_PATTERNS = [
    re.compile(r"(?:^|\n)\s*observed\s+trajectory\s+prefix\s*:\s*(.*)\Z", re.IGNORECASE | re.DOTALL),
    re.compile(r"(?:^|\n)\s*tpast\s*:\s*(.*)\Z", re.IGNORECASE | re.DOTALL),
]

_SINGLE_BRACE_VAR_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return repr(value)



def split_instruction_tpast(raw_input: Any) -> tuple[str, str]:
    text = _safe_str(raw_input).strip()
    if not text:
        return "", ""

    instruction = ""
    tpast = ""

    for pattern in _INSTRUCTION_PATTERNS:
        match = pattern.search(text)
        if match:
            instruction = match.group(1).strip()
            break

    for pattern in _TPAST_PATTERNS:
        match = pattern.search(text)
        if match:
            tpast = match.group(1).strip()
            break

    if not instruction and not tpast:
        return text, ""

    if not instruction:
        instruction = text

    return instruction, tpast



def build_prompt_context(
    raw_input: Any,
    summary: Optional[Any] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    content = _safe_str(raw_input)
    summary_text = _safe_str(summary)
    instruction, tpast = split_instruction_tpast(content)

    context: dict[str, Any] = {
        "content": content,
        "input": content,
        "raw_input": content,
        "prompt": content,
        "question": instruction or content,
        "instruction": instruction or content,
        "tpast": tpast,
        "trajectory_prefix": tpast,
        "observed_trajectory_prefix": tpast,
        "summary": summary_text,
        "caption": summary_text,
        "future_trajectory_summary": summary_text,
    }

    if extra:
        context.update(extra)

    return context



def _render_single_brace_placeholders(template_text: str, context: dict[str, Any]) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            return _safe_str(context[key])
        return match.group(0)

    return _SINGLE_BRACE_VAR_PATTERN.sub(_replace, template_text)


@lru_cache(maxsize=None)
def load_template_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read().strip()



def render_template_string(
    template_text: str,
    raw_input: Any,
    summary: Optional[Any] = None,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    context = build_prompt_context(raw_input, summary=summary, extra=extra)
    rendered = Template(template_text.strip()).render(**context)
    rendered = _render_single_brace_placeholders(rendered, context)
    if summary is not None and "<caption>" in rendered:
        rendered = rendered.replace("<caption>", context["caption"])
    return rendered



def render_prompt_template(
    template_path: str,
    raw_input: Any,
    summary: Optional[Any] = None,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    abs_path = os.path.abspath(template_path)
    template_text = load_template_text(abs_path)
    return render_template_string(template_text, raw_input, summary=summary, extra=extra)
