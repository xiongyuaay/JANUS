from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, List, Optional, Tuple

from openai import AsyncOpenAI, OpenAI
from ..config import LLMConfig
from ..modules.logging import Logger
from ..modules.parsing import extract_json_and_code, parse_json_response
from ..config.prompts import (
    CRITERION_FORMULATOR_SYSTEM_PROMPT,
    INSTRUCTION_DESIGNER_SYSTEM_PROMPT,
    TOOL_DEVELOPER_SYSTEM_PROMPT,
)


class BaseAgent(ABC):
    """Shared LLM-calling behavior for worker agents."""

    def __init__(self, config: LLMConfig, system_prompt: str, name: str):
        self.config = config
        self.system_prompt = system_prompt
        self.name = name
        if OpenAI is None or AsyncOpenAI is None:
            raise ImportError("The 'openai' package is required to use compiled_prompt_pipeline workers.")
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self.async_client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key)
        Logger.info(f"Agent initialized (model: {config.model})", self.name)

    def call_llm(self, user_message: str, temperature: Optional[float] = None) -> str:
        """Run a synchronous LLM call with the agent's system prompt."""

        Logger.info(f"Calling LLM... (prompt length: {len(user_message)} chars)", self.name)
        start_time = time.time()
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature or self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        content = response.choices[0].message.content or ""
        elapsed = time.time() - start_time
        Logger.success(f"LLM response received (time: {elapsed:.2f}s, length: {len(content)} chars)", self.name)
        return content

    async def call_llm_async(self, user_message: str, temperature: Optional[float] = None) -> str:
        """Run an asynchronous LLM call with the agent's system prompt."""

        Logger.info(f"Calling LLM asynchronously... (prompt length: {len(user_message)} chars)", self.name)
        start_time = time.time()
        response = await self.async_client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature or self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        content = response.choices[0].message.content or ""
        elapsed = time.time() - start_time
        Logger.success(f"LLM async response received (time: {elapsed:.2f}s, length: {len(content)} chars)", self.name)
        return content

    @abstractmethod
    def run(self, input_data: Any) -> dict:
        """Execute the agent synchronously."""

    @abstractmethod
    async def run_async(self, input_data: Any) -> dict:
        """Execute the agent asynchronously."""


