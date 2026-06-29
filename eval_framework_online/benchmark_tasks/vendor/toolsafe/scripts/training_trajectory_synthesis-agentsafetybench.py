import re
import json,csv
import pandas as pd
import sys
from tqdm import tqdm
from copy import deepcopy
import os
import shutil
sys.path.append('./agent_simulation/environments')
sys.path.append('./agent_simulation')
from EnvManager import EnvManager
from openai import OpenAI
from agent_prompts import *
api_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
#api_base = "http://0.0.0.0:8848/v1"
api_key = os.getenv("OPENAI_API_KEY", "")


client = OpenAI(
    api_key=api_key,
    base_url=api_base
)

env_manager = EnvManager()


# known_actions = {
#     "calculate": calculate,
#     "average_dog_weight": average_dog_weight
# }

# available_tool_descriptions = {
#     "calculate": "calculate the result of the expression",
#     "average_dog_weight": "get the average weight of a dog"
# }

# available_tool_params = {
#     "calculate": {
#         "what": "the expression to calculate"
#     },
#     "average_dog_weight": {
#         "name": "the breed of the dog"
#     }
# }

def parse_envs(envs_info):
    envs = []
    tool_descs = []

    known_actions = {}
    available_tool_descriptions = {}
    available_tool_params = {}

    for env_info in envs_info:
        env_name = env_info['name']
        if 'parameters' in env_info:
            env_params = env_info['parameters'] or None
        else:
            env_params = None
        env = env_manager.init_env(env_name, env_params)
        if env is None:
            print("No environment:", envs_info)
            raise ValueError(f"Environment {env_name} not found.")
        envs.append(env) # 每个env是一个class object
        
        tool_names = env_info['tools']
        tool_desc = env.get_tool_descs(tool_names)
        tool_descs.append(tool_desc)
    
    assert len(envs) == len(tool_descs)

    for i in range(len(envs)):
        env = envs[i]
        tool_desc = tool_descs[i]
        for k in range(len(tool_desc)):
            if tool_desc[k]['name'] in available_tool_descriptions:
                print("Duplicate Name Tool:", tool_desc[k]['name'], env)
            available_tool_descriptions[tool_desc[k]['name']] = tool_desc[k]['description']
            available_tool_params[tool_desc[k]['name']] = tool_desc[k]['parameters']['properties']
            known_actions[tool_desc[k]['name']] = env

    # for tool_desc in tool_descs:
    #     available_tool_descriptions[tool_desc['name']] = tool_desc['description']
    #     available_tool_params[tool_desc['name']] = tool_desc['parameters']['properties']

    #     for env in envs:
    #         if env.has_tool(tool_desc['name']):
    #             known_actions[tool_desc['name']] = env

    return known_actions, available_tool_descriptions, available_tool_params


def format_tools_for_prompt(tool_descriptions, tool_params):
    result = []
    for tool, desc in tool_descriptions.items():
        result.append(f"{tool}: {desc}")
        params = tool_params.get(tool, {})
        if params:
            param_str = "\n    ".join([f"{k}: {v}" for k, v in params.items()])
            result.append(f"  parameters:\n    {param_str}")
        result.append("")
    return "\n".join(result)

def extract_tool_params(text):
    """
    从模型输出中提取 tool_name 和 tool_params。
    支持以下两种格式：
        Action: average_dog_weight
        Action Input: breed="Bulldog"
    或：
        Action: average_dog_weight
        Action Input: {"breed": "Bulldog"}
    """
    pattern = r"Action:\s*(\w+).*?Action Input:\s*(.*)"
    match = re.search(pattern, text, re.S)
    if not match:
        return "", {}

    tool_name = match.group(1).strip()
    raw_params = match.group(2).strip()

    # 尝试 JSON 解析
    try:
        # 处理 JSON 坏格式，比如末尾多句号、反引号等
        raw_params_clean = raw_params.strip().rstrip('.').strip('`')
        params_dict = json.loads(raw_params_clean)
        if isinstance(params_dict, dict):
            return tool_name, params_dict
    except json.JSONDecodeError:
        pass  # 不是 JSON，就进入下一步解析

    # 尝试从形如 breed="Bulldog", age=2 的文本解析
    params_dict = {}
    pairs = re.findall(r'(\w+)\s*=\s*["\']?([^,"\']+)["\']?', raw_params)
    for k, v in pairs:
        params_dict[k] = v.strip()

    return tool_name, params_dict

class Agent:
    def __init__(self, system="", model_name="gpt-4o"):
        self.system = system
        self.messages = []
        self.model_name = model_name
        if self.system:
            self.messages.append({"role": "system", "content": system})

    def __call__(self, message):
        self.messages.append({"role": "user", "content": message})
        result = self.execute()
        self.messages.append({"role": "assistant", "content": result})
        return result

    def execute(self):
        response = client.chat.completions.create(
            model=self.model_name,
            messages=self.messages
        )
        return response.choices[0].message.content

