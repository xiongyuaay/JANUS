# AgentGuard Demo Data

这个目录只用于调试训练框架，不是正式训练集。

文件说明：

- `generate_agentguard_demo_dataset.py`
  - 从 `AgentAlign/multi_step_trajectory_generation/multi_step_harmful_responses.json` 抽取少量 harmful 轨迹。
  - 每条轨迹切成前一半和后一半，同时生成 `prediction_task` 和 `judge_task` 两个监督信号。
- `agentguard_rl_debug_dataset.json`
  - 便于直接查看的 JSON 列表格式。
- `agentguard_rl_debug_dataset.jsonl`
  - 便于训练代码逐行读取的 JSONL 格式。
- `prediction_prompt.txt`
  - 预测“后续会不会继续执行有害动作”的提示词。
- `judge_prompt.txt`
  - 判断“当前状态是否有害并输出理由”的提示词。

数据集路径：

- `JSON`：`/mnt/shared-storage-user/miaoziqi/2025_Research/bestwishes/anew/EasyR1_AgentGuard/data/demo/agentguard_rl_debug_dataset.json`
- `JSONL`：`/mnt/shared-storage-user/miaoziqi/2025_Research/bestwishes/anew/EasyR1_AgentGuard/data/demo/agentguard_rl_debug_dataset.jsonl`

顶层键说明（每条样本只有这 3 个 key）：

- `input`
  - 输入给模型的文本，内容是“用户指令 + 已观察到的前半段轨迹”。
- `gt`
  - 训练目标文本，内容是“后半段轨迹（未来真实输出）”。
- `label`
  - 三分类标签（`safe/unsafe/unknown`）：
  - `unsafe`：后半段会继续执行有害动作。
  - `safe`：后半段不会继续执行有害动作（通常是拒答或转向安全回应）。
  - `unknown`：无法明确判断或标签缺失时的兜底值。
