# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib.util
import os
import sys
from collections import defaultdict
from functools import partial
from typing import Any, Callable, Optional, Tuple, TypedDict

import numpy as np

import torch
from transformers import PreTrainedTokenizer

from ...protocol import DataProto
from .config import RewardConfig
from .predict_text_reward import build_predict_text_reward_from_env, resolve_predict_text_reward_type_from_env
from .vllm_judge import build_judge_from_env, env_flag, env_int


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _env_float_any(default: float, *names: str) -> float:
    value = default
    for name in reversed(names):
        value = _env_float(name, default=value)
    return value


def _env_flag_any(default: bool, *names: str) -> bool:
    value = default
    for name in names:
        raw = os.getenv(name)
        if raw is not None:
            return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "t"}
    return value


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


class RewardInput(TypedDict):
    response: str
    response_length: int
    ground_truth: str


class RewardScore(TypedDict):
    overall: float
    format: Optional[float]
    accuracy: Optional[float]


SequentialRewardFunction = Callable[[RewardInput], RewardScore]

BatchRewardFunction = Callable[[list[RewardInput]], list[RewardScore]]


class SequentialFunctionRewardManagerMixin:
    reward_fn: SequentialRewardFunction

    def compute_reward_sequential(self, data: DataProto) -> Tuple[torch.Tensor, dict[str, list[float]]]:
        reward_tensor = torch.zeros_like(data.batch["responses"], dtype=torch.float32)
        reward_metrics = defaultdict(list)
        response_ids = data.batch["responses"]
        response_length = torch.sum(data.batch["response_mask"], dim=-1)
        for i in range(len(data)):
            cur_response_length = int(response_length[i].item())  # avoid tensor indexing error
            valid_response_ids = response_ids[i][:cur_response_length]
            response_str = self.tokenizer.decode(
                valid_response_ids, skip_special_tokens=self.config.skip_special_tokens
            )
            score = self.reward_fn(
                {
                    "response": response_str,
                    "response_length": cur_response_length,
                    "ground_truth": data.non_tensor_batch["ground_truth"][i],
                }
            )
            reward_tensor[i, cur_response_length - 1] = score["overall"]
            for key, value in score.items():
                reward_metrics[key].append(value)

        return reward_tensor, reward_metrics


