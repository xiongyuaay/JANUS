# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
from typing import Any

REWARD_NAME = "janus_predict_judge"
REWARD_TYPE = "batch"

_VALID_LABELS = {"safe", "unsafe", "potential_unsafe"}
_RISK_LEVEL = {"safe": 0, "potential_unsafe": 1, "unsafe": 2}
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.IGNORECASE | re.DOTALL)
# Long alternations come first so that `unsafe` does not greedily match the suffix
# of `potential[\s_]?unsafe`. We canonicalize to `potential_unsafe` (underscore)
# at extraction time, regardless of which form the model emitted.
_LABEL_GROUP = r"(potential[\s_]?unsafe|unsafe|safe)"
_BOXED_LABEL_RE = re.compile(r"\\boxed\{\s*" + _LABEL_GROUP + r"\s*\}", flags=re.IGNORECASE)
_JSON_LABEL_RE = re.compile(r'"label"\s*:\s*"?\s*' + _LABEL_GROUP + r'\s*"?', flags=re.IGNORECASE)
_TAIL_LABEL_RE = re.compile(
    r"(?:^|[\n\r])\s*(?:label\s*[:=]\s*)?" + _LABEL_GROUP + r"\s*$", flags=re.IGNORECASE
)


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return repr(value)


def _canonicalize_label(text: str) -> str:
    """Map any of `potential unsafe` / `potential_unsafe` / `potentialunsafe` to
    the canonical `potential_unsafe`. `safe` and `unsafe` are already canonical."""
    t = _safe_str(text).strip().lower()
    t = re.sub(r"\s+", " ", t)
    if re.fullmatch(r"potential[\s_]?unsafe", t):
        return "potential_unsafe"
    return t


def _normalize_gold_label(label: Any) -> str:
    text = _canonicalize_label(label)
    if text in _VALID_LABELS:
        return text
    return ""


def _strip_think_blocks(text: str) -> str:
    return _THINK_BLOCK_RE.sub(" ", _safe_str(text))


def _extract_last_group(pattern: re.Pattern[str], text: str) -> str:
    matches = list(pattern.finditer(text))
    if not matches:
        return ""
    return _canonicalize_label(matches[-1].group(1))


def extract_label(response: str) -> str:
    text = _safe_str(response).strip()
    if not text:
        return ""

    text_wo_think = _strip_think_blocks(text).strip()

    pred = _extract_last_group(_BOXED_LABEL_RE, text_wo_think)
    if pred:
        return pred

    pred = _extract_last_group(_JSON_LABEL_RE, text_wo_think)
    if pred:
        return pred

    pred = _extract_last_group(_TAIL_LABEL_RE, text_wo_think)
    if pred:
        return pred

    return ""


_FORMAT_RE = re.compile(
    r"\s*<think>.*?</think>.*?\\boxed\{\s*" + _LABEL_GROUP + r"\s*\}.*",
    re.IGNORECASE | re.DOTALL,
)


def format_reward(response: str) -> float:
    return 1.0 if re.fullmatch(_FORMAT_RE, _safe_str(response).strip()) else 0.0


def accuracy_reward(response: str, ground_truth: Any) -> float:
    pred = extract_label(response)
    gold = _normalize_gold_label(ground_truth)
    return 1.0 if pred and pred == gold else 0.0


def risk_level_reward(response: str, ground_truth: Any) -> float:
    pred = extract_label(response)
    gold = _normalize_gold_label(ground_truth)
    if pred not in _RISK_LEVEL or gold not in _RISK_LEVEL:
        return 0.0
    max_distance = max(_RISK_LEVEL.values()) - min(_RISK_LEVEL.values())
    distance = abs(_RISK_LEVEL[pred] - _RISK_LEVEL[gold])
    return 1.0 - float(distance) / float(max_distance)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "t"}


def compute_score(
    reward_inputs: list[dict[str, Any]],
    format_weight: float = 0.1,
    use_risk_level_reward: bool = True,
) -> list[dict[str, float]]:
    scores: list[dict[str, float]] = []
    weight = float(format_weight)
    weight = max(min(weight, 1.0), 0.0)
    use_risk_level = _as_bool(use_risk_level_reward)
    for reward_input in reward_inputs:
        response = _safe_str(reward_input.get("response", ""))
        gold_label = reward_input.get("ground_truth", reward_input.get("label", ""))
        format_score = float(format_reward(response))
        accuracy_score = float(accuracy_reward(response, gold_label))
        risk_level_score = float(risk_level_reward(response, gold_label))
        label_score = risk_level_score if use_risk_level else accuracy_score
        overall = (1.0 - weight) * label_score + weight * format_score
        scores.append(
            {
                "overall": float(overall),
                "format": float(format_score),
                "accuracy": float(accuracy_score),
                "risk_level": float(risk_level_score),
            }
        )
    return scores
