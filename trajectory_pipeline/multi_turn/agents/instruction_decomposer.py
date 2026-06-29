from __future__ import annotations

import json
from typing import Any, Dict

from multi_turn.modules.log import Log
from multi_turn.modules.parse import parse_json
from multi_turn.prompts.system import INSTRUCTION_DECOMPOSER_STRATEGY7_PROMPT

from .base_agent import BaseAgent


class InstructionDecomposer(BaseAgent):
    """Decomposes one master instruction into sequential user turns."""

    def __init__(
        self,
        config,
        system_prompt: str = INSTRUCTION_DECOMPOSER_STRATEGY7_PROMPT,
        name: str = "InstructionDecomposer",
    ):
        super().__init__(config, system_prompt, name)

    def run(self, task_from_orchestrator: str, instruction_output: Dict[str, Any]) -> dict:
        Log.section("Instruction Decomposition")
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
        }
        response = self.call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("instruction_decomposition_parse_failure", response)
            raise
        Log.success("Instruction decomposition complete", self.name)
        Log.json_preview(result)
        return result

    async def run_async(self, task_from_orchestrator: str, instruction_output: Dict[str, Any]) -> dict:
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
        }
        response = await self.call_llm_async(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            return parse_json(response)
        except Exception:
            self.dump_raw_response("instruction_decomposition_parse_failure", response)
            raise
