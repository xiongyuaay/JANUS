#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${CONDA_SH:-}" ]]; then
  source "$CONDA_SH"
fi
if [[ -n "${CONDA_ENV:-}" ]]; then
  conda activate "$CONDA_ENV"
fi

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"

STRATEGY="${STRATEGY:-${PROMPT_STRATEGY:-strategy_1_harmful_user_instruction}}"
PROMPT_DIR="${PROMPT_DIR:-${ROOT_DIR}/mcp_prompts/${STRATEGY}}"
RESULTS_DIR="${RESULTS_DIR:-${ROOT_DIR}/results}"
RUN_NAME_PREFIX="${RUN_NAME_PREFIX:-mcp_${STRATEGY}}"
RUN_NAME="${RUN_NAME:-${RUN_NAME_PREFIX}_${TIMESTAMP}}"

COUNT="${COUNT:-1}"
COUNTS="${COUNTS:-}"
CATEGORIES="${CATEGORIES:-}"

RESUME="${RESUME:-false}"
RUN_TRAJECTORY="${RUN_TRAJECTORY:-true}"
AUTO_APPROVE="${AUTO_APPROVE:-true}"
CONTINUE_ON_ERROR="${CONTINUE_ON_ERROR:-true}"

MAX_ITERATIONS="${MAX_ITERATIONS:-3}"
CASE_RETRY_LIMIT="${CASE_RETRY_LIMIT:-2}"
CASE_SCHEDULE="${CASE_SCHEDULE:-sequential}"
CASE_WORKERS="${CASE_WORKERS:-4}"

BASE_URL="${BASE_URL:-http://localhost:8000/v1}"
MODEL="${MODEL:-SyntheticTuringExperienceTechnologies/qwen-3.5-122B-uncensored-stxt}"
API_KEY="${API_KEY:-EMPTY}"
TEMPERATURE="${TEMPERATURE:-0.7}"

TRACE_RUN_NAME="${TRACE_RUN_NAME:-trajectory_${TIMESTAMP}}"
TRAJECTORY_STYLE="${TRAJECTORY_STYLE:-auto}"
MAX_TURNS="${MAX_TURNS:-25}"
TRAJECTORY_WORKERS="${TRAJECTORY_WORKERS:-4}"

TRAJECTORY_BASE_URL="${TRAJECTORY_BASE_URL:-http://localhost:8000/v1}"
TRAJECTORY_API_KEY="${TRAJECTORY_API_KEY:-EMPTY}"
TRAJECTORY_MODEL="${TRAJECTORY_MODEL:-$MODEL}"
TRAJECTORY_TEMPERATURE="${TRAJECTORY_TEMPERATURE:-0.0}"

TOOL_EXECUTOR_BASE_URL="${TOOL_EXECUTOR_BASE_URL:-http://localhost:8000/v1}"
TOOL_EXECUTOR_API_KEY="${TOOL_EXECUTOR_API_KEY:-EMPTY}"
TOOL_EXECUTOR_MODEL="${TOOL_EXECUTOR_MODEL:-$MODEL}"
TOOL_EXECUTOR_TEMPERATURE="${TOOL_EXECUTOR_TEMPERATURE:-0.0}"

if [[ ! -d "$PROMPT_DIR" ]]; then
  echo "Prompt dir does not exist: $PROMPT_DIR" >&2
  echo "Set STRATEGY to a directory name under mcp_prompts, or set PROMPT_DIR directly." >&2
  exit 2
fi

COUNT_ARGS=()

add_count() {
  local item="$1"
  if [[ ! "$item" =~ ^[A-Za-z0-9_]+=[1-9][0-9]*$ ]]; then
    echo "Invalid count item: $item" >&2
    echo "Expected format: category=positive_integer" >&2
    exit 2
  fi
  COUNT_ARGS+=(--count "$item")
}

add_count_list() {
  local list="$1"
  local item
  IFS=',' read -ra items <<< "$list"
  for item in "${items[@]}"; do
    [[ -n "$item" ]] && add_count "$item"
  done
}

