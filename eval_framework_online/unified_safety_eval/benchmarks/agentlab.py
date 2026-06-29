"""AgentLAB adapter.

Wraps AgentLAB's two tool-using attack tracks:

* ``tool_chaining``    -> ``data/filtered_top_200_attacks.json``  (default ReAct mode)
* ``memory_poisoning`` -> ``data/all_refused_combined_200.json``  (default ReAct mode)

The ``intent_hijacking`` track is intentionally excluded from this adapter:
its upstream victim is a plain chat agent with no tool calls, so it neither
benefits from nor needs ReAct conversion (and would degenerate into a no-op
ReAct loop). Run upstream ``Intent-Hijacking.py`` directly if you need it.

Each track can be driven either through the in-process ReAct loop
(``mode: react``, the default) or by invoking the upstream Python script as a
subprocess (``mode: subprocess``). The subprocess path requires the victim
vLLM to expose OpenAI tool-calling (``--enable-auto-tool-choice
--tool-call-parser <parser>``); the ReAct path does not.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .common import BenchmarkAdapter, BenchmarkRunOutput
from . import agentlab_react
from ..progress import error as log_error
from ..progress import info, is_quiet
from ..utils import ensure_dir, write_json


_DEFAULT_TRACKS = ["tool_chaining", "memory_poisoning"]
_VALID_MODES = {"react", "subprocess"}


class AgentLABAdapter(BenchmarkAdapter):
    name = "agentlab"

    # ------------------------------------------------------------------ paths
    def _root(self) -> Path:
        return (self.project_root / self.cfg.get("root", "benchmark_tasks/agentlab")).resolve()

    def load_cases(self):
        return []

    def evaluate(self, results, output_dir: Path):
        return {}

    # ----------------------------------------------------------------- helpers
    def _component_cfg(self, name: str) -> dict[str, Any]:
        cfg = self.cfg.get(name) or {}
        return cfg if isinstance(cfg, dict) else {}

    def _victim(self) -> tuple[str, str, str]:
        return (
            str(self.run_cfg.get("model_name", "") or ""),
            str(self.run_cfg.get("model_base_url", "") or ""),
            str(self.run_cfg.get("model_api_key", "") or ""),
        )

    def _track_cfg(self, track: str) -> dict[str, Any]:
        tracks_cfg = self.cfg.get("tracks") or {}
        return dict((tracks_cfg.get(track) or {}))

    def _resolve_mode(self, track: str) -> str:
        """Resolve effective execution mode for a track.

        Lookup order: per-track override (``tracks.<track>.mode``) -> benchmark
        default (``mode``) -> hard-coded ``"react"``. If ``react`` is requested
        for a track that has no ReAct adapter implemented, we fall back to
        ``subprocess`` with a warning so existing setups don't break.
        """
        track_cfg = self._track_cfg(track)
        bench_default = str(self.cfg.get("mode", "react") or "react").lower()
        mode = str(track_cfg.get("mode", bench_default) or bench_default).lower()
        if mode not in _VALID_MODES:
            info(f"agentlab: unknown mode '{mode}' for track {track}; defaulting to react")
            mode = "react"
        if mode == "react" and track not in agentlab_react.REACT_TRACK_RUNNERS:
            info(
                f"agentlab: react mode requested for {track} but no ReAct adapter is "
                f"implemented yet; falling back to subprocess mode for this track."
            )
            mode = "subprocess"
        return mode

    # -------------------------------------------------------- command builders
    def _cmd_tool_chaining(self, attack_out: Path) -> list[str]:
        tcfg = self._track_cfg("tool_chaining")
        victim, victim_url, _ = self._victim()
        parts = [
            "python", str(tcfg.get("script", "Tool-chaining.py")),
            "--dataset", str(tcfg.get("dataset", "data/filtered_top_200_attacks.json")),
            "--num_samples", str(tcfg.get("num_samples", 200)),
            "--output_dir", str(attack_out),
        ]
        if victim:
            parts.extend(["--victim_model", victim])
        if victim_url:
            parts.extend(["--victim_base_url", victim_url])
        for key in ("max_turns", "max_agent_rounds", "success_threshold", "seed"):
            if key in tcfg:
                parts.extend([f"--{key}", str(tcfg[key])])
        # Tool-chaining exposes only --attacker_model and uses it for the planner/judge roles too
        # (see Tool-chaining.py:1816-1817). base_url/api_key are redirected via env vars in run().
        attacker = self._component_cfg("attacker") or self._component_cfg("planner")
        attacker_model = attacker.get("model") or attacker.get("name")
        if attacker_model:
            parts.extend(["--attacker_model", str(attacker_model)])
        if tcfg.get("silent"):
            parts.append("--silent")
        return parts

    def _cmd_memory_poisoning(self) -> list[str]:
        """Memory-Poisoning.py has no --output_dir; results land in
        ``AgentLAB/agentlab_results_<TS>/``. The caller relocates the directory
        after the subprocess exits. The script's planner/judge/attacker models are
        hard-coded to gpt-5.1 in the Config dataclass; only the OpenAI endpoint can
        be redirected (via env vars set by run()).
        """
        tcfg = self._track_cfg("memory_poisoning")
        victim, victim_url, _ = self._victim()
        parts = [
            "python", str(tcfg.get("script", "Memory-Poisoning.py")),
            "--data_path", str(tcfg.get("data_path", "data/all_refused_combined_200.json")),
            "--target_samples", str(tcfg.get("target_samples", 200)),
            "--targets", "local",
        ]
        if victim:
            parts.extend(["--local_model", victim])
        if victim_url:
            parts.extend(["--local_url", victim_url])
        for key in ("num_strategies", "start_idx"):
            if key in tcfg:
                parts.extend([f"--{key}", str(tcfg[key])])
        if bool(tcfg.get("sequential", True)):
            parts.append("--sequential")
        return parts

    # --------------------------------------------- per-track env (redirect OpenAI client)
    def _track_env(self, attack_name: str, base_env: dict[str, str]) -> dict[str, str]:
        """Per-track env. For scripts that don't expose --planner_url / --judge_url etc.,
        we redirect the underlying OpenAI client by exporting OPENAI_BASE_URL and
        OPENAI_API_KEY for the duration of that subprocess.
        """
        env = dict(base_env)
        if attack_name == "tool_chaining":
            # Tool-chaining's attacker/planner/judge all share the OpenAI client; redirect via attacker block.
            comp = self._component_cfg("attacker") or self._component_cfg("planner")
        elif attack_name == "memory_poisoning":
            # Memory-Poisoning's planner/judge/attacker/evasiveness_judge all use the OpenAI client;
            # redirect via the planner block (judge falls back if planner is unset).
            comp = self._component_cfg("planner") or self._component_cfg("judge")
        else:
            return env
        base_url = str(comp.get("base_url", "") or "")
        api_key = str(comp.get("api_key", "") or "")
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        return env

    # ------------------------------------------------------- streaming subprocess
    @staticmethod
    def _run_streaming(
        cmd: str,
        *,
        cwd: str,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
        line_prefix: str,
    ) -> int:
        """Run ``cmd`` and tee its stdout/stderr to files AND parent stderr in
        real time so progress from the subprocess is visible."""
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            env=env,
        )

        def _drain(stream: Any, file_path: Path, tag: str) -> None:
            with open(file_path, "w", encoding="utf-8", buffering=1) as f:
                for line in iter(stream.readline, ""):
                    f.write(line)
                    if not is_quiet():
                        sys.stderr.write(f"{line_prefix}{tag} {line.rstrip()}\n")
                        sys.stderr.flush()
                    elif tag != "│":
                        log_error(f"[error] {line_prefix}{line.rstrip()}")
                stream.close()

        t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_path, "│"), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_path, "✗"), daemon=True)
        t_out.start()
        t_err.start()
        rc = proc.wait()
        t_out.join()
        t_err.join()
        return rc

    # ------------------------------------------------------------ resume helper
    @staticmethod
    def _react_trajectories_complete(attack_out: Path) -> bool:
        traj_path = attack_out / "trajectories.jsonl"
        if not traj_path.exists():
            return False
        rows = 0
        with traj_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                rows += 1
                messages = row.get("messages")
                has_error = bool(row.get("error"))
                if not has_error and (not isinstance(messages, list) or len(messages) <= 1):
                    return False
        return rows > 0

    @staticmethod
    def _track_already_done(attack_out: Path, mode: str) -> bool:
        """A track is considered already complete if its output dir contains
        at least one parseable results.json / final_results.json with a
        non-empty `results` (or summaries) array."""
        if mode == "react" and not AgentLABAdapter._react_trajectories_complete(attack_out):
            return False
        for path in attack_out.rglob("results.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("results"):
                return True
        for path in attack_out.rglob("final_results.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and (data.get("results") or data.get("statistics")):
                return True
        return False

    # ----------------------------------------------------------- output relocate
    @staticmethod
    def _capture_existing_results_dirs(root: Path) -> set[str]:
        return {p.name for p in root.glob("agentlab_results_*") if p.is_dir()}

    @staticmethod
    def _relocate_new_results(root: Path, existing: set[str], attack_out: Path) -> Path | None:
        candidates = [p for p in root.glob("agentlab_results_*") if p.is_dir() and p.name not in existing]
        if not candidates:
            return None
        # Newest by mtime.
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        target = attack_out / newest.name
        # If somehow the same name already exists in attack_out, append a suffix.
        if target.exists():
            target = attack_out / f"{newest.name}__{int(time.time())}"
        shutil.move(str(newest), str(target))
        return target

    # ------------------------------------------------------- in-process ReAct path
    def _run_track_react(
        self,
        attack_name: str,
        attack_out: Path,
        track_idx: int,
        total_tracks: int,
        max_workers: int = 1,
    ) -> dict[str, Any]:
        """Drive a track via the unified ReAct loop instead of the upstream subprocess.

        Produces ``trajectories.jsonl`` (STATIC_TRACE-shaped) and
        ``final_results.json`` in ``attack_out``.
        """
        runner = agentlab_react.REACT_TRACK_RUNNERS.get(attack_name)
        if runner is None:
            return {
                "attack": attack_name,
                "status": "skipped_no_react_adapter",
                "mode": "react",
            }
        track_cfg = self._track_cfg(attack_name)
        component_cfg = {
            "attacker": self._component_cfg("attacker"),
            "planner": self._component_cfg("planner"),
            "judge": self._component_cfg("judge"),
        }
        # Per-track override: tracks.<name>.max_workers wins over run.max_workers.
        track_workers = int(track_cfg.get("max_workers", max_workers) or 1)
        info(
            f"  [{track_idx}/{total_tracks}] {attack_name} starting "
            f"(react; in-process, max_workers={track_workers}, output written to {attack_out})"
        )
        track_t0 = time.time()
        try:
            final_results = runner(
                agentlab_root=self._root(),
                track_cfg=track_cfg,
                component_cfg=component_cfg,
                victim_cfg=self.run_cfg,
                output_dir=attack_out,
                defense=str(self.run_cfg.get("defense", "react_base") or "react_base"),
                log_prefix=f"  [{attack_name} ReAct] ",
                max_workers=track_workers,
            )
            status = "completed"
        except Exception as exc:
            log_error(f"[error] agentlab/{attack_name}: react path failed: {exc}")
            return {
                "attack": attack_name,
                "status": "failed",
                "mode": "react",
                "error": str(exc),
            }
        info(
            f"  [{track_idx}/{total_tracks}] {attack_name} {status} (react) "
            f"in {time.time() - track_t0:.1f}s"
        )
        # Surface the same shape the subprocess path emits via _parse_summaries.
        parsed = self._parse_summaries(attack_out)
        return {
            "attack": attack_name,
            "status": status,
            "mode": "react",
            "summaries": parsed,
            "react_statistics": final_results.get("statistics") if isinstance(final_results, dict) else None,
        }

    # -------------------------------------------------------------------- runner
    def run(self, agent, output_dir: Path, max_workers: int = 1) -> BenchmarkRunOutput:
        ensure_dir(output_dir)
        root = self._root()
        if not root.exists():
            metrics = {"status": "skipped_missing_data", "reason": f"AgentLAB root not found: {root}"}
            write_json(output_dir / "metrics.json", metrics)
            return BenchmarkRunOutput(self.name, output_dir, "skipped_missing_data", metrics)

        attacks = list(self.cfg.get("attacks") or _DEFAULT_TRACKS)

        base_env = os.environ.copy()
        base_env.update({str(k): str(v) for k, v in (self.cfg.get("env", {}) or {}).items()})
        base_env.setdefault("PYTHONUNBUFFERED", "1")

        resumed = bool(self.run_cfg.get("resume", False))
        run_records: list[dict[str, Any]] = []
        modes_summary = ", ".join(f"{a}={self._resolve_mode(a)}" for a in attacks)
        info(
            f"agentlab{' (resume)' if resumed else ''}: "
            f"{len(attacks)} track(s) -> {modes_summary}"
        )
        for track_idx, attack_name in enumerate(attacks, start=1):
            attack_out = output_dir / attack_name
            attack_out.mkdir(parents=True, exist_ok=True)
            mode = self._resolve_mode(attack_name)

            # Resume: if this track already produced a non-empty results file
            # in attack_out, skip rerunning and just re-parse it.
            if resumed and self._track_already_done(attack_out, mode):
                parsed = self._parse_summaries(attack_out)
                info(
                    f"  [{track_idx}/{len(attacks)}] {attack_name} skipped (resume; "
                    f"existing results found in {attack_out})"
                )
                run_records.append({
                    "attack": attack_name,
                    "command": "",
                    "returncode": 0,
                    "status": "resumed",
                    "summaries": parsed,
                })
                continue

            if mode == "react":
                record = self._run_track_react(
                    attack_name,
                    attack_out,
                    track_idx,
                    len(attacks),
                    max_workers=max_workers,
                )
                run_records.append(record)
                continue

            try:
                if attack_name == "tool_chaining":
                    parts = self._cmd_tool_chaining(attack_out)
                    needs_relocate = False
                elif attack_name == "memory_poisoning":
                    parts = self._cmd_memory_poisoning()
                    needs_relocate = True
                else:
                    run_records.append({"attack": attack_name, "status": "skipped_unknown_track"})
                    continue
            except Exception as exc:
                run_records.append({"attack": attack_name, "status": "build_failed", "reason": str(exc)})
                continue

            cmd = shlex.join(parts)
            track_env = self._track_env(attack_name, base_env)
            existing = self._capture_existing_results_dirs(root) if needs_relocate else set()
            info(f"  [{track_idx}/{len(attacks)}] {attack_name} starting (subprocess; output streamed below)")
            track_t0 = time.time()
            rc = self._run_streaming(
                cmd,
                cwd=str(root),
                env=track_env,
                stdout_path=attack_out / "stdout.txt",
                stderr_path=attack_out / "stderr.txt",
                line_prefix=f"  [{attack_name}] ",
            )
            info(
                f"  [{track_idx}/{len(attacks)}] {attack_name} "
                f"{'completed' if rc == 0 else f'failed(rc={rc})'} "
                f"in {time.time() - track_t0:.1f}s"
            )

            relocated_to: str | None = None
            if needs_relocate:
                moved = self._relocate_new_results(root, existing, attack_out)
                relocated_to = str(moved) if moved else None

            parsed = self._parse_summaries(attack_out)
            record = {
                "attack": attack_name,
                "command": cmd,
                "returncode": rc,
                "status": "completed" if rc == 0 else "failed",
                "summaries": parsed,
                "mode": "subprocess",
            }
            if relocated_to:
                record["relocated_results_dir"] = relocated_to
            run_records.append(record)

        metrics = {
            "benchmark": self.name,
            "official_standard": "AgentLAB official scripts: per-attack success rate / success count and score distributions parsed from each script's final_results.json.",
            "status": "completed",
            "datasets": {
                "tool_chaining": "data/filtered_top_200_attacks.json",
                "memory_poisoning": "data/all_refused_combined_200.json",
            },
            "runs": run_records,
            "aggregate": self._aggregate(run_records),
        }
        write_json(output_dir / "metrics.json", metrics)
        return BenchmarkRunOutput(self.name, output_dir, "completed", metrics)

    # -------------------------------------------------------------- summaries
    @staticmethod
    def _parse_summaries(out_dir: Path) -> list[dict[str, Any]]:
        summaries = []
        for path in out_dir.rglob("final_results.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                summaries.append({"path": str(path), "error": str(exc)})
                continue
            item: dict[str, Any] = {"path": str(path)}
            if isinstance(data, dict):
                if "statistics" in data and isinstance(data["statistics"], dict):
                    item.update(data["statistics"])
                if "metrics" in data and isinstance(data["metrics"], dict):
                    item.update(data["metrics"])
                if not any(k in item for k in ["asr", "rate", "successful", "success_rate"]):
                    item["keys"] = list(data.keys())
            summaries.append(item)
        return summaries

    @staticmethod
    def _aggregate(run_records: list[dict[str, Any]]) -> dict[str, Any]:
        totals = 0
        successes = 0
        asr_values: list[float] = []
        for rec in run_records:
            for s in rec.get("summaries", []):
                total = s.get("total", s.get("tested"))
                success = s.get("successful")
                if isinstance(total, (int, float)) and isinstance(success, (int, float)):
                    totals += int(total)
                    successes += int(success)
                if isinstance(s.get("asr"), (int, float)):
                    asr_values.append(float(s["asr"]))
                elif isinstance(s.get("success_rate"), (int, float)):
                    asr_values.append(float(s["success_rate"]) * 100.0)
                elif isinstance(s.get("rate"), (int, float)):
                    asr_values.append(float(s["rate"]) * 100.0)
        return {
            "total": totals,
            "successful": successes,
            "micro_asr_percent": (successes / totals * 100.0) if totals else float("nan"),
            "macro_asr_percent": (sum(asr_values) / len(asr_values)) if asr_values else float("nan"),
        }