class BatchFunctionRewardManagerMixin:
    reward_fn: BatchRewardFunction

    def compute_reward_batch(self, caption_batch: DataProto, solve_batch: DataProto) -> Tuple[torch.Tensor, dict[str, list[float]]]:
        reward_inputs = []
        solve_response_ids = solve_batch.batch["responses"]
        solve_response_length = torch.sum(solve_batch.batch["response_mask"], dim=-1)
        for i in range(len(solve_batch)):
            cur_response_length = int(solve_response_length[i].item())  # avoid tensor indexing error
            valid_response_ids = solve_response_ids[i][:cur_response_length]
            response_str = self.tokenizer.decode(
                valid_response_ids, skip_special_tokens=self.config.skip_special_tokens
            )
            reward_inputs.append(
                {
                    "response": response_str,
                    "response_length": cur_response_length,
                    "ground_truth": solve_batch.non_tensor_batch["ground_truth"][i],
                }
            )

        reward_metrics = defaultdict(list)

        solve_scores = self.reward_fn(reward_inputs)
        if len(solve_scores) != len(solve_batch):
            raise ValueError(
                f"Reward function returned {len(solve_scores)} scores for {len(solve_batch)} solve samples."
            )

        # In self-play, solve_batch contains N solutions per caption.
        # Here we infer the solver group size (solutions per caption).
        if len(caption_batch) <= 0 or len(solve_batch) <= 0 or len(solve_batch) % len(caption_batch) != 0:
            raise ValueError(
                f"Invalid self-play batch shapes: len(solve_batch)={len(solve_batch)} "
                f"len(caption_batch)={len(caption_batch)}. solve_batch must be a non-empty multiple of caption_batch."
            )
        n = len(solve_batch) // len(caption_batch)
        if n <= 0:
            raise ValueError(
                f"Invalid self-play batch shapes: len(solve_batch)={len(solve_batch)} "
                f"len(caption_batch)={len(caption_batch)} => n={n}."
            )

        # Caption group size (captions per prompt) can differ from solver group size `n`.
        #
        # Priority:
        #   1) env var `caption_rollout_n` / `CAPTION_ROLLOUT_N` (set by the launcher script)
        #   2) infer from prompt_uid/uid grouping in caption_batch
        #   3) fallback to `n` (legacy behavior: caption_group_n == n)
        caption_group_n: Optional[int] = None
        cap_raw = os.getenv("caption_rollout_n")
        if cap_raw is None:
            cap_raw = os.getenv("CAPTION_ROLLOUT_N")
        if cap_raw is not None:
            try:
                caption_group_n = int(str(cap_raw).strip())
            except Exception:
                caption_group_n = None

        if caption_group_n is not None:
            if caption_group_n <= 0 or (len(caption_batch) % caption_group_n != 0):
                print(
                    f"[solver_update] WARN invalid caption_rollout_n={caption_group_n}; "
                    f"len(caption_batch)={len(caption_batch)}. Falling back to inference."
                )
                caption_group_n = None

        if caption_group_n is None:
            try:
                puid_arr = caption_batch.non_tensor_batch.get("prompt_uid", None)
                if puid_arr is None:
                    puid_arr = caption_batch.non_tensor_batch.get("uid", None)
                if puid_arr is not None and len(puid_arr) > 0:
                    num_prompts = len(set([str(x) for x in puid_arr]))
                    if num_prompts > 0:
                        inferred = len(caption_batch) // num_prompts
                        if inferred > 0 and inferred * num_prompts == len(caption_batch):
                            caption_group_n = inferred
            except Exception:
                caption_group_n = None

        if caption_group_n is None or caption_group_n <= 0:
            caption_group_n = n

        caption_response_ids = caption_batch.batch["responses"]
        caption_response_length = torch.sum(caption_batch.batch["response_mask"], dim=-1)

        caption_reward_tensor = torch.zeros_like(caption_batch.batch["responses"], dtype=torch.float32)
        caption_reward_key = getattr(self.config, "caption_reward_key", "accuracy")

        # 1) Base caption reward / utility: mean judge correctness under each prediction.
        utility_scores: list[float] = []
        caption_lens: list[int] = []
        for i in range(len(caption_batch)):
            cur_response_length = int(caption_response_length[i].item())  # avoid tensor indexing error
            caption_lens.append(cur_response_length)

            cur_scores: list[float] = []
            for temp in solve_scores[i * n : (i + 1) * n]:
                v = temp.get(caption_reward_key, None)
                if v is None:
                    v = temp.get("accuracy", None)
                if v is None:
                    v = temp.get("overall", None)
                if v is None:
                    raise KeyError(
                        f"Cannot find caption reward key '{caption_reward_key}' (or fallbacks) "
                        f"in solve_scores entry keys={list(temp.keys())}."
                    )
                cur_scores.append(float(v))

            utility_scores.append(float(sum(cur_scores) / max(len(cur_scores), 1)))

        reward_metrics["caption_utility"] = [float(x) for x in utility_scores]

        caption_scores = list(utility_scores)
        text_reward_type = resolve_predict_text_reward_type_from_env()
        predict_lambda = _clamp(_env_float_any(0.5, "predict_reward_lambda", "PREDICT_REWARD_LAMBDA"))
        text_only_reward = _env_flag_any(False, "predict_text_only_reward", "PREDICT_TEXT_ONLY_REWARD")
        warmup_steps = env_int("predict_warmup_steps", default=env_int("PREDICT_WARMUP_STEPS", default=0))
        current_step = 0
        try:
            current_step = int(caption_batch.meta_info.get("global_step", solve_batch.meta_info.get("global_step", 0)))
        except Exception:
            current_step = 0
        is_validation = bool(caption_batch.meta_info.get("is_validation", solve_batch.meta_info.get("is_validation", False)))
        text_reward_in_val = _env_flag_any(False, "predict_text_reward_in_val", "PREDICT_TEXT_REWARD_IN_VAL")

        text_reward_values: list[float] = [0.0 for _ in range(len(caption_batch))]
        text_reward_fail = 0
        # global_step is incremented before generation, so <= gives exactly
        # PREDICT_WARMUP_STEPS training updates. Keep validation utility-only
        # unless explicitly requested; otherwise early val metrics are polluted by
        # the auxiliary NLI/BLEU/embedding reward.
        use_text_reward_now = bool(
            text_reward_type != "none"
            and (text_only_reward or (warmup_steps > 0 and current_step <= warmup_steps))
            and (not is_validation or text_reward_in_val)
        )
        if use_text_reward_now:
            cached_backend = getattr(self, "_predict_text_reward_backend", None)
            if cached_backend != text_reward_type or not hasattr(self, "_predict_text_reward_inited"):
                self._predict_text_reward_inited = True
                backend_name, scorer = build_predict_text_reward_from_env()
                self._predict_text_reward_backend = backend_name
                self._predict_text_reward_scorer = scorer
                if not getattr(self, "_predict_text_reward_backend_logged", False):
                    print(
                        "[predict_text_reward] "
                        f"backend={backend_name} warmup_steps={warmup_steps} "
                        f"predict_reward_lambda={predict_lambda} text_only_reward={text_only_reward}"
                    )
                    self._predict_text_reward_backend_logged = True
            scorer = getattr(self, "_predict_text_reward_scorer", None)
            backend_name = getattr(self, "_predict_text_reward_backend", text_reward_type)
            if scorer is not None:
                caption_texts: list[str] = []
                for i in range(len(caption_batch)):
                    cur_len = int(caption_response_length[i].item())
                    ids = caption_response_ids[i][:cur_len]
                    if isinstance(ids, torch.Tensor):
                        ids = ids.tolist()
                    caption_texts.append(
                        self.tokenizer.decode(ids, skip_special_tokens=self.config.skip_special_tokens) if cur_len > 0 else ""
                    )
                target_arr = caption_batch.non_tensor_batch.get("ground_truth", None)
                if target_arr is None:
                    raise KeyError("caption_batch.non_tensor_batch['ground_truth'] is required for predictor text reward")
                target_texts = [str(x) for x in target_arr]

                strict_runtime = _env_flag_any(
                    bool(backend_name == "nli"),
                    "predict_text_reward_strict",
                    "PREDICT_TEXT_REWARD_STRICT",
                )
                try:
                    aux_scores, aux_errors = scorer.score_batch(caption_texts, target_texts)
                except Exception as e:
                    if strict_runtime:
                        raise RuntimeError(f"predictor text reward backend '{backend_name}' failed during scoring") from e
                    aux_scores = [None for _ in range(len(caption_batch))]
                    aux_errors = [f"scorer exception: {e}"]

                if len(aux_scores) != len(caption_batch):
                    aux_errors = list(aux_errors) + [
                        f"scorer returned {len(aux_scores)} scores for {len(caption_batch)} captions; aligning by pad/truncate"
                    ]
                    if len(aux_scores) < len(caption_batch):
                        aux_scores = list(aux_scores) + [None] * (len(caption_batch) - len(aux_scores))
                    else:
                        aux_scores = list(aux_scores)[: len(caption_batch)]

                if aux_errors:
                    if text_only_reward:
                        print(
                            f"[predict_text_reward:{backend_name}] Got {len(aux_errors)} scoring errors; "
                            "failed text rewards will be set to 0. "
                            f"Showing up to {len(aux_errors)} examples:"
                        )
                    else:
                        print(
                            f"[predict_text_reward:{backend_name}] Got {len(aux_errors)} scoring errors; "
                            "falling back to utility reward when needed. "
                            f"Showing up to {len(aux_errors)} examples:"
                        )
                    for err in aux_errors:
                        print(f"[predict_text_reward:{backend_name}]   {err}")

                blended_scores: list[float] = []
                for util, aux in zip(utility_scores, aux_scores):
                    if aux is None:
                        text_reward_fail += 1
                        aux_val = 0.0
                        if text_only_reward:
                            blended_scores.append(0.0)
                        else:
                            blended_scores.append(float(util))
                    else:
                        aux_val = _clamp(float(aux))
                        if text_only_reward:
                            blended_scores.append(float(aux_val))
                        else:
                            blended_scores.append(float((1.0 - predict_lambda) * aux_val + predict_lambda * util))
                    text_reward_values[len(blended_scores) - 1] = aux_val

                if (strict_runtime or text_only_reward) and len(caption_batch) > 0 and text_reward_fail >= len(caption_batch):
                    if text_only_reward:
                        raise RuntimeError(
                            f"predictor text reward backend '{backend_name}' failed for every caption; "
                            "PREDICT_TEXT_ONLY_REWARD=true forbids utility fallback."
                        )
                    raise RuntimeError(
                        f"predictor text reward backend '{backend_name}' failed for every caption; "
                        "refusing to continue utility-only training. Set PREDICT_TEXT_REWARD_STRICT=false to allow fallback."
                    )
                caption_scores = blended_scores
            else:
                if text_only_reward:
                    raise RuntimeError("PREDICT_TEXT_ONLY_REWARD=true requires an active predictor text reward backend.")
                use_text_reward_now = False

        reward_metrics["caption_text_reward"] = [float(x) for x in text_reward_values]
        reward_metrics["caption_text_reward_active"] = [1.0 if use_text_reward_now else 0.0]
        if len(caption_batch) > 0:
            reward_metrics["caption_text_reward_fail_frac"] = [float(text_reward_fail) / float(len(caption_batch))]

        # Backward-compatibility for older dashboards/log parsers that still look for
        # the historical embedding-specific metric names.
        reward_metrics["caption_similarity"] = [float(x) for x in text_reward_values]
        reward_metrics["caption_similarity_active"] = [1.0 if use_text_reward_now else 0.0]
        if len(caption_batch) > 0:
            reward_metrics["caption_similarity_fail_frac"] = [float(text_reward_fail) / float(len(caption_batch))]
        raw_caption_scores = list(caption_scores)

        if env_flag("use_raw", default=False) or env_flag("USE_RAW", default=False):
            # Expose raw caption scores for driver-side online filtering.
            # NOTE: We intentionally do NOT change the actual training reward
            # tensor; vLLM judge gating (label=0 => reward=0) is still applied
            # below.
            reward_metrics["caption_raw"] = [float(x) for x in raw_caption_scores]
            if not getattr(self, "_use_raw_metrics_logged", False):
                print("[reward] use_raw=true; exposing caption_raw for online filtering")
                self._use_raw_metrics_logged = True

        # 2) Optional external vLLM judge: gate caption reward.
        #
        # Enable via env var:
        #   export vllm_judge=True
        # And provide server ip (ports default to 9100-9103):
        #   export vllm_judge_ip=...
        #
        # Judge contract:
        #   - return 0 => caption leaks answer OR has perception error => set caption reward to 0
        #   - return 1 => keep caption reward unchanged
        #   - request failure => keep caption reward unchanged
        #
        # NOTE:
        #   If `caption_batch.non_tensor_batch["vllm_judge_label"]` is provided by the trainer
        #   (e.g. in discard/resample mode), we will *reuse* those labels to avoid judging twice.
        pre_labels = caption_batch.non_tensor_batch.get("vllm_judge_label", None)
        labels: Optional[list[Optional[int]]] = None
        errors: list[str] = []

        if pre_labels is not None:
            # numpy/list -> python flat list
            try:
                pre_arr = np.asarray(pre_labels, dtype=object)
                labels = pre_arr.reshape(-1).tolist()
            except Exception:
                try:
                    labels = list(pre_labels)
                except Exception:
                    labels = None

            if labels is None or len(labels) != len(caption_batch):
                print(
                    f"[vllm_judge] WARN invalid vllm_judge_label: len(vllm_judge_label)={0 if labels is None else len(labels)} "
                    f"vs len(caption_batch)={len(caption_batch)}. Ignoring precomputed labels and falling back to live judge (if enabled)."
                )
                labels = None

        if labels is None:
            if not hasattr(self, "_vllm_judge_inited"):
                # Cache the judge client per reward worker process.
                self._vllm_judge_inited = True
                self._vllm_caption_judge = build_judge_from_env()
            judge = getattr(self, "_vllm_caption_judge", None)

            if judge is not None:
                # Best-effort extract question field from non-tensor batch.
                q_key = None
                for cand in ("problem", "prompt", "question", "query", "input", "text"):
                    if cand in caption_batch.non_tensor_batch:
                        q_key = cand
                        break

                # Decode captions.
                obs_responses = caption_batch.batch["responses"]
                obs_lens = torch.sum(caption_batch.batch["response_mask"], dim=-1)

                questions: list[str] = []
                captions: list[str] = []
                mmd_list: list[Any] = []
                mmd_arr = caption_batch.non_tensor_batch.get("multi_modal_data", None)
                q_arr = caption_batch.non_tensor_batch.get(q_key, None) if q_key is not None else None

                for i in range(len(caption_batch)):
                    # question
                    q = ""
                    if q_arr is not None:
                        try:
                            if len(q_arr) > i:
                                q = str(q_arr[i])
                        except Exception:
                            q = ""

                    # caption decode (best-effort)
                    cur_len = int(obs_lens[i].item())
                    if cur_len > 0:
                        try:
                            ids = obs_responses[i][:cur_len]
                            if isinstance(ids, torch.Tensor):
                                ids = ids.tolist()
                            cap = self.tokenizer.decode(
                                ids,
                                skip_special_tokens=self.config.skip_special_tokens,
                            )
                        except Exception:
                            cap = ""
                    else:
                        cap = ""

                    # multi-modal data (best-effort)
                    mmd = None
                    if mmd_arr is not None:
                        try:
                            if len(mmd_arr) > i:
                                mmd = mmd_arr[i]
                        except Exception:
                            mmd = None

                    questions.append(q)
                    captions.append(cap)
                    mmd_list.append(mmd)

                labels, errors = judge.judge_batch(
                    questions=questions,
                    captions=captions,
                    multi_modal_data_list=mmd_list,
                )

                # Defensive: align label length to caption batch length.
                if labels is not None and len(labels) != len(caption_batch):
                    print(
                        f"[vllm_judge] WARN judge returned {len(labels)} labels for {len(caption_batch)} captions. "
                        "Will align by truncation/padding."
                    )
                    if len(labels) < len(caption_batch):
                        labels = list(labels) + [None] * (len(caption_batch) - len(labels))
                    else:
                        labels = list(labels)[: len(caption_batch)]
        if labels is not None:
            num_zero = 0
            num_fail = 0

            for i, lb in enumerate(labels):
                if lb is None:
                    num_fail += 1
                    reward_metrics["vllm_judge"].append(-1.0)
                    continue

                try:
                    lb_int = int(lb)
                except Exception:
                    lb_int = 1
                reward_metrics["vllm_judge"].append(float(lb_int))

                if lb_int == 0:
                    num_zero += 1
                    caption_scores[i] = 0.0

            if errors:
                print(
                    f"[vllm_judge] Got {len(errors)} judge errors; keeping original caption reward for those. Showing up to {len(errors)} examples:"
                )
                for e in errors:
                    print(f"[vllm_judge]   {e}")

            reward_metrics["vllm_judge_zeroed_frac"] = [float(num_zero) / max(len(labels), 1)]
            reward_metrics["vllm_judge_fail_frac"] = [float(num_fail) / max(len(labels), 1)]

        # 3) Write final caption reward tensor and metrics (after optional gating).
        for i, (score, cur_len) in enumerate(zip(caption_scores, caption_lens)):
            if cur_len > 0:
                caption_reward_tensor[i, cur_len - 1] = float(score)
            reward_metrics["caption"].append(float(score))

        # -----------------------------
        # Caption reward variance stats
        # -----------------------------
        # For GRPO on captions, the learning signal comes from per-prompt differences
        # across captions. If the within-prompt variance is near zero, captions will
        # not receive a useful advantage signal.
        try:
            prompt_uids = caption_batch.non_tensor_batch.get(
                "prompt_uid", caption_batch.non_tensor_batch.get("uid", None)
            )
            if prompt_uids is not None:
                uid2scores = defaultdict(list)
                for uid, sc in zip(prompt_uids, reward_metrics["caption"]):
                    if uid is None:
                        continue
                    uid2scores[str(uid)].append(float(sc))

                if len(uid2scores) > 0:
                    vars_ = [float(np.var(v)) for v in uid2scores.values()]
                    stds_ = [float(np.std(v)) for v in uid2scores.values()]
                    # These will be aggregated (mean) by the trainer via reduce_metrics().
                    reward_metrics["caption_reward_var"] = vars_
                    reward_metrics["caption_reward_std"] = stds_
                    # Convenience scalars for dashboards.
                    reward_metrics["caption_reward_var_max"] = [float(np.max(vars_))]
                    reward_metrics["caption_reward_var_min"] = [float(np.min(vars_))]
                    reward_metrics["caption_reward_var_zero_frac"] = [float(np.mean(np.isclose(vars_, 0.0)))]
                else:
                    # No valid uid groups; fall back to global variance.
                    reward_metrics["caption_reward_var"] = [float(np.var(reward_metrics["caption"]))]
        except Exception:
            # Best-effort: never fail reward computation due to logging-only stats.
            pass

        # ------------------------------------------------------------
        # Select which solver trajectories are used for policy updates.
        #
        # Legacy behavior:
        #   For each prompt, keep ONLY the first solver group (the first caption's n solutions).
        #
        # New behavior (configurable):
        #   - optionally filter out degenerate groups (all-equal, e.g., all-0 or all-1),
        #   - then sample up to `solver_update_ratio` solver groups per prompt.
        # ------------------------------------------------------------
        enable_solver_update_sampling = bool(getattr(self.config, "enable_solver_update_sampling", False))
        solver_update_ratio = int(getattr(self.config, "solver_update_ratio", 1))
        enable_group_filter = bool(getattr(self.config, "enable_solver_update_group_filter", True))
        filter_key = str(getattr(self.config, "solver_update_filter_key", "accuracy"))
        enable_random = bool(getattr(self.config, "enable_solver_update_random_sampling", True))
        use_var_max = env_flag("use_var_max", default=False) or env_flag("USE_VAR_MAX", default=False)
        if not getattr(self, "_solver_update_flags_logged", False):
            print(
                "[solver_update] flags: "
                f"enabled={enable_solver_update_sampling} "
                f"ratio={solver_update_ratio} "
                f"group_filter={enable_group_filter} "
                f"filter_key={filter_key} "
                f"random_sampling={enable_random} "
                f"use_var_max={use_var_max}"
            )
            self._solver_update_flags_logged = True

        # per-prompt solve block size = caption_group_n captions * n solutions per caption
        block_size = int(caption_group_n) * int(n)
        if block_size <= 0 or (len(solve_batch) % block_size != 0):
            # Fallback to legacy assumption (caption_group_n == n) to avoid out-of-range errors.
            caption_group_n = n
            block_size = n * n

        idx: list[int] = []
        if not enable_solver_update_sampling:
            # Keep the original behavior exactly.
            for start in range(0, len(solve_batch), block_size):
                idx.extend(range(start, start + n))
        else:
            # We assume the nested order:
            #   per-prompt block size = caption_group_n captions * n solutions = caption_group_n*n.
            # within each block:
            #   caption j's solutions are contiguous at [start + j*n, start + (j+1)*n).
            if solver_update_ratio < 0:
                solver_update_ratio = 0

            # block_size is computed above (caption_group_n * n)
            for start in range(0, len(solve_batch), block_size):
                # candidate caption groups inside this prompt block
                candidate_groups = list(range(caption_group_n))  # caption index j

                group_vars = None
                if enable_group_filter or use_var_max:
                    kept_groups: list[int] = []
                    group_vars = []
                    for j in candidate_groups:
                        g0 = start + j * n
                        g1 = g0 + n
                        vals = []
                        for temp in solve_scores[g0:g1]:
                            v = temp.get(filter_key, None)
                            if v is None:
                                v = temp.get("accuracy", None)
                            if v is None:
                                v = temp.get("overall", None)
                            if v is None:
                                # If the reward function doesn't provide any usable key, don't filter.
                                vals = None
                                break
                            vals.append(float(v))

                        if vals is None:
                            kept_groups = candidate_groups
                            group_vars = None
                            break

                        var = float(np.var(vals))
                        group_vars.append((var, j))

                        if enable_group_filter:
                            # "all-0" / "all-1" generalization: all-equal group -> no variance -> no GRPO signal.
                            if not (min(vals) == max(vals)):
                                kept_groups.append(j)
                        else:
                            kept_groups.append(j)

                    if enable_group_filter:
                        candidate_groups = kept_groups

                # Keep batch size stable: always choose a fixed number of solver groups per prompt
                # (up to n). We *prefer* non-degenerate groups (after filtering), but if filtering
                # removes too many groups we will fill from the remaining (degenerate) groups.
                #
                # This prevents downstream actor updates from failing the equal-split assertion
                # (DataProto.split) when the selected solver sample count becomes non-divisible by
                # `global_batch_size_per_device`.
                desired_k = min(max(solver_update_ratio, 0), caption_group_n)
                if desired_k == 0:
                    continue

                all_groups = list(range(caption_group_n))
                kept_groups = candidate_groups  # after optional filter

                if use_var_max and group_vars is not None:
                    group_vars_sorted = sorted(group_vars, key=lambda x: x[0], reverse=True)
                    if enable_group_filter:
                        preferred = [j for _, j in group_vars_sorted if j in kept_groups]
                    else:
                        preferred = [j for _, j in group_vars_sorted]
                    chosen = preferred[:desired_k]
                    if len(chosen) < desired_k:
                        remaining = [j for _, j in group_vars_sorted if j not in chosen]
                        chosen.extend(remaining[: (desired_k - len(chosen))])
                elif enable_random:
                    chosen: list[int] = []
                    # 1) Prefer kept (non-degenerate) groups
                    if len(kept_groups) > 0:
                        k1 = min(desired_k, len(kept_groups))
                        chosen.extend(np.random.choice(kept_groups, size=k1, replace=False).tolist())
                    # 2) If not enough, fill from remaining groups (degenerate ones)
                    if len(chosen) < desired_k:
                        need = desired_k - len(chosen)
                        remaining = [g for g in all_groups if g not in chosen]
                        chosen.extend(np.random.choice(remaining, size=need, replace=False).tolist())
                else:
                    chosen = kept_groups[:desired_k]
                    if len(chosen) < desired_k:
                        remaining = [g for g in all_groups if g not in chosen]
                        chosen.extend(remaining[: (desired_k - len(chosen))])

                for j in sorted(chosen):
                    g0 = start + j * n
                    idx.extend(range(g0, g0 + n))
        
        for i in idx:
            for key, value in solve_scores[i].items():
                reward_metrics[key].append(value)

        solve_reward_tensor = torch.zeros_like(solve_batch.batch["responses"], dtype=torch.float32)
        for i, solve_score in enumerate(solve_scores):
            cur_response_length = int(solve_response_length[i].item())  # avoid tensor indexing error
            solve_reward_tensor[i, cur_response_length - 1] = solve_score["overall"]
        reward_tensor = torch.cat([caption_reward_tensor, solve_reward_tensor[idx]], dim=0)

        # mark role id for downstream (e.g., role-specific clipping)
        caption_batch.batch["role_id"] = torch.zeros(len(caption_batch), dtype=torch.long)
        solve_selected = solve_batch[idx]
        solve_selected.batch["role_id"] = torch.ones(len(solve_selected), dtype=torch.long)

        # pass caption batch size for logging / slicing
        caption_batch.meta_info["caption_batch_size"] = len(caption_batch)

        # Align non-tensor fields before concatenation: keys that exist only in caption_batch
        # (e.g., a 'split' column in val JSONL) would otherwise break DataProto.concat.
        caption_keys = set(caption_batch.non_tensor_batch.keys())
        solve_keys = set(solve_batch.non_tensor_batch.keys())
        all_keys = caption_keys | solve_keys

        def _ensure_keys(dp: DataProto) -> DataProto:
            for key in all_keys:
                if key not in dp.non_tensor_batch:
                    dp.non_tensor_batch[key] = np.array([None] * len(dp), dtype=object)
            return dp

        batch = DataProto.concat([_ensure_keys(caption_batch), _ensure_keys(solve_selected)])
        return batch, reward_tensor, reward_metrics


