from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from multi_turn.modules.utils import _normalize_json_text, _split_response


def _extract_first_json_object(text: str) -> str:
    """Return the first balanced JSON object found in text."""

    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No JSON object found", text, 0)

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise json.JSONDecodeError("Unterminated JSON object", text, start)


def parse_json(text: str) -> dict:
    """Parse the first JSON object returned by a worker response."""

    stripped = _split_response(text)
    if "```json" in stripped:
        start = stripped.find("```json") + len("```json")
        end = stripped.find("```", start)
        stripped = stripped[start:end].strip()
    else:
        stripped = re.sub(r"```python.*?```", "", stripped, flags=re.DOTALL).strip()
        stripped = _extract_first_json_object(stripped)
    stripped = _normalize_json_text(stripped)
    return json.loads(stripped)


def parse_reply(text: str) -> Tuple[dict, Optional[str]]:
    """Extract a JSON object and the first Python code block from a response."""

    code_block = None
    match = re.search(r"```python\s*(.*?)```", text, flags=re.DOTALL)
    if match:
        code_block = match.group(1).strip()

    json_obj = parse_json(text)
    if not code_block:
        artifacts = json_obj.get("artifacts", {}) if isinstance(json_obj, dict) else {}
        mcp_file = artifacts.get("mcp_file", {}) if isinstance(artifacts, dict) else {}
        inline_code = mcp_file.get("content_py") if isinstance(mcp_file, dict) else None
        if isinstance(inline_code, str) and inline_code.strip():
            code_block = inline_code.strip()
    return json_obj, code_block
