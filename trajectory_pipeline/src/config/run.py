from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass
class RunConfig:
    """Runtime settings for generation."""

    prompt_dir: Optional[Path]
    output_dir: Path
    category_counts: Dict[str, int]
    category_display_names: Dict[str, str]
    resume: bool
    auto_approve: bool
    max_iterations: int
    base_url: str
    api_key: str
    model: str
    temperature: float
    case_retry_limit: int
    run_trajectory: bool
    trace_run_name: str
    trajectory_base_url: str
    trajectory_api_key: str
    trajectory_model: str
    trajectory_temperature: float
    trajectory_style: str
    tool_executor_base_url: str
    tool_executor_api_key: str
    tool_executor_model: str
    tool_executor_temperature: float
    trajectory_max_turns: int
    trajectory_workers: int
    case_workers: int
    continue_on_error: bool
    case_schedule: str
    # Rejection sampling parameters
    enable_rejection_sampling: bool
    max_rejection_retries: int
    reviewer_base_url: str
    reviewer_api_key: str
    reviewer_model: str
    reviewer_temperature: float


def build_config(
    *,
    prompt_dir: Optional[Path],
    output_dir: Path,
    category_counts: Dict[str, int],
    category_display_names: Dict[str, str],
    resume: bool,
    auto_approve: bool,
    max_iterations: int,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    case_retry_limit: int,
    run_trajectory: bool,
    trace_run_name: str,
    trajectory_base_url: str,
    trajectory_api_key: str,
    trajectory_model: str,
    trajectory_temperature: float,
    trajectory_style: str,
    tool_executor_base_url: str,
    tool_executor_api_key: str,
    tool_executor_model: str,
    tool_executor_temperature: float,
    trajectory_max_turns: int,
    trajectory_workers: int,
    case_workers: int,
    continue_on_error: bool,
    case_schedule: str = "sequential",
    # Rejection sampling parameters
    enable_rejection_sampling: bool = True,
    max_rejection_retries: int = 3,
    reviewer_base_url: Optional[str] = None,
    reviewer_api_key: Optional[str] = None,
    reviewer_model: Optional[str] = None,
    reviewer_temperature: float = 0.0,
) -> RunConfig:
    """Build a RunConfig from module-level constants."""

    return RunConfig(
        prompt_dir=prompt_dir,
        output_dir=output_dir,
        category_counts=dict(category_counts),
        category_display_names=dict(category_display_names),
        resume=resume,
        auto_approve=auto_approve,
        max_iterations=max_iterations,
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        case_retry_limit=case_retry_limit,
        run_trajectory=run_trajectory,
        trace_run_name=trace_run_name,
        trajectory_base_url=trajectory_base_url,
        trajectory_api_key=trajectory_api_key,
        trajectory_model=trajectory_model,
        trajectory_temperature=trajectory_temperature,
        trajectory_style=trajectory_style,
        tool_executor_base_url=tool_executor_base_url,
        tool_executor_api_key=tool_executor_api_key,
        tool_executor_model=tool_executor_model,
        tool_executor_temperature=tool_executor_temperature,
        trajectory_max_turns=trajectory_max_turns,
        trajectory_workers=trajectory_workers,
        case_workers=case_workers,
        continue_on_error=continue_on_error,
        case_schedule=case_schedule,
        enable_rejection_sampling=enable_rejection_sampling,
        max_rejection_retries=max_rejection_retries,
        reviewer_base_url=reviewer_base_url or trajectory_base_url,
        reviewer_api_key=reviewer_api_key or trajectory_api_key,
        reviewer_model=reviewer_model or trajectory_model,
        reviewer_temperature=reviewer_temperature,
    )


def validate_config(config: RunConfig) -> None:
    """Fail fast on placeholder or invalid settings."""

    placeholder_values = {
        "http://YOUR_HOST:YOUR_PORT/v1": "BASE_URL",
        "YOUR_API_KEY": "API_KEY",
        "YOUR_MODEL_NAME": "MODEL",
    }
    missing = [field_name for value, field_name in placeholder_values.items() if getattr(config, field_name.lower()) == value]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Edit the hardcoded configuration at the top of this file before running. Missing values: {joined}")
    if not config.category_counts:
        raise ValueError("category_counts cannot be empty")
    invalid_counts = {category: count for category, count in config.category_counts.items() if count <= 0}
    if invalid_counts:
        raise ValueError(f"All category_counts must be positive integers. Invalid entries: {invalid_counts}")
    missing_display_names = [category for category in config.category_counts if category not in config.category_display_names]
    if missing_display_names:
        raise ValueError(f"Missing display names for categories: {missing_display_names}")
    if config.trajectory_max_turns <= 0:
        raise ValueError("trajectory_max_turns must be positive")
    if config.trajectory_workers <= 0:
        raise ValueError("trajectory_workers must be positive")
    if config.case_workers <= 0:
        raise ValueError("case_workers must be positive")
    if config.trajectory_style not in {"auto", "openai", "xml", "react"}:
        raise ValueError("trajectory_style must be one of: auto, openai, xml, react")
    if config.case_retry_limit < 0:
        raise ValueError("case_retry_limit cannot be negative")
    if config.case_schedule not in {"sequential", "proportional"}:
        raise ValueError("case_schedule must be one of: sequential, proportional")
    if config.max_rejection_retries < 0:
        raise ValueError("max_rejection_retries cannot be negative")
    if config.reviewer_temperature < 0.0 or config.reviewer_temperature > 2.0:
        raise ValueError("reviewer_temperature must be between 0.0 and 2.0")
