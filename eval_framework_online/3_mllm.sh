set -euo pipefail

# ===== Path and model =====
BASE="${BASE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# MODEL_DIR="Qwen/Qwen2.5-VL-72B-Instruct"   
# MODEL_DIR="OpenGVLab/InternVL2_5-78B"   
# MODEL_DIR="Fancy-MLLM/R1-Onevision-7B"   
# MODEL_DIR="llava-onevision-qwen2-7b-ov-chat"   
# MODEL_DIR="deepseek-ocr"   
# MODEL_DIR="mistralai/Mixtral-8x7B/snapshots/Mixtral-8x7B-Instruct-v0.1"
# MODEL_DIR="Qwen2-VL-7B-SafeRLHF"
# MODEL_DIR="guard/Llama-Guard-3-11B-Vision"
# MODEL_DIR="Qwen/Qwen3-235B-A22B-Instruct-2507-hf"
# MODEL_DIR="Qwen3-Embedding-4B"
# MODEL_DIR="Qwen/Qwen3.5-35B-A3B"
# MODEL_DIR="Qwen/Qwen3.5-397B-A17B-FP8"
MODEL_DIR="${MODEL_DIR:-Qwen/Qwen3-32B}"
PORT="${PORT:-8000}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-256}"
# MODEL_DIR="Qwen/Qwen3-30B-A3B-Instruct-2507"
# MODEL_DIR="SyntheticTuringExperienceTechnologies/qwen-3.5-122B-uncensored-stxt"



LOG_DIR="$BASE/logs"

# ===== 日志命名：mllm + 模型名 + 主机 + 时间戳 =====
mkdir -p "$LOG_DIR"
ts="$(date '+%Y%m%d-%H%M%S')"
model_base="$(basename "$MODEL_DIR")"
name_sanitized="$(echo "$model_base" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9._-' '-')"
host="$(hostname)"
log_file="$LOG_DIR/mllm_${name_sanitized}_${host}_${ts}.log"
ln -sfn "$log_file" "$LOG_DIR/mllm_${name_sanitized}_latest.log"

if [[ -n "${CONDA_SH:-}" ]]; then
  source "$CONDA_SH"
fi
if [[ -n "${CONDA_ENV:-}" ]]; then
  conda activate "$CONDA_ENV"
fi


# ===== 让 vllm 的输出也进入日志 =====
# export PYTHONUNBUFFERED=1
# export VLLM_USE_FA2=0  # Disable FlashAttention v2 kernels to avoid unsupported PTX
# export VLLM_USE_FLASH_ATTENTION=0
# export VLLM_ATTENTION_BACKEND=TORCH_SDPA
# export VLLM_DISABLED_KERNELS=flash_attn_varlen_func,flash_attn_with_kvcache
# export VLLM_USE_FLASH_ATTENTION=0  # Uncomment if you need to fully opt out of FlashAttention
exec >>"$log_file" 2>&1

echo "[$(date '+%F %T')] Launching vLLM (MLLM)..."
echo "Model: $MODEL_DIR"
echo "Log  : $log_file"
echo "Host : $host"
echo "-----"

# vllm serve "$MODEL_DIR" \
#   --port 10033 \
#   --tensor-parallel-size 2 \
#   --max-num-seqs 256 \
#   --limit-mm-per-prompt.video 0 \
#   --trust_remote_code

# embedding model
# vllm serve "$MODEL_DIR" \
#   --task embed \
#   --port 10034 \
#   --tensor-parallel-size 1 \
#   --max-num-seqs 256 \
#   --trust_remote_code

vllm serve "$MODEL_DIR" \
  --port "$PORT" \
  --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
  --max-num-seqs "$MAX_NUM_SEQS" \
  --trust_remote_code \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
