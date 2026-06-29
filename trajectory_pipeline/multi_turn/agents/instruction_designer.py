from __future__ import annotations

import json
from typing import Any, Dict

from multi_turn.modules.log import Log
from multi_turn.modules.parse import parse_json
from multi_turn.prompts.system import INSTRUCTION_DESIGNER_SYSTEM_PROMPT

from .base_agent import BaseAgent


class InstructionDesigner(BaseAgent):
    """Writes the final instruction."""

    def __init__(self, config, system_prompt: str = INSTRUCTION_DESIGNER_SYSTEM_PROMPT, name: str = "InstructionDesigner"):
        super().__init__(config, system_prompt, name)

    def _run_with_payload(self, payload: Dict[str, Any], failure_stage: str) -> dict:
        response = self.call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response(failure_stage, response)
            raise
        Log.success("Instruction design complete", self.name)
        Log.json_preview(result)
        return result

    def run(self, task_from_orchestrator: str) -> dict:
        Log.section("Instruction Design")
        payload = {"task_from_Orchestrator": task_from_orchestrator}
        return self._run_with_payload(payload, "instruction_parse_failure")

    async def run_async(self, task_from_orchestrator: str) -> dict:
        payload = {"task_from_Orchestrator": task_from_orchestrator}
        response = await self.call_llm_async(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            return parse_json(response)
        except Exception:
            self.dump_raw_response("instruction_parse_failure", response)
            raise
