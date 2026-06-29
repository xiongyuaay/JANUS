from __future__ import annotations

import json
from typing import Any, Dict

from multi_turn.modules.log import Log
from multi_turn.modules.parse import parse_json
from multi_turn.prompts.system import TRAJECTORY_AGENT_MULTI_TURN_DECOMPOSITION_PROMPT

from .base_agent import BaseAgent


class TrajectoryAgent(BaseAgent):
    """Generates the next trajectory step."""

    def __init__(self, config, system_prompt: str = TRAJECTORY_AGENT_MULTI_TURN_DECOMPOSITION_PROMPT):
        super().__init__(config, system_prompt, "TrajectoryAgent")

    def run(self, input_data: Dict[str, Any]) -> dict:
        Log.section("Trajectory Step")
        response = self.call_llm(json.dumps(input_data, ensure_ascii=False, indent=2))
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("trajectory_parse_failure", response)
            raise
        Log.success("Trajectory step generated", self.name)
        Log.json_preview(result)
        return result

    async def run_async(self, input_data: Dict[str, Any]) -> dict:
        response = await self.call_llm_async(json.dumps(input_data, ensure_ascii=False, indent=2))
        try:
            return parse_json(response)
        except Exception:
            self.dump_raw_response("trajectory_parse_failure", response)
            raise
