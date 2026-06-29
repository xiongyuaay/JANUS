from __future__ import annotations

import time
from typing import Any, Dict, Optional

from multi_turn.agents import (
    CriterionFormulator,
    HumanEvaluator,
    InstructionDecomposer,
    InstructionDesigner,
    OrchestratorAgent,
    ToolDeveloper,
)
from multi_turn.config import LLMConfig, PipelineConfig, RunConfig
from .log import Log
from .repo import Repo


class Flow:
    """Run the generation flow for one prompt."""

    def __init__(self, config: RunConfig):
        llm_config = LLMConfig(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
        )
        pipeline_config = PipelineConfig(
            max_iterations=config.max_iterations,
            auto_approve=config.auto_approve,
            parallel_execution=True,
        )

        self.pipeline_config = pipeline_config
        self.category_display_names = config.category_display_names
        self.output_dir = config.output_dir
        self.artifact_dir = self.output_dir / "artifacts" / "mcp_files"
        self.debug_dir = self.output_dir / "logs" / "raw_llm_responses"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        Log.set_debug_dir(self.debug_dir)

        self.orchestrator = OrchestratorAgent(llm_config)
        self.instruction_designer = InstructionDesigner(llm_config, name="InstructionDesigner")
        self.instruction_decomposer = InstructionDecomposer(llm_config)
        self.tool_developer = ToolDeveloper(llm_config)
        self.criterion_formulator = CriterionFormulator(llm_config)
        self.evaluator = HumanEvaluator(auto_approve=pipeline_config.auto_approve)
        self.repository = Repo(storage_dir=str(self.output_dir / "approved_cases"))

    @staticmethod
    def _attach_metadata(case_payload: Dict[str, Any], category: str, display_name: str, index: int) -> Dict[str, Any]:
        """Attach minimal run metadata to the approved case payload."""

        if not isinstance(case_payload, dict):
            return case_payload
        enriched = dict(case_payload)
        existing_metadata = enriched.get("metadata", {}) if isinstance(enriched.get("metadata"), dict) else {}
        existing_metadata.update(
            {
                "name": f"{category}_{index:04d}",
                "category": category,
                "display_name": display_name,
                "index": index,
                "strategy": "strategy_7_multiturn_decomposition",
            }
        )
        enriched["metadata"] = existing_metadata
        return enriched

    def _validate_case_shape(self, merged_case: Dict[str, Any]) -> None:
        container = merged_case.get("merged", merged_case) if isinstance(merged_case, dict) else {}
        case = container.get("case", {}) if isinstance(container, dict) else {}
        if not isinstance(case, dict):
            raise ValueError("Merged case is missing case object")

        if "environment_strategy" in case or "failure_strategy" in case:
            raise ValueError("Multi-turn cases must not contain environment_strategy or failure_strategy")
        multi_turn = case.get("multi_turn_strategy")
        if not isinstance(multi_turn, dict):
            raise ValueError("Multi-turn cases require multi_turn_strategy")
        user_turns = multi_turn.get("user_turns")
        if not isinstance(user_turns, list) or not 6 <= len(user_turns) <= 10:
            raise ValueError("Multi-turn cases require 6 to 10 user turns")
        if any(not isinstance(turn, str) or not turn.strip() for turn in user_turns):
            raise ValueError("Multi-turn case user turns must be non-empty strings")

    @staticmethod
    def _extract_instruction(dispatch_result: Dict[str, Any], role: str) -> str:
        """Pull one worker instruction out of the orchestrator dispatch payload."""

        for item in dispatch_result.get("dispatch", []):
            if item.get("to") == role:
                return item.get("instructions", "")
        raise ValueError(f"Dispatch result missing task for {role}")

    def _persist_artifacts(self, merged_case: Dict[str, Any], category: str, index: int) -> Dict[str, Any]:
        """Write generated MCP code to disk and replace inline source with a path."""

        if not isinstance(merged_case, dict):
            return merged_case
        container = merged_case.get("merged", merged_case)
        artifacts = container.get("artifacts") if isinstance(container, dict) else None
        if not artifacts or "mcp_file" not in artifacts:
            return merged_case
        mcp_file = artifacts.get("mcp_file", {})
        filename = f"{category}_{index:04d}.py"
        content = mcp_file.get("content_py")
        if not content:
            return merged_case

        filepath = self.artifact_dir / filename
        filepath.write_text(content, encoding="utf-8")
        case = container.get("case", {}) if isinstance(container, dict) else {}
        if isinstance(case, dict):
            mcp = case.get("MCP", {})
            if isinstance(mcp, dict):
                mcp["file"] = filename
                case["MCP"] = mcp
            container["case"] = case
        mcp_file["filename"] = filename
        mcp_file["path"] = str(filepath)
        mcp_file.pop("content_py", None)
        artifacts["mcp_file"] = mcp_file
        return merged_case

    def _build_instruction_output(self, instruction_task: str, decomposition_task: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
        instruction_output = self.instruction_designer.run(instruction_task)
        decomposition_output = self.instruction_decomposer.run(decomposition_task, instruction_output)
        user_turns = decomposition_output.get("user_turns")
        if not isinstance(user_turns, list):
            raise ValueError("Instruction decomposer must return a user_turns list")
        cleaned_turns = [str(turn).strip() for turn in user_turns if str(turn).strip()]
        if not 6 <= len(cleaned_turns) <= 10:
            raise ValueError("Instruction decomposer must return 6 to 10 non-empty user turns")
        enriched_instruction = dict(instruction_output)
        enriched_instruction["user_turns"] = cleaned_turns
        enriched_instruction["decomposition_rationale"] = str(decomposition_output.get("decomposition_rationale", "")).strip()
        return enriched_instruction, decomposition_output

    def _execute_round(self, template: str, feedback: Optional[str], previous_case: Optional[Dict[str, Any]], category: str, index: int):
        """Run one dispatch/revision round and merge the worker outputs."""

        if feedback:
            dispatch_result = self.orchestrator.revision(feedback, previous_case)
            if "dispatch" not in dispatch_result and "merged" in dispatch_result:
                merged_direct = self._persist_artifacts(dispatch_result, category, index)
                self._validate_case_shape(merged_direct)
                return dispatch_result, {}, {}, {}, None, merged_direct
        else:
            dispatch_result = self.orchestrator.dispatch(template)

        instruction_task = self._extract_instruction(dispatch_result, "instruction_designer")
        decomposition_task = self._extract_instruction(dispatch_result, "instruction_decomposer")
        tool_task = self._extract_instruction(dispatch_result, "tool_developer")
        criteria_task = self._extract_instruction(dispatch_result, "criteria_formulator")

        instruction_output, decomposition_output = self._build_instruction_output(instruction_task, decomposition_task)
        tool_output, tool_code = self.tool_developer.run(tool_task, instruction_output)
        criterion_output = self.criterion_formulator.run(criteria_task, instruction_output, tool_output, tool_code)
        merged_case = self.orchestrator.merge(
            instruction_output,
            decomposition_output,
            tool_output,
            criterion_output,
            tool_code,
        )
        merged_case = self._persist_artifacts(merged_case, category, index)
        self._validate_case_shape(merged_case)
        return dispatch_result, instruction_output, tool_output, criterion_output, tool_code, merged_case

    def run(self, prompt_template: str, category: str, index: int) -> Dict[str, Any]:
        pipeline_start = time.time()
        iteration = 0
        feedback = None
        merged_case = None

        while iteration < self.pipeline_config.max_iterations:
            iteration += 1
            dispatch_result, instruction_output, tool_output, criterion_output, tool_code, merged_case = self._execute_round(
                prompt_template, feedback, merged_case, category, index
            )
            case_for_eval = merged_case.get("merged", merged_case) if isinstance(merged_case, dict) else merged_case
            approved, feedback = self.evaluator.evaluate(case_for_eval)
            if approved:
                final_case = merged_case.get("merged", merged_case) if isinstance(merged_case, dict) else merged_case
                display_name = self.category_display_names[category]
                final_case = self._attach_metadata(final_case, category, display_name, index)
                case_id = self.repository.save_case(final_case)
                return {
                    "status": "approved",
                    "case_id": case_id,
                    "case": final_case,
                    "total_time": time.time() - pipeline_start,
                    "dispatch": dispatch_result,
                    "instruction_output": instruction_output,
                    "tool_output": tool_output,
                    "criterion_output": criterion_output,
                }

        return {
            "status": "max_iterations_reached",
            "draft": merged_case,
            "last_feedback": feedback,
            "total_time": time.time() - pipeline_start,
        }
