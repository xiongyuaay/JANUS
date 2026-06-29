"""AgentDojo adapter using the official AgentDojo task suites and metrics."""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .common import BenchmarkAdapter, BenchmarkRunOutput, normalize_run_specs
from ..agent import Tool, UnifiedReActAgent
from ..progress import ProgressTracker, info
from ..utils import ensure_dir, write_json


class AgentDojoAdapter(BenchmarkAdapter):
    name = "agentdojo"

    def _add_path(self) -> Path:
        root = (self.project_root / self.cfg.get("root", "benchmark_tasks/agentdojo")).resolve()
        src = root / "src"
        if not src.exists():
            raise FileNotFoundError(f"AgentDojo source directory not found: {src}")
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        return root

    def load_cases(self):  # not used; AgentDojo is driven by official suites.
        return []

    def evaluate(self, results, output_dir: Path):  # not used
        return {}

    def run(self, agent: UnifiedReActAgent, output_dir: Path, max_workers: int = 1) -> BenchmarkRunOutput:
        ensure_dir(output_dir)
        if self.cfg.get("runs"):
            run_outputs: dict[str, Any] = {}
            total_cases = 0
            base_cfg = {k: v for k, v in self.cfg.items() if k != "runs"}
            for run_name, run_spec in normalize_run_specs(self.cfg.get("runs")):
                child_cfg = {**base_cfg, **run_spec}
                child = AgentDojoAdapter(self.project_root, child_cfg, self.run_cfg)
                result = child.run(agent, output_dir / run_name, max_workers=max_workers)
                total_cases += result.num_cases
                run_outputs[run_name] = {
                    "status": result.status,
                    "output_dir": str(result.output_dir),
                    "num_cases": result.num_cases,
                    "metrics": result.metrics,
                }
            metrics = {
                "benchmark": self.name,
                "official_standard": "AgentDojo benign and attack runs. Benign reports BU/CU; attack reports UA/ASR.",
                "runs": run_outputs,
            }
            write_json(output_dir / "metrics.json", metrics)
            return BenchmarkRunOutput(self.name, output_dir, "completed", metrics, total_cases)
        try:
            root = self._add_path()
            from agentdojo.attacks.attack_registry import load_attack  # type: ignore
            from agentdojo.benchmark import benchmark_suite_with_injections, benchmark_suite_without_injections  # type: ignore
            from agentdojo.logging import OutputLogger  # type: ignore
            from agentdojo.task_suite.load_suites import get_suite  # type: ignore
            from agentdojo.task_suite.task_suite import TaskSuite  # type: ignore
        except Exception as exc:
            metrics = {"status": "load_failed", "reason": str(exc)}
            write_json(output_dir / "metrics.json", metrics)
            return BenchmarkRunOutput(self.name, output_dir, "load_failed", metrics)

        # Pipeline name must contain a key from agentdojo.models.MODEL_NAMES so that
        # attacks like `important_instructions` can resolve the {model} placeholder.
        # The unified runner targets local vLLM-served models, so we tag with `local`.
        pipeline_tag = str(self.cfg.get("pipeline_model_tag", "local"))
        pipeline = _AgentDojoReActPipeline(
            agent,
            name=f"unified_{self.run_cfg.get('defense', 'react')}_{pipeline_tag}",
        )
        version = str(self.cfg.get("benchmark_version", "v1.2.2"))
        suites = list(self.cfg.get("suites", self.cfg.get("environment", ["banking", "workspace", "travel", "slack"])))
        user_tasks = tuple(self.cfg.get("user_tasks", self.cfg.get("user_task", [])) or ())
        injection_tasks = tuple(self.cfg.get("injection_tasks", []) or ()) or None
        attack_type = self.cfg.get("attack_type", "tool_knowledge")
        force_rerun = bool(self.cfg.get("force_rerun", False))
        all_results: dict[str, Any] = {}
        metrics: dict[str, Any] = {
            "benchmark": self.name,
            "official_standard": (
                "AgentDojo official metrics. Utility = mean(utility_results). "
                "Targeted ASR = mean(security_results) for attack runs (per util_scripts/create_results_table.py:222-223; "
                "security_results[i] is True iff the injection target was achieved in post_environment). "
                "Benign runs hard-code security=True in run_task_with_pipeline (task_suite.py:408-410), so security/ASR "
                "fields are not reported for benign runs."
            ),
            "benchmark_version": version,
            "suites": suites,
            "attack_type": attack_type,
        }
        combined_utility: list[bool] = []
        combined_security: list[bool] = []
        combined_injection_utility: list[bool] = []
        total_tools = 0
        total_user_tasks = 0
        total_injection_tasks = 0
        attack_active = attack_type not in {None, "", "none", "null"}
        suite_logdir = output_dir / "logs"
        suite_logdir.mkdir(parents=True, exist_ok=True)
        # Pre-load suites to compute total cases for the progress bar.
        loaded_suites: list[tuple[str, Any, int]] = []
        grand_total_cases = 0
        for suite_name in suites:
            suite = get_suite(version, suite_name)
            n_user = len(suite.user_tasks)
            n_inj = len(suite.injection_tasks) if attack_active else 0
            cases_in_suite = n_user * n_inj if attack_active else n_user
            loaded_suites.append((suite_name, suite, cases_in_suite))
            grand_total_cases += cases_in_suite
        run_kind = "attack" if attack_active else "benign"
        resumed = bool(self.run_cfg.get("resume", False))
        # Resume: AgentDojo's `force_rerun=False` natively skips tasks whose
        # per-task JSON already exists in logdir. Pre-count those so the
        # progress bar starts at the right place.
        already_done = 0
        rerun_removed = 0
        if resumed and suite_logdir.exists():
            if bool(self.run_cfg.get("rerun_errors", False)):
                patterns = _normalize_error_patterns(self.run_cfg.get("rerun_error_patterns"))
                rerun_removed = _remove_rerunnable_error_logs(suite_logdir, patterns)
            already_done = sum(1 for _ in suite_logdir.rglob("*.json"))
        info(
            f"agentdojo[{run_kind}]{' (resume)' if resumed else ''}: "
            f"{len(loaded_suites)} suite(s), {grand_total_cases} total cases"
            + (f" — {already_done} already on disk" if resumed and already_done else "")
            + (f", {rerun_removed} error log(s) queued for rerun" if rerun_removed else "")
        )
        progress = ProgressTracker(total=grand_total_cases, desc=f"agentdojo/{run_kind}")
        if resumed and already_done:
            progress.update(min(already_done, grand_total_cases))
        pipeline.attach_progress(progress)
        failed_suites: list[dict[str, Any]] = []
        try:
            # Push an OutputLogger so AgentDojo's TraceLogger writes per-task JSON
            # under suite_logdir (otherwise it falls back to agentdojo/../runs).
            with OutputLogger(str(suite_logdir)):
                for suite_idx, (suite_name, suite, cases_in_suite) in enumerate(loaded_suites, start=1):
                    total_tools += len(suite.tools)
                    total_user_tasks += len(suite.user_tasks)
                    total_injection_tasks += len(suite.injection_tasks)
                    progress.set_postfix(suite=suite_name)
                    info(
                        f"  [{suite_idx}/{len(loaded_suites)}] {suite_name}: "
                        f"{len(suite.user_tasks)} user_tasks"
                        + (
                            f" × {len(suite.injection_tasks)} injections"
                            if attack_active else ""
                        )
                        + f" ({cases_in_suite} cases)"
                    )
                    suite_t0 = time.time()
                    # Per-suite try/except so a crash in one suite (e.g. an
                    # unhandled error inside AgentDojo's task runner after
                    # max_turns_exceeded) doesn't terminate the whole AgentDojo
                    # benchmark. The traceback is logged so root causes are
                    # visible instead of being collapsed into ``str(exc)``.
                    try:
                        if not attack_active:
                            result = benchmark_suite_without_injections(
                                pipeline,
                                suite,
                                user_tasks=user_tasks or None,
                                logdir=suite_logdir,
                                force_rerun=force_rerun,
                                benchmark_version=version,
                            )
                        else:
                            attacker = load_attack(str(attack_type), suite, pipeline)
                            result = benchmark_suite_with_injections(
                                pipeline,
                                suite,
                                attacker,
                                user_tasks=user_tasks or None,
                                injection_tasks=injection_tasks,
                                logdir=suite_logdir,
                                force_rerun=force_rerun,
                                benchmark_version=version,
                            )
                    except Exception as suite_exc:
                        info(
                            f"  [{suite_idx}/{len(loaded_suites)}] {suite_name} FAILED "
                            f"in {time.time() - suite_t0:.1f}s: "
                            f"{type(suite_exc).__name__}: {suite_exc}"
                        )
                        info(traceback.format_exc())
                        failed_suites.append(
                            {
                                "suite": suite_name,
                                "error": f"{type(suite_exc).__name__}: {suite_exc}",
                                "traceback": traceback.format_exc(),
                            }
                        )
                        metrics[suite_name] = {
                            "status": "suite_failed",
                            "reason": str(suite_exc),
                        }
                        continue
                    utility_vals = list(result["utility_results"].values())
                    security_vals = list(result["security_results"].values())
                    inj_util_vals = list(result.get("injection_tasks_utility_results", {}).values())
                    all_results[suite_name] = _serialize_suite_results(result)
                    avg_utility = sum(utility_vals) / len(utility_vals) if utility_vals else float("nan")
                    # AgentDojo: in attack runs, security_results[i] is True iff the injection
                    # target was achieved in post_environment, so Targeted ASR = mean(security_results).
                    # In benign runs, run_task_with_pipeline hard-codes security=True (placeholder),
                    # so we omit security/ASR fields for benign to avoid reporting meaningless 1.0s.
                    suite_metrics: dict[str, Any] = {
                        "avg_utility": avg_utility,
                        "num_utility_cases": len(utility_vals),
                    }
                    if not attack_active:
                        suite_metrics["BU_benign_utility"] = avg_utility
                        suite_metrics["CU_clean_utility"] = avg_utility
                        avg_security = float("nan")
                        asr_value = float("nan")
                        injection_utility = float("nan")
                    else:
                        avg_security = (
                            sum(security_vals) / len(security_vals) if security_vals else float("nan")
                        )
                        asr_value = avg_security
                        injection_utility = (
                            sum(inj_util_vals) / len(inj_util_vals) if inj_util_vals else float("nan")
                        )
                        suite_metrics["UA_utility_under_attack"] = avg_utility
                        suite_metrics["targeted_ASR"] = asr_value
                        suite_metrics["ASR_attack_success_rate"] = asr_value
                        suite_metrics["avg_security"] = avg_security
                        suite_metrics["injection_task_utility"] = injection_utility
                        suite_metrics["num_security_cases"] = len(security_vals)
                    metrics[suite_name] = suite_metrics
                    combined_utility.extend(utility_vals)
                    combined_security.extend(security_vals)
                    combined_injection_utility.extend(inj_util_vals)
                    extra = (
                        f" sec={avg_security:.3f} ASR={asr_value:.3f}"
                        if attack_active and asr_value == asr_value
                        else ""
                    )
                    info(
                        f"  [{suite_idx}/{len(loaded_suites)}] {suite_name} done in "
                        f"{time.time() - suite_t0:.1f}s util={avg_utility:.3f}{extra}"
                    )
            progress.close()
            combined_avg_utility = sum(combined_utility) / len(combined_utility) if combined_utility else float("nan")
            metrics["suite_counts"] = {
                "num_tools": total_tools,
                "num_user_tasks": total_user_tasks,
                "num_injection_targets": total_injection_tasks,
            }
            combined_metrics: dict[str, Any] = {
                "avg_utility": combined_avg_utility,
                "num_utility_cases": len(combined_utility),
            }
            if not attack_active:
                combined_metrics["BU_benign_utility"] = combined_avg_utility
                combined_metrics["CU_clean_utility"] = combined_avg_utility
            else:
                combined_avg_security = (
                    sum(combined_security) / len(combined_security) if combined_security else float("nan")
                )
                combined_asr = combined_avg_security
                combined_injection_utility_avg = (
                    sum(combined_injection_utility) / len(combined_injection_utility)
                    if combined_injection_utility
                    else float("nan")
                )
                combined_metrics["UA_utility_under_attack"] = combined_avg_utility
                combined_metrics["targeted_ASR"] = combined_asr
                combined_metrics["ASR_attack_success_rate"] = combined_asr
                combined_metrics["avg_security"] = combined_avg_security
                combined_metrics["injection_tasks_utility"] = combined_injection_utility_avg
                combined_metrics["num_security_cases"] = len(combined_security)
            metrics["combined"] = combined_metrics
            if failed_suites:
                metrics["failed_suites"] = failed_suites
            run_status = "completed_with_errors" if failed_suites else "completed"
            metrics["status"] = run_status
            write_json(output_dir / "suite_results.json", all_results)
            write_json(output_dir / "metrics.json", metrics)
            return BenchmarkRunOutput(self.name, output_dir, run_status, metrics, len(combined_utility))
        except Exception as exc:
            try:
                progress.close()
            except Exception:
                pass
            metrics["status"] = "run_failed"
            metrics["reason"] = str(exc)
            metrics["traceback"] = traceback.format_exc()
            if failed_suites:
                metrics["failed_suites"] = failed_suites
            write_json(output_dir / "metrics.json", metrics)
            return BenchmarkRunOutput(self.name, output_dir, "run_failed", metrics)


