"""LPS-Bench adapter with ToolSafe-style ReAct execution and official evaluator hooks."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .common import BenchmarkAdapter, BenchmarkCase, run_react_cases
from ..agent import Tool
from ..model_client import build_chat_client
from ..utils import function_to_schema, load_module_from_path, normalize_tool_callable, resolve_path, write_json


class _InvokeWrapper:
    def __init__(self, client: Any):
        self.client = client

    def invoke(self, prompt: str) -> Any:
        content = self.client.chat([{"role": "user", "content": prompt}])
        return SimpleNamespace(content=content)


def _load_prior_case_results(output_dir: Path) -> dict[str, dict[str, Any]]:
    path = output_dir / "metrics.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("case_results", []) if isinstance(data, dict) else []
    return {
        str(row.get("case_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("case_id") is not None
    }


class LPSBenchAdapter(BenchmarkAdapter):
    name = "lps_bench"

    def _root(self) -> Path:
        root = resolve_path(self.project_root, self.cfg.get("root", "benchmark_tasks/lps_bench"))
        if root is None:
            raise FileNotFoundError("LPS-Bench root path not configured")
        return root

    def _install_langchain_tool_stub(self) -> None:
        fake_langchain = sys.modules.get("langchain") or types.ModuleType("langchain")
        fake_langchain_core = sys.modules.get("langchain_core") or types.ModuleType("langchain_core")
        fake_tools = types.ModuleType("langchain.tools")
        fake_core_tools = types.ModuleType("langchain_core.tools")
        def tool(fn=None, *args, **kwargs):
            if fn is None:
                return lambda f: f
            return fn
        fake_tools.tool = tool  # type: ignore[attr-defined]
        fake_core_tools.tool = tool  # type: ignore[attr-defined]
        sys.modules.setdefault("langchain", fake_langchain)
        sys.modules.setdefault("langchain_core", fake_langchain_core)
        sys.modules["langchain.tools"] = fake_tools
        sys.modules["langchain_core.tools"] = fake_core_tools

    def _case_files(self) -> list[Path]:
        root = self._root()
        files: list[Path] = []
        for item in self.cfg.get("cases", []) or []:
            p = resolve_path(self.project_root, item) or resolve_path(root, item)
            if p and p.exists():
                files.append(p)
        for pattern in self.cfg.get("case_globs", []) or []:
            base = root
            files.extend(sorted(base.glob(pattern)))
        if not files:
            default = root / "examples" / "multi_media" / "FA_4.json"
            files = [default]
        return files

    def load_cases(self) -> list[BenchmarkCase]:
        root = self._root()
        cases: list[BenchmarkCase] = []
        self._install_langchain_tool_stub()
        for case_file in self._case_files():
            if not case_file.exists():
                raise FileNotFoundError(f"LPS-Bench case file not found: {case_file}")
            with case_file.open("r", encoding="utf-8") as f:
                case_json = json.load(f)
            mcp = case_json.get("MCP", {})
            tool_file_name = mcp.get("file")
            tool_names = list(mcp.get("tools") or [])
            tools: dict[str, Tool] = {}
            if tool_file_name:
                tool_path = root / "tools" / tool_file_name
                if not tool_path.exists():
                    raise FileNotFoundError(f"LPS-Bench tool file not found: {tool_path}")
                module = load_module_from_path(tool_path)
                for name in tool_names:
                    if not hasattr(module, name):
                        continue
                    fn = normalize_tool_callable(getattr(module, name))
                    schema = function_to_schema(fn, name=name)
                    tools[name] = Tool(
                        name=name,
                        description=schema["description"],
                        parameters=schema["parameters"],
                        executor=fn,
                    )
            cases.append(
                BenchmarkCase(
                    id=self._case_id(root, case_file),
                    prompt=case_json["instruction"],
                    tools=tools,
                    metadata={"case_file": str(case_file), "case": case_json},
                )
            )
        return cases

    @staticmethod
    def _case_id(root: Path, case_file: Path) -> str:
        root_resolved = root.resolve()
        case_resolved = case_file.resolve()
        rel = case_resolved.relative_to(root_resolved) if case_resolved.is_relative_to(root_resolved) else Path(case_resolved.name)
        return str(rel.with_suffix("")).replace("/", "__")

    def evaluate(self, results, output_dir: Path) -> dict[str, Any]:
        root = self._root()
        plan_dir = output_dir / "execution_logs"
        plan_dir.mkdir(parents=True, exist_ok=True)
        judge_cfg = self.cfg.get("judge", {})
        run_official = bool(self.cfg.get("run_official_evaluator", True))
        eval_model = None
        judge_enabled = False
        if judge_cfg.get("provider") and judge_cfg.get("enabled", True):
            eval_model = _InvokeWrapper(build_chat_client(judge_cfg))
            judge_enabled = True
        prior_results = _load_prior_case_results(output_dir)
        rerun_ids = set(self.run_cfg.get("_rerun_case_ids_by_output", {}).get(str(output_dir), set()))
        cached_evaluator_results = 0
        case_eval_results: list[dict[str, Any]] = []
        for case, result in results:
            plan_file = plan_dir / f"{case.id}__{self.run_cfg.get('defense','react')}.txt"
            plan_file.write_text(result.logs, encoding="utf-8")
            row = {
                "case_id": case.id,
                "case_file": case.metadata.get("case_file"),
                "plan_file": str(plan_file),
                "run_error": result.error,
                "num_tool_calls": len(result.tool_calls),
                "num_blocked_actions": len(result.blocked_actions),
            }
            if run_official:
                prior = prior_results.get(case.id)
                if case.id not in rerun_ids and prior and prior.get("run_error") == result.error and "official_result" in prior:
                    row.update({
                        "official_result": prior.get("official_result"),
                        "pass": prior.get("pass"),
                        "execution_status": prior.get("execution_status"),
                        "official_evaluator_cached": True,
                    })
                    cached_evaluator_results += 1
                else:
                    try:
                        case_json = case.metadata["case"]
                        evaluator = case_json.get("evaluator", {})
                        evaluator_file = evaluator.get("file")
                        funcs = evaluator.get("func") or []
                        if not evaluator_file or not funcs:
                            raise RuntimeError("case JSON has no evaluator.file or evaluator.func")
                        evaluator_path = root / "evaluator" / evaluator_file
                        module = load_module_from_path(evaluator_path)
                        func = getattr(module, funcs[0])
                        if judge_enabled:
                            official = func(
                                plan_files=[str(plan_file)],
                                case_file=str(case.metadata["case_file"]),
                                mode="api",
                                eval_model=eval_model,
                                eval_processor=None,
                                verbose=bool(self.cfg.get("judge_verbose", False)),
                            )
                        else:
                            row["official_evaluator_status"] = "not_run_no_judge_model"
                            official = None
                        if official:
                            # The official batch evaluator returns one item per plan file.
                            row["official_result"] = official
                            first = next(iter(official.values()))
                            row["pass"] = bool(first.get("pass"))
                            row["execution_status"] = first.get("execution_status")
                        row["official_evaluator_cached"] = False
                    except Exception as exc:
                        row["official_evaluator_status"] = "failed"
                        row["official_evaluator_error"] = str(exc)
            case_eval_results.append(row)
        evaluated = [r for r in case_eval_results if "pass" in r]
        pass_rate = sum(1 for r in evaluated if r.get("pass")) / len(evaluated) if evaluated else float("nan")
        status_counts: dict[str, int] = {}
        for r in evaluated:
            status = str(r.get("execution_status", "unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "benchmark": self.name,
            "official_standard": "LPS-Bench LLM-as-a-judge evaluator returns PASS/FAIL and safe/unsafe/execution_failed status for each case.",
            "num_cases": len(results),
            "judge_enabled": judge_enabled,
            "official_evaluator_run": run_official and judge_enabled,
            "official_evaluator_cached_count": cached_evaluator_results,
            "pass_rate": pass_rate,
            "execution_status_counts": status_counts,
            "case_results": case_eval_results,
        }

    def run(self, agent, output_dir: Path, max_workers: int = 1):
        return run_react_cases(adapter=self, agent=agent, output_dir=output_dir, max_workers=max_workers)
