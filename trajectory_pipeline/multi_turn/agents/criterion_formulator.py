from __future__ import annotations

import json
from typing import Optional

from multi_turn.modules.log import Log
from multi_turn.modules.parse import parse_json
from multi_turn.prompts.system import CRITERION_FORMULATOR_SYSTEM_PROMPT

from .base_agent import BaseAgent


class CriterionFormulator(BaseAgent):
    """Writes evaluator criteria."""

    def __init__(self, config):
        super().__init__(config, CRITERION_FORMULATOR_SYSTEM_PROMPT, "CriterionFormulator")

    def run(
        self,
        task_from_orchestrator: str,
        instruction_output: dict,
        tool_output: dict,
        tool_code: Optional[str],
    ) -> dict:
        Log.section("Criteria Formulation")
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
            "output_from_Tool_Developer": tool_output,
        }
        user_message = json.dumps(payload, ensure_ascii=False, indent=2)
        if tool_code:
            user_message += f"\n\n```python\n{tool_code}\n```"
        response = self.call_llm(user_message)
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("criterion_parse_failure", response)
            raise
        Log.success("Criteria formulation complete", self.name)
        Log.json_preview(result)
        return result

    async def run_async(
        self,
        task_from_orchestrator: str,
        instruction_output: dict,
        tool_output: dict,
        tool_code: Optional[str],
    ) -> dict:
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
            "output_from_Tool_Developer": tool_output,
        }
        user_message = json.dumps(payload, ensure_ascii=False, indent=2)
        if tool_code:
            user_message += f"\n\n```python\n{tool_code}\n```"
        response = await self.call_llm_async(user_message)
        try:
            return parse_json(response)
        except Exception:
            self.dump_raw_response("criterion_parse_failure", response)
            raise
