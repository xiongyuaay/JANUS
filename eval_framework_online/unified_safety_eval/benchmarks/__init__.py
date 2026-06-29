from __future__ import annotations

from pathlib import Path
from typing import Any

from .agentharm import AgentHarmAdapter
from .agent_safetybench import AgentSafetyBenchAdapter
from .agentdojo_adapter import AgentDojoAdapter
from .agentlab import AgentLABAdapter
from .asb import ASBAdapter
from .lps_bench import LPSBenchAdapter
from .common import BenchmarkAdapter

REGISTRY: dict[str, type[BenchmarkAdapter]] = {
    "agentharm": AgentHarmAdapter,
    "agent_harm": AgentHarmAdapter,
    "agent-safetybench": AgentSafetyBenchAdapter,
    "agent_safetybench": AgentSafetyBenchAdapter,
    "agentsafetybench": AgentSafetyBenchAdapter,
    "agentdojo": AgentDojoAdapter,
    "agent_dojo": AgentDojoAdapter,
    "agentlab": AgentLABAdapter,
    "agent_lab": AgentLABAdapter,
    "asb": ASBAdapter,
    "lps": LPSBenchAdapter,
    "lps_bench": LPSBenchAdapter,
    "lps-bench": LPSBenchAdapter,
}


def build_adapter(name: str, project_root: Path, cfg: dict[str, Any], run_cfg: dict[str, Any]) -> BenchmarkAdapter:
    key = name.lower().replace(" ", "_")
    if key not in REGISTRY:
        raise ValueError(f"Unknown benchmark '{name}'. Known: {sorted(REGISTRY)}")
    return REGISTRY[key](project_root, cfg.get(key, cfg.get(name, {})), run_cfg)
