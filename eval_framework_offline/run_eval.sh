#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# eval_framework launcher
#
# Edit the CONFIG block below, then run:
#     bash run_eval.sh
#
# The same entrypoint handles both static-trace data and offline eval_data.
# Select the dataset by changing DATA_ROOT and BENCHMARKS.
# ----------------------------------------------------------------------------
set -euo pipefail

# ============================= CONFIG =======================================
DATA_ROOT="${DATA_ROOT:-../eval_framework_online/STATIC_TRACE}" # --data-root: path to static trace or eval_data root

# Static-trace benchmarks: agentdojo, agentharm, asb, agent_safetybench, agentlab, lps_bench
# Offline eval_data benchmarks: agentdojo, agentharm, asb_light, asb_standard, lps_bench, mt
BENCHMARKS="${BENCHMARKS:-agentdojo,agentlab,lps_bench}" # --benchmarks: all | comma-separated subset

MODELS="${MODELS:-react_base}" # --models: empty for all | comma-separated model/static-trace folders
SPLITS="${SPLITS:-benign,adversarial}" # --splits: benign | adversarial | benign,adversarial
GUARD="${GUARD:-vllm}" # --guard: empty for baseline | vllm | llamaguard | qwen3guard | llamafirewall | alignmentcheck | sandwichdefense
GUARD_CONFIG="${GUARD_CONFIG:-configs/vllm_example.json}" # --guard-config: empty for guard defaults | path to guard JSON config
MODE="${MODE:-lenient}" # --mode: lenient blocks unsafe only | strict blocks unsafe and potential_unsafe
LIMIT="${LIMIT:-}" # --limit: empty for no cap | positive integer cap per benchmark
STOP_ON_FIRST_FLAG="${STOP_ON_FIRST_FLAG:-1}" # --stop-on-first-flag: 1 enables step replay short-circuit | 0 disables it
CONCURRENCY="${CONCURRENCY:-12}" # --concurrency: positive integer trajectory replay workers per benchmark worker group
BENCHMARK_CONCURRENCY="${BENCHMARK_CONCURRENCY:-6}" # --benchmark-concurrency: positive integer worker groups
MAX_INPUT_CHARS="${MAX_INPUT_CHARS:-20000}" # --max-input-chars: empty for guard default | positive integer total guard input char cap

VLLM_PREDICT_FUTURE_SUMMARY="${VLLM_PREDICT_FUTURE_SUMMARY:-1}" # --vllm-predict-future-summary: 1 enables predicted future summary before judge; only valid when GUARD=vllm
VLLM_TRUTH_FUTURE_SUMMARY="${VLLM_TRUTH_FUTURE_SUMMARY:-0}" # --vllm-truth-future-summary: 1 enables cached ground-truth future summary before judge; only valid when GUARD=vllm
VLLM_FUTURE_SUMMARY_CACHE="${VLLM_FUTURE_SUMMARY_CACHE:-}" # --vllm-future-summary-cache: empty uses default cache path below when truth/random summary is enabled
VLLM_DEFAULT_FUTURE_SUMMARY_CACHE="${VLLM_DEFAULT_FUTURE_SUMMARY_CACHE:-}" # empty uses runs/future_summary_cache_<data_root>_<guard_config>.json
VLLM_RANDOM_FUTURE_SUMMARY="${VLLM_RANDOM_FUTURE_SUMMARY:-0}" # --vllm-random-future-summary: 1 uses a random mismatched cached future summary before judge

JUDGE_PREFIX_RATIO="${JUDGE_PREFIX_RATIO:-0.75}" # --judge-prefix-ratio: empty disables cap | one of 0.25,0.5,0.75
OUT="${OUT:-runs/eval_report_$(date +%Y%m%d_%H%M%S).json}" # --out: output JSON path
LOG_LEVEL="${LOG_LEVEL:-INFO}" # --log-level: DEBUG | INFO | WARNING | ERROR
# ============================== END =========================================

cd "$(dirname "$0")"

default_cache_path() {
    local data_name config_name
    data_name="${DATA_ROOT%/}"
    data_name="${data_name##*/}"
    config_name="${GUARD_CONFIG##*/}"
    config_name="${config_name%.json}"
    echo "runs/future_summary_cache_${data_name}_${config_name}.json"
}

