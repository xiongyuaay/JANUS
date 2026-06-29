# JANUS

JANUS contains tools for generating agent-safety trajectories, training guard models, and evaluating them on offline and online benchmarks.

## Layout

- `trajectory_pipeline/`: generate synthetic cases and tool-use trajectories.
- `training/`: train JANUS guard models with GRPO-style Predictor/Judge rewards.
- `eval_framework_offline/`: evaluate on offline or static traces.
- `eval_framework_online/`: evaluate agents in online benchmark environments.

## Basic Usage

Install shared dependencies:

```bash
pip install -r requirements.txt
```

Generate trajectory data:

```bash
cd trajectory_pipeline
STRATEGY=strategy_1_harmful_user_instruction COUNT=10 ./run.sh
```

Train a model:

```bash
cd training
MODEL_PATH=/path/to/base_model \
TRAIN_FILE=/path/to/train_quick.json \
VAL_FILE=/path/to/val.json \
bash examples/tricks/train_janus_bleu.sh
```

Run online evaluation:

```bash
cd eval_framework_online
python run_unified_eval.py
```

See module-level README files for detailed configuration.
