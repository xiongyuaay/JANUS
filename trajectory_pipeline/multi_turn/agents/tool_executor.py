from __future__ import annotations

import json
from typing import Any, Dict

from multi_turn.modules.log import Log
from multi_turn.modules.parse import parse_json
from multi_turn.prompts.system import TOOL_EXECUTOR_SYSTEM_PROMPT

from .base_agent import BaseAgent


class ToolExecutor(BaseAgent):
    """Simulates one tool call result from a tool specification."""

    def __init__(self, config, system_prompt: str = TOOL_EXECUTOR_SYSTEM_PROMPT):
        super().__init__(config, system_prompt, "ToolExecutor")

    def run(self, input_data: Dict[str, Any]) -> dict:
        Log.section("Tool Execution")
        response = self.call_llm(json.dumps(input_data, ensure_ascii=False, indent=2))
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("tool_executor_parse_failure", response)
            raise
        Log.success("Tool output generated", self.name)
        Log.json_preview(result)
        return result

    async def run_async(self, input_data: Dict[str, Any]) -> dict:
        response = await self.call_llm_async(json.dumps(input_data, ensure_ascii=False, indent=2))
        try:
            return parse_json(response)
        except Exception:
            self.dump_raw_response("tool_executor_parse_failure", response)
            raise
