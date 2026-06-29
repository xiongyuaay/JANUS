"""ASB ReAct-uncensor loader (light + standard variants).

On-disk layout (two independent variants):

    <data_root>/asb_react_uncensor_light/{clean,DPI,MP,OPI}/<agent>/<task>.json
    <data_root>/asb_react_uncensor_standard/{clean,DPI,MP,OPI}/<agent>/<task>.json

``clean`` -> canonical ``benign`` split.
``DPI`` / ``MP`` / ``OPI`` -> all map to canonical ``adversarial``; the
original attack method is preserved in ``Trajectory.extra.attack_method``
so downstream analysis can stratify by it.

Each file carries ``messages`` (OpenAI ReAct), plus top-level
``utility`` / ``security`` booleans once annotated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..schema import Split
from .generic import GenericSplitLoader


class _ASBBase(GenericSplitLoader):
    split_map = {
        "clean": "benign",
        "DPI": "adversarial",
        "MP": "adversarial",
        "OPI": "adversarial",
    }
    has_model_dim = False

    def _extra_for(
        self,
        raw: dict[str, Any],
        *,
        on_disk_split: str,
        canonical_split: Split,
        json_path: Path,
    ) -> dict[str, Any]:
        return {
            "attack_method": raw.get("attack_method") or on_disk_split,
            "attack_type": raw.get("attack_type"),
            "asb_attack_type": raw.get("asb_attack_type"),
            "agent_name": raw.get("agent_name") or json_path.parent.name,
            "attacker_tool": raw.get("attacker_tool"),
            "attack_success": raw.get("attack_success"),
            "workflow_success": raw.get("workflow_success"),
            "annotation_status": raw.get("annotation_status"),
        }


class ASBLightLoader(_ASBBase):
    name = "asb_light"
    dir_name = "asb_react_uncensor_light"


class ASBStandardLoader(_ASBBase):
    name = "asb_standard"
    dir_name = "asb_react_uncensor_standard"
