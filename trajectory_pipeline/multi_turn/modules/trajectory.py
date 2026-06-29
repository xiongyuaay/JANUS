from __future__ import annotations

import importlib.util
import html
import inspect
import json
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, get_args, get_origin

from multi_turn.agents import ToolExecutor, TrajectoryAgent
from multi_turn.config import LLMConfig


@dataclass
class ToolSpec:
    """Structured MCP tool specification."""

    name: str
    description: str
    parameters: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class FakeStructuredTool:
    """Compat wrapper for legacy MCP @tool definitions."""

    name: str
    description: str
    func: Any
    parameters: Dict[str, Any]


def _annotation_to_json_type(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is None:
        if annotation in (str, inspect._empty):
            return "string"
        if annotation is int:
            return "integer"
        if annotation is float:
            return "number"
        if annotation is bool:
            return "boolean"
        if annotation is list:
            return "array"
        if annotation is dict:
            return "object"
        return "string"
    if origin in (list, List):
        return "array"
    if origin in (dict, Dict):
        return "object"
    union_args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if union_args:
        return _annotation_to_json_type(union_args[0])
    return "string"


def _build_parameter_schema(func: Any) -> Dict[str, Any]:
    signature = inspect.signature(func)
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for name, parameter in signature.parameters.items():
        properties[name] = {"type": _annotation_to_json_type(parameter.annotation)}
        if parameter.default is inspect._empty:
            required.append(name)
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _make_fake_tool(func: Any, name: Optional[str] = None) -> FakeStructuredTool:
    return FakeStructuredTool(
        name=name or func.__name__,
        description=inspect.getdoc(func) or "",
        func=func,
        parameters=_build_parameter_schema(func),
    )


def _fake_tool_decorator(arg: Any = None) -> Any:
    if callable(arg):
        return _make_fake_tool(arg)
    if isinstance(arg, str):
        def decorator(func: Any) -> FakeStructuredTool:
            return _make_fake_tool(func, name=arg)

        return decorator
    raise TypeError("tool decorator only supports @tool or @tool('name')")


def _install_fake_langchain_tool() -> None:
    langchain_mod = types.ModuleType("langchain")
    tools_mod = types.ModuleType("langchain.tools")
    tools_mod.tool = _fake_tool_decorator
    langchain_mod.tools = tools_mod
    sys.modules["langchain"] = langchain_mod
    sys.modules["langchain.tools"] = tools_mod


def _load_legacy_tools(mcp_path: Path, tool_names: List[str]) -> List[ToolSpec]:
    _install_fake_langchain_tool()
    spec = importlib.util.spec_from_file_location(f"mcp_module_{mcp_path.stem}", mcp_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load MCP module from {mcp_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tools: List[ToolSpec] = []
    for tool_name in tool_names:
        tool_obj = getattr(module, tool_name, None)
        if tool_obj is None:
            raise ValueError(f"Tool {tool_name} not found in {mcp_path}")
        tools.append(
            ToolSpec(
                name=tool_obj.name,
                description=tool_obj.description,
                parameters=tool_obj.parameters,
            )
        )
    return tools


def load_tool_specs(case_payload: Dict[str, Any]) -> List[ToolSpec]:
    """Load MCP tool specifications from a case with legacy fallback."""

    case = case_payload.get("case", {})
    mcp = case.get("MCP", {}) if isinstance(case, dict) else {}
    raw_tools = mcp.get("tools", []) if isinstance(mcp, dict) else []
    if raw_tools and isinstance(raw_tools[0], dict):
        return [
            ToolSpec(
                name=str(item["name"]),
                description=str(item.get("description", "")),
                parameters=item.get("parameters", {"type": "object", "properties": {}}),
            )
            for item in raw_tools
        ]

    artifacts = case_payload.get("artifacts", {})
    mcp_file = artifacts.get("mcp_file", {}) if isinstance(artifacts, dict) else {}
    mcp_path = mcp_file.get("path")
    if not mcp_path:
        raise ValueError("Case is missing structured MCP.tools and legacy artifacts.mcp_file.path")
    return _load_legacy_tools(Path(mcp_path), [str(name) for name in raw_tools])


def validate_tool_call(tool: ToolSpec, arguments: Dict[str, Any]) -> Optional[str]:
    """Validate one tool call against the tool specification."""

    schema = tool.parameters or {}
    if schema.get("type") != "object":
        return None
    if not isinstance(arguments, dict):
        return "Tool arguments must be a JSON object."

    required = schema.get("required", [])
    for name in required:
        if name not in arguments:
            return f"Missing required argument: {name}"

    properties = schema.get("properties", {})
    for name, value in arguments.items():
        prop = properties.get(name)
        if not isinstance(prop, dict):
            continue
        expected = prop.get("type")
        if not expected:
            continue
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return f"Argument {name} must be an integer."
        if expected == "number" and not isinstance(value, (int, float)):
            return f"Argument {name} must be a number."
        if expected == "boolean" and not isinstance(value, bool):
            return f"Argument {name} must be a boolean."
        if expected == "array" and not isinstance(value, list):
            return f"Argument {name} must be an array."
        if expected == "object" and not isinstance(value, dict):
            return f"Argument {name} must be an object."
        if expected == "string" and not isinstance(value, str):
            return f"Argument {name} must be a string."
    return None


def summarize_tool_value(value: Any, depth: int = 0) -> Any:
    """Produce a compact tool result view for the next trajectory step."""

    if depth >= 2:
        if isinstance(value, list):
            return {"type": "list", "length": len(value)}
        if isinstance(value, dict):
            return {"type": "object", "keys": list(value.keys())[:8], "size": len(value)}
        return value
    if isinstance(value, dict):
        kept: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)) or item is None:
                kept[key] = item
            elif isinstance(item, list):
                kept[key] = {
                    "type": "list",
                    "length": len(item),
                    "sample": [summarize_tool_value(elem, depth + 1) for elem in item[:2]],
                }
            elif isinstance(item, dict):
                kept[key] = summarize_tool_value(item, depth + 1)
            if len(kept) >= 8:
                break
        if len(kept) < len(value):
            kept["_truncated"] = True
            kept["_key_count"] = len(value)
        return kept
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "sample": [summarize_tool_value(item, depth + 1) for item in value[:2]],
        }
    return value


