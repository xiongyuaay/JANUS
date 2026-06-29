#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Optional conda activation. Leave CONDA_SH empty to use the current environment.
CONDA_SH=${CONDA_SH:-}
CONDA_ENV=${CONDA_ENV:-}
if [ -n "${CONDA_ENV}" ]; then
  if [ -n "${CONDA_SH}" ] && [ -f "${CONDA_SH}" ]; then
    # shellcheck disable=SC1090
    source "${CONDA_SH}"
  elif command -v conda >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
  else
    echo "[ERROR] conda not found; cannot activate ${CONDA_ENV}." >&2
    exit 1
  fi
  conda activate "${CONDA_ENV}"
fi

PROJECT_NAME=${PROJECT_NAME:-easy_r1_janus}
EXPERIMENT=${EXPERIMENT:?Set EXPERIMENT to the JANUS training experiment name.}
GLOBAL_STEP=${GLOBAL_STEP:?Set GLOBAL_STEP, for example global_step_150.}
BASE_MODEL=${BASE_MODEL:-}

MERGE_DIR=${MERGE_DIR:-"${PROJECT_ROOT}/checkpoints/${PROJECT_NAME}/${EXPERIMENT}/${GLOBAL_STEP}/actor"}
HF_DIR="${MERGE_DIR}/huggingface"
OUTPUT_DIR=${OUTPUT_DIR:-"${PROJECT_ROOT}/res_model/${EXPERIMENT}/${GLOBAL_STEP}"}

python -u "${PROJECT_ROOT}/scripts/model_merger.py" --local_dir "${MERGE_DIR}"

mkdir -p "${OUTPUT_DIR}"
cp -a "${HF_DIR}/." "${OUTPUT_DIR}/"

cat > "${OUTPUT_DIR}/SOURCE.md" <<EOF
# Model source

Exported by: $(basename "$0")
Exported at: $(date -Is)

- Base model: ${BASE_MODEL}
- Project: ${PROJECT_NAME}
- Experiment: ${EXPERIMENT}
- Checkpoint step: ${GLOBAL_STEP}
- Source actor dir: ${MERGE_DIR}
- Training config: ${PROJECT_ROOT}/checkpoints/${PROJECT_NAME}/${EXPERIMENT}/experiment_config.json
- Checkpoint tracker: ${PROJECT_ROOT}/checkpoints/${PROJECT_NAME}/${EXPERIMENT}/checkpoint_tracker.json
- Merger: ${PROJECT_ROOT}/scripts/model_merger.py
- Conda env used: ${CONDA_ENV:-current environment}
EOF

echo "Done. HF model written to: ${OUTPUT_DIR}"
