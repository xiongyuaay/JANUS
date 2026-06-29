"""Externalised judge prompts.

Loads the default user prompt template from a sibling `.txt` file so the text
can be edited without touching Python code.
"""
from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent

DEFAULT_USER_TEMPLATE: str = (_PROMPT_DIR / "judge_user.txt").read_text(encoding="utf-8")
PREDICT_FUTURE_SUMMARY_TEMPLATE: str = (
    _PROMPT_DIR / "predict_future_summary.txt"
).read_text(encoding="utf-8")
TRUTH_FUTURE_SUMMARY_TEMPLATE: str = (
    _PROMPT_DIR / "truth_future_summary.txt"
).read_text(encoding="utf-8")
JUDGE_USER_WITH_PREDICTED_SUMMARY_TEMPLATE: str = (
    _PROMPT_DIR / "judge_user_with_predicted_summary.txt"
).read_text(encoding="utf-8")
JUDGE_USER_WITH_TRUTH_SUMMARY_TEMPLATE: str = (
    _PROMPT_DIR / "judge_user_with_truth_summary.txt"
).read_text(encoding="utf-8")

__all__ = [
    "DEFAULT_USER_TEMPLATE",
    "PREDICT_FUTURE_SUMMARY_TEMPLATE",
    "TRUTH_FUTURE_SUMMARY_TEMPLATE",
    "JUDGE_USER_WITH_PREDICTED_SUMMARY_TEMPLATE",
    "JUDGE_USER_WITH_TRUTH_SUMMARY_TEMPLATE",
]
