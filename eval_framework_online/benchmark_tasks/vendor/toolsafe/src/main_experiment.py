#!/usr/bin/env python3
"""Compatibility wrapper for the unified safety-eval runner.

ToolSafe's README/scripts reference src/main_experiment.py, but the uploaded tree did not include
that file. This wrapper delegates to the root-level unified runner and still reads the single YAML
config in unified_safety_eval/config.yaml unless UNIFIED_SAFETY_EVAL_CONFIG is set.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_safety_eval.config import load_config
from unified_safety_eval.runner import run_from_config


if __name__ == "__main__":
    cfg = load_config()
    print(json.dumps(run_from_config(cfg), ensure_ascii=False, indent=2, default=str))
