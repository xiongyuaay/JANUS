from __future__ import annotations

import json
from typing import Any, Dict, List

from modules.log import Log
from modules.parse import parse_json
from prompts.system import TRAJECTORY_AGENT_SYSTEM_PROMPT

from .base_agent import BaseAgent


class TrajectoryAgent(BaseAgent):
    """Generates the next trajectory step."""

    def __init__(self, config, system_prompt: str = TRAJECTORY_AGENT_SYSTEM_PROMPT):
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

    def run_text(self, input_data: Dict[str, Any]) -> str:
        """Generate one raw trajectory step."""

        Log.section("Trajectory Step")
        response = self.call_llm(json.dumps(input_data, ensure_ascii=False, indent=2))
        Log.success("Raw trajectory step generated", self.name)
        return response

    def run_react(self, messages: List[Dict[str, str]]) -> str:
        """Generate one raw ToolSafe ReAct step from chat history."""

        Log.section("Trajectory Step")
        response = self.call_llm_messages(messages)
        Log.success("Raw ReAct trajectory step generated", self.name)
        return response

    async def run_async(self, input_data: Dict[str, Any]) -> dict:
        response = await self.call_llm_async(json.dumps(input_data, ensure_ascii=False, indent=2))
        try:
            return parse_json(response)
        except Exception:
            self.dump_raw_response("trajectory_parse_failure", response)
            raise
