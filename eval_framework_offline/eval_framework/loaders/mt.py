"""MT-AgentRisk (mt) loader.

On-disk layout (directories currently empty but structure is fixed):

    <data_root>/mt/{benign,adversarial}/<...>/*.json

No per-model dimension; files are expected to follow the generic
``messages`` / ``chat_history`` convention plus top-level
``utility`` / ``security`` once annotated.
"""
from __future__ import annotations

from .generic import GenericSplitLoader


class MTLoader(GenericSplitLoader):
    name = "mt"
    has_model_dim = False
