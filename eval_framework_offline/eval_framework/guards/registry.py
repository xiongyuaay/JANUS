"""Guard factory: `build_guard(name, config_dict) -> Guard`.

Each entry maps a name to a factory function that accepts a config dict
loaded from JSON. The factory is responsible for translating JSON-native
values (strings, lists, dicts) into constructor arguments.

Registering additional guards at runtime is supported via `register`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .base import Guard
from .llamafirewall_guard import LlamaFirewallGuard
from .llamaguard_guard import LlamaGuardVLLMGuard
from .qwen3guard_guard import Qwen3GuardVLLMGuard
from .stub import AlwaysSafeGuard, AlwaysUnsafeGuard
from .toolsafe_guard import AlignmentCheckGuard, SandwichDefenseGuard
from .vllm_guard import DEFAULT_USER_TEMPLATE, VLLMGuard, load_future_summary_cache


def _build_stub_safe(config: dict[str, Any]) -> Guard:
    return AlwaysSafeGuard()


def _build_stub_unsafe(config: dict[str, Any]) -> Guard:
    return AlwaysUnsafeGuard()


def _build_vllm(config: dict[str, Any]) -> Guard:
    required = ("base_url", "model_name")
    missing = [k for k in required if k not in config]
    if missing:
        raise ValueError(f"vllm guard config missing keys: {missing}")
    max_input_chars = config.get("max_input_chars")
    future_summary_mode = config.get("future_summary_mode")
    requested_modes = {future_summary_mode} if future_summary_mode else set()
    if config.get("predict_future_summary"):
        requested_modes.add("predicted")
        future_summary_mode = "predicted"
    if config.get("truth_future_summary"):
        requested_modes.add("truth")
        future_summary_mode = "truth"
    if config.get("future_summary_mismatch"):
        requested_modes.add("truth")
        future_summary_mode = "truth"
    if len(requested_modes) > 1:
        raise ValueError("predicted and truth future-summary modes are mutually exclusive")
    future_summary_cache = None
    if future_summary_mode == "truth":
        cache_path = config.get("future_summary_cache")
        if not cache_path:
            raise ValueError("future_summary_cache is required when truth_future_summary is enabled")
        future_summary_cache = load_future_summary_cache(Path(cache_path))
    return VLLMGuard(
        base_url=config["base_url"],
        model_name=config["model_name"],
        api_key=config.get("api_key"),
        user_prompt_template=config.get("user_prompt_template") or DEFAULT_USER_TEMPLATE,
        max_tokens=int(config.get("max_tokens", 256)),
        temperature=float(config.get("temperature", 0.0)),
        timeout=float(config.get("timeout", 60.0)),
        extra_body=config.get("extra_body"),
        max_input_chars=int(max_input_chars) if max_input_chars is not None else None,
        future_summary_mode=future_summary_mode,
        future_summary_cache=future_summary_cache,
        future_summary_mismatch=bool(config.get("future_summary_mismatch", False)),
        summary_max_tokens=int(config.get("summary_max_tokens", 256)),
        summary_temperature=float(config.get("summary_temperature", config.get("temperature", 0.0))),
    )


def _build_llamafirewall(config: dict[str, Any]) -> Guard:
    return LlamaFirewallGuard(
        scanners=config.get("scanners"),
        usecase=config.get("usecase"),
    )


def _build_classifier_common(config: dict[str, Any]) -> dict[str, Any]:
    required = ("base_url", "model_name")
    missing = [k for k in required if k not in config]
    if missing:
        raise ValueError(f"classifier guard config missing keys: {missing}")
    max_input_chars = config.get("max_input_chars")
    kwargs: dict[str, Any] = {
        "base_url": config["base_url"],
        "model_name": config["model_name"],
        "api_key": config.get("api_key"),
        "max_tokens": int(config.get("max_tokens", 64)),
        "temperature": float(config.get("temperature", 0.0)),
        "timeout": float(config.get("timeout", 60.0)),
        "extra_body": config.get("extra_body"),
        "max_input_chars": int(max_input_chars) if max_input_chars is not None else None,
    }
    return kwargs


def _build_llamaguard(config: dict[str, Any]) -> Guard:
    return LlamaGuardVLLMGuard(**_build_classifier_common(config))


def _build_qwen3guard(config: dict[str, Any]) -> Guard:
    return Qwen3GuardVLLMGuard(**_build_classifier_common(config))


def _build_toolsafe_common(config: dict[str, Any]) -> dict[str, Any]:
    required = ("base_url", "model_name")
    missing = [k for k in required if k not in config]
    if missing:
        raise ValueError(f"toolsafe guard config missing keys: {missing}")
    max_input_chars = config.get("max_input_chars")
    kwargs: dict[str, Any] = {
        "base_url": config["base_url"],
        "model_name": config["model_name"],
        "api_key": config.get("api_key"),
        "max_tokens": int(config.get("max_tokens", 512)),
        "temperature": float(config.get("temperature", 0.0)),
        "timeout": float(config.get("timeout", 60.0)),
        "extra_body": config.get("extra_body"),
        "max_input_chars": int(max_input_chars) if max_input_chars is not None else None,
        "response_format": config.get("response_format"),
    }
    return kwargs


def _build_alignmentcheck(config: dict[str, Any]) -> Guard:
    kwargs = _build_toolsafe_common(config)
    if "response_format" not in config:
        kwargs["response_format"] = {"type": "json_object"}
    return AlignmentCheckGuard(**kwargs)


def _build_sandwichdefense(config: dict[str, Any]) -> Guard:
    return SandwichDefenseGuard(**_build_toolsafe_common(config))


_GUARDS: dict[str, Callable[[dict[str, Any]], Guard]] = {
    "stub_safe": _build_stub_safe,
    "stub_unsafe": _build_stub_unsafe,
    "vllm": _build_vllm,
    "llamafirewall": _build_llamafirewall,
    "llamaguard": _build_llamaguard,
    "qwen3guard": _build_qwen3guard,
    "alignmentcheck": _build_alignmentcheck,
    "sandwichdefense": _build_sandwichdefense,
    "sandwich_defense": _build_sandwichdefense,
}


def available() -> list[str]:
    return sorted(_GUARDS.keys())


def build_guard(name: str, config: dict[str, Any] | None = None) -> Guard:
    try:
        factory = _GUARDS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown guard {name!r}; available: {available()}") from exc
    return factory(config or {})


def register(name: str, factory: Callable[[dict[str, Any]], Guard]) -> None:
    _GUARDS[name] = factory