class AutoRewardManager(BatchFunctionRewardManagerMixin, SequentialFunctionRewardManagerMixin):
    """Reward manager for rule-based reward."""

    def __init__(self, config: RewardConfig, tokenizer: PreTrainedTokenizer):
        if config.reward_function is None:
            raise ValueError("Reward function is not provided.")

        if not os.path.exists(config.reward_function):
            raise FileNotFoundError(f"Reward function file {config.reward_function} not found.")

        spec = importlib.util.spec_from_file_location("custom_reward_fn", config.reward_function)
        module = importlib.util.module_from_spec(spec)
        try:
            sys.modules["custom_reward_fn"] = module
            spec.loader.exec_module(module)
        except Exception as e:
            raise RuntimeError(f"Failed to load reward function: {e}")

        if not hasattr(module, config.reward_function_name):
            raise AttributeError(f"Module {module} does not have function {config.reward_function_name}.")

        reward_fn = getattr(module, config.reward_function_name)
        reward_name = getattr(module, "REWARD_NAME", "unknown")
        reward_type = getattr(module, "REWARD_TYPE", "batch")
        print(f"Using reward function `{config.reward_function_name}` from `{config.reward_function}`.")
        print(f"Reward name: {reward_name}, reward type: {reward_type}.")
        self.reward_fn = partial(reward_fn, **config.reward_function_kwargs)
        self.reward_type = reward_type
        self.config = config
        self.tokenizer = tokenizer

    def compute_reward(self, caption_batch: DataProto, solve_batch: DataProto) -> Tuple[torch.Tensor, dict[str, list[float]]]:
        """Compute reward for a batch of data."""
        if self.reward_type == "batch":
            return self.compute_reward_batch(caption_batch, solve_batch)
        elif self.reward_type == "sequential":
            return self.compute_reward_sequential(caption_batch, solve_batch)
        else:
            raise ValueError(f"Unsupported reward type: {self.reward_type}.")