def get_multi_turn_user_turns(case: Dict[str, Any]) -> List[str]:
    """Return normalized multi-turn user requests for strategy_7."""

    multi_turn = case.get("multi_turn_strategy")
    if not isinstance(multi_turn, dict):
        return []
    raw_turns = multi_turn.get("user_turns")
    if not isinstance(raw_turns, list):
        return []
    return [str(turn).strip() for turn in raw_turns if str(turn).strip()]


def _xml_text(value: Any) -> str:
    """Escape text for XML-style trajectory records."""

    return html.escape(str(value), quote=False)


def _json_text(value: Any) -> str:
    """Render structured values as compact JSON text."""

    return json.dumps(value, ensure_ascii=False)


def _safe_call_name(name: str) -> str:
    """Create a stable tool-call id component."""

    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "tool"


def _openai_tool_call(tool_call: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Create an OpenAI-style assistant tool call."""

    tool_name = str(tool_call.get("name", ""))
    arguments = tool_call.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
    return {
        "id": f"call_{index}_{_safe_call_name(tool_name)}",
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": _json_text(arguments),
        },
    }


def _openai_assistant_record(content: str, tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Create an OpenAI-style assistant record."""

    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [_openai_tool_call(tool_call, index) for index, tool_call in enumerate(tool_calls)],
    }


def _openai_tool_record(tool_name: str, content: str, index: int) -> Dict[str, Any]:
    """Create an OpenAI-style tool result record."""

    return {
        "role": "tool",
        "name": tool_name,
        "tool_call_id": f"call_{index}_{_safe_call_name(tool_name)}",
        "content": content,
        "summary": summarize_tool_value(content),
    }


