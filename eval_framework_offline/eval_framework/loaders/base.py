from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator, Optional, Sequence

from ..schema import Split, Trajectory


class TrajectoryLoader(ABC):
    """Benchmark-specific loader that walks ``<data_root>/<dir_name>/...``.

    Each loader knows its own on-disk layout and translates raw JSON into
    canonical ``Trajectory`` objects.

    Class attributes
    ----------------
    name
        Stable identifier exposed to the CLI (``--benchmarks name``).
    dir_name
        Directory under ``data_root`` that holds the benchmark's data.
        Defaults to ``name``; override when the eval-data folder is named
        differently (e.g. ``agentharm_react_uncensor``).
    split_map
        Mapping from on-disk subfolder name to the canonical split the
        framework works with (``benign`` / ``adversarial``). Benchmarks
        that use non-standard subfolder names (``harmful``, ``clean``,
        ``DPI``, ...) declare the translation here.
    has_model_dim
        True if the benchmark's layout has a per-model level just below
        the split (e.g. AgentDojo). The CLI uses this to decide whether
        ``--models`` is meaningful.
    """

    name: str = "loader"
    dir_name: Optional[str] = None
    split_map: dict[str, Split] = {"benign": "benign", "adversarial": "adversarial"}
    has_model_dim: bool = False

    @property
    def resolved_dir_name(self) -> str:
        return self.dir_name or self.name

    # ---- on-disk discovery helpers -----------------------------------

    def resolved_root(self, data_root: Path) -> Path:
        """Locate the benchmark's directory under ``data_root``.

        Accepts both ``<data_root>/<dir_name>`` and being pointed at the
        benchmark root directly.
        """
        candidate = data_root / self.resolved_dir_name
        return candidate if candidate.exists() else data_root

    def on_disk_dirs_for(self, split: Split) -> list[str]:
        """Return on-disk subfolder names that map to the canonical split."""
        return [name for name, canonical in self.split_map.items() if canonical == split]

    # ---- abstract interface ------------------------------------------

    @abstractmethod
    def iter_trajectories(
        self,
        data_root: Path,
        *,
        splits: Sequence[Split] = ("benign", "adversarial"),
        models: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Trajectory]:
        raise NotImplementedError

    def available_models(self, data_root: Path) -> list[str]:
        """Return the list of model subdirectories present on disk.

        Default implementation returns an empty list; loaders that expose
        a per-model layout override this.
        """
        return []

    def uses_model_filter(self, data_root: Path) -> bool:
        """Return whether ``--models`` applies for this data root."""
        return self.has_model_dim