class OrchestratorAgent:
    """Deterministic orchestrator that keeps the compiled prompt authoritative."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.name = "Orchestrator"
        Logger.info("Deterministic orchestrator initialized", self.name)

    @staticmethod
    def _build_instruction_task(template: str) -> str:
        return (
            "Authoritative compiled training prompt:\n"
            "Follow this prompt exactly when writing the final user instruction.\n"
            "Do not compress it into a stock legacy LPS scenario unless the prompt explicitly asks for one.\n\n"
            f"{template}"
        )

    @staticmethod
    def _build_tool_task(template: str) -> str:
        return (
            "Authoritative compiled training prompt:\n"
            "Use this prompt as the binding specification for the MCP environment.\n"
            "Preserve any assigned filename, tool-count minimum, category/domain constraints, and implementation rules.\n\n"
            f"{template}"
        )

    @staticmethod
    def _build_criteria_task(template: str) -> str:
        return (
            "Authoritative compiled training prompt:\n"
            "Use this prompt as the binding specification for evaluator selection.\n"
            "Preserve the allowed evaluator families and choose the one that best matches the induced failure mode.\n\n"
            f"{template}"
        )

    @staticmethod
    def _build_revision_context(feedback: str, previous_case: Optional[dict]) -> str:
        previous_case_json = json.dumps(previous_case or {}, ensure_ascii=False, indent=2)
        return (
            "Revision brief:\n"
            "Update the previously generated case to address the feedback while preserving the existing schema.\n"
            "Treat the feedback as binding and keep the category/domain intent visible in the previous case.\n\n"
            f"Feedback:\n{feedback}\n\n"
            f"Previous case:\n{previous_case_json}"
        )

    def dispatch(self, template: str) -> dict:
        """Emit deterministic worker tasks from the compiled prompt."""

        Logger.section("Stage: DISPATCH")
        result = {
            "stage": "DISPATCH",
            "dispatch": [
                {"to": "instruction_designer", "instructions": self._build_instruction_task(template)},
                {"to": "tool_developer", "instructions": self._build_tool_task(template)},
                {"to": "criteria_formulator", "instructions": self._build_criteria_task(template)},
            ],
        }
        Logger.success("Prepared deterministic DISPATCH result", self.name)
        Logger.json_preview(result)
        return result

    def revision(self, feedback: str, previous_case: Optional[dict]) -> dict:
        """Emit deterministic worker tasks for revising an existing case."""

        Logger.section("Stage: REVISION")
        revision_context = self._build_revision_context(feedback, previous_case)
        result = {
            "stage": "DISPATCH",
            "dispatch": [
                {"to": "instruction_designer", "instructions": revision_context},
                {"to": "tool_developer", "instructions": revision_context},
                {"to": "criteria_formulator", "instructions": revision_context},
            ],
        }
        Logger.success("Prepared deterministic REVISION dispatch", self.name)
        Logger.json_preview(result)
        return result

    def merge(
        self,
        instruction_output: dict,
        tool_output: dict,
        criterion_output: dict,
        tool_code: Optional[str],
    ) -> dict:
        """Combine worker outputs into the final merged case without LLM rewriting."""

        Logger.section("Stage: MERGE")
        mcp = tool_output.get("MCP", {}) if isinstance(tool_output, dict) else {}
        artifacts = tool_output.get("artifacts", {}) if isinstance(tool_output, dict) else {}
        mcp_file = artifacts.get("mcp_file", {}) if isinstance(artifacts, dict) else {}

        filename = mcp.get("file") or mcp_file.get("filename") or "generated_case_tools.py"
        tools = mcp.get("tools", [])
        if not isinstance(tools, list):
            tools = []

        content_py = tool_code
        if not content_py and isinstance(mcp_file, dict):
            inline_code = mcp_file.get("content_py")
            if isinstance(inline_code, str) and inline_code.strip():
                content_py = inline_code.strip()

        merged = {
            "case": {
                "instruction": instruction_output.get("instruction", ""),
                "evaluator": {
                    "file": criterion_output.get("file", ""),
                    "func": criterion_output.get("func", []),
                    "criterion": criterion_output.get("criterion", ""),
                },
                "MCP": {
                    "file": filename,
                    "tools": tools,
                },
            },
            "artifacts": {
                "mcp_file": {
                    "filename": filename,
                }
            },
        }
        if content_py:
            merged["artifacts"]["mcp_file"]["content_py"] = content_py

        result = {"stage": "MERGE", "merged": merged}
        Logger.success("Merged worker outputs deterministically", self.name)
        Logger.json_preview(result)
        return result

    def run(self, input_data: dict) -> dict:
        """Compatibility wrapper that dispatches from an input payload."""

        return self.dispatch(input_data.get("template", ""))

    async def run_async(self, input_data: dict) -> dict:
        """Async compatibility wrapper."""

        return self.run(input_data)


class InstructionDesigner(BaseAgent):
    """Worker that turns the compiled task into the final instruction text."""

    def __init__(self, config: LLMConfig):
        super().__init__(config, INSTRUCTION_DESIGNER_SYSTEM_PROMPT, "InstructionDesigner")

    def run(self, task_from_orchestrator: str) -> dict:
        Logger.section("Instruction Design")
        payload = {"task_from_Orchestrator": task_from_orchestrator}
        response = self.call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
        result = parse_json_response(response)
        Logger.success("Instruction design complete", self.name)
        Logger.json_preview(result)
        return result

    async def run_async(self, task_from_orchestrator: str) -> dict:
        payload = {"task_from_Orchestrator": task_from_orchestrator}
        response = await self.call_llm_async(json.dumps(payload, ensure_ascii=False, indent=2))
        return parse_json_response(response)


class ToolDeveloper(BaseAgent):
    """Worker that creates the MCP schema plus the Python tool implementation."""

    def __init__(self, config: LLMConfig):
        super().__init__(config, TOOL_DEVELOPER_SYSTEM_PROMPT, "ToolDeveloper")

    def run(self, task_from_orchestrator: str, instruction_output: dict) -> Tuple[dict, Optional[str]]:
        Logger.section("Tool Development")
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
        }
        response = self.call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
        result, code_block = extract_json_and_code(response)
        Logger.success("Tool development complete", self.name)
        Logger.json_preview(result)
        if code_block:
            Logger.info("Tool code block detected", self.name)
        else:
            Logger.warning("No tool code block detected", self.name)
        return result, code_block

    async def run_async(self, task_from_orchestrator: str, instruction_output: dict) -> Tuple[dict, Optional[str]]:
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
        }
        response = await self.call_llm_async(json.dumps(payload, ensure_ascii=False, indent=2))
        return extract_json_and_code(response)


class CriterionFormulator(BaseAgent):
    """Worker that selects the evaluator and writes the pass/fail criterion."""

    def __init__(self, config: LLMConfig):
        super().__init__(config, CRITERION_FORMULATOR_SYSTEM_PROMPT, "CriterionFormulator")

    def run(
        self,
        task_from_orchestrator: str,
        instruction_output: dict,
        tool_output: dict,
        tool_code: Optional[str],
    ) -> dict:
        Logger.section("Criteria Formulation")
        payload = {
            "task_from_Orchestrator": task_from_orchestrator,
            "output_from_Instruction_Designer": instruction_output,
            "output_from_Tool_Developer": tool_output,
        }
        user_message = json.dumps(payload, ensure_ascii=False, indent=2)
        if tool_code:
            user_message += f"\n\n```python\n{tool_code}\n```"
        response = self.call_llm(user_message)
        result = parse_json_response(response)
        Logger.success("Criteria formulation complete", self.name)
        Logger.json_preview(result)
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
        return parse_json_response(response)


class HumanEvaluator:
    """Optional human approval gate for merged cases."""

    def __init__(self, auto_approve: bool = False):
        self.auto_approve = auto_approve

    def evaluate(self, draft_case: dict) -> Tuple[bool, str]:
        """Approve automatically or prompt the user for pass/fail feedback."""

        if self.auto_approve:
            return True, "Auto-approved"

        print("\n" + "=" * 60)
        print("Draft case to evaluate:")
        print("=" * 60)
        print(json.dumps(draft_case, ensure_ascii=False, indent=2))
        print("=" * 60)

        while True:
            choice = input("\nChoose: [P]ass / [F]ail with feedback / [Q]uit: ").strip().upper()
            if choice == "P":
                return True, "Approved by human evaluator"
            if choice == "F":
                feedback = input("Enter suggestions for improvement: ").strip()
                return False, feedback
            if choice == "Q":
                raise KeyboardInterrupt("User aborted evaluation")
            print("Invalid choice, please retry")


class CaseRepository:
    """Store approved cases as individual JSON files in a directory."""

    def __init__(self, storage_dir: str = "approved_cases"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.cases = self._load_cases()

    def _load_cases(self) -> List[dict]:
        cases: List[dict] = []
        for path in sorted(self.storage_dir.glob("case_*.json")):
            try:
                cases.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                Logger.warning(f"Failed to read case file: {path}", "CaseRepository")
        return cases

    def _next_id(self) -> int:
        ids = []
        for path in self.storage_dir.glob("case_*.json"):
            try:
                ids.append(int(path.stem.split("_")[1]))
            except Exception:
                continue
        return max(ids) + 1 if ids else 1

    def save_case(self, case: dict) -> int:
        """Persist a case and return its assigned numeric identifier."""

        case_id = self._next_id()
        case["case_id"] = case_id
        path = self.storage_dir / f"case_{case_id}.json"
        path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cases.append(case)
        Logger.success(f"Case saved: {path}", "CaseRepository")
        return case_id

    def get_all_cases(self) -> List[dict]:
        """Return the in-memory list of previously saved cases."""

        return self.cases
