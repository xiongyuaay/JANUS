from __future__ import annotations

import json
from typing import Optional

from config.llm import LLMConfig
from modules.log import Log
from modules.parse import parse_json
from prompts.system import ORCHESTRATOR_SYSTEM_PROMPT

from .base_agent import BaseAgent


class OrchestratorAgent(BaseAgent):
    """LLM-driven orchestrator for dispatch, merge, and revision."""

    def __init__(self, config: LLMConfig, system_prompt: str = ORCHESTRATOR_SYSTEM_PROMPT):
        super().__init__(config, system_prompt, "Orchestrator")

    def dispatch(self, template: str) -> dict:
        """Ask the LLM orchestrator to split the authoritative compiled prompt."""

        Log.section("Stage: DISPATCH")
        payload = {
            "stage": "DISPATCH",
            "template": template,
        }
        Log.info("Sending DISPATCH request to Orchestrator", self.name)
        response = self.call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("dispatch_parse_failure", response)
            raise
        Log.success("Received DISPATCH result", self.name)
        Log.json_preview(result)
        return result

    def revision(self, feedback: str, previous_case: Optional[dict]) -> dict:
        """Ask the LLM orchestrator to apply human feedback."""

        Log.section("Stage: REVISION")
        payload = {
            "stage": "REVISION",
            "feedback": feedback,
            "previous_case": previous_case or {},
        }
        Log.info("Sending REVISION request to Orchestrator", self.name)
        response = self.call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("revision_parse_failure", response)
            raise
        Log.success("Received REVISION result", self.name)
        Log.json_preview(result)
        return result

    def merge(
        self,
        instruction_output: dict,
        tool_output: dict,
        criterion_output: dict,
        tool_code: Optional[str],
        environment_output: Optional[dict] = None,
    ) -> dict:
        """Ask the LLM orchestrator to combine worker outputs into one case.

        ``environment_output`` is passed through only for the
        strategy_2_harmful_environment pipeline. For all other strategies the
        orchestrator system prompt does not mention environment_strategy, so
        callers omit this argument and behavior is unchanged.
        """

        Log.section("Stage: MERGE")
        payload = {
            "stage": "MERGE",
            "instruction_designer": instruction_output,
            "tool_developer": tool_output,
            "criteria_formulator": criterion_output,
        }
        if environment_output is not None:
            payload["environment_strategy_generator"] = environment_output
        Log.info("Sending MERGE request to Orchestrator", self.name)
        user_message = json.dumps(payload, ensure_ascii=False, indent=2)
        if tool_code:
            user_message += f"\n\n```python\n{tool_code}\n```"
        response = self.call_llm(user_message)
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("merge_parse_failure", response)
            raise
        Log.success("Received MERGE result", self.name)
        Log.json_preview(result)
        return result

    def run(self, input_data: dict) -> dict:
        return self.dispatch(input_data.get("template", ""))

    async def run_async(self, input_data: dict) -> dict:
        return self.run(input_data)
