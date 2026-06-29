from __future__ import annotations

import time
from typing import Any, Dict, Optional

from agents import (
    CriterionFormulator,
    EnvironmentStrategyGenerator,
    HumanEvaluator,
    InstructionDesigner,
    OrchestratorAgent,
    ToolDeveloper,
)
from config import LLMConfig, PipelineConfig, RunConfig
from prompts import get_prompt_profile, resolve_strategy_name
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
        self.strategy_name = resolve_strategy_name(config.prompt_dir.name if config.prompt_dir else None)
        prompt_profile = get_prompt_profile(self.strategy_name)
        self.category_display_names = config.category_display_names
        self.output_dir = config.output_dir
        self.artifact_dir = self.output_dir / "artifacts" / "mcp_files"
        self.debug_dir = self.output_dir / "logs" / "raw_llm_responses"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        Log.set_debug_dir(self.debug_dir)

        self.orchestrator = OrchestratorAgent(llm_config, system_prompt=prompt_profile["orchestrator"])
        self.instruction_designer = InstructionDesigner(
            llm_config,
            system_prompt=prompt_profile["instruction_designer"],
            name="InstructionDesigner",
        )
        self.tool_developer = ToolDeveloper(llm_config, system_prompt=prompt_profile["tool_developer"])
        self.criterion_formulator = CriterionFormulator(llm_config)
        self.environment_strategy_generator: Optional[EnvironmentStrategyGenerator] = None
        env_prompt = prompt_profile.get("environment_strategy_generator")
        if env_prompt:
            self.environment_strategy_generator = EnvironmentStrategyGenerator(
                llm_config,
                system_prompt=env_prompt,
            )
        self.evaluator = HumanEvaluator(auto_approve=pipeline_config.auto_approve)
        self.repository = Repo(storage_dir=str(self.output_dir / "approved_cases"))

    @staticmethod
    def _attach_metadata(case_payload: Dict[str, Any], category: str, display_name: str, index: int, strategy_name: str) -> Dict[str, Any]:
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
                "strategy": strategy_name,
            }
        )
        enriched["metadata"] = existing_metadata
        return enriched

    def _validate_case_shape(self, merged_case: Dict[str, Any]) -> None:
        container = merged_case.get("merged", merged_case) if isinstance(merged_case, dict) else {}
        case = container.get("case", {}) if isinstance(container, dict) else {}
        if not isinstance(case, dict):
            raise ValueError("Merged case is missing case object")

        has_environment = "environment_strategy" in case
        has_failure = "failure_strategy" in case
        environment = case.get("environment_strategy")

        if self.strategy_name == "default":
            if has_environment or has_failure:
                raise ValueError("strategy_1/3 cases must not contain environment_strategy or failure_strategy")
            return

        if self.strategy_name == "strategy_2_harmful_environment":
            if not isinstance(environment, dict):
                raise ValueError("strategy_2_harmful_environment requires environment_strategy")
            narrative = environment.get("narrative")
            if not isinstance(narrative, str) or not narrative.strip():
                raise ValueError("strategy_2_harmful_environment requires environment_strategy.narrative")
            carrier_tools = environment.get("carrier_tools")
            if not isinstance(carrier_tools, list) or not carrier_tools:
                raise ValueError("strategy_2_harmful_environment requires non-empty environment_strategy.carrier_tools")
            attack_type = environment.get("attack_type")
            if not isinstance(attack_type, str) or not attack_type.strip():
                raise ValueError("strategy_2_harmful_environment requires environment_strategy.attack_type")
            if has_failure:
                raise ValueError("strategy_2_harmful_environment must not contain failure_strategy")
            return

        if self.strategy_name == "strategy_4_environment_injected":
            if not isinstance(environment, dict):
                raise ValueError("strategy_4_environment_injected requires environment_strategy")
            directives = environment.get("attack_directives")
            if not isinstance(directives, list) or not directives:
                raise ValueError("strategy_4_environment_injected requires non-empty environment_strategy.attack_directives")
            if has_failure:
                raise ValueError("strategy_4_environment_injected must not contain failure_strategy")
            return

        if self.strategy_name == "strategy_5_harmful_environment_soft":
            if not isinstance(environment, dict):
                raise ValueError("strategy_5_harmful_environment_soft requires environment_strategy")
            if "attack_directives" in environment or "injection_style" in environment:
                raise ValueError("strategy_5_harmful_environment_soft must not contain explicit injection fields")
            if has_failure:
                raise ValueError("strategy_5_harmful_environment_soft must not contain failure_strategy")
            return

        if self.strategy_name == "strategy_6_failure_strategy":
            if not isinstance(case.get("failure_strategy"), dict):
                raise ValueError("strategy_6_failure_strategy requires failure_strategy")
            if has_environment:
                raise ValueError("strategy_6_failure_strategy must not contain environment_strategy")
            return

        if self.strategy_name == "strategy_8_agentdojo_suite":
            agentdojo_case = case.get("agentdojo_case")
            if not isinstance(agentdojo_case, dict):
                raise ValueError("strategy_8_agentdojo_suite requires agentdojo_case")
            if has_environment or has_failure:
                raise ValueError("strategy_8_agentdojo_suite must not contain environment_strategy or failure_strategy")
            if "agentharm_case" in case or "asb_case" in case:
                raise ValueError("strategy_8_agentdojo_suite must not contain agentharm_case or asb_case")
            if agentdojo_case.get("suite_name") not in {"workspace", "banking", "travel", "slack"}:
                raise ValueError("strategy_8_agentdojo_suite requires valid suite_name")
            attack_type = agentdojo_case.get("attack_type")
            injections = agentdojo_case.get("injections")
            if attack_type is None:
                if injections not in (None, {}):
                    raise ValueError("benign agentdojo_case requires injections == {}")
            else:
                if attack_type not in {"direct", "ignore_previous", "important_instructions", "injecagent", "tool_knowledge"}:
                    raise ValueError(f"unknown agentdojo attack_type {attack_type!r}")
                if not isinstance(injections, dict) or not injections:
                    raise ValueError("adversarial agentdojo_case requires non-empty injections")
            return

        if self.strategy_name == "strategy_9_agentharm_paired":
            agentharm_case = case.get("agentharm_case")
            if not isinstance(agentharm_case, dict):
                raise ValueError("strategy_9_agentharm_paired requires agentharm_case")
            if has_environment or has_failure:
                raise ValueError("strategy_9_agentharm_paired must not contain environment_strategy or failure_strategy")
            if "agentdojo_case" in case or "asb_case" in case:
                raise ValueError("strategy_9_agentharm_paired must not contain agentdojo_case or asb_case")
            if agentharm_case.get("category") not in {
                "Copyright", "Cybercrime", "Disinformation", "Drugs",
                "Fraud", "Harassment", "Hate", "Sexual",
            }:
                raise ValueError("strategy_9_agentharm_paired requires a valid category")
            target_functions = agentharm_case.get("target_functions")
            if not isinstance(target_functions, list) or not target_functions:
                raise ValueError("strategy_9_agentharm_paired requires non-empty target_functions")
            paired = agentharm_case.get("paired_benign")
            if not isinstance(paired, dict):
                raise ValueError("strategy_9_agentharm_paired requires paired_benign twin")
            if paired.get("target_functions") != target_functions:
                raise ValueError("paired_benign.target_functions must equal agentharm_case.target_functions (same order)")
            return

        if self.strategy_name == "strategy_10_asb_methods":
            asb_case = case.get("asb_case")
            if not isinstance(asb_case, dict):
                raise ValueError("strategy_10_asb_methods requires asb_case")
            if has_environment or has_failure:
                raise ValueError("strategy_10_asb_methods must not contain environment_strategy or failure_strategy")
            if "agentdojo_case" in case or "agentharm_case" in case:
                raise ValueError("strategy_10_asb_methods must not contain agentdojo_case or agentharm_case")
            method = asb_case.get("attack_method")
            if method not in {"clean", "DPI", "MP", "OPI"}:
                raise ValueError(f"unknown asb attack_method {method!r}")
            if asb_case.get("task") != case.get("instruction"):
                raise ValueError("asb_case.task must equal top-level instruction")
            attacker_tool = asb_case.get("attacker_tool")
            if not isinstance(attacker_tool, dict) or not attacker_tool.get("name"):
                raise ValueError("strategy_10_asb_methods requires asb_case.attacker_tool with name")
            normal_tools = asb_case.get("normal_tools")
            if not isinstance(normal_tools, list) or not normal_tools:
                raise ValueError("strategy_10_asb_methods requires non-empty normal_tools")
            if method == "DPI" and not asb_case.get("dpi_payload_template"):
                raise ValueError("DPI cases require asb_case.dpi_payload_template")
            if method == "MP" and (not asb_case.get("mp_system_suffix") or not asb_case.get("mp_similar_task")):
                raise ValueError("MP cases require mp_system_suffix and mp_similar_task")
            if method == "OPI":
                if not asb_case.get("opi_observation_template") or not asb_case.get("opi_carrier_tool"):
                    raise ValueError("OPI cases require opi_observation_template and opi_carrier_tool")
                if asb_case.get("opi_carrier_tool") not in normal_tools:
                    raise ValueError("opi_carrier_tool must be in normal_tools")
            return

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

    def _build_instruction_output(self, instruction_task: str) -> Dict[str, Any]:
        return self.instruction_designer.run(instruction_task)

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
        tool_task = self._extract_instruction(dispatch_result, "tool_developer")
        criteria_task = self._extract_instruction(dispatch_result, "criteria_formulator")

        instruction_output = self._build_instruction_output(instruction_task)
        tool_output, tool_code = self.tool_developer.run(tool_task, instruction_output)
        environment_output = None
        if self.environment_strategy_generator is not None:
            environment_output = self.environment_strategy_generator.run(
                instruction_output,
                tool_output,
                category,
            )
        criterion_output = self.criterion_formulator.run(criteria_task, instruction_output, tool_output, tool_code)
        merged_case = self.orchestrator.merge(
            instruction_output,
            tool_output,
            criterion_output,
            tool_code,
            environment_output=environment_output,
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
                final_case = self._attach_metadata(final_case, category, display_name, index, self.strategy_name)
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
