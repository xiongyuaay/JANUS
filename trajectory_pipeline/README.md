# LPS Train Pipeline

当前项目是一套基于 `compiled_prompts/*.md` 的 case 与 trajectory 生成管线。
默认 taxonomy 为 8 个小写机器名 category：`copyright`、`fraud`、`harassment`、`sexual`、`cybercrime`、`disinformation`、`drugs`、`hate`。
代码集中在 `src/`，根目录只保留文档、编译后的 prompt 和生成产物。

## 目录

- `src/cases.py`
  - case 生成入口
- `src/trace.py`
  - trajectory 生成入口
- `src/agents/`
  - 多代理角色实现
- `src/config/`
  - 运行配置和 LLM 配置
- `src/modules/`
  - 主流程、日志、解析、存储等模块
- `src/prompts/`
  - system prompt 常量
- `compiled_prompts/`
  - 每个 category 对应一个 authoritative prompt，作为 Orchestrator 的输入模板

## 当前生成流程

### 1. 生成 case

运行：

```bash
python src/cases.py
```

这个脚本会：

- 读取 `compiled_prompts/<category>.md`
- 按 `CATEGORY_COUNTS` 为每个 category 重复生成多条 case
- 调用多代理流程完成：
  - `OrchestratorAgent`
  - `InstructionDesigner`
  - `ToolDeveloper`
  - `CriterionFormulator`
  - `HumanEvaluator`
- 其中 `OrchestratorAgent` 使用 LLM 执行 `DISPATCH / MERGE / REVISION`
- 不再依赖 `LPS-Bench` 里写死的默认 prompt template，真实输入源是 `compiled_prompts/*.md`
- 将 MCP Python 代码落盘
- 将审批通过的 case 保存到输出目录

也支持命令行覆盖配置，例如：

```bash
python src/cases.py \
  --count cybercrime=3 \
  --count fraud=2 \
  --model YOUR_MODEL \
  --base-url http://HOST:PORT/v1 \
  --api-key YOUR_API_KEY \
  --results-dir results \
  --run-name generated_original_flow_v1
```

主要参数：

- `--prompt-dir`
- `--results-dir`
- `--run-name`
- `--output-dir`
- `--count category=count`
- `--auto-approve` / `--no-auto-approve`
- `--max-iterations`
- `--base-url`
- `--api-key`
- `--model`
- `--temperature`

### 统一运行脚本

根目录只保留一个批量运行入口：

```bash
STRATEGY=strategy_1_harmful_user_instruction COUNT=70 ./run.sh
```

`STRATEGY` 对应 `mcp_prompts/` 下的目录名。脚本会读取该目录下的 `*.md`，自动排除 `_shared_base.md`、`_strategy_overview.md` 这类以下划线开头的辅助文件。

数量控制有两种方式：

- `COUNT=70`：给该 strategy 下所有 category 使用同一个数量
- `COUNTS=copyright=70,fraud=20`：只运行指定 category，并分别指定数量

也可以用 `CATEGORIES` 限定 category，再用 `COUNT` 指定统一数量：

```bash
STRATEGY=strategy_8_agentdojo_suite CATEGORIES=benign,direct COUNT=50 ./run.sh
```

常用示例：

```bash
STRATEGY=strategy_8_agentdojo_suite COUNTS=benign=300,direct=80,ignore_previous=80 ./run.sh
STRATEGY=strategy_10_asb_methods COUNTS=clean=500,DPI=150,MP=150,OPI=150 TRAJECTORY_STYLE=react CASE_SCHEDULE=proportional ./run.sh
STRATEGY=strategy_9_agentharm_paired COUNT=1000 MODEL=Qwen/Qwen3.5-397B-A17B-FP8 RESUME=true ./run.sh
```

其它关键配置仍然通过环境变量覆盖，例如 `MODEL`、`BASE_URL`、`API_KEY`、`RUN_NAME`、`PROMPT_DIR`、`TRAJECTORY_STYLE`、`TRAJECTORY_WORKERS`、`CASE_WORKERS`。

### 2. 生成 trajectory

运行：

```bash
python src/trace.py
```

这个脚本会：

- 从 `approved_cases/` 读取 case
- 根据 case 中记录的 MCP 文件路径动态加载工具
- 把工具转换成 OpenAI function calling schema
- 让模型围绕 case instruction 进行多轮 tool-using 对话
- 保存完整 trace

常用参数：

```bash
python src/trace.py \
  --results-dir results \
  --run-name generated_original_flow_v1 \
  --max-cases 10 \
  --model YOUR_MODEL \
  --base-url http://HOST:PORT/v1 \
  --api-key YOUR_API_KEY
```

## 输出目录

默认输出根目录是 `results/`，默认运行名是 `generated_original_flow`。

不同批次建议通过 `--run-name` 区分，例如：

- `results/generated_original_flow/`
- `results/generated_original_flow_v1/`
- `results/generated_original_flow_v2/`
- `results/generated_original_flow_v5/`

case 生成阶段会写出：

- `results/<run-name>/results/*.json`
- `results/<run-name>/approved_cases/case_*.json`
- `results/<run-name>/artifacts/mcp_files/*.py`
- `results/<run-name>/manifest.json`

trajectory 生成阶段会写出：

- `results/<run-name>/trajectories/case_*_trajectory.json`

## 当前代码结构

### case 主链路

- `src/cases.py`
  - 解析命令行参数，构造 `RunConfig`
- `src/modules/run.py`
  - 逐个 category 读取 prompt 并驱动生成
- `src/modules/flow.py`
  - 执行多代理 dispatch / merge / evaluate 主循环

### 关键模块

- `src/agents/orchestrator_agent.py`
- `src/agents/instruction_designer.py`
- `src/agents/tool_developer.py`
- `src/agents/criterion_formulator.py`
- `src/agents/human_evaluator.py`
- `src/modules/parse.py`
- `src/modules/repo.py`

## 推荐使用顺序

1. 准备好 `compiled_prompts/*.md`
2. 运行 `python src/cases.py`
3. 检查 `approved_cases` 和 `artifacts/mcp_files`
4. 运行 `python src/trace.py`
5. 检查 `trajectories`