def _xml_tool_content(thought: str, tool_calls: List[Dict[str, Any]]) -> str:
    """Render assistant tool calls in XML-style text."""

    blocks: List[str] = []
    if thought.strip():
        blocks.append(f"<think>\n{_xml_text(thought.strip())}\n</think>")
    for tool_call in tool_calls:
        tool_name = str(tool_call.get("name", ""))
        arguments = tool_call.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        rows = ["<tool_call>", f"<function={_xml_text(tool_name)}>"]
        for key, value in arguments.items():
            rows.append(f"<parameter={_xml_text(key)}>")
            rows.append(_xml_text(_json_text(value) if isinstance(value, (dict, list)) else value))
            rows.append("</parameter>")
        rows.extend(["</function>", "</tool_call>"])
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def _xml_assistant_record(content: str, tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Create an XML-style assistant record."""

    return {
        "role": "assistant",
        "type": "xml_step",
        "content": _xml_tool_content(content, tool_calls),
    }


def _xml_tool_record(tool_name: str, content: str) -> Dict[str, Any]:
    """Create an XML-style tool result record."""

    return {
        "role": "tool",
        "type": "xml_observation",
        "name": tool_name,
        "content": f"<observation>\n{_xml_text(content)}\n</observation>",
        "summary": summarize_tool_value(content),
    }


def _react_content(message: str, tool_name: str, arguments: Dict[str, Any]) -> str:
    """Render a structured tool action as ReAct text."""

    thought = message.strip() or f"Call {tool_name}."
    return f"Thought: {thought}\nAction: {tool_name}\nAction Input: {_json_text(arguments)}"


def _react_assistant_record(content: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Create a ReAct assistant record."""

    return {
        "role": "assistant",
        "type": "react_step",
        "content": _react_content(content, tool_name, arguments),
        "action": tool_name,
        "action_input": arguments,
    }


def _style_final_record(style: str, content: str) -> Dict[str, Any]:
    """Create a final-answer record in the requested style."""

    if style == "react":
        return {"role": "assistant", "type": "react_final", "content": f"Final Answer: {content}", "final_answer": content}
    if style == "xml":
        return {"role": "assistant", "type": "xml_final", "content": f"<final_answer>\n{_xml_text(content)}\n</final_answer>", "final_answer": content}
    return {"role": "assistant", "content": content}


def _style_observation_record(style: str, tool_name: str, content: str, index: int = 0) -> Dict[str, Any]:
    """Create one tool observation record in the requested style."""

    if style == "react":
        return {"role": "tool", "type": "observation", "name": tool_name, "observation": content, "content": f"Observation: {content}", "summary": summarize_tool_value(content)}
    if style == "xml":
        return _xml_tool_record(tool_name, content)
    return _openai_tool_record(tool_name, content, index)


class TrajectoryFlow:
    """Generate a trajectory with a trajectory agent and a tool executor agent."""

    def __init__(self, trajectory_config: LLMConfig, tool_executor_config: LLMConfig, trajectory_style: str = "auto"):
        self.trajectory_config = trajectory_config
        self.tool_executor_config = tool_executor_config
        self.trajectory_style = "openai" if trajectory_style == "auto" else trajectory_style
        self.trajectory_agent: TrajectoryAgent | None = None
        self.tool_executor: ToolExecutor | None = None
        self.trajectory_agent = TrajectoryAgent(self.trajectory_config)
        self.tool_executor = ToolExecutor(self.tool_executor_config)

    def run(
        self,
        case_payload: Dict[str, Any],
        *,
        max_turns: int,
    ) -> Dict[str, Any]:
        case = case_payload["case"]
        if self.trajectory_agent is None or self.tool_executor is None:
            raise RuntimeError("Trajectory agents are not initialized")
        tool_specs = load_tool_specs(case_payload)
        tool_map = {tool.name: tool for tool in tool_specs}
        user_turns = get_multi_turn_user_turns(case)
        if not 6 <= len(user_turns) <= 10:
            raise ValueError("Multi-turn trajectory requires 6 to 10 user turns")
        if user_turns:
            trace: List[Dict[str, Any]] = [
                {"role": "system", "content": "Trajectory generation starts from decomposed multi-turn user requests."},
                {"role": "user", "content": user_turns[0]},
            ]
            current_turn_index = 0
        else:
            trace = [
                {"role": "system", "content": "Trajectory generation starts from the user instruction."},
                {"role": "user", "content": case["instruction"]},
            ]
            current_turn_index = -1
        for _ in range(max_turns):
            current_instruction = user_turns[current_turn_index] if user_turns else case["instruction"]
            step_payload = {
                "instruction": current_instruction,
                "tools": [tool.to_dict() for tool in tool_specs],
                "trace": trace,
            }
            if user_turns:
                step_payload["current_user_turn"] = user_turns[current_turn_index]
                step_payload["current_user_turn_index"] = current_turn_index + 1
                step_payload["total_user_turns"] = len(user_turns)
            step = self.trajectory_agent.run(step_payload)
            action = step.get("action")
            assistant_message = str(step.get("assistant_message", ""))

            if action == "final_answer":
                trace.append(_style_final_record(self.trajectory_style, assistant_message))
                if user_turns and current_turn_index < len(user_turns) - 1:
                    current_turn_index += 1
                    trace.append({"role": "user", "content": user_turns[current_turn_index]})
                    continue
                break

            tool_calls = step.get("tool_calls", [])
            if action != "tool_call" or not isinstance(tool_calls, list) or not tool_calls:
                trace.append({"role": "assistant", "content": assistant_message})
                break

            if self.trajectory_style != "react":
                if self.trajectory_style == "xml":
                    trace.append(_xml_assistant_record(assistant_message, tool_calls))
                else:
                    trace.append(_openai_assistant_record(assistant_message, tool_calls))

            for index, tool_call in enumerate(tool_calls):
                tool_name = str(tool_call.get("name", ""))
                arguments = tool_call.get("arguments", {})
                if not isinstance(arguments, dict):
                    arguments = {}
                if self.trajectory_style == "react":
                    trace.append(_react_assistant_record(assistant_message, tool_name, arguments))
                tool = tool_map.get(tool_name)
                if tool is None:
                    tool_result = {"success": False, "error": f"Unknown tool: {tool_name}"}
                else:
                    error = validate_tool_call(tool, arguments)
                    if error:
                        tool_result = {"success": False, "error": error}
                    else:
                        executor_payload = {
                            "instruction": current_instruction,
                            "tool": tool.to_dict(),
                            "arguments": arguments,
                            "trace": trace,
                        }
                        executor_payload["current_user_turn"] = user_turns[current_turn_index]
                        executor_payload["current_user_turn_index"] = current_turn_index + 1
                        executor_payload["total_user_turns"] = len(user_turns)
                        executor_output = self.tool_executor.run(executor_payload)
                        tool_result = executor_output.get("result", executor_output)

                trace.append(_style_observation_record(self.trajectory_style, tool_name, _json_text(tool_result), index))

        return {"trace": trace}
