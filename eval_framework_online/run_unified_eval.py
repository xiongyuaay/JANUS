#!/usr/bin/env python3
"""Run the unified real-agent safety evaluation from unified_safety_eval/config.yaml.

No command-line arguments are required. Optionally set UNIFIED_SAFETY_EVAL_CONFIG to point to
another YAML file. This entrypoint is quiet by default: it only emits tqdm progress bars and
error messages. Full summaries are written to the configured output directory.
"""
from __future__ import annotations

import warnings

from unified_safety_eval.config import load_config
from unified_safety_eval.progress import set_quiet
from unified_safety_eval.runner import run_from_config


if __name__ == "__main__":
    set_quiet(True)
    warnings.filterwarnings("ignore")
    config = load_config()
    run_from_config(config)
