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

import json
import os
import subprocess
from collections import defaultdict
from typing import Any, Optional

import ray
import torch
from omegaconf import OmegaConf

from ..protocol import DataProto
from ..single_controller.ray import RayWorkerGroup
from ..utils.tokenizer import get_processor, get_tokenizer
from ..workers.fsdp_workers import FSDPWorker
from ..workers.reward import AutoRewardManager
from .config import PPOConfig
from .data_loader import create_dataloader
from .ray_trainer import RayPPOTrainer, ResourcePoolManager, Role


def _env_positive_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if value.isdigit() and int(value) > 0:
        return int(value)
    return default


def _env_first_nonempty(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _as_positive_int(value: Any, default: int = 1) -> int:
    try:
        parsed = int(float(value))
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _split_device_list(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if text.lower() in {"", "none", "no", "false", "-1", "nodevfiles"}:
        return []
    devices: list[str] = []
    for item in text.split(","):
        item = item.strip()
        if item and item.lower() not in {"none", "no", "false", "-1", "nodevfiles"}:
            devices.append(item)
    return devices


def _normalize_text_reward_backend(raw: Optional[str]) -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "mnli": "nli",
        "nli": "nli",
        "embed": "embedding",
        "embedding": "embedding",
        "similarity": "embedding",
        "sim": "embedding",
        "bleu4": "bleu",
        "bleu": "bleu",
        "none": "none",
        "off": "none",
        "false": "none",
        "0": "none",
        "": "none",
    }
    return aliases.get(value, value)


def _is_nli_text_reward_enabled() -> bool:
    backend = _env_first_nonempty(
        "predict_text_reward_type",
        "PREDICT_TEXT_REWARD_TYPE",
        "predict_aux_reward_type",
        "PREDICT_AUX_REWARD_TYPE",
    )
    return _normalize_text_reward_backend(backend) == "nli"


def _nli_device_allows_gpu(raw: Optional[str]) -> bool:
    value = str(raw or "auto").strip().lower()
    return value in {"", "auto", "gpu"} or value.startswith("cuda")


def _detect_gpu_ids_for_sharing() -> list[str]:
    # Prefer the driver's original CUDA_VISIBLE_DEVICES when it existed. Ray may
    # clear CUDA_VISIBLE_DEVICES inside CPU-only actors, but main() preserves the
    # driver value in JANUS_ORIGINAL_CUDA_VISIBLE_DEVICES before ray.init().
    for name in ("JANUS_ORIGINAL_CUDA_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"):
        devices = _split_device_list(os.environ.get(name, ""))
        if devices:
            return devices

    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
        count = sum(1 for line in result.stdout.splitlines() if line.strip().startswith("GPU "))
        if count > 0:
            return [str(i) for i in range(count)]
    except Exception:
        pass
    return []


def _default_cpu_reward_workers(num_cpus_per_worker: Any) -> int:
    per_worker = _as_positive_int(num_cpus_per_worker, default=1)
    total_cpus = os.cpu_count() or 1
    # Each CPU reward actor loads its own NLI model, so cap the implicit default
    # to avoid excessive memory use. Users can raise/lower this with
    # REWARD_CPU_FALLBACK_ACTORS or REWARD_NUM_ACTORS.
    return max(1, min(8, total_cpus // per_worker))


def _caption_group_size(caption_batch: DataProto) -> int:
    for name in ("caption_rollout_n", "CAPTION_ROLLOUT_N"):
        raw = os.environ.get(name)
        if raw is not None:
            raw = raw.strip()
            if raw.isdigit():
                value = int(raw)
                if value > 0 and len(caption_batch) % value == 0:
                    return value

    uid_arr = caption_batch.non_tensor_batch.get("prompt_uid", caption_batch.non_tensor_batch.get("uid", None))
    if uid_arr is not None and len(uid_arr) > 0:
        first = uid_arr[0]
        run = 1
        while run < len(uid_arr) and uid_arr[run] == first:
            run += 1
        if run > 0 and len(caption_batch) % run == 0:
            return int(run)

    return 1


@ray.remote(num_cpus=1)
class ParallelRewardManager:
    """Split reward batches across several reward workers and merge results."""

    def __init__(self, config, tokenizer, worker_options: list[dict[str, Any]]):
        self.workers = []
        for options in worker_options:
            worker_cls = ray.remote(AutoRewardManager).options(**options)
            self.workers.append(worker_cls.remote(config, tokenizer))
        print(f"[reward_pool] initialized workers={len(self.workers)}")

    def compute_reward(self, caption_batch: DataProto, solve_batch: DataProto):
        if len(self.workers) <= 1 or len(caption_batch) <= 1:
            return ray.get(self.workers[0].compute_reward.remote(caption_batch, solve_batch))

        chunks = self._split_batches(caption_batch, solve_batch)
        futures = [
            self.workers[idx % len(self.workers)].compute_reward.remote(cap_part, solve_part)
            for idx, (cap_part, solve_part) in enumerate(chunks)
            if len(cap_part) > 0
        ]
        return self._merge_outputs(ray.get(futures))

    def _split_batches(self, caption_batch: DataProto, solve_batch: DataProto) -> list[tuple[DataProto, DataProto]]:
        if len(caption_batch) <= 0:
            raise ValueError("Cannot split an empty caption batch for reward computation.")
        if len(solve_batch) % len(caption_batch) != 0:
            raise ValueError(
                f"solve batch size must be a multiple of caption batch size, got {len(solve_batch)} and {len(caption_batch)}."
            )

        solver_n = len(solve_batch) // len(caption_batch)
        caption_group_n = _caption_group_size(caption_batch)
        if len(caption_batch) % caption_group_n != 0:
            raise ValueError(
                f"caption batch size {len(caption_batch)} is not divisible by caption group size {caption_group_n}."
            )

        prompt_groups = len(caption_batch) // caption_group_n
        worker_count = min(len(self.workers), prompt_groups)
        base = prompt_groups // worker_count
        extra = prompt_groups % worker_count

        chunks: list[tuple[DataProto, DataProto]] = []
        group_start = 0
        for idx in range(worker_count):
            group_count = base + (1 if idx < extra else 0)
            group_end = group_start + group_count
            cap_start = group_start * caption_group_n
            cap_end = group_end * caption_group_n
            solve_start = cap_start * solver_n
            solve_end = cap_end * solver_n
            chunks.append((caption_batch[cap_start:cap_end], solve_batch[solve_start:solve_end]))
            group_start = group_end

        return chunks

    def _merge_outputs(self, outputs):
        caption_parts: list[DataProto] = []
        solve_parts: list[DataProto] = []
        caption_rewards: list[torch.Tensor] = []
        solve_rewards: list[torch.Tensor] = []
        merged_metrics = defaultdict(list)

        for batch, reward_tensor, reward_metrics in outputs:
            caption_size = int(batch.meta_info.get("caption_batch_size", 0))
            if caption_size <= 0 and "role_id" in batch.batch:
                caption_size = int((batch.batch["role_id"] == 0).sum().item())
            caption_size = max(min(caption_size, len(batch)), 0)

            if caption_size > 0:
                caption_parts.append(batch[:caption_size])
                caption_rewards.append(reward_tensor[:caption_size])
            if caption_size < len(batch):
                solve_parts.append(batch[caption_size:])
                solve_rewards.append(reward_tensor[caption_size:])

            for key, value in reward_metrics.items():
                merged_metrics[key].extend(value)

        merged_parts = caption_parts + solve_parts
        if not merged_parts:
            raise ValueError("Parallel reward workers returned no samples.")

        merged_batch = DataProto.concat(merged_parts)
        merged_batch.meta_info["caption_batch_size"] = sum(len(part) for part in caption_parts)
        merged_reward = torch.cat(caption_rewards + solve_rewards, dim=0)
        return merged_batch, merged_reward, merged_metrics


def _build_reward_actor_options(config) -> tuple[list[dict[str, Any]], int]:
    reward_num_cpus = config.worker.reward.num_cpus
    reward_num_gpus = _as_float(config.worker.reward.num_gpus, default=0.0)
    base_options: dict[str, Any] = {"num_cpus": reward_num_cpus}
    if reward_num_gpus > 0:
        base_options["num_gpus"] = reward_num_gpus

    nli_enabled = _is_nli_text_reward_enabled()
    nli_device_raw = _env_first_nonempty("predict_nli_device", "PREDICT_NLI_DEVICE")
    nli_batch_explicit = _env_first_nonempty("predict_nli_batch_size", "PREDICT_NLI_BATCH_SIZE") is not None
    nli_threads_explicit = _env_first_nonempty("predict_nli_cpu_threads", "PREDICT_NLI_CPU_THREADS") is not None

    share_env_set = "REWARD_GPU_SHARE_DEVICE" in os.environ
    share_devices = _split_device_list(os.environ.get("REWARD_GPU_SHARE_DEVICE", ""))
    auto_shared_gpu = False
    if nli_enabled and reward_num_gpus <= 0 and not share_env_set and _nli_device_allows_gpu(nli_device_raw):
        share_devices = _detect_gpu_ids_for_sharing()
        auto_shared_gpu = bool(share_devices)

    cpu_fallback = bool(nli_enabled and reward_num_gpus <= 0 and not share_devices)
    if share_devices:
        default_workers = len(share_devices)
        mode = "shared_gpu_auto" if auto_shared_gpu else "shared_gpu"
    elif cpu_fallback:
        default_workers = _env_positive_int("REWARD_CPU_FALLBACK_ACTORS", _default_cpu_reward_workers(reward_num_cpus))
        mode = "cpu_parallel" if default_workers > 1 else "cpu"
    else:
        default_workers = 1
        mode = "single"

    num_workers = _env_positive_int("REWARD_NUM_ACTORS", default_workers)
    num_workers = max(num_workers, 1)

    worker_options = []
    for idx in range(num_workers):
        options = dict(base_options)
        env_vars = {"REWARD_WORKER_INDEX": str(idx)}

        if share_devices:
            device = share_devices[idx % len(share_devices)]
            env_vars.update(
                {
                    "CUDA_VISIBLE_DEVICES": device,
                    "REWARD_WORKER_DEVICE": device,
                }
            )
            # In shared-GPU mode each reward worker sees exactly its assigned
            # physical device as cuda:0. Force the NLI scorer to use CUDA unless
            # the user explicitly requested CPU. This also avoids bugs from
            # passing physical indexes such as cuda:7 after CUDA_VISIBLE_DEVICES
            # has remapped the local device list.
            if nli_enabled and _nli_device_allows_gpu(nli_device_raw):
                env_vars["PREDICT_NLI_DEVICE"] = "cuda"
                env_vars["predict_nli_device"] = "cuda"
            if nli_enabled and not nli_batch_explicit:
                env_vars["PREDICT_NLI_BATCH_SIZE"] = "256"
                env_vars["predict_nli_batch_size"] = "256"

        elif cpu_fallback:
            env_vars.update(
                {
                    "CUDA_VISIBLE_DEVICES": "",
                    "REWARD_WORKER_DEVICE": "",
                    "PREDICT_NLI_DEVICE": "cpu",
                    "predict_nli_device": "cpu",
                }
            )
            if not nli_batch_explicit:
                env_vars["PREDICT_NLI_BATCH_SIZE"] = "16"
                env_vars["predict_nli_batch_size"] = "16"
            if not nli_threads_explicit:
                threads = str(_as_positive_int(reward_num_cpus, default=1))
                env_vars["PREDICT_NLI_CPU_THREADS"] = threads
                env_vars["predict_nli_cpu_threads"] = threads

        options["runtime_env"] = {"env_vars": env_vars}
        worker_options.append(options)

    print(
        "[reward_pool] "
        f"mode={mode} workers={num_workers} nli={nli_enabled} "
        f"num_cpus_each={reward_num_cpus} num_gpus_each={reward_num_gpus} "
        f"share_devices={share_devices}"
    )
    return worker_options, num_workers


def _create_reward_manager(config, tokenizer):
    worker_options, num_workers = _build_reward_actor_options(config)
    if num_workers <= 1:
        RemoteRewardManager = ray.remote(AutoRewardManager).options(**worker_options[0])
        return RemoteRewardManager.remote(config.worker.reward, tokenizer)

    return ParallelRewardManager.remote(config.worker.reward, tokenizer, worker_options)


# please make sure main_task is not scheduled on head
@ray.remote(num_cpus=1)
class Runner:
    """A runner for RL training."""

    def run(self, config: PPOConfig):
        # print config
        print(json.dumps(config.to_dict(), indent=2))

        # instantiate tokenizer
        tokenizer = get_tokenizer(
            config.worker.actor.model.model_path,
            override_chat_template=config.data.override_chat_template,
            trust_remote_code=config.worker.actor.model.trust_remote_code,
            use_fast=True,
        )
        processor = get_processor(
            config.worker.actor.model.model_path,
            override_chat_template=config.data.override_chat_template,
            trust_remote_code=config.worker.actor.model.trust_remote_code,
            use_fast=True,
        )

        # define worker classes
        ray_worker_group_cls = RayWorkerGroup
        role_worker_mapping = {
            Role.ActorRolloutRef: ray.remote(FSDPWorker),
            Role.Critic: ray.remote(FSDPWorker),
        }
        global_pool_id = "global_pool"
        resource_pool_spec = {
            global_pool_id: [config.trainer.n_gpus_per_node] * config.trainer.nnodes,
        }
        mapping = {
            Role.ActorRolloutRef: global_pool_id,
            Role.Critic: global_pool_id,
        }
        resource_pool_manager = ResourcePoolManager(resource_pool_spec=resource_pool_spec, mapping=mapping)

        reward_fn = _create_reward_manager(config, tokenizer)
        val_reward_fn = reward_fn

        train_dataloader, val_dataloader = create_dataloader(config.data, tokenizer, processor)

        trainer = RayPPOTrainer(
            config=config,
            tokenizer=tokenizer,
            processor=processor,
            train_dataloader=train_dataloader,
            val_dataloader=val_dataloader,
            role_worker_mapping=role_worker_mapping,
            resource_pool_manager=resource_pool_manager,
            ray_worker_group_cls=ray_worker_group_cls,
            reward_fn=reward_fn,
            val_reward_fn=val_reward_fn,
        )
        trainer.init_workers()
        trainer.fit()


def main():
    cli_args = OmegaConf.from_cli()
    default_config = OmegaConf.structured(PPOConfig())

    if hasattr(cli_args, "config"):
        config_path = cli_args.pop("config", None)
        file_config = OmegaConf.load(config_path)
        default_config = OmegaConf.merge(default_config, file_config)

    ppo_config = OmegaConf.merge(default_config, cli_args)
    ppo_config: PPOConfig = OmegaConf.to_object(ppo_config)
    ppo_config.deep_post_init()

    if not ray.is_initialized():
        runtime_env = {
            "env_vars": {
                "TOKENIZERS_PARALLELISM": "true",
                "NCCL_DEBUG": "WARN",
                "VLLM_LOGGING_LEVEL": "WARN",
                "TORCH_NCCL_AVOID_RECORD_STREAMS": "1",
                "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:False",
                "CUDA_DEVICE_MAX_CONNECTIONS": "1",
                "VLLM_ALLREDUCE_USE_SYMM_MEM": "0",
                "JANUS_ORIGINAL_CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            }
        }
        ray.init(runtime_env=runtime_env)

    runner = Runner.remote()
    ray.get(runner.run.remote(ppo_config))

    if ppo_config.trainer.ray_timeline is not None:
        # use `export RAY_PROFILING=1` to record the ray timeline
        ray.timeline(filename=ppo_config.trainer.ray_timeline)


if __name__ == "__main__":
    main()
