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
Reward config
"""

from dataclasses import dataclass, field
from typing import Optional

from ...utils.py_functional import get_abs_path


@dataclass
class RewardConfig:
    reward_function: Optional[str] = None
    reward_function_kwargs: dict = field(default_factory=dict)
    skip_special_tokens: bool = True
    num_cpus: int = 1
    num_gpus: float = 0
    """Fractional GPUs reserved for each reward actor (train + val).

    Default 0 keeps the legacy CPU-only placement. Set >0 (e.g. 0.5) when the
    reward function loads a GPU model itself (e.g. local NLI). Two reward
    actors are spawned, so total GPU reservation = 2 * num_gpus; the trainer's
    actor pool (n_gpus_per_node) must leave that much free.
    """

    # -------- Self-play / DAPO-related knobs (EasyR1-share custom) --------
    # NOTE: These knobs control how we *select* solver trajectories for training
    # when we have a nested sampling structure:
    #   prompt -> N captions -> N solutions per caption.
    # Caption reward is computed from *all* solutions, while a subset of solver
    # trajectories are used for policy update.
    enable_solver_update_sampling: bool = False
    """Enable the custom solver trajectory sampling logic.

    - False: keep legacy behavior (use the first solver group per prompt).
    - True : allow filtering and/or upsampling (see the knobs below).
    """

    solver_update_ratio: int = 1
    """How many solver groups to use per prompt (multiplier over legacy).

    Legacy behavior uses exactly 1 solver group per prompt. When set to k,
    we try to use k solver groups per prompt (i.e., k times as many solver
    trajectories), subject to availability after optional filtering.
    """

    enable_solver_update_group_filter: bool = True
    """Filter out degenerate solver groups (all-0 / all-1, i.e., all-equal)."""

    solver_update_filter_key: str = "accuracy"
    """Which score field to inspect when filtering solver groups."""

    enable_solver_update_random_sampling: bool = True
    """Randomly sample solver groups after filtering (otherwise take the first k)."""

    caption_reward_key: str = "accuracy"
    """Which score field to average for caption reward (e.g., accuracy or accuracy_normalized)."""
    # below are auto keys
    reward_function_name: Optional[str] = field(default=None, init=False)

    def post_init(self):
        if self.reward_function is not None:  # support custom reward function, e.g., ./math.py:main
            if ":" not in self.reward_function:
                self.reward_function_name = "main"
            else:
                self.reward_function, self.reward_function_name = self.reward_function.rsplit(":", maxsplit=1)

            self.reward_function = get_abs_path(self.reward_function, prompt="Reward function")
