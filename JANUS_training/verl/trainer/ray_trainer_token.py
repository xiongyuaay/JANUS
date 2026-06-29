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
"""
PPO Trainer with Ray-based single controller.
This trainer supports model-agonistic model initialization with huggingface.
"""

from io import BytesIO
import json
import math
import os
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any, Optional, Type, Union
from jinja2 import Template

import numpy as np
import ray
import torch
from ray.experimental.tqdm_ray import tqdm
from torchdata.stateful_dataloader import StatefulDataLoader
from transformers import PreTrainedTokenizer, ProcessorMixin
from PIL import Image
from PIL.Image import Image as ImageObject

from ..protocol import DataProto, pad_dataproto_to_divisor, unpad_dataproto
from ..single_controller.base import Worker
from ..single_controller.ray import RayClassWithInitArgs, RayResourcePool, RayWorkerGroup
from ..single_controller.ray.base import create_colocated_worker_cls
from ..utils import torch_functional as VF
from ..utils.checkpoint import CHECKPOINT_TRACKER, find_latest_ckpt, remove_obsolete_ckpt
from ..utils.logger import Tracker
from ..utils.py_functional import convert_dict_to_str, timer, unflatten_dict
from ..utils.seqlen_balancing import get_seqlen_balanced_partitions, log_seqlen_unbalance
from ..workers.fsdp_workers import FSDPWorker
from ..workers.reward import AutoRewardManager
from .config import PPOConfig
from .core_algos import (
    AdvantageEstimator,
    FixedKLController,
    KLController,
    compute_advantage_return,
    compute_kl,
    get_kl_controller,
)
from .metrics import (
    compute_data_metrics,
    compute_length_metrics,
    compute_throughout_metrics,
    compute_timing_metrics,
    reduce_metrics,
)


class Role(IntEnum):
    """
    To create more roles dynamically, you can subclass Role and add new members
    """

    Actor = auto()
    Rollout = auto()
    ActorRollout = auto()
    Critic = auto()
    RefPolicy = auto()
    RewardModel = auto()
    ActorRolloutRef = auto()


@dataclass
class ResourcePoolManager:
    """
    Define a resource pool specification. Resource pool will be initialized first.
    """

    resource_pool_spec: dict[str, list[int]]
    mapping: dict[Role, str]
    resource_pool_dict: dict[str, RayResourcePool] = field(default_factory=dict)

    def create_resource_pool(self):
        """Create ray resource pools for distributed training."""
        for resource_pool_name, process_on_nodes in self.resource_pool_spec.items():
            # max_colocate_count means the number of WorkerGroups (i.e. processes) in each RayResourcePool
            # For FSDP backend, we recommend using max_colocate_count=1 that merge all WorkerGroups into one.
            # For Megatron backend, we recommend using max_colocate_count>1 that can utilize different WorkerGroup for different models
            resource_pool = RayResourcePool(
                process_on_nodes=process_on_nodes, use_gpu=True, max_colocate_count=1, name_prefix=resource_pool_name
            )
            self.resource_pool_dict[resource_pool_name] = resource_pool

        self._check_resource_available()

    def get_resource_pool(self, role: Role) -> RayResourcePool:
        """Get the resource pool of the worker."""
        return self.resource_pool_dict[self.mapping[role]]

    def get_num_gpus(self) -> int:
        """Get the number of gpus in this cluster."""
        return sum([n_gpus for process_on_nodes in self.resource_pool_spec.values() for n_gpus in process_on_nodes])

    def _check_resource_available(self):
        """Check if the resource pool can be satisfied in this ray cluster."""
        gpus_available = ray.available_resources().get("GPU", 0)
        gpus_required = self.get_num_gpus()
        if gpus_available < gpus_required:
            raise ValueError(f"Total available GPUs {gpus_available} is less than total desired GPUs {gpus_required}.")


def apply_kl_penalty(data: DataProto, kl_ctrl: KLController, kl_penalty="kl"):
    """Apply KL penalty to the token-level rewards."""
    token_level_scores = data.batch["token_level_scores"]
    batch_size = data.batch.batch_size[0]
    response_mask = data.batch["response_mask"]

    # compute kl between ref_policy and current policy
    kld = compute_kl(data.batch["old_log_probs"], data.batch["ref_log_probs"], kl_penalty=kl_penalty)
    kld = kld * response_mask  # (batch_size, response_length)

    data.batch["token_level_rewards"] = token_level_scores - kl_ctrl.kl_coef * kld

    current_kl = torch.mean(VF.masked_mean(kld, mask=response_mask, dim=-1)).item()
    metrics = {"actor/kl_penalty": current_kl, "actor/kl_coef": kl_ctrl.kl_coef}

    # According to https://github.com/huggingface/trl/blob/v0.11.0/trl/trainer/ppo_trainer.py#L880
    kl_ctrl.update(current_kl=current_kl, n_steps=batch_size)
    return data, metrics


def compute_advantage(data: DataProto, adv_estimator: AdvantageEstimator, gamma: float = 1.0, lam: float = 1.0):
    """Compute advantage estimates for policy optimization."""
    adv_inputs = {
        "token_level_rewards": data.batch["token_level_rewards"],
        "response_mask": data.batch["response_mask"],
        "index": data.non_tensor_batch["uid"],
        "gamma": gamma,
        "lam": lam,
    }
    if "values" in data.batch:
        adv_inputs["values"] = data.batch["values"]

    if "reward_baselines" in data.batch:
        adv_inputs["reward_baselines"] = data.batch["reward_baselines"]

    advantages, returns = compute_advantage_return(adv_estimator, **adv_inputs)
    data.batch["advantages"] = advantages
    data.batch["returns"] = returns
    return data


def process_image(
    image: Union[dict[str, Any], ImageObject, str], min_pixels: Optional[int], max_pixels: Optional[int]
) -> ImageObject:
    if isinstance(image, str):
        image = Image.open(image)
    elif isinstance(image, dict):
        image = Image.open(BytesIO(image["bytes"]))
    elif isinstance(image, bytes):
        image = Image.open(BytesIO(image))

    image.load()  # avoid "Too many open files" errors
    if max_pixels is not None and (image.width * image.height) > max_pixels:
        resize_factor = math.sqrt(max_pixels / (image.width * image.height))
        width, height = int(image.width * resize_factor), int(image.height * resize_factor)
        image = image.resize((width, height))

    if min_pixels is not None and (image.width * image.height) < min_pixels:
        resize_factor = math.sqrt(min_pixels / (image.width * image.height))
        width, height = int(image.width * resize_factor), int(image.height * resize_factor)
        image = image.resize((width, height))

    # NOTE:
    # Some datasets (e.g. MMK12/ViRL conversions) contain PNGs whose visible content is stored in
    # the alpha channel ("alpha-only RGBA"): RGB channels are (near) all zeros while alpha varies.
    # A naive `image.convert("RGB")` drops the alpha channel and turns these images into pure black,
    # which later gets described by the model as a "black/blank rectangle".
    #
    # Fix: if the image has transparency, composite it over an opaque background (white by default)
    # before converting to RGB.
    if image.mode != "RGB":
        has_transparency = (
            image.mode in {"RGBA", "LA"}
            or (image.mode == "P" and "transparency" in image.info)
            or (image.mode == "RGBa")
        )
        if has_transparency:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            image = image.convert("RGB")

    return image


class RayPPOTrainer:
    """
    Note that this trainer runs on the driver process on a single CPU/GPU node.
    """

    def __init__(
        self,
        config: PPOConfig,
        tokenizer: PreTrainedTokenizer,
        processor: Optional[ProcessorMixin],
        train_dataloader: StatefulDataLoader,
        val_dataloader: StatefulDataLoader,
        role_worker_mapping: dict[Role, Type[Worker]],
        resource_pool_manager: ResourcePoolManager,
        ray_worker_group_cls: Type[RayWorkerGroup] = RayWorkerGroup,
        reward_fn: Optional[AutoRewardManager] = None,
        val_reward_fn: Optional[AutoRewardManager] = None,
    ):
        self.tokenizer = tokenizer
        self.processor = processor
        self.train_dataloader = train_dataloader
        self.val_dataloader = val_dataloader
        self.config = config
        self.reward_fn = reward_fn
        self.val_reward_fn = val_reward_fn

        self.val_reward_score = 0.0
        self.best_val_reward_score = -1.0
        self.best_global_step = None

        self.hybrid_engine = config.worker.hybrid_engine
        self.role_worker_mapping = role_worker_mapping
        self.resource_pool_manager = resource_pool_manager
        self.use_reward_model = Role.RewardModel in role_worker_mapping
        self.ray_worker_group_cls = ray_worker_group_cls

        # define KL control
        if config.algorithm.disable_kl:
            self.use_reference_policy = False
            self.kl_ctrl = FixedKLController(init_kl_coef=0.0)
            print("KL is disabled, no KL metrics will be logged. Please set `kl_coef=0` to log KL metrics.")
        else:
            self.use_reference_policy = True
            self.kl_ctrl = get_kl_controller(config.algorithm)

        if config.algorithm.adv_estimator == AdvantageEstimator.GAE:
            self.use_critic = True
        else:
            self.use_critic = False

        if config.algorithm.adv_estimator not in list(AdvantageEstimator):
            raise NotImplementedError(f"Unknown advantage estimator: {config.algorithm.adv_estimator}.")

        if config.data.rollout_batch_size % config.worker.actor.global_batch_size != 0:
            raise ValueError("Rollout batch size must be divisible by actor global batch size.")

        if (
            config.data.rollout_batch_size * config.worker.rollout.n
        ) % config.worker.actor.micro_batch_size_per_device_for_experience != 0:
            raise ValueError(
                "Rollout batch size * rollout.n must be divisible by actor micro batch size for experience."
            )

        if self.use_critic:
            if config.data.rollout_batch_size % config.worker.critic.global_batch_size != 0:
                raise ValueError("Rollout batch size must be divisible by critic global batch size.")

            if (
                config.data.rollout_batch_size * config.worker.rollout.n
            ) % config.worker.critic.micro_batch_size_per_device_for_experience != 0:
                raise ValueError(
                    "Rollout batch size * rollout.n must be divisible by critic micro batch size for experience."
                )

        if (
            config.algorithm.adv_estimator in (AdvantageEstimator.GRPO, AdvantageEstimator.RLOO)
            and config.worker.rollout.n == 1
        ):
            raise ValueError("GRPO and RLOO algorithm need `config.worker.rollout.n > 1`.")

        if config.trainer.max_steps is not None:
            self.training_steps = config.trainer.max_steps
        elif config.data.mini_rollout_batch_size is not None:
            num_examples = len(train_dataloader) * config.data.mini_rollout_batch_size
            self.training_steps = num_examples // config.data.rollout_batch_size * config.trainer.total_epochs
        else:
            self.training_steps = len(train_dataloader) * config.trainer.total_epochs

        config.worker.actor.optim.training_steps = self.training_steps
        config.worker.critic.optim.training_steps = self.training_steps
        print(f"Total training steps: {self.training_steps}")

    def init_workers(self) -> None:
        """Init resource pool and worker group"""
        self.resource_pool_manager.create_resource_pool()
        self.resource_pool_to_cls = {pool: {} for pool in self.resource_pool_manager.resource_pool_dict.values()}

        # create actor, rollout and ref
        if self.hybrid_engine:
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.ActorRolloutRef)
            actor_rollout_ref_cls = RayClassWithInitArgs(
                cls=self.role_worker_mapping[Role.ActorRolloutRef], config=self.config.worker, role="actor_rollout_ref"
            )
            self.resource_pool_to_cls[resource_pool]["actor_rollout_ref"] = actor_rollout_ref_cls
        else:
            raise NotImplementedError

        # create critic
        if self.use_critic:
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.Critic)
            critic_cls = RayClassWithInitArgs(
                cls=self.role_worker_mapping[Role.Critic], config=self.config.worker, role="critic"
            )
            self.resource_pool_to_cls[resource_pool]["critic"] = critic_cls

        # create a reward model if reward_fn is None
        if self.use_reward_model:
            # we create a RM here
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.RewardModel)
            rm_cls = RayClassWithInitArgs(
                cls=self.role_worker_mapping[Role.RewardModel], config=self.config.worker, role="reward"
            )
            self.resource_pool_to_cls[resource_pool]["rm"] = rm_cls

        # initialize WorkerGroup
        # NOTE: if you want to use a different resource pool for each role, which can support different parallel size,
        # you should not use `create_colocated_worker_cls`. Instead, directly pass different resource pool to different worker groups.
        # See https://github.com/volcengine/verl/blob/master/examples/ray/tutorial.ipynb for more information.
        all_wg: dict[str, FSDPWorker] = {}
        self.wg_dicts = []
        for resource_pool, class_dict in self.resource_pool_to_cls.items():
            worker_dict_cls = create_colocated_worker_cls(class_dict=class_dict)
            wg_dict = self.ray_worker_group_cls(resource_pool=resource_pool, ray_cls_with_init=worker_dict_cls)
            spawn_wg = wg_dict.spawn(prefix_set=class_dict.keys())
            all_wg.update(spawn_wg)
            # keep the referece of WorkerDict to support ray >= 2.31. Ref: https://github.com/ray-project/ray/pull/45699
            self.wg_dicts.append(wg_dict)

        if self.use_critic:
            self.critic_wg = all_wg["critic"]
            self.critic_wg.init_model()

        if self.use_reward_model:
            self.rm_wg = all_wg["rm"]
            self.rm_wg.init_model()

        # we should create rollout at the end so that vllm can have a better estimation of kv cache memory
        self.actor_rollout_ref_wg = all_wg["actor_rollout_ref"]
        self.actor_rollout_ref_wg.init_model()

    def _save_checkpoint(self) -> None:
        # path: {save_checkpoint_path}/global_step_{global_step}/{actor,critic}
        if self.val_reward_score > self.best_val_reward_score:
            self.best_val_reward_score = self.val_reward_score
            self.best_global_step = self.global_step

        remove_obsolete_ckpt(
            self.config.trainer.save_checkpoint_path,
            self.global_step,
            self.best_global_step,
            self.config.trainer.save_limit,
        )
        folder_path = os.path.join(self.config.trainer.save_checkpoint_path, f"global_step_{self.global_step}")
        actor_path = os.path.join(folder_path, "actor")
        self.actor_rollout_ref_wg.save_checkpoint(actor_path, save_model_only=self.config.trainer.save_model_only)

        if self.use_critic:
            critic_path = os.path.join(folder_path, "critic")
            self.critic_wg.save_checkpoint(critic_path, save_model_only=self.config.trainer.save_model_only)

        dataloader_path = os.path.join(folder_path, "dataloader.pt")
        dataloader_state_dict = self.train_dataloader.state_dict()
        torch.save(dataloader_state_dict, dataloader_path)

        checkpointer_tracker_info = {
            "best_global_step": self.best_global_step,
            "best_val_reward_score": round(self.best_val_reward_score, 4),
            "last_global_step": self.global_step,
            "last_actor_path": os.path.abspath(actor_path),
        }
        checkpointer_tracker_path = os.path.join(self.config.trainer.save_checkpoint_path, CHECKPOINT_TRACKER)
        with open(checkpointer_tracker_path, "w") as f:
            json.dump(checkpointer_tracker_info, f, ensure_ascii=False, indent=2)

    def _load_checkpoint(self) -> None:
        if self.config.trainer.load_checkpoint_path is not None:
            load_checkpoint_path = self.config.trainer.load_checkpoint_path
        elif self.config.trainer.find_last_checkpoint:
            load_checkpoint_path, tracker_info = find_latest_ckpt(self.config.trainer.save_checkpoint_path)
            if tracker_info is not None:
                self.best_val_reward_score = tracker_info.get("best_val_reward_score", 0.0)
                self.best_global_step = tracker_info.get("best_global_step", 0)
        else:
            load_checkpoint_path = None

        if load_checkpoint_path is None:
            return

        if "global_step_" not in load_checkpoint_path.strip(os.path.sep).split(os.path.sep)[-1]:
            raise ValueError("`load_checkpoint_path` should end with `global_step_*`.")

        print(f"Load from checkpoint: {load_checkpoint_path}.")
        self.global_step = int(load_checkpoint_path.strip(os.path.sep).split("global_step_")[-1])
        actor_path = os.path.join(load_checkpoint_path, "actor")
        self.actor_rollout_ref_wg.load_checkpoint(actor_path)
        if self.use_critic:
            critic_path = os.path.join(load_checkpoint_path, "critic")
            self.critic_wg.load_checkpoint(critic_path)

        dataloader_path = os.path.join(load_checkpoint_path, "dataloader.pt")
        if os.path.exists(dataloader_path):
            dataloader_state_dict = torch.load(dataloader_path, weights_only=False)
            self.train_dataloader.load_state_dict(dataloader_state_dict)
        else:
            print(f"No dataloader state found at {dataloader_path}, will start from scratch.")

    def _maybe_log_val_generations(
        self,
        inputs: list[str],
        outputs: list[str],
        labels: list[str],
        scores: list[float],
        role_ids: Optional[list[int]] = None,
        prompt_uids: Optional[list[Any]] = None,
    ) -> None:
        """Log validation generations.

        NOTE: In self-play (caption -> solver) validation, the reward manager returns a mixed batch that
        contains both caption (Observer) and solve (Solver) trajectories. Previously we truncated by
        `trainer.val_generations_to_log`, which caused generations.log to only include captions.

        We now interpret `trainer.val_generations_to_log` as the number of *prompt examples* to log,
        and for each example we log one Observer generation and one Solver generation.
        """
        if self.config.trainer.val_generations_to_log <= 0:
            return

        n = int(self.config.trainer.val_generations_to_log)
        samples = list(zip(inputs, outputs, labels, scores))

        # Legacy fallback: if role metadata is missing, keep old behaviour.
        if role_ids is None or len(role_ids) != len(samples):
            self.logger.log_generation(samples[:n], self.global_step)
            return

        caption_indices = [i for i, r in enumerate(role_ids) if int(r) == 0]
        solver_indices = [i for i, r in enumerate(role_ids) if int(r) == 1]

        if len(caption_indices) == 0 or len(solver_indices) == 0:
            self.logger.log_generation(samples[:n], self.global_step)
            return

        paired: list[tuple[str, str, str, float]] = []

        # Prefer pairing by prompt_uid (more robust when data is shuffled).
        if prompt_uids is not None and len(prompt_uids) == len(samples):
            solver_by_uid = defaultdict(list)
            for idx in solver_indices:
                solver_by_uid[prompt_uids[idx]].append(idx)

            seen = set()
            for cap_idx in caption_indices:
                uid = prompt_uids[cap_idx]
                if uid in seen:
                    continue
                seen.add(uid)
                sol_list = solver_by_uid.get(uid, [])
                if len(sol_list) == 0:
                    continue
                sol_idx = sol_list[0]

                cap_inp, cap_out, cap_lab, cap_score = samples[cap_idx]
                sol_inp, sol_out, sol_lab, sol_score = samples[sol_idx]

                paired.append((f"[Observer]\n{cap_inp}", cap_out, cap_lab, float(cap_score)))
                paired.append((f"[Solver]\n{sol_inp}", sol_out, sol_lab, float(sol_score)))

                if len(paired) >= 2 * n:
                    break
        else:
            for cap_idx, sol_idx in zip(caption_indices, solver_indices):
                cap_inp, cap_out, cap_lab, cap_score = samples[cap_idx]
                sol_inp, sol_out, sol_lab, sol_score = samples[sol_idx]

                paired.append((f"[Observer]\n{cap_inp}", cap_out, cap_lab, float(cap_score)))
                paired.append((f"[Solver]\n{sol_inp}", sol_out, sol_lab, float(sol_score)))

                if len(paired) >= 2 * n:
                    break

        if len(paired) == 0:
            self.logger.log_generation(samples[:n], self.global_step)
            return

        self.logger.log_generation(paired, self.global_step)

    def _validate(self) -> dict[str, Any]:
        reward_tensor_lst = []
        # Lists to collect samples for the table
        sample_inputs, sample_outputs, sample_labels, sample_scores = [], [], [], []
        sample_role_ids, sample_prompt_uids = [], []
        reward_metrics_lst = defaultdict(list)
        length_metrics_lst = defaultdict(list)
        print("Start validation...")
        self.actor_rollout_ref_wg.prepare_rollout_engine()
        for batch_dict in self.val_dataloader:
            caption_test_batch = DataProto.from_single_dict(batch_dict)
            # Add stable ids for grouping / logging (best-effort).
            if 'uid' not in caption_test_batch.non_tensor_batch:
                caption_test_batch.non_tensor_batch['uid'] = np.array(
                    [str(uuid.uuid4()) for _ in range(len(caption_test_batch))], dtype=object
                )
            if 'prompt_uid' not in caption_test_batch.non_tensor_batch:
                caption_test_batch.non_tensor_batch['prompt_uid'] = caption_test_batch.non_tensor_batch['uid']
            test_gen_batch = caption_test_batch.pop(
                batch_keys=["input_ids", "attention_mask", "position_ids"],
                non_tensor_batch_keys=["raw_prompt_ids", "multi_modal_data"],
            )
            repeat_times = self.config.worker.rollout.val_override_config.get("n", 1)
            test_gen_batch.meta_info = self.config.worker.rollout.val_override_config
            test_gen_batch.meta_info["min_pixels"] = self.config.data.min_pixels
            test_gen_batch.meta_info["max_pixels"] = self.config.data.max_pixels
            test_gen_batch.meta_info["video_fps"] = self.config.data.video_fps

            test_gen_batch, pad_size = pad_dataproto_to_divisor(test_gen_batch, self.actor_rollout_ref_wg.world_size)
            test_output_gen_batch = self.actor_rollout_ref_wg.generate_sequences(test_gen_batch)
            test_output_gen_batch = unpad_dataproto(test_output_gen_batch, pad_size=pad_size * repeat_times)

            # repeat to align with repeated responses in rollout
            caption_test_batch = caption_test_batch.repeat(repeat_times=repeat_times, interleave=True)
            caption_test_batch = caption_test_batch.union(test_output_gen_batch)

            prompt_uid_arr = caption_test_batch.non_tensor_batch.get(
                'prompt_uid', caption_test_batch.non_tensor_batch.get('uid', None)
            )

            tensors = defaultdict(list)
            non_tensors = defaultdict(list)
            for i in range(len(caption_test_batch)):
                problem = caption_test_batch.non_tensor_batch[self.config.data.prompt_key][i]
                caption = self.tokenizer.decode(caption_test_batch.batch['responses'][i], skip_special_tokens=True)
                caption = caption.replace("<image>", "[image]")
                with open(self.config.data.solve_prompt, encoding="utf-8") as f:
                    solve_prompt = Template(f.read().strip()).render(content=problem)
                solve_prompt = "<image>" + solve_prompt.replace("<image>", "")
                solve_prompt = solve_prompt.replace('<caption>', caption)

                content_list = []
                for j, content in enumerate(solve_prompt.split("<image>")):
                    if j != 0:
                        content_list.append({"type": "image"})

                    if content:
                        content_list.append({"type": "text", "text": content})

                messages = [{"role": "user", "content": content_list}]

                solve_prompt = self.processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
                processed_images = caption_test_batch.non_tensor_batch['multi_modal_data'][i]['processed_images']
                model_inputs = self.processor(processed_images, [solve_prompt], add_special_tokens=False, return_tensors="pt")
                input_ids = model_inputs.pop("input_ids")[0]
                attention_mask = model_inputs.pop("attention_mask")[0]

                if self.processor is not None and "Qwen2VLImageProcessor" in self.processor.image_processor.__class__.__name__:
                    # qwen-vl mrope
                    if "Qwen3VLProcessor" in self.processor.__class__.__name__:
                        from ..models.transformers.qwen3_vl import get_rope_index
                    else:
                        from ..models.transformers.qwen2_vl import get_rope_index

                    vision_position_ids = get_rope_index(
                        self.processor,
                        input_ids=input_ids,
                        image_grid_thw=model_inputs.get("image_grid_thw", None),
                        video_grid_thw=model_inputs.get("video_grid_thw", None),
                        second_per_grid_ts=model_inputs.get("second_per_grid_ts", None),
                        attention_mask=attention_mask,
                    )  # (3, seq_length)
                    text_position_ids = torch.arange(len(input_ids)).unsqueeze(0)  # (1, seq_length)
                    position_ids = torch.cat((text_position_ids, vision_position_ids), dim=0)  # (4, seq_length)
                else:
                    position_ids = torch.clip(attention_mask.cumsum(dim=0) - 1, min=0, max=None)  # (seq_length,)

                input_ids, attention_mask, position_ids = VF.postprocess_data(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    max_length=self.config.data.max_prompt_length,
                    pad_token_id=self.tokenizer.pad_token_id,
                    left_pad=True,
                    truncation=self.config.data.truncation,
                )
                raw_prompt_ids = self.tokenizer.encode(solve_prompt, add_special_tokens=False)
                if len(raw_prompt_ids) > self.config.data.max_prompt_length:
                    if self.config.data.truncation == "left":
                        raw_prompt_ids = raw_prompt_ids[-self.config.data.max_prompt_length :]
                    elif self.config.data.truncation == "right":
                        raw_prompt_ids = raw_prompt_ids[: self.config.data.max_prompt_length]
                    elif self.config.data.truncation == "error":
                        raise RuntimeError(f"Prompt length {len(raw_prompt_ids)} is longer than {self.config.data.max_prompt_length}.")
                tensors["input_ids"].append(input_ids)
                tensors["attention_mask"].append(attention_mask)
                tensors["position_ids"].append(position_ids)
                non_tensors["raw_prompt_ids"].append(raw_prompt_ids)
                non_tensors["multi_modal_data"].append(caption_test_batch.non_tensor_batch['multi_modal_data'][i])
                non_tensors["problem"].append(problem)
                non_tensors["ground_truth"].append(caption_test_batch.non_tensor_batch['ground_truth'][i])
                non_tensors["prompt_uid"].append(prompt_uid_arr[i] if prompt_uid_arr is not None else None)
            for key, value in tensors.items():
                tensors[key] = torch.stack(value, dim=0)
            for key, value in non_tensors.items():
                non_tensors[key] = np.array(value, dtype=object)
            solve_batch_dict = {**tensors, **non_tensors}
            solve_test_batch: DataProto = DataProto.from_single_dict(solve_batch_dict)
            test_gen_batch = solve_test_batch.pop(
                batch_keys=["input_ids", "attention_mask", "position_ids"],
                non_tensor_batch_keys=["raw_prompt_ids", "multi_modal_data"],
            )
            repeat_times = self.config.worker.rollout.val_override_config.get("n", 1)
            test_gen_batch.meta_info = self.config.worker.rollout.val_override_config
            test_gen_batch.meta_info["min_pixels"] = self.config.data.min_pixels
            test_gen_batch.meta_info["max_pixels"] = self.config.data.max_pixels
            test_gen_batch.meta_info["video_fps"] = self.config.data.video_fps

            test_gen_batch, pad_size = pad_dataproto_to_divisor(test_gen_batch, self.actor_rollout_ref_wg.world_size)
            test_output_gen_batch = self.actor_rollout_ref_wg.generate_sequences(test_gen_batch)
            test_output_gen_batch = unpad_dataproto(test_output_gen_batch, pad_size=pad_size * repeat_times)

            # repeat to align with repeated responses in rollout
            solve_test_batch = solve_test_batch.repeat(repeat_times=repeat_times, interleave=True)
            solve_test_batch = solve_test_batch.union(test_output_gen_batch)

            # evaluate using reward_function
            test_batch, reward_tensor, reward_metrics = ray.get(self.val_reward_fn.compute_reward.remote(caption_test_batch, solve_test_batch))

            # store generations
            input_ids = test_batch.batch["prompts"]
            input_texts = [self.tokenizer.decode(ids, skip_special_tokens=True) for ids in input_ids]
            output_ids = test_batch.batch["responses"]
            output_texts = [self.tokenizer.decode(ids, skip_special_tokens=True) for ids in output_ids]
            scores = reward_tensor.sum(-1).cpu().tolist()
            sample_inputs.extend(input_texts)
            sample_outputs.extend(output_texts)
            sample_labels.extend(test_batch.non_tensor_batch["ground_truth"].tolist())
            sample_scores.extend(scores)
            # Track role / prompt ids for paired generation logging.
            if 'role_id' in test_batch.batch:
                sample_role_ids.extend(test_batch.batch['role_id'].detach().cpu().tolist())
            if 'prompt_uid' in test_batch.non_tensor_batch:
                sample_prompt_uids.extend(test_batch.non_tensor_batch['prompt_uid'].tolist())

            reward_tensor_lst.append(reward_tensor)
            for key, value in reward_metrics.items():
                reward_metrics_lst[key].extend(value)

            for key, value in compute_length_metrics(test_batch).items():
                length_metrics_lst[key].append(value)

        self.actor_rollout_ref_wg.release_rollout_engine()
        self._maybe_log_val_generations(
            sample_inputs,
            sample_outputs,
            sample_labels,
            sample_scores,
            role_ids=sample_role_ids if len(sample_role_ids) == len(sample_inputs) else None,
            prompt_uids=sample_prompt_uids if len(sample_prompt_uids) == len(sample_inputs) else None,
        )
        self.val_reward_score = torch.cat(reward_tensor_lst, dim=0).sum(-1).mean().item()
        val_reward_metrics = {f"val/{key}_reward": value for key, value in reduce_metrics(reward_metrics_lst).items()}
        val_length_metrics = {f"val_{key}": value for key, value in reduce_metrics(length_metrics_lst).items()}
        print("Finish validation.")
        return {**val_reward_metrics, **val_length_metrics}

    def _balance_batch(self, batch: DataProto, metrics: dict[str, Any], logging_prefix: str = "global_seqlen") -> None:
        """Reorder the data on single controller such that each dp rank gets similar total tokens"""
        attention_mask = batch.batch["attention_mask"]
        batch_size = attention_mask.shape[0]
        global_seqlen_lst = batch.batch["attention_mask"].view(batch_size, -1).sum(-1).tolist()  # (train_batch_size,)
        world_size = self.actor_rollout_ref_wg.world_size
        global_partition_lst = get_seqlen_balanced_partitions(
            global_seqlen_lst, k_partitions=world_size, equal_size=True
        )
        # reorder based on index. The data will be automatically equally partitioned by dispatch function
        global_idx = torch.tensor([j for partition in global_partition_lst for j in partition])
        batch.reorder(global_idx)
        global_balance_stats = log_seqlen_unbalance(
            seqlen_list=global_seqlen_lst, partitions=global_partition_lst, prefix=logging_prefix
        )
        metrics.update(global_balance_stats)

    def _make_batch_data(self, metrics: dict[str, Any]) -> DataProto:
        try:
            batch_dict = next(self.data_iterator)
        except StopIteration:
            self.data_iterator = iter(self.train_dataloader)
            batch_dict = next(self.data_iterator)

        meta_info = {
            "min_pixels": self.config.data.min_pixels,
            "max_pixels": self.config.data.max_pixels,
            "video_fps": self.config.data.video_fps,
        }

        print("Start generating caption...")
        caption_batch: DataProto = DataProto.from_single_dict(batch_dict, meta_info=meta_info)
        caption_batch.non_tensor_batch["uid"] = np.array(
            [str(uuid.uuid4()) for _ in range(len(caption_batch.batch))], dtype=object
        )

        # pop those keys for generation
        gen_batch = caption_batch.pop(
            batch_keys=["input_ids", "attention_mask", "position_ids"],
            non_tensor_batch_keys=["raw_prompt_ids", "multi_modal_data"],
            meta_info_keys=["min_pixels", "max_pixels", "video_fps"],
        )

        # generate a batch
        gen_batch_output = self.actor_rollout_ref_wg.generate_sequences(gen_batch)

        # repeat to align with repeated responses in rollout
        caption_batch = caption_batch.repeat(repeat_times=self.config.worker.rollout.n, interleave=True)
        caption_batch = caption_batch.union(gen_batch_output)
        print(f"Finish generating.")
        
        print("Start generating solution...")
        tensors = defaultdict(list)
        non_tensors = defaultdict(list)
        for i in range(len(caption_batch)):
            problem = caption_batch.non_tensor_batch[self.config.data.prompt_key][i]
            caption = self.tokenizer.decode(caption_batch.batch['responses'][i], skip_special_tokens=True)
            caption = caption.replace("<image>", "[image]")
            with open(self.config.data.solve_prompt, encoding="utf-8") as f:
                solve_prompt = Template(f.read().strip()).render(content=problem)
            solve_prompt = "<image>" + solve_prompt.replace("<image>", "")
            solve_prompt = solve_prompt.replace('<caption>', caption)
            solve_prompt_raw = solve_prompt

            content_list = []
            for j, content in enumerate(solve_prompt.split("<image>")):
                if j != 0:
                    content_list.append({"type": "image"})

                if content:
                    content_list.append({"type": "text", "text": content})

            messages = [{"role": "user", "content": content_list}]

            solve_prompt = self.processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            processed_images = caption_batch.non_tensor_batch['multi_modal_data'][i]['processed_images']
            model_inputs = self.processor(processed_images, [solve_prompt], add_special_tokens=False, return_tensors="pt")
            input_ids = model_inputs.pop("input_ids")[0]
            attention_mask = model_inputs.pop("attention_mask")[0]

            if self.processor is not None and "Qwen2VLImageProcessor" in self.processor.image_processor.__class__.__name__:
                # qwen-vl mrope
                if "Qwen3VLProcessor" in self.processor.__class__.__name__:
                    from ..models.transformers.qwen3_vl import get_rope_index
                else:
                    from ..models.transformers.qwen2_vl import get_rope_index

                vision_position_ids = get_rope_index(
                    self.processor,
                    input_ids=input_ids,
                    image_grid_thw=model_inputs.get("image_grid_thw", None),
                    video_grid_thw=model_inputs.get("video_grid_thw", None),
                    second_per_grid_ts=model_inputs.get("second_per_grid_ts", None),
                    attention_mask=attention_mask,
                )  # (3, seq_length)
                text_position_ids = torch.arange(len(input_ids)).unsqueeze(0)  # (1, seq_length)
                position_ids = torch.cat((text_position_ids, vision_position_ids), dim=0)  # (4, seq_length)
            else:
                position_ids = torch.clip(attention_mask.cumsum(dim=0) - 1, min=0, max=None)  # (seq_length,)

            try:
                input_ids, attention_mask, position_ids = VF.postprocess_data(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    max_length=self.config.data.max_prompt_length,
                    pad_token_id=self.tokenizer.pad_token_id,
                    left_pad=True,
                    truncation=self.config.data.truncation,
                )
            except RuntimeError as e:
                msg = str(e)
                if "Input sequence length" in msg and "max length" in msg:
                    uid = caption_batch.non_tensor_batch["uid"][i]
                    debug_dir = os.path.join(self.config.trainer.save_checkpoint_path, "debug_long_prompt")
                    os.makedirs(debug_dir, exist_ok=True)
                    debug_path = os.path.join(
                        debug_dir,
                        (
                            "solve_prompt_uid="
                            f"{uid}_i={i}_seqlen={len(input_ids)}_max={self.config.data.max_prompt_length}.txt"
                        ),
                    )
                    try:
                        with open(debug_path, "w", encoding="utf-8") as f:
                            f.write(f"uid={uid}\n")
                            f.write(f"i={i}\n")
                            f.write(f"seq_len(input_ids)={len(input_ids)}\n")
                            f.write(f"max_prompt_length={self.config.data.max_prompt_length}\n")
                            f.write(f"error={msg}\n")
                            f.write("\n===== problem =====\n")
                            f.write(str(problem))
                            f.write("\n\n===== caption =====\n")
                            f.write(str(caption))
                            f.write("\n\n===== solve_prompt_raw(with <image>) =====\n")
                            f.write(str(solve_prompt_raw))
                            f.write("\n\n===== solve_prompt_chat_template =====\n")
                            f.write(str(solve_prompt))
                    except Exception as write_exc:
                        print(f"[PromptTooLong] Failed to write debug prompt to {debug_path}: {write_exc}")
                    else:
                        print(f"[PromptTooLong] Wrote debug prompt to: {debug_path}")
                raise
            raw_prompt_ids = self.tokenizer.encode(solve_prompt, add_special_tokens=False)
            if len(raw_prompt_ids) > self.config.data.max_prompt_length:
                if self.config.data.truncation == "left":
                    raw_prompt_ids = raw_prompt_ids[-self.config.data.max_prompt_length :]
                elif self.config.data.truncation == "right":
                    raw_prompt_ids = raw_prompt_ids[: self.config.data.max_prompt_length]
                elif self.config.data.truncation == "error":
                    raise RuntimeError(f"Prompt length {len(raw_prompt_ids)} is longer than {self.config.data.max_prompt_length}.")
            tensors["input_ids"].append(input_ids)
            tensors["attention_mask"].append(attention_mask)
            tensors["position_ids"].append(position_ids)
            non_tensors["raw_prompt_ids"].append(raw_prompt_ids)
            non_tensors["multi_modal_data"].append(caption_batch.non_tensor_batch['multi_modal_data'][i])
            non_tensors["problem"].append(problem)
            non_tensors["ground_truth"].append(caption_batch.non_tensor_batch['ground_truth'][i])
        for key, value in tensors.items():
            tensors[key] = torch.stack(value, dim=0)
        for key, value in non_tensors.items():
            non_tensors[key] = np.array(value, dtype=object)
        solve_batch_dict = {**tensors, **non_tensors}
        solve_batch: DataProto = DataProto.from_single_dict(solve_batch_dict, meta_info=meta_info)
        solve_batch.non_tensor_batch["uid"] = np.array(
            [str(uuid.uuid4()) for _ in range(len(solve_batch.batch))], dtype=object
        )
        # pop those keys for generation
        gen_batch = solve_batch.pop(
            batch_keys=["input_ids", "attention_mask", "position_ids"],
            non_tensor_batch_keys=["raw_prompt_ids", "multi_modal_data"],
            meta_info_keys=["min_pixels", "max_pixels", "video_fps"],
        )

        # generate a batch
        gen_batch_output = self.actor_rollout_ref_wg.generate_sequences(gen_batch)

        # repeat to align with repeated responses in rollout
        solve_batch = solve_batch.repeat(repeat_times=self.config.worker.rollout.n, interleave=True)
        solve_batch = solve_batch.union(gen_batch_output)
        print(f"Finish generating.")

        # print(len(caption_batch))
        # print(caption_batch.batch.keys())
        # print(caption_batch.non_tensor_batch.keys())
        # print(caption_batch.meta_info.keys())
        # print(batch_dict.keys())
        # for key in batch_dict:
        #     print(f"{key}:{type(batch_dict[key])}")

        # print(len(solve_batch))
        # print(solve_batch.batch.keys())
        # print(solve_batch.non_tensor_batch.keys())
        # print(solve_batch.meta_info.keys())
        # print(solve_batch_dict.keys())
        # for key in solve_batch_dict:
        #     print(f"{key}:{type(solve_batch_dict[key])}")

        # caption_prompt0 = self.tokenizer.decode(caption_batch.batch['prompts'][0], skip_special_tokens=True)
        # print(caption_prompt0)
        # caption0 = self.tokenizer.decode(caption_batch.batch['responses'][0], skip_special_tokens=True)
        # print(caption0)
        # solve_prompt0 = self.tokenizer.decode(solve_batch.batch['prompts'][0], skip_special_tokens=True)
        # print(solve_prompt0)
        # solve0 = self.tokenizer.decode(solve_batch.batch['responses'][0], skip_special_tokens=True)
        # print(solve0)
        # solve_prompt1 = self.tokenizer.decode(solve_batch.batch['prompts'][1], skip_special_tokens=True)
        # print(solve_prompt1)
        # solve1 = self.tokenizer.decode(solve_batch.batch['responses'][1], skip_special_tokens=True)
        # print(solve1)
        # caption_prompt1 = self.tokenizer.decode(caption_batch.batch['prompts'][5], skip_special_tokens=True)
        # print(caption_prompt1)
        # caption1 = self.tokenizer.decode(caption_batch.batch['responses'][5], skip_special_tokens=True)
        # print(caption1)
        # solve_prompt5 = self.tokenizer.decode(solve_batch.batch['prompts'][25], skip_special_tokens=True)
        # print(solve_prompt5)
        # solve5 = self.tokenizer.decode(solve_batch.batch['responses'][25], skip_special_tokens=True)
        # print(solve5)

        return caption_batch, solve_batch

    def fit(self):
        """
        The training loop of PPO.
        The driver process only need to call the compute functions of the worker group through RPC to construct the PPO dataflow.
        The light-weight advantage computation is done on the driver process.
        """
        self.logger = Tracker(loggers=self.config.trainer.logger, config=self.config.to_dict())
        self.global_step = 0
        main_tqdm = tqdm(range(self.training_steps), desc="Running step", position=0)
        val_metrics: Optional[dict[str, Any]] = None

        # load checkpoint before doing anything
        self._load_checkpoint()
        main_tqdm.update(self.global_step)

        # perform validation before training
        # currently, we only support validation using the reward_function.
        if self.val_reward_fn is not None and self.config.trainer.val_before_train:
            val_metrics = self._validate()
            self.logger.log(data=val_metrics, step=self.global_step)
            if self.config.trainer.val_only:
                return

        self.data_iterator = iter(self.train_dataloader)
        while self.global_step < self.training_steps:
            self.global_step += 1

            metrics, timing_raw = {}, {}
            with timer("step", timing_raw):
                # make a batch of data
                with timer("gen", timing_raw):
                    self.actor_rollout_ref_wg.prepare_rollout_engine()
                    caption_batch, solve_batch = self._make_batch_data(metrics=metrics)
                    self.actor_rollout_ref_wg.release_rollout_engine()

                # balance the number of valid tokens on each dp rank.
                # NOTE: this breaks the order of data inside the batch.
                # Please take care when you implement group based adv computation such as GRPO and rloo
                # self._balance_batch(batch, metrics=metrics)

                # compute global valid tokens
                # batch.meta_info["global_token_num"] = torch.sum(batch.batch["attention_mask"], dim=-1).tolist()

                # compute reward
                # if "token_level_scores" not in batch.batch:
                with timer("reward", timing_raw):
                    batch, reward_tensor, reward_metrics = ray.get(
                        self.reward_fn.compute_reward.remote(caption_batch, solve_batch)
                    )
                    batch.batch["token_level_scores"] = reward_tensor
                    reward_metrics = {f"reward/{k}": v for k, v in reduce_metrics(reward_metrics).items()}
                    metrics.update(reward_metrics)

                # recompute old_log_probs
                with timer("old", timing_raw):
                    old_log_probs = self.actor_rollout_ref_wg.compute_log_probs(batch)
                    batch = batch.union(old_log_probs)

                # compute ref_log_probs
                if self.use_reference_policy:
                    with timer("ref", timing_raw):
                        ref_log_probs = self.actor_rollout_ref_wg.compute_ref_log_probs(batch)
                        batch = batch.union(ref_log_probs)

                # compute values
                if self.use_critic:
                    with timer("values", timing_raw):
                        values = self.critic_wg.compute_values(batch)
                        batch = batch.union(values)

                with timer("adv", timing_raw):
                    # apply kl penalty if available
                    if not self.config.algorithm.use_kl_loss and self.use_reference_policy:
                        # apply kl penalty to reward
                        batch, kl_metrics = apply_kl_penalty(batch, self.kl_ctrl, self.config.algorithm.kl_penalty)
                        metrics.update(kl_metrics)
                    else:
                        batch.batch["token_level_rewards"] = batch.batch["token_level_scores"]

                    # compute advantages, executed on the driver process
                    batch = compute_advantage(
                        batch,
                        adv_estimator=self.config.algorithm.adv_estimator,
                        gamma=self.config.algorithm.gamma,
                        lam=self.config.algorithm.lam,
                    )

                # update critic
                if self.use_critic:
                    with timer("update_critic", timing_raw):
                        critic_output = self.critic_wg.update_critic(batch)

                    critic_metrics = reduce_metrics(critic_output.non_tensor_batch)
                    metrics.update(critic_metrics)

                # update actor
                if self.config.trainer.critic_warmup <= self.global_step:
                    with timer("update_actor", timing_raw):
                        actor_output = self.actor_rollout_ref_wg.update_actor(batch)

                    actor_metrics = reduce_metrics(actor_output.non_tensor_batch)
                    metrics.update(actor_metrics)

                # validate
                if (
                    self.val_reward_fn is not None
                    and self.config.trainer.val_freq > 0
                    and self.global_step % self.config.trainer.val_freq == 0
                ):
                    with timer("validation", timing_raw):
                        val_metrics = self._validate()

                    metrics.update(val_metrics)

                if self.config.trainer.save_freq > 0 and self.global_step % self.config.trainer.save_freq == 0:
                    with timer("save_checkpoint", timing_raw):
                        self._save_checkpoint()

            # collect metrics
            num_gpus = self.resource_pool_manager.get_num_gpus()
            metrics.update(compute_data_metrics(batch=batch, use_critic=self.use_critic))
            metrics.update(compute_timing_metrics(batch=batch, timing_raw=timing_raw))
            metrics.update(compute_throughout_metrics(batch=batch, timing_raw=timing_raw, num_gpus=num_gpus))

            self.logger.log(data=metrics, step=self.global_step)
            main_tqdm.update()

        # perform validation after training
        if self.val_reward_fn is not None:
            if (
                val_metrics is None
                or self.config.trainer.val_freq <= 0
                or self.global_step % self.config.trainer.val_freq != 0
            ):
                val_metrics = self._validate()
                self.logger.log(data=val_metrics, step=self.global_step)

            print(f"Final validation metrics:\n{convert_dict_to_str(unflatten_dict(val_metrics))}")

        if self.config.trainer.save_freq <= 0 or self.global_step % self.config.trainer.save_freq != 0:
            self._save_checkpoint()
