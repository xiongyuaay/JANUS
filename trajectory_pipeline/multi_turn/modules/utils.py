from __future__ import annotations

import re


def _split_response(raw: str) -> str:
    """Remove reasoning blocks before structured content parsing."""

    cleaned = re.sub(r"<(?:think|reason)>.*?</(?:think|reason)>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"^.*?</(?:think|reason)>", "", cleaned, flags=re.DOTALL)
    if cleaned.startswith("Thinking Process:"):
        first_json = cleaned.find("{")
        if first_json != -1:
            cleaned = cleaned[first_json:]
    return cleaned.strip()


def _normalize_json_text(raw: str) -> str:
    """Repair a few frequent JSON formatting mistakes from model outputs."""

    cleaned = raw
    cleaned = re.sub(r'"type"\s*:\s*"type"\s*:\s*"', '"type": "', cleaned)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned
