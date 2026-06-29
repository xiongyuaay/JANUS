from __future__ import annotations

import json
from typing import Any, Dict

from modules.log import Log
from modules.parse import parse_json

from .base_agent import BaseAgent


class TrajectoryReviewer(BaseAgent):
    """Reviews generated trajectories for strategy compliance."""

    def __init__(self, config, system_prompt: str):
        super().__init__(config, system_prompt, "TrajectoryReviewer")

    def run(self, input_data: Dict[str, Any]) -> dict:
        Log.section("Trajectory Review")
        response = self.call_llm(json.dumps(input_data, ensure_ascii=False, indent=2))
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("trajectory_review_parse_failure", response)
            raise
        Log.success("Trajectory review completed", self.name)
        Log.json_preview(result)
        return result

    async def run_async(self, input_data: Dict[str, Any]) -> dict:
        response = await self.call_llm_async(json.dumps(input_data, ensure_ascii=False, indent=2))
        try:
            return parse_json(response)
        except Exception:
            self.dump_raw_response("trajectory_review_parse_failure", response)
            raise