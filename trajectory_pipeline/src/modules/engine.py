from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Tuple

from agents import (
    CriterionFormulator,
    HumanEvaluator,
    InstructionDesigner,
    OrchestratorAgent,
    ToolDeveloper,
)
from config import LLMConfig, PipelineConfig
from .log import Log
from .repo import Repo


class Engine:
    """Compiled-prompt generation engine."""

    def __init__(self, llm_config: LLMConfig, pipeline_config: PipelineConfig):
        self.pipeline_config = pipeline_config
        self.artifact_dir = Path("artifacts/mcp_files")
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        Log.section("Initialize Pipeline")
        Log.info(f"LLM config: base_url={llm_config.base_url}, model={llm_config.model}", "Pipeline")
        Log.info(
            f"Pipeline config: max_iterations={pipeline_config.max_iterations}, parallel={pipeline_config.parallel_execution}",
            "Pipeline",
        )

        self.orchestrator = OrchestratorAgent(llm_config)
        self.instruction_designer = InstructionDesigner(llm_config)
        self.tool_developer = ToolDeveloper(llm_config)
        self.criterion_formulator = CriterionFormulator(llm_config)
        self.evaluator = HumanEvaluator(auto_approve=pipeline_config.auto_approve)
        self.repository = Repo()

        Log.success("Compiled-prompt pipeline initialized", "Pipeline")

    @staticmethod
    def _extract_instruction(dispatch_result: dict, role: str) -> str:
        """Extract role-specific worker instructions from a dispatch payload."""

        for item in dispatch_result.get("dispatch", []):
            if item.get("to") == role:
                return item.get("instructions", "")
        raise ValueError(f"Dispatch result missing task for {role}")

    def _persist_artifacts(self, merged_case: dict) -> dict:
        """Write inline MCP code to disk and replace it with a file path."""

        if not isinstance(merged_case, dict):
            return merged_case

        container = merged_case.get("merged", merged_case)
        artifacts = container.get("artifacts") if isinstance(container, dict) else None
        if not artifacts or "mcp_file" not in artifacts:
            return merged_case

        mcp_file = artifacts.get("mcp_file", {})
        filename = mcp_file.get("filename")
        content = mcp_file.get("content_py")
        if not filename or not content:
            return merged_case

        path = self.artifact_dir / filename
        path.write_text(content, encoding="utf-8")
        Log.success(f"MCP code written to file: {path}", "Pipeline")
        mcp_file["path"] = str(path)
        mcp_file.pop("content_py", None)
        artifacts["mcp_file"] = mcp_file
        return merged_case

    def _execute_round(
        self,
        template: str,
        feedback: Optional[str],
        previous_case: Optional[dict],
    ) -> Tuple[dict, dict, dict, dict, Optional[str], dict]:
        """Execute one full dispatch -> worker -> merge cycle."""

        if feedback:
            dispatch_result = self.orchestrator.revision(feedback, previous_case)
        else:
            dispatch_result = self.orchestrator.dispatch(template)

        instruction_task = self._extract_instruction(dispatch_result, "instruction_designer")
        tool_task = self._extract_instruction(dispatch_result, "tool_developer")
        criteria_task = self._extract_instruction(dispatch_result, "criteria_formulator")

        instruction_output = self.instruction_designer.run(instruction_task)
        tool_output, tool_code = self.tool_developer.run(tool_task, instruction_output)
        criterion_output = self.criterion_formulator.run(criteria_task, instruction_output, tool_output, tool_code)

        merged_case = self.orchestrator.merge(instruction_output, tool_output, criterion_output, tool_code)
        merged_case = self._persist_artifacts(merged_case)
        return dispatch_result, instruction_output, tool_output, criterion_output, tool_code, merged_case

    def run(self, prompt_template: str) -> dict:
        """Run the compiled-prompt pipeline until approval or iteration cap."""

        Log.step(0, 5, "Start Pipeline")
        Log.info(f"Template length: {len(prompt_template)} chars", "Pipeline")

        pipeline_start = time.time()
        iteration = 0
        feedback = None
        merged_case = None

        while iteration < self.pipeline_config.max_iterations:
            iteration += 1
            Log.step(1, 5, f"Iteration {iteration}/{self.pipeline_config.max_iterations}")

            dispatch_result, instruction_output, tool_output, criterion_output, tool_code, merged_case = self._execute_round(
                prompt_template, feedback, merged_case
            )

            Log.step(2, 5, "Human evaluation")
            try:
                case_for_eval = merged_case.get("merged", merged_case) if isinstance(merged_case, dict) else merged_case
                approved, feedback = self.evaluator.evaluate(case_for_eval)
            except KeyboardInterrupt:
                Log.warning("User aborted pipeline", "Pipeline")
                return {"status": "cancelled", "draft": merged_case}

            if approved:
                final_case = merged_case.get("merged", merged_case) if isinstance(merged_case, dict) else merged_case
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