class _AgentDojoReActPipeline:
    """Bridge AgentDojo's BasePipelineElement API to the unified text ReAct agent."""
    def __init__(self, agent: UnifiedReActAgent, name: str):
        self.agent = agent
        self.name = name
        self._progress: ProgressTracker | None = None
        self._seen_tasks: set[tuple[str, str]] = set()

    def attach_progress(self, tracker: "ProgressTracker") -> None:
        self._progress = tracker
        self._seen_tasks = set()

    def query(self, query: str, runtime: Any, env: Any = None, messages: Any = None, extra_args: dict | None = None):
        from agentdojo.functions_runtime import FunctionCall  # type: ignore
        from agentdojo.logging import Logger  # type: ignore
        from agentdojo.types import text_content_block_from_string  # type: ignore

        tools: dict[str, Tool] = {}
        for fname, fn in runtime.functions.items():
            schema = fn.parameters.model_json_schema()
            def _make_executor(tool_name: str):
                def _exec(**kwargs: Any) -> Any:
                    value, error = runtime.run_function(env, tool_name, kwargs)
                    if error:
                        return {"error": error, "value": value}
                    return value
                return _exec
            tools[fname] = Tool(
                name=fname,
                description=getattr(fn, "description", fname),
                parameters=schema,
                executor=_make_executor(fname),
            )
        run_result = self.agent.run_case(case_id="agentdojo", query=query, tools=tools)
        dojo_messages: list[dict[str, Any]] = [
            {"role": "user", "content": [text_content_block_from_string(query)]}
        ]
        # Preserve the ToolSafe-style ReAct text in each assistant message rather
        # than throwing it away. Each assistant turn from `run_result.messages`
        # corresponds to one ReAct step; if that turn produced a tool call we
        # also emit an `assistant.tool_calls` + `tool` pair so AgentDojo's
        # checkers can read the function_stack_trace.
        asst_texts = [
            (m.get("content") or "")
            for m in run_result.messages
            if m.get("role") == "assistant"
        ]
        turn_to_call = {tc["turn"]: (idx, tc) for idx, tc in enumerate(run_result.tool_calls)}
        for turn_idx, asst_text in enumerate(asst_texts, start=1):
            content_blocks: list[Any] | None = (
                [text_content_block_from_string(asst_text)] if asst_text else None
            )
            if turn_idx in turn_to_call:
                idx, call = turn_to_call[turn_idx]
                fc = FunctionCall(
                    function=call["tool_name"],
                    args=call["arguments"],
                    id=f"call_{idx}",
                )
                dojo_messages.append(
                    {
                        "role": "assistant",
                        "content": content_blocks,
                        "tool_calls": [fc],
                    }
                )
                dojo_messages.append(
                    {
                        "role": "tool",
                        "tool_call": fc,
                        "tool_call_id": f"call_{idx}",
                        "content": [
                            text_content_block_from_string(str(call.get("observation", "")))
                        ],
                        "error": None,
                    }
                )
            else:
                dojo_messages.append(
                    {
                        "role": "assistant",
                        "content": content_blocks,
                        "tool_calls": None,
                    }
                )
        # AgentDojo's `model_output_from_messages` raises ValueError if the last
        # message is not an assistant message (task_suite.py:70-75). Whenever the
        # ReAct loop ended with a tool turn (i.e. max_turns_exceeded before a
        # Final Answer) the loop above leaves a `role=tool` message at the end,
        # which would otherwise propagate the ValueError up through
        # benchmark_suite_with_injections and kill the entire AgentDojo run.
        # Always append a closing assistant message with the (possibly empty)
        # final answer plus a fallback marker so downstream checkers see a
        # well-formed transcript.
        final_text = (run_result.final_answer or "").strip()
        if not final_text:
            final_text = (
                f"[unified-react-bridge] No final answer produced "
                f"({run_result.error or 'unknown reason'})."
            )
        if not dojo_messages or dojo_messages[-1].get("role") != "assistant" or dojo_messages[-1].get("tool_calls"):
            dojo_messages.append(
                {
                    "role": "assistant",
                    "content": [text_content_block_from_string(final_text)],
                    "tool_calls": None,
                }
            )
        # Push the trace into TraceLogger so the per-task JSON contains the full
        # ReAct conversation and any agent-side errors.
        active_logger = Logger.get()
        try:
            active_logger.log(dojo_messages)
        except Exception:
            pass
        if run_result.error:
            try:
                active_logger.log_error(run_result.error)
            except Exception:
                pass
        # Bump the progress tracker once per (user_task, injection_task) pair so
        # AgentDojo's internal retry loop doesn't double-count.
        if self._progress is not None:
            ctx = getattr(active_logger, "context", {}) or {}
            key = (
                str(ctx.get("user_task_id", "?")),
                str(ctx.get("injection_task_id", "?")),
            )
            if key not in self._seen_tasks:
                self._seen_tasks.add(key)
                try:
                    self._progress.update(1)
                except Exception:
                    pass
        return query, runtime, env, dojo_messages, (extra_args or {})


def _serialize_suite_results(result: Any) -> dict[str, Any]:
    return {
        "utility_results": {f"{k[0]}|{k[1]}": bool(v) for k, v in result["utility_results"].items()},
        "security_results": {f"{k[0]}|{k[1]}": bool(v) for k, v in result["security_results"].items()},
        "injection_tasks_utility_results": {str(k): bool(v) for k, v in result.get("injection_tasks_utility_results", {}).items()},
    }


def _normalize_error_patterns(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.lower()] if value else []
    return [str(item).lower() for item in value if str(item)]


def _has_rerunnable_error(path: Path, patterns: list[str]) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    err = data.get("error") if isinstance(data, dict) else None
    if not err:
        return False
    err_text = str(err).lower()
    if not patterns:
        return True
    return any(pattern in err_text for pattern in patterns)


def _remove_rerunnable_error_logs(logdir: Path, patterns: list[str]) -> int:
    removed = 0
    for path in logdir.rglob("*.json"):
        try:
            should_remove = _has_rerunnable_error(path, patterns)
        except Exception:
            continue
        if should_remove:
            path.unlink()
            removed += 1
    return removed
