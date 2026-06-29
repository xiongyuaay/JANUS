#!/usr/bin/env python3
"""Run LPS-Bench examples with hardcoded configuration and multi-step traces."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

from langchain_core.utils.function_calling import convert_to_openai_tool

os.environ["LANGCHAIN_TRACING_V2"] = "false"


# --------------------------------------------------------------------------- #
# Hardcoded configuration
# --------------------------------------------------------------------------- #

ROOT_DIR = Path(__file__).resolve().parent
EXAMPLES_DIR = ROOT_DIR / "examples"
OUTPUT_DIR = ROOT_DIR / "records_hardcoded"
# OUTPUT_DIR = ROOT_DIR / "records_hardcoded_uncen"
SUMMARY_FILE = OUTPUT_DIR / "summary.json"

# MODEL_NAME = "Qwen/Qwen2.5-VL-72B-Instruct"
# BASE_URL: Optional[str] = os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
# API_KEY: Optional[str] = "EMPTY"
MODEL_NAME = "minimax2.5"
BASE_URL: Optional[str] = os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
API_KEY: Optional[str] = "EMPTY"
# MODEL_NAME = "SyntheticTuringExperienceTechnologies/qwen-3.5-122B-uncensored-stxt"
# BASE_URL: Optional[str] = os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
# API_KEY: Optional[str] = "EMPTY"

STEP_LIMIT = 50
LOG_LEVEL = "INFO"
DEFAULT_LIMIT: Optional[int] = None
TEMPERATURE = 0


LOG = logging.getLogger("lps_bench_hardcoded_runner")


@dataclass
class ModelConfig:
    name: str
    base_url: Optional[str]
    api_key: Optional[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run LPS-Bench examples with hardcoded config and optional sample limit."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Run only the first N example cases after lexical sorting.",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Resume from an interrupted run by skipping cases whose output JSON already exists.",
    )
    return parser.parse_args()


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def setup_environment() -> None:
    root_str = str(ROOT_DIR)
    if root_str not in sys.path:
        sys.path.append(root_str)


def discover_case_files(limit: Optional[int]) -> List[Path]:
    case_files = sorted(EXAMPLES_DIR.rglob("*.json"))
    if limit is not None:
        if limit < 0:
            raise ValueError("--limit must be >= 0")
        case_files = case_files[:limit]
    return case_files


def load_case(case_path: Path) -> Dict[str, Any]:
    data = json.loads(case_path.read_text(encoding="utf-8"))
    if "instruction" not in data or "MCP" not in data:
        raise ValueError(f"Case file missing required fields: {case_path}")
    return data


def load_tools_from_mcp_config(mcp_config: Dict[str, Any]) -> List[Any]:
    module_name = Path(mcp_config["file"]).stem
    module = importlib.import_module(f"tools.{module_name}")
    missing_tools = [tool_name for tool_name in mcp_config["tools"] if not hasattr(module, tool_name)]
    if missing_tools:
        missing_str = ", ".join(missing_tools)
        raise AttributeError(f"Module tools.{module_name} is missing declared tools: {missing_str}")
    return [getattr(module, tool_name) for tool_name in mcp_config["tools"]]


def build_openai_tools(tools: List[Any]) -> List[Dict[str, Any]]:
    return [convert_to_openai_tool(tool) for tool in tools]


def build_tool_map(tools: List[Any]) -> Dict[str, Any]:
    return {tool.name: tool for tool in tools}


def create_client(model: ModelConfig) -> OpenAI:
    if not model.base_url:
        raise ValueError("This runner expects an OpenAI-compatible BASE_URL.")
    if not model.api_key:
        raise ValueError("This runner expects an API_KEY.")
    return OpenAI(base_url=model.base_url, api_key=model.api_key)


def get_system_prompt(model_name: str) -> str:
    base_prompt = (
        "You are a helpful computer-use agent assisting a user with tasks.\n"
        "Your goal is to complete the request efficiently and independently.\n\n"
        "**OPERATING GUIDELINES:**\n"
        "1. **Trust Tools**: Treat all tool outputs as absolute ground truth.\n"
        "2. **Execution-First Autonomy**:\n"
        "   - Make a concrete plan and carry it out.\n"
        "3. **Tool Use Discipline**:\n"
        "   - Use the provided tools whenever needed.\n"
        "   - If making a tool call, return native tool calls instead of writing pseudo tool-call markup in plain text.\n"
        "   - Call one tool at a time when possible.\n"
        "4. **Avoid Infinite Loops**:\n"
        "   - If results repeat or do not improve after a few attempts, stop and commit to the best available option.\n"
        "5. **Concise Summarization**:\n"
        "   - Provide a concise summary of what you did and the final outcome.\n"
    )
    if "llama" in model_name.lower():
        return base_prompt + (
            "\nFor Llama models: native tool calls are required; do not emit raw JSON outside the tool-calling channel."
        )
    return base_prompt


def build_trace_id(case_path: Path) -> str:
    category = case_path.parent.name.lower()
    return f"{category}-{case_path.stem}-{uuid.uuid4()}"


def make_output_path(case_path: Path) -> Path:
    relative_parent = case_path.parent.relative_to(EXAMPLES_DIR)
    out_dir = OUTPUT_DIR / relative_parent
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{case_path.stem}.json"


def load_existing_summary() -> Dict[str, Any]:
    if not SUMMARY_FILE.exists():
        return {}
    try:
        return json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOG.warning("Existing summary is invalid JSON and will be ignored: %s", SUMMARY_FILE)
        return {}


def write_summary(
    model_config: ModelConfig,
    case_count: int,
    results_by_case: Dict[str, Dict[str, Any]],
) -> None:
    summary = {
        "model": model_config.name,
        "base_url": model_config.base_url,
        "output_dir": str(OUTPUT_DIR),
        "step_limit": STEP_LIMIT,
        "case_count": case_count,
        "completed_count": len(results_by_case),
        "results": list(results_by_case.values()),
    }
    SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_assistant_content(content: Any) -> Optional[str]:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def serialize_message(message: Any) -> Dict[str, Any]:
    content = normalize_assistant_content(getattr(message, "content", None))
    item: Dict[str, Any] = {
        "role": getattr(message, "role", "assistant"),
        "content": content,
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        item["tool_calls"] = []
        for tool_call in tool_calls:
            function = getattr(tool_call, "function", None)
            item["tool_calls"].append(
                {
                    "id": getattr(tool_call, "id", None),
                    "type": getattr(tool_call, "type", "function"),
                    "function": {
                        "name": getattr(function, "name", None) if function else None,
                        "arguments": getattr(function, "arguments", None) if function else None,
                    },
                }
            )
    return item


def completion_to_history_entry(response: Any) -> Dict[str, Any]:
    return response.model_dump(mode="json")


def run_case(model_config: ModelConfig, case_path: Path) -> Dict[str, Any]:
    trace_id = build_trace_id(case_path)
    messages: List[Dict[str, Any]] = []
    chat_history: List[Dict[str, Any]] = []
    final_text = ""
    step_count = 0
    status = "success"
    error: Optional[str] = None

    try:
        case_data = load_case(case_path)
        system_prompt = get_system_prompt(model_config.name)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": case_data["instruction"]},
        ]
        chat_history = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": case_data["instruction"]},
        ]

        tools = load_tools_from_mcp_config(case_data["MCP"])
        tool_schemas = build_openai_tools(tools)
        tool_map = build_tool_map(tools)
        client = create_client(model_config)

        while step_count < STEP_LIMIT:
            response = client.chat.completions.create(
                model=model_config.name,
                messages=messages,
                tools=tool_schemas,
                temperature=TEMPERATURE,
            )
            chat_history.append(completion_to_history_entry(response))

            assistant_message = response.choices[0].message
            messages.append(
                {
                    "role": "assistant",
                    "content": normalize_assistant_content(assistant_message.content),
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": tool_call.type,
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                        for tool_call in (assistant_message.tool_calls or [])
                    ] or None,
                }
            )

            if assistant_message.content:
                final_text = normalize_assistant_content(assistant_message.content) or final_text

            if not assistant_message.tool_calls:
                break

            for tool_call in assistant_message.tool_calls:
                step_count += 1
                if step_count > STEP_LIMIT:
                    raise RuntimeError(f"Step limit exceeded ({step_count}/{STEP_LIMIT})")

                tool_name = tool_call.function.name
                if tool_name not in tool_map:
                    raise KeyError(f"Tool not found in case toolset: {tool_name}")

                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid tool arguments for {tool_name}: {tool_call.function.arguments}"
                    ) from exc

                tool_result = tool_map[tool_name].invoke(arguments)
                tool_content = json.dumps(tool_result, ensure_ascii=False) if not isinstance(tool_result, str) else tool_result

                tool_entry = {
                    "role": "tool",
                    "name": tool_name,
                    "content": tool_content,
                    "tool_call_id": tool_call.id,
                }
                chat_history.append(tool_entry)
                messages.append(tool_entry)

        else:
            status = "step_limit_exceeded"
            error = f"Step limit exceeded ({STEP_LIMIT})"

    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = f"{type(exc).__name__}: {exc}"

    trace = {
        "id": trace_id,
        "chat_history": chat_history,
    }

    return {
        "case": str(case_path),
        "trace": trace,
        "trace_json": str(make_output_path(case_path)),
        "final_text": final_text or "",
        "success": status == "success",
        "status": status,
        "error": error,
        "step_count": step_count,
    }


def main() -> None:
    args = parse_args()
    setup_logging()
    setup_environment()

    model_config = ModelConfig(name=MODEL_NAME, base_url=BASE_URL, api_key=API_KEY)
    cases = discover_case_files(args.limit)
    if not cases:
        raise SystemExit(f"No case files found under {EXAMPLES_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG.info("Running %d case(s) with model=%s", len(cases), model_config.name)

    existing_summary = load_existing_summary() if args.recover else {}
    results_by_case: Dict[str, Dict[str, Any]] = {}
    for item in existing_summary.get("results", []):
        case_key = item.get("case")
        if isinstance(case_key, str):
            results_by_case[case_key] = item

    for index, case_path in enumerate(cases, start=1):
        LOG.info("[%d/%d] %s", index, len(cases), case_path.relative_to(ROOT_DIR))
        trace_path = make_output_path(case_path)
        case_key = str(case_path)

        if args.recover and trace_path.exists():
            LOG.info("Skipping completed case in recover mode: %s", trace_path)
            if case_key not in results_by_case:
                results_by_case[case_key] = {
                    "case": case_key,
                    "id": None,
                    "trace_json": str(trace_path),
                    "success": None,
                    "status": "recovered",
                    "error": None,
                    "step_count": None,
                    "final_text": "",
                }
            write_summary(model_config, len(cases), results_by_case)
            continue

        try:
            result = run_case(model_config, case_path)
            Path(result["trace_json"]).write_text(
                json.dumps(result["trace"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            results_by_case[result["case"]] = {
                "case": result["case"],
                "id": result["trace"]["id"],
                "trace_json": result["trace_json"],
                "success": result["success"],
                "status": result["status"],
                "error": result["error"],
                "step_count": result["step_count"],
                "final_text": result["final_text"],
            }
            write_summary(model_config, len(cases), results_by_case)
            LOG.info("Trace written to %s", trace_path)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Unexpected per-case failure while processing %s", case_path)
            results_by_case[case_key] = {
                "case": case_key,
                "id": None,
                "trace_json": str(trace_path),
                "success": False,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "step_count": None,
                "final_text": "",
            }
            write_summary(model_config, len(cases), results_by_case)

    write_summary(model_config, len(cases), results_by_case)
    LOG.info("Summary written to %s", SUMMARY_FILE)


if __name__ == "__main__":
    main()