if [[ "$GUARD" == "vllm" && ( "$VLLM_TRUTH_FUTURE_SUMMARY" == "1" || "$VLLM_RANDOM_FUTURE_SUMMARY" == "1" ) ]]; then
    if [[ -z "$VLLM_FUTURE_SUMMARY_CACHE" ]]; then
        if [[ -z "$VLLM_DEFAULT_FUTURE_SUMMARY_CACHE" ]]; then
            VLLM_DEFAULT_FUTURE_SUMMARY_CACHE="$(default_cache_path)"
        fi
        VLLM_FUTURE_SUMMARY_CACHE="$VLLM_DEFAULT_FUTURE_SUMMARY_CACHE"
    fi
    if [[ ! -f "$VLLM_FUTURE_SUMMARY_CACHE" ]]; then
        CACHE_CMD=(python scripts/cache_future_summaries.py
            --data-root "$DATA_ROOT"
            --benchmarks "$BENCHMARKS"
            --splits "$SPLITS"
            --guard-config "$GUARD_CONFIG"
            --out "$VLLM_FUTURE_SUMMARY_CACHE"
            --concurrency "$CONCURRENCY"
            --log-level "$LOG_LEVEL"
        )
        [[ -n "$MODELS" ]] && CACHE_CMD+=(--models "$MODELS")
        [[ -n "$LIMIT" ]] && CACHE_CMD+=(--limit "$LIMIT")
        [[ -n "$MAX_INPUT_CHARS" ]] && CACHE_CMD+=(--max-input-chars "$MAX_INPUT_CHARS")
        echo "+ ${CACHE_CMD[*]}"
        "${CACHE_CMD[@]}"
    fi
fi

CMD=(python -m eval_framework.cli
    --data-root "$DATA_ROOT"         # options: path to data root
    --benchmarks "$BENCHMARKS"       # options: all | comma-separated benchmark list
    --splits "$SPLITS"               # options: benign | adversarial | benign,adversarial
    --mode "$MODE"                   # options: lenient | strict
    --out "$OUT"                     # options: output JSON path
    --log-level "$LOG_LEVEL"         # options: DEBUG | INFO | WARNING | ERROR
)

[[ -n "$MODELS"       ]] && CMD+=(--models "$MODELS") # options: empty for all | comma-separated model/static trace folders
[[ -n "$GUARD"        ]] && CMD+=(--guard "$GUARD") # options: empty for baseline | vllm | llamaguard | qwen3guard | llamafirewall | alignmentcheck | sandwichdefense | stub_safe | stub_unsafe
[[ -n "$GUARD_CONFIG" ]] && CMD+=(--guard-config "$GUARD_CONFIG") # options: empty | path to JSON config
[[ -n "$LIMIT"        ]] && CMD+=(--limit "$LIMIT") # options: empty | positive integer
[[ "$STOP_ON_FIRST_FLAG" == "1" ]] && CMD+=(--stop-on-first-flag) # options: enabled when STOP_ON_FIRST_FLAG=1
[[ -n "$CONCURRENCY"  ]] && CMD+=(--concurrency "$CONCURRENCY") # options: positive integer
[[ -n "$BENCHMARK_CONCURRENCY"  ]] && CMD+=(--benchmark-concurrency "$BENCHMARK_CONCURRENCY") # options: positive integer
[[ -n "$MAX_INPUT_CHARS" ]] && CMD+=(--max-input-chars "$MAX_INPUT_CHARS") # options: empty | positive integer
[[ -n "$JUDGE_PREFIX_RATIO" ]] && CMD+=(--judge-prefix-ratio "$JUDGE_PREFIX_RATIO") # options: empty | 0.25 | 0.5 | 0.75
if [[ "$GUARD" == "vllm" ]]; then
    [[ "$VLLM_PREDICT_FUTURE_SUMMARY" == "1" ]] && CMD+=(--vllm-predict-future-summary) # options: enabled when VLLM_PREDICT_FUTURE_SUMMARY=1
    [[ "$VLLM_TRUTH_FUTURE_SUMMARY" == "1" ]] && CMD+=(--vllm-truth-future-summary) # options: enabled when VLLM_TRUTH_FUTURE_SUMMARY=1
    [[ "$VLLM_RANDOM_FUTURE_SUMMARY" == "1" ]] && CMD+=(--vllm-random-future-summary) # options: enabled when VLLM_RANDOM_FUTURE_SUMMARY=1
    [[ -n "$VLLM_FUTURE_SUMMARY_CACHE" ]] && CMD+=(--vllm-future-summary-cache "$VLLM_FUTURE_SUMMARY_CACHE") # options: path to cache generated by scripts/cache_future_summaries.py
fi

echo "+ ${CMD[*]}"
"${CMD[@]}"