def react_agent_invoke(model_name="gpt-4o-mini", query="", available_tool_descriptions={}, available_tool_params={}, known_actions={}, max_turns=5):
    i = 0
    logs = ""
    available_tool_descriptions_str=format_tools_for_prompt(available_tool_descriptions, available_tool_params)
    available_tool_list_str = str(list(available_tool_descriptions.keys()))
    logs += "[System Prompt]:\n" + REACT_SYSTEM_PROMPT.format(available_tool_descriptions=available_tool_descriptions_str, available_tool_names=available_tool_list_str)
    logs += "\n-----------------------------------\n"
    bot = Agent(system=REACT_SYSTEM_PROMPT.format(available_tool_descriptions=available_tool_descriptions_str, available_tool_names=available_tool_list_str), model_name=model_name)
    next_prompt = query
    logs += query
    logs += "\n-----------------------------------\n"

    while i < max_turns:
        i += 1
        result = bot(next_prompt)
        # print(result)
        # print("-----------------------------")
        logs += result
        logs += "\n-----------------------------------\n"

        tool_name, tool_params = extract_tool_params(result)

        if tool_name:
            #print(f" -- running {tool_name} {tool_params}")
            logs += f" -- running {tool_name} {tool_params}\n"
            if tool_name in known_actions:
                observation = known_actions[tool_name].call_tool(tool_name, deepcopy(tool_params))
            else:
                observation = f"Unknown tool: {tool_name}"
            #print("Observation:", observation)
            logs += "Observation: "+ str(observation)
            logs += "\n-----------------------------------\n"

            next_prompt = f"Observation: {observation}"
        else:
            #print("Response:", result)
            break
    return logs, bot.messages

def filter_data(data_dir_path, log_csv_path):
    output_dir = data_dir_path + "_filter"
    os.makedirs(output_dir, exist_ok=True)

    matched_files = []

    for filename in os.listdir(data_dir_path):
        if filename.endswith(".txt"):
            file_path = os.path.join(data_dir_path, filename)

            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if "Observation:" in content:
                matched_files.append(file_path)
                shutil.copy(file_path, os.path.join(output_dir, filename))
                print(f"✅ save: {filename}")

    print(f"filter completed, results in: {output_dir}")
    
    df = pd.read_csv(log_csv_path)

    if "log_path" not in df.columns:
        raise ValueError("CSV not found 'log_path' columns!")

    filtered_df = df[df["log_path"].isin(matched_files)]

    # 5. 保存到新的 CSV
    filtered_csv_path = os.path.splitext(log_csv_path)[0] + "_filtered.csv"
    filtered_df.to_csv(filtered_csv_path, index=False, encoding='utf-8-sig')

    print(f"✅ filtered CSV: {filtered_csv_path}")



if __name__ == "__main__":
    model_name = "gpt-4o-2024-08-06"

    data_path = "./agent_simulation/data/released_data.json"
    with open(data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    print("the number of samples in seed dataset:", len(data))

    output_save_dir = "./agent_simulation/logs_released_data/{model_name}_{agent_type}".format(model_name=model_name, agent_type="react")
    os.makedirs(output_save_dir, exist_ok=True)

    output_csv_path = "./agent_simulation/logs_released_data/{model_name}_{agent_type}_.csv".format(model_name=model_name, agent_type="react")

    # === 1. 初始化 CSV 文件（写表头）===
    csv_headers = ["id", "instruction", "risks", "failure_modes", "fulfillable",
                   "environment", "messages", "log_path"]
    with open(output_csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writeheader()

    # === 2. 循环处理每个样本(进入agent) ===
    idx = 0
    for sample in data:
        idx += 1
        # if idx < 364:
        #     continue
        output_save_path = output_save_dir + "/log_{id}.txt".format(id=sample["id"])
        user_query = sample["instruction"]
        
        if sample['environments'] and sample['environments'][0]['name'] != '':
            known_actions, available_tool_descriptions, available_tool_params = parse_envs(sample['environments'])
            # print(known_actions)
            # print("--------------------------------")
            # print(available_tool_descriptions)
            # print("--------------------------------")
            # print(available_tool_params)
            # print("--------------------------------")
    
        logs, messages = react_agent_invoke(
            model_name=model_name, 
            query=user_query, 
            available_tool_descriptions=available_tool_descriptions, 
            available_tool_params=available_tool_params, 
            known_actions=known_actions
        )

        with open(output_save_path, 'a', encoding='utf-8') as f:
            f.write(logs)
            f.write("\n-----------------------------------\n")
        
        # === 3. 写入 CSV 一行 ===
        with open(output_csv_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers)
            writer.writerow({
                "id": sample.get("id", ""),
                "instruction": sample.get("instruction", ""),
                "risks": json.dumps(sample.get("risks", ""), ensure_ascii=False),
                "failure_modes": json.dumps(sample.get("failure_modes", ""), ensure_ascii=False),
                "fulfillable": sample.get("fulfillable", ""),
                "environment": json.dumps(sample.get("environments", ""), ensure_ascii=False),
                "messages": json.dumps(messages, ensure_ascii=False),
                "log_path": output_save_path
            })
        print(f"[{idx}/{len(data)}] ✅ Finished sample id={sample.get('id', '')}")

    # === 全部完成后打印一句 ===
    print(f"\n🎉 All {len(data)} samples processed successfully!")
    print(f"Results saved to: {output_csv_path}")

    filter_data(output_save_dir, output_csv_path)