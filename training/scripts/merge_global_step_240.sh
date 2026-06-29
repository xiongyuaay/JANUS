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
GLOBAL_STEP=${GLOBAL_STEP:-global_step_240}
MERGE_DIR=${MERGE_DIR:-"${PROJECT_ROOT}/checkpoints/${PROJECT_NAME}/${EXPERIMENT}/${GLOBAL_STEP}/actor"}

python -u "${PROJECT_ROOT}/scripts/model_merger.py" --local_dir "${MERGE_DIR}"