add_uniform_counts() {
  local count="$1"
  local categories=()
  local category
  local file

  if [[ ! "$count" =~ ^[1-9][0-9]*$ ]]; then
    echo "COUNT must be a positive integer: $count" >&2
    exit 2
  fi

  if [[ -n "$CATEGORIES" ]]; then
    IFS=',' read -ra categories <<< "$CATEGORIES"
  else
    while IFS= read -r file; do
      category="${file%.md}"
      [[ "$category" == _* ]] && continue
      categories+=("$category")
    done < <(find "$PROMPT_DIR" -maxdepth 1 -type f -name '*.md' -printf '%f\n' | sort)
  fi

  if [[ "${#categories[@]}" -eq 0 ]]; then
    echo "No prompt categories found in: $PROMPT_DIR" >&2
    exit 2
  fi

  for category in "${categories[@]}"; do
    if [[ ! "$category" =~ ^[A-Za-z0-9_]+$ ]]; then
      echo "Invalid category name: $category" >&2
      exit 2
    fi
    add_count "${category}=${count}"
  done
}

if [[ -n "$COUNTS" ]]; then
  add_count_list "$COUNTS"
else
  add_uniform_counts "$COUNT"
fi

AUTO_APPROVE_ARG="--auto-approve"
if [[ "$AUTO_APPROVE" != "true" ]]; then
  AUTO_APPROVE_ARG="--no-auto-approve"
fi

RESUME_ARG="--no-resume"
if [[ "$RESUME" == "true" ]]; then
  RESUME_ARG="--resume"
fi

RUN_TRAJECTORY_ARG="--run-trajectory"
if [[ "$RUN_TRAJECTORY" != "true" ]]; then
  RUN_TRAJECTORY_ARG="--no-run-trajectory"
fi

CONTINUE_ON_ERROR_ARG="--continue-on-error"
if [[ "$CONTINUE_ON_ERROR" != "true" ]]; then
  CONTINUE_ON_ERROR_ARG="--no-continue-on-error"
fi

echo "Strategy: $STRATEGY"
echo "Prompt dir: $PROMPT_DIR"
echo "Results dir: $RESULTS_DIR"
echo "Run name: $RUN_NAME"
echo "Output dir: ${RESULTS_DIR}/${RUN_NAME}"
echo "Resume: $RESUME"
echo "Run trajectory: $RUN_TRAJECTORY"
echo "Trajectory style: $TRAJECTORY_STYLE"
echo "Case retry limit: $CASE_RETRY_LIMIT"
echo "Continue on error: $CONTINUE_ON_ERROR"
echo "Case schedule: $CASE_SCHEDULE"
echo "Case workers: $CASE_WORKERS"
echo "Trajectory workers: $TRAJECTORY_WORKERS"
echo "Case model: $MODEL"
echo "Trajectory model: $TRAJECTORY_MODEL"
echo "Tool executor model: $TOOL_EXECUTOR_MODEL"
echo "Counts: ${COUNT_ARGS[*]}"
echo "Target counts apply to the whole run directory, not only new items."

python src/cases.py \
  --prompt-dir "$PROMPT_DIR" \
  --results-dir "$RESULTS_DIR" \
  --run-name "$RUN_NAME" \
  "$RESUME_ARG" \
  "$AUTO_APPROVE_ARG" \
  --max-iterations "$MAX_ITERATIONS" \
  --base-url "$BASE_URL" \
  --api-key "$API_KEY" \
  --model "$MODEL" \
  --temperature "$TEMPERATURE" \
  --case-retry-limit "$CASE_RETRY_LIMIT" \
  --case-schedule "$CASE_SCHEDULE" \
  "$RUN_TRAJECTORY_ARG" \
  "$CONTINUE_ON_ERROR_ARG" \
  --trace-run-name "$TRACE_RUN_NAME" \
  --trajectory-base-url "$TRAJECTORY_BASE_URL" \
  --trajectory-api-key "$TRAJECTORY_API_KEY" \
  --trajectory-model "$TRAJECTORY_MODEL" \
  --trajectory-temperature "$TRAJECTORY_TEMPERATURE" \
  --trajectory-style "$TRAJECTORY_STYLE" \
  --tool-executor-base-url "$TOOL_EXECUTOR_BASE_URL" \
  --tool-executor-api-key "$TOOL_EXECUTOR_API_KEY" \
  --tool-executor-model "$TOOL_EXECUTOR_MODEL" \
  --tool-executor-temperature "$TOOL_EXECUTOR_TEMPERATURE" \
  --trajectory-max-turns "$MAX_TURNS" \
  --trajectory-workers "$TRAJECTORY_WORKERS" \
  --case-workers "$CASE_WORKERS" \
  "${COUNT_ARGS[@]}"
