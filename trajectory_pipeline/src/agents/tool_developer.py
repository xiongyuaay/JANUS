from __future__ import annotations

import json
from typing import Optional, Tuple

from modules.log import Log
from modules.parse import parse_reply
from prompts.system import TOOL_DEVELOPER_SYSTEM_PROMPT

from .base_agent import BaseAgent


class ToolDeveloper(BaseAgent):
    """Builds the tool schema and implementation."""

    def __init__(self, config, system_prompt: str = TOOL_DEVELOPER_SYSTEM_PROMPT):
        super().__init__(config, system_prompt, "ToolDeveloper")

    def run(self, task_from_orchestrator: str, instruction_output: dict) -> Tuple[dict, Optional[str]]:
        Log.section("Tool Development")
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
        }
        response = self.call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            result, code_block = parse_reply(response)
        except Exception:
            self.dump_raw_response("tool_parse_failure", response)
            raise
        Log.success("Tool development complete", self.name)
        Log.json_preview(result)
        if code_block:
            Log.info("Tool code block detected", self.name)
        else:
            Log.warning("No tool code block detected", self.name)
        return result, code_block

    async def run_async(self, task_from_orchestrator: str, instruction_output: dict) -> Tuple[dict, Optional[str]]:
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
        }
        response = await self.call_llm_async(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            return parse_reply(response)
        except Exception:
            self.dump_raw_response("tool_parse_failure", response)
            raise
