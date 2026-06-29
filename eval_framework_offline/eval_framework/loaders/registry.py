from __future__ import annotations

from typing import Callable

from .agentdojo import AgentDojoLoader
from .agentharm import AgentHarmLoader
from .asb import ASBLightLoader, ASBStandardLoader
from .base import TrajectoryLoader
from .lpsbench import LPSBenchLoader
from .mt import MTLoader
from .static_trace import (
    StaticASBLoader,
    StaticAgentLabLoader,
    StaticAgentSafetyBenchLoader,
)

_LOADERS: dict[str, Callable[[], TrajectoryLoader]] = {
    AgentDojoLoader.name: AgentDojoLoader,
    AgentHarmLoader.name: AgentHarmLoader,
    StaticAgentLabLoader.name: StaticAgentLabLoader,
    StaticAgentSafetyBenchLoader.name: StaticAgentSafetyBenchLoader,
    ASBLightLoader.name: ASBLightLoader,
    ASBStandardLoader.name: ASBStandardLoader,
    StaticASBLoader.name: StaticASBLoader,
    LPSBenchLoader.name: LPSBenchLoader,
    MTLoader.name: MTLoader,
}


def available() -> list[str]:
    return sorted(_LOADERS.keys())


def get_loader(name: str) -> TrajectoryLoader:
    try:
        factory = _LOADERS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown benchmark {name!r}; available: {available()}") from exc
    return factory()


def register(name: str, factory: Callable[[], TrajectoryLoader]) -> None:
    """Register a new loader. For use by external plugins/tests."""
    _LOADERS[name] = factory
