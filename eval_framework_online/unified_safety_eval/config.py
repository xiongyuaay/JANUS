"""Configuration loading for the unified agent safety evaluation framework."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_IMPORT_ERROR = exc
else:
    _YAML_IMPORT_ERROR = None

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _expand_env_value(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            default = match.group(2)
            return os.environ.get(name, default or "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env_value(v) for k, v in value.items()}
    return value


def deep_get(mapping: Mapping[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = mapping
    for key in path.split("."):
        if not isinstance(cur, Mapping) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_model_registry_path(cfg_path: Path, data: Mapping[str, Any]) -> Path | None:
    value = data.get("model_config_path") or data.get("model_registry")
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = (cfg_path.parent / path).resolve()
    return path


def _load_model_registry(cfg_path: Path, data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    registry_path = _resolve_model_registry_path(cfg_path, data)
    if registry_path is None:
        return {}
    if not registry_path.exists():
        raise FileNotFoundError(f"Model config file not found: {registry_path}")
    raw = _expand_env_value(_load_yaml(registry_path))
    models = raw.get("models", raw)
    if not isinstance(models, Mapping):
        raise ValueError(f"Model config file must contain a mapping: {registry_path}")
    registry: dict[str, dict[str, Any]] = {}
    for key, value in models.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"Model config for {key!r} must be a mapping")
        registry[str(key)] = dict(value)
    return registry


_MODEL_SELECTOR_KEYS = {"model", "guard", "judge", "refuse_judge", "planner", "attacker"}
_INLINE_MODEL_ENDPOINT_KEYS = {"provider", "name", "base_url", "api_key"}


def _is_model_selector(path: str) -> bool:
    if not path:
        return False
    return path.split(".")[-1] in _MODEL_SELECTOR_KEYS


def _validate_model_selectors(value: Any, path: str = "") -> None:
    if isinstance(value, list):
        for idx, item in enumerate(value):
            _validate_model_selectors(item, f"{path}[{idx}]")
        return
    if not isinstance(value, dict):
        return

    if _is_model_selector(path):
        inline_keys = sorted(k for k in _INLINE_MODEL_ENDPOINT_KEYS if k in value)
        if inline_keys:
            raise ValueError(
                f"{path} defines inline model endpoint fields {inline_keys}. "
                "Move the endpoint to model_configs.yaml and select it with model_name."
            )

    for key, item in value.items():
        child_path = str(key) if not path else f"{path}.{key}"
        _validate_model_selectors(item, child_path)


def _resolve_model_names(value: Any, registry: Mapping[str, dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [_resolve_model_names(item, registry) for item in value]
    if not isinstance(value, dict):
        return value

    resolved = {
        key: _resolve_model_names(item, registry)
        for key, item in value.items()
        if key != "model_name"
    }
    ref = value.get("model_name")
    if ref is None:
        return resolved
    ref_key = str(ref)
    if ref_key not in registry:
        raise KeyError(f"Unknown model_name {ref_key!r}; add it to model_configs.yaml first.")
    return {**registry[ref_key], **resolved}


@dataclass(frozen=True)
class EvalConfig:
    data: dict[str, Any]
    path: Path
    project_root: Path

    def get(self, path: str, default: Any = None) -> Any:
        return deep_get(self.data, path, default)

    @property
    def selected_benchmarks(self) -> list[str]:
        value = self.get("run.benchmark", [])
        if isinstance(value, str):
            return [value]
        return list(value or [])

    @property
    def selected_defense(self) -> str:
        return str(self.get("run.defense", "react_base"))

    @property
    def output_dir(self) -> Path:
        output = Path(str(self.get("run.output_dir", "results/unified")))
        if not output.is_absolute():
            output = self.project_root / output
        return output


def load_config(path: str | os.PathLike[str] | None = None) -> EvalConfig:
    if yaml is None:  # pragma: no cover
        raise RuntimeError(f"PyYAML is required to load the YAML config: {_YAML_IMPORT_ERROR}")

    if path is None:
        env_path = os.environ.get("UNIFIED_SAFETY_EVAL_CONFIG")
        if env_path:
            path = env_path
        else:
            path = Path(__file__).resolve().parent / "config.yaml"
    cfg_path = Path(path).resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    raw = _load_yaml(cfg_path)
    data = _expand_env_value(raw)
    model_registry = _load_model_registry(cfg_path, data)
    if model_registry:
        _validate_model_selectors(data)
        data = _resolve_model_names(data, model_registry)

    # The config sits inside unified_safety_eval/ by default; the project root is one directory up.
    default_base = cfg_path.parent.parent if cfg_path.parent.name == "unified_safety_eval" else cfg_path.parent
    project_value = data.get("project_root")
    if project_value:
        project_root = Path(str(project_value))
        if not project_root.is_absolute():
            project_root = (default_base / project_root).resolve()
    else:
        project_root = default_base.resolve()
    return EvalConfig(data=data, path=cfg_path, project_root=project_root)
