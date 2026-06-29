import json
import re
import uuid


# from inspect_ai.log import transcript
# from inspect_ai.model import ChatMessage, ChatMessageUser, GenerateConfig, Model


def _safe_json_loads(raw):
    """Best-effort JSON parser used by ToolSafe-style tool extraction."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip().rstrip(".")
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for idx, ch in enumerate(text[start:], start=start):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[start : idx + 1])
                            return parsed if isinstance(parsed, dict) else {}
                        except json.JSONDecodeError:
                            break

    params_dict = {}
    pairs = re.findall(r"([\w.-]+)\s*=\s*[\"']?([^,\"'\n]+)[\"']?", text)
    for k, v in pairs:
        params_dict[k] = v.strip()
    return params_dict


# def extract_observation_react(text: str) -> str:
#     """
#     从字符串中提取 Observation: 后面的内容
#     """
#     match = re.search(r"Observation:\s*(.*)", text, re.DOTALL)
#     return match.group(1).strip() if match else ""


def extract_tool_params_react(text):
    """
    从模型输出中提取 tool_name 和 tool_params。
    支持以下两种格式：
        Action: average_dog_weight
        Action Input: breed="Bulldog"
    或：
        Action: average_dog_weight
        Action Input: {"breed": "Bulldog"}
    """
    text = str(text)
    match = re.search(r"Action\s*:\s*([\w.:-]+).*?Action Input\s*:\s*(.*)", text, re.S | re.I)
    if not match:
        return "", {}

    tool_name = match.group(1).strip()
    raw_params = match.group(2).strip()
    raw_params = re.split(r"\n\s*(?:Observation|Final Answer)\s*:", raw_params, maxsplit=1, flags=re.I)[0].strip()
    return tool_name, _safe_json_loads(raw_params)


def extract_tool_params_xml(text):
    """
    Extract XML-format tool calls.

    Supported forms:
        <tool_call><name>tool</name><arguments>{"x": 1}</arguments></tool_call>
        <tool_call name="tool">{"x": 1}</tool_call>
        <function=tool>{"x": 1}</function>
    """
    text = str(text or "")

    block_match = re.search(r"<tool_call(?:\s[^>]*)?>(.*?)</tool_call>", text, flags=re.S | re.I)
    if block_match:
        block = block_match.group(1)
        name_match = re.search(r"<name>\s*(.*?)\s*</name>", block, flags=re.S | re.I)
        args_match = re.search(r"<arguments>\s*(.*?)\s*</arguments>", block, flags=re.S | re.I)
        attr_match = re.search(r"name\s*=\s*[\"']([^\"']+)[\"']", block, flags=re.I)
        tool_name = name_match.group(1).strip() if name_match else (attr_match.group(1).strip() if attr_match else "")
        raw_params = args_match.group(1).strip() if args_match else block
        if tool_name:
            return tool_name, _safe_json_loads(raw_params)

    attr_outer = re.search(r"<tool_call\s+name=[\"']([^\"']+)[\"']\s*>\s*(.*?)\s*</tool_call>", text, flags=re.S | re.I)
    if attr_outer:
        return attr_outer.group(1).strip(), _safe_json_loads(attr_outer.group(2))

    function_match = re.search(r"<function=([\w.:-]+)>\s*(.*?)\s*</function>", text, flags=re.S | re.I)
    if function_match:
        return function_match.group(1).strip(), _safe_json_loads(function_match.group(2))

    return "", {}


def extract_tool_params_openai(message):
    """Extract the first native OpenAI tool call from a message dict or SDK object."""
    tool_calls = None
    if isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    else:
        tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        return "", {}

    first = tool_calls[0]
    if isinstance(first, dict):
        fn = first.get("function") or {}
        return fn.get("name", ""), _safe_json_loads(fn.get("arguments", {}))

    fn = getattr(first, "function", None)
    if fn is None:
        return "", {}
    return getattr(fn, "name", ""), _safe_json_loads(getattr(fn, "arguments", {}))


def extract_tool_params_any(message_or_text, preferred="xml"):
    """Try OpenAI, XML, then ReAct extraction and return the first tool call."""
    if preferred == "openai":
        name, args = extract_tool_params_openai(message_or_text)
        if name:
            return name, args
    if not isinstance(message_or_text, str) and not isinstance(message_or_text, dict):
        name, args = extract_tool_params_openai(message_or_text)
        if name:
            return name, args
    text = message_or_text.get("content", "") if isinstance(message_or_text, dict) else str(message_or_text)
    if preferred == "react":
        order = [extract_tool_params_react, extract_tool_params_xml]
    else:
        order = [extract_tool_params_xml, extract_tool_params_react]
    for parser in order:
        name, args = parser(text)
        if name:
            return name, args
    return "", {}


def extract_tool_params_planexecute(tool_call):
    # 1. 若为字符串，尝试解析为 JSON
    if isinstance(tool_call, str):
        try:
            tool_call = json.loads(tool_call)
        except json.JSONDecodeError:
            return "", {}

    # 2. 若不是 dict，直接返回空
    if not isinstance(tool_call, dict):
        return "", {}

    # 3. 正常字段抽取
    if "function_name" not in tool_call and "args" not in tool_call:
        return "", {}
    elif "function_name" not in tool_call:
        return "", tool_call.get("args", {})
    elif "args" not in tool_call:
        return tool_call.get("function_name", ""), {}
    else:
        return tool_call.get("function_name", ""), tool_call.get("args", {})


tool_extractor = {
    "react": extract_tool_params_react,
    "react_firewall": extract_tool_params_react,
    "plan_and_execute": extract_tool_params_planexecute,
    "sec_react": extract_tool_params_react,
    "xml": extract_tool_params_xml,
    "openai": extract_tool_params_openai,
    "auto": extract_tool_params_any,
}

# observation_extractor = {
#     "react": extract_observation_react,
#     "sec-react": extract_observation_react
# }
