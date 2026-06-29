from __future__ import annotations

import json
from typing import Any, Dict, List

from modules.log import Log
from modules.parse import parse_json
from prompts.system import ENVIRONMENT_STRATEGY_GENERATOR_SYSTEM_PROMPT

from .base_agent import BaseAgent


class EnvironmentStrategyGenerator(BaseAgent):
    """Generates a hybrid (narrative + anchor fields) environment strategy.

    Used only by the strategy_2_harmful_environment pipeline. It consumes the
    master instruction, the tool specifications produced by ToolDeveloper, and
    the attack type, then produces an environment_strategy object with the
    injection/DoS payload that downstream ToolExecutor can embed into simulated
    tool outputs.
    """

    def __init__(self, config, system_prompt: str = ENVIRONMENT_STRATEGY_GENERATOR_SYSTEM_PROMPT):
        super().__init__(config, system_prompt, "EnvironmentStrategyGenerator")

    @staticmethod
    def _extract_tools(tool_output: Dict[str, Any]) -> List[Dict[str, Any]]:
        mcp = tool_output.get("MCP") if isinstance(tool_output, dict) else None
        if not isinstance(mcp, dict):
            return []
        tools = mcp.get("tools")
        return tools if isinstance(tools, list) else []

    @staticmethod
    def _extract_instruction(instruction_output: Dict[str, Any]) -> str:
        if not isinstance(instruction_output, dict):
            return ""
        value = instruction_output.get("instruction", "")
        return value if isinstance(value, str) else ""

    def _build_payload(
        self,
        instruction_output: Dict[str, Any],
        tool_output: Dict[str, Any],
        attack_type: str,
    ) -> Dict[str, Any]:
        return {
            "master_instruction": self._extract_instruction(instruction_output),
            "available_tools": self._extract_tools(tool_output),
            "attack_type": attack_type,
        }

    def run(
        self,
        instruction_output: Dict[str, Any],
        tool_output: Dict[str, Any],
        attack_type: str,
    ) -> Dict[str, Any]:
        Log.section("Environment Strategy Generation")
        payload = self._build_payload(instruction_output, tool_output, attack_type)
        response = self.call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            result = parse_json(response)
        except Exception:
            self.dump_raw_response("environment_strategy_parse_failure", response)
            raise
        Log.success("Environment strategy generation complete", self.name)
        Log.json_preview(result)
        return result

    async def run_async(
        self,
        instruction_output: Dict[str, Any],
        tool_output: Dict[str, Any],
        attack_type: str,
    ) -> Dict[str, Any]:
        payload = self._build_payload(instruction_output, tool_output, attack_type)
        response = await self.call_llm_async(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            return parse_json(response)
        except Exception:
            self.dump_raw_response("environment_strategy_parse_failure", response)
            raise
