# JANUS Training 使用说明

本目录是 JANUS 的训练模块，基于 `verl` / EasyR1 风格的 GRPO 训练流程。核心目标是训练一个两阶段模型：先根据 `instruction + trajectory_1` 预测后续风险摘要，再根据预测结果输出安全标签。

## 目录结构

- `examples/config.yaml`：默认训练配置。
- `examples/tricks/train_janus_bleu.sh`：使用本地 BLEU 作为 Predictor 辅助文本奖励。
- `examples/tricks/train_janus_embedding.sh`：使用 embedding 服务作为 Predictor 辅助文本奖励。
- `examples/tricks/train_janus_nli.sh`：使用本地 NLI 模型作为 Predictor 辅助文本奖励。
- `examples/reward_function/janus_predict_judge.py`：Judge 输出标签的 reward 函数。
- `scripts/split_val.py`：从原始训练 JSON 中切分 `train_quick.json` 和 `val.json`。
- `scripts/merge_janus_checkpoint.sh`：把训练 checkpoint 合并导出为 HuggingFace 模型目录。

## 1. 环境准备

建议使用独立 conda 环境。训练脚本默认会尝试激活 `vllm-fla`：

```bash
conda create -n vllm-fla python=3.10 -y
conda activate vllm-fla
pip install -e .
```

如果你的环境名不同，运行训练时设置：

```bash
TARGET_CONDA_ENV=your_env bash examples/tricks/train_janus_bleu.sh
```

训练前需要准备一个可加载的基础模型，例如 Qwen 系列 instruct 模型，并在运行时用 `MODEL_PATH` 指定。

## 2. 数据格式

训练脚本默认使用结构化 JANUS 数据，单条样本至少需要这些字段：

```json
{
  "instruction": "...",
  "trajectory_1": "...",
  "summary": "...",
  "label": "safe"
}
```

字段含义：

- `instruction`：用户任务。
- `trajectory_1`：已发生的 agent 轨迹前缀。
- `summary`：未来风险摘要，作为 Predictor 的文本目标。
- `label`：Judge 的安全标签，支持 `safe`、`potential_unsafe`、`unsafe`。
- `category`：可选，但 `scripts/split_val.py` 会用它和 `label` 做分层切分。

## 3. 准备训练和验证集

如果已有 `train_converted_part_*.json`，先切分训练集和验证集：

```bash
python scripts/split_val.py \
  --src "/path/to/janus/train/train_converted_part_*.json" \
  --out_dir /path/to/janus_dataset \
  --val_per_group 2 \
  --min_per_class 30 \
  --train_cap 1700
```

输出：

- `/path/to/janus_dataset/train_quick.json`
- `/path/to/janus_dataset/val.json`

正式训练时可以去掉 `--train_cap`，使用完整训练集。

## 4. Prompt 文件

训练脚本需要两个 prompt：

- `PREDICT_PROMPT`：Predictor 使用，默认路径是 `examples/prompts/prediction_prompt.jinja`。
- `JUDGE_PROMPT`：Judge 使用，默认路径是 `examples/prompts/judge_prompt.jinja`。

当前仓库里可参考的 prompt 在 `examples/demo/` 下。如果默认路径不存在，运行时显式指定：

```bash
PREDICT_PROMPT=examples/demo/prediction_prompt.jinja \
JUDGE_PROMPT=examples/demo/judge_prompt.jinja \
bash examples/tricks/train_janus_bleu.sh
```

## 5. 选择训练入口

推荐先用 BLEU 版本做 smoke test，因为它不依赖外部服务：

```bash
MODEL_PATH=/path/to/base_model \
TRAIN_FILE=/path/to/janus_dataset/train_quick.json \
VAL_FILE=/path/to/janus_dataset/val.json \
PREDICT_PROMPT=examples/demo/prediction_prompt.jinja \
JUDGE_PROMPT=examples/demo/judge_prompt.jinja \
bash examples/tricks/train_janus_bleu.sh
```

三种入口的区别：

- `train_janus_bleu.sh`：本地 BLEU 辅助奖励，最容易跑通。
- `train_janus_embedding.sh`：需要可访问的 vLLM embedding 服务，配置 `VLLM_EMBEDDING_BASE_URL`、`VLLM_EMBEDDING_MODEL`。
- `train_janus_nli.sh`：需要本地 NLI 模型快照，配置 `PREDICT_NLI_MODEL_PATH`，适合离线文本一致性奖励。

## 6. 常用训练参数

这些参数都可以通过环境变量覆盖：

```bash
EXPERIMENT_NAME=janus_exp \
MAX_STEPS=300 \
ROLLOUT_BATCH_SIZE=256 \
VAL_BATCH_SIZE=256 \
PREDICT_ROLL_N=4 \
JUDGE_ROLL_N=8 \
TENSOR_PARALLEL_SIZE=1 \
N_GPUS_PER_NODE=8 \
bash examples/tricks/train_janus_bleu.sh
```

重要参数：

- `MAX_STEPS`：训练步数。
- `ROLLOUT_BATCH_SIZE`：每轮 rollout 的 prompt 数。
- `PREDICT_ROLL_N`：Predictor 每个样本采样数量。
- `JUDGE_ROLL_N`：Judge 每个样本采样数量。
- `MAX_PROMPT_LENGTH`：输入长度上限，默认 `8192`。
- `MAX_RESPONSE_LENGTH`：Judge 输出长度上限，默认 `1536`。
- `TRAJECTORY_PREFIX_MAX_CHARS`：`trajectory_1` 的尾部保留字符数，默认 `6000`。
- `VAL_FREQ`：验证频率。
- `SAVE_FREQ`：保存 checkpoint 频率。

## 7. 输出位置

训练日志默认写入：

```text
log/<EXPERIMENT_NAME>_<timestamp>.log
```

checkpoint 默认写入：

```text
checkpoints/<PROJECT_NAME>/<EXPERIMENT_NAME>/
```

其中 `PROJECT_NAME` 默认是 `easy_r1_janus`。

## 8. 合并导出模型

训练完成后，把 actor checkpoint 合并成 HuggingFace 格式：

```bash
EXPERIMENT=janus_exp \
GLOBAL_STEP=global_step_300 \
BASE_MODEL=/path/to/base_model \
bash scripts/merge_janus_checkpoint.sh
```

默认输入：

```text
checkpoints/easy_r1_janus/<EXPERIMENT>/<GLOBAL_STEP>/actor
```

默认输出：

```text
res_model/<EXPERIMENT>/<GLOBAL_STEP>
```

如果 checkpoint 不在默认位置，可以覆盖：

```bash
MERGE_DIR=/path/to/checkpoint/actor \
OUTPUT_DIR=/path/to/exported_hf_model \
bash scripts/merge_janus_checkpoint.sh
```

## 9. 推荐流程

1. 准备基础模型，并确认 `MODEL_PATH` 可加载。
2. 准备包含 `instruction`、`trajectory_1`、`summary`、`label` 的 JANUS 数据。
3. 用 `scripts/split_val.py` 生成 `train_quick.json` 和 `val.json`。
4. 先运行 `train_janus_bleu.sh` 做小规模 smoke test。
5. 确认日志、验证和 checkpoint 正常后，增大 `MAX_STEPS`、`ROLLOUT_BATCH_SIZE` 或切换到完整训练集。
6. 如需更强 Predictor 文本奖励，再切换到 embedding 或 NLI 入口。
7. 训练完成后用 `scripts/merge_janus_checkpoint.sh` 导出 HuggingFace 模型。
