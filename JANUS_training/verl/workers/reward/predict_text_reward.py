"""Auxiliary text reward backends for Predictor warmup rewards.

This module generalizes the original embedding-based summary similarity reward and
adds two fully local alternatives:

- BLEU: lightweight sentence BLEU-4 with optional smoothing.
- NLI : local Hugging Face sequence-classification model loaded from disk.

All backends expose the same batch interface and return aligned scores in [0, 1].
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Optional, Protocol

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from .vllm_embedding import build_embedder_from_env
from .vllm_judge import env_flag


_BOOL_TRUE = {"1", "true", "yes", "y", "on", "t"}
_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[A-Za-z0-9_]+(?:'[A-Za-z0-9_]+)?|[^\s]")


class BatchTextRewardScorer(Protocol):
    backend_name: str

    def score_batch(self, left: list[str], right: list[str]) -> tuple[list[Optional[float]], list[str]]:
        ...


@dataclass
class BleuTextRewardConfig:
    max_order: int = 4
    smooth: bool = True


@dataclass
class LocalNLIConfig:
    model_path: str
    tokenizer_path: str = ""
    # "auto" means: use CUDA when the reward actor can see a GPU; otherwise CPU.
    # This prevents NLI from silently defaulting to CPU in RL warmup.
    device: str = "auto"
    # <= 0 means choose a device-aware default: large GPU batch, small CPU batch.
    batch_size: int = 0
    max_length: int = 512
    bidirectional: bool = True
    label_map: str = ""
    local_files_only: bool = True
    trust_remote_code: bool = False
    # Only applied when device == cpu and > 0. Useful when many CPU reward
    # actors are spawned so PyTorch intra-op threads do not oversubscribe cores.
    cpu_threads: int = 0
    # Do not ask the NLI model to score empty candidate/target pairs: many
    # classifiers assign arbitrary entailment probability to empty strings.
    empty_score: float = 0.0


class EmbeddingTextRewardScorer:
    backend_name = "embedding"

    def __init__(self, embedder):
        self.embedder = embedder

    def score_batch(self, left: list[str], right: list[str]) -> tuple[list[Optional[float]], list[str]]:
        return self.embedder.similarity_batch(left, right)


class BleuTextRewardScorer:
    backend_name = "bleu"

    def __init__(self, cfg: BleuTextRewardConfig):
        self.cfg = cfg

    def score_batch(self, left: list[str], right: list[str]) -> tuple[list[Optional[float]], list[str]]:
        if len(left) != len(right):
            raise ValueError(f"BLEU scorer expects aligned inputs, got {len(left)} and {len(right)}")
        return [self._sentence_bleu(str(cand), str(ref)) for cand, ref in zip(left, right)], []

    def _sentence_bleu(self, candidate: str, reference: str) -> float:
        cand_tokens = _tokenize(candidate)
        ref_tokens = _tokenize(reference)

        if not cand_tokens and not ref_tokens:
            return 1.0
        if not cand_tokens or not ref_tokens:
            return 0.0

        precisions: list[float] = []
        max_order = max(int(self.cfg.max_order), 1)
        for order in range(1, max_order + 1):
            possible = max(len(cand_tokens) - order + 1, 0)
            if possible <= 0:
                precisions.append(1.0)
                continue

            cand_counts = _extract_ngrams(cand_tokens, order)
            ref_counts = _extract_ngrams(ref_tokens, order)
            matches = 0
            for ngram, count in cand_counts.items():
                matches += min(count, ref_counts.get(ngram, 0))

            if self.cfg.smooth:
                precisions.append(float(matches + 1.0) / float(possible + 1.0))
            else:
                if matches <= 0:
                    return 0.0
                precisions.append(float(matches) / float(possible))

        ref_len = len(ref_tokens)
        cand_len = len(cand_tokens)
        if cand_len <= 0:
            return 0.0
        if cand_len > ref_len:
            bp = 1.0
        else:
            bp = math.exp(1.0 - (float(ref_len) / float(cand_len)))

        geo_mean = math.exp(sum(math.log(max(p, 1e-12)) for p in precisions) / float(max_order))
        return _clamp01(bp * geo_mean)


class LocalNLITextRewardScorer:
    backend_name = "nli"

    def __init__(self, cfg: LocalNLIConfig):
        self.cfg = cfg
        if not str(cfg.model_path).strip():
            raise ValueError(
                "PREDICT_NLI_MODEL_PATH is empty. Please provide a local Hugging Face sequence-classification model path."
            )

        self._restore_shared_cuda_visible_devices(cfg.device)
        self.device = self._resolve_device(cfg.device)
        self._configure_cpu_threads()
        if int(self.cfg.batch_size) <= 0:
            self.cfg.batch_size = self._default_batch_size_for_device(self.device)

        tokenizer_path = str(cfg.tokenizer_path).strip() or str(cfg.model_path).strip()
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(
                tokenizer_path,
                use_fast=True,
                local_files_only=cfg.local_files_only,
                trust_remote_code=cfg.trust_remote_code,
            )
        except Exception:
            self.tokenizer = AutoTokenizer.from_pretrained(
                tokenizer_path,
                use_fast=False,
                local_files_only=cfg.local_files_only,
                trust_remote_code=cfg.trust_remote_code,
            )

        self.model = AutoModelForSequenceClassification.from_pretrained(
            cfg.model_path,
            local_files_only=cfg.local_files_only,
            trust_remote_code=cfg.trust_remote_code,
        )
        self.model.to(self.device)
        self.model.eval()
        self.entailment_id, self.contradiction_id = self._resolve_label_ids(cfg.label_map)
        self._validate_label_ids()
        print(
            "[predict_text_reward:nli] "
            f"device={self.device} batch_size={self.cfg.batch_size} "
            f"max_length={self.cfg.max_length} bidirectional={self.cfg.bidirectional} "
            f"cuda_visible={os.getenv('CUDA_VISIBLE_DEVICES', '')!r} "
            f"worker={os.getenv('REWARD_WORKER_INDEX', '')}"
        )

    @staticmethod
    def _restore_shared_cuda_visible_devices(device_raw: str) -> None:
        raw = str(device_raw or "auto").strip().lower()
        if raw == "cpu":
            return
        assigned = str(os.getenv("REWARD_WORKER_DEVICE", "")).strip()
        current = str(os.getenv("CUDA_VISIBLE_DEVICES", "")).strip()
        # Ray may clear CUDA_VISIBLE_DEVICES for num_gpus=0 actors. In shared-GPU
        # reward mode the launcher/main.py passes REWARD_WORKER_DEVICE explicitly;
        # restore it before the first torch.cuda query so NLI can actually use GPU.
        if assigned and current in {"", "NoDevFiles", "none", "None"}:
            os.environ["CUDA_VISIBLE_DEVICES"] = assigned

    def _configure_cpu_threads(self) -> None:
        if self.device.type != "cpu":
            return
        try:
            threads = int(getattr(self.cfg, "cpu_threads", 0) or 0)
        except Exception:
            threads = 0
        if threads <= 0:
            return
        try:
            torch.set_num_threads(max(1, threads))
        except Exception:
            pass
        try:
            torch.set_num_interop_threads(max(1, min(threads, 4)))
        except Exception:
            # set_num_interop_threads can only be called before parallel work starts.
            pass

    @staticmethod
    def _default_batch_size_for_device(device: torch.device) -> int:
        # NLI is the critical path between rollout and update. Use a large GPU
        # batch by default; OOM is handled by recursive splitting below.
        if device.type == "cuda":
            return 256
        return 16

    @staticmethod
    def _resolve_device(device_raw: str) -> torch.device:
        raw = str(device_raw or "auto").strip().lower()
        if raw in {"", "auto", "gpu"}:
            raw = "cuda" if torch.cuda.is_available() else "cpu"
        if raw.startswith("cuda"):
            if not torch.cuda.is_available():
                print("[predict_text_reward:nli] WARN CUDA requested but unavailable; falling back to CPU.")
                return torch.device("cpu")
            try:
                dev = torch.device(raw)
            except Exception:
                print(f"[predict_text_reward:nli] WARN invalid CUDA device='{device_raw}', using cuda:0.")
                return torch.device("cuda:0")
            if dev.index is not None and dev.index >= torch.cuda.device_count():
                print(
                    f"[predict_text_reward:nli] WARN device='{device_raw}' is outside visible "
                    f"CUDA device count={torch.cuda.device_count()}; using cuda:0."
                )
                return torch.device("cuda:0")
            return dev
        try:
            return torch.device(raw)
        except Exception:
            print(f"[predict_text_reward:nli] WARN invalid device='{device_raw}', falling back to CPU.")
            return torch.device("cpu")

    def _resolve_label_ids(self, label_map_raw: str) -> tuple[int, Optional[int]]:
        explicit = _parse_label_map(label_map_raw)
        discovered: dict[str, int] = {}

        label2id = getattr(self.model.config, "label2id", None) or {}
        for label, idx in label2id.items():
            canonical = _canonical_label_name(label)
            if canonical is not None:
                try:
                    discovered[canonical] = int(idx)
                except Exception:
                    continue

        id2label = getattr(self.model.config, "id2label", None) or {}
        for idx, label in id2label.items():
            canonical = _canonical_label_name(label)
            if canonical is not None:
                try:
                    discovered.setdefault(canonical, int(idx))
                except Exception:
                    continue

        mapping = {**discovered, **explicit}
        entailment_id = mapping.get("entailment", None)
        contradiction_id = mapping.get("contradiction", None)
        if entailment_id is None:
            raise ValueError(
                "Cannot infer entailment label id from the NLI model config. "
                "Please set PREDICT_NLI_LABEL_MAP, e.g. 'contradiction:0,neutral:1,entailment:2'."
            )
        return int(entailment_id), None if contradiction_id is None else int(contradiction_id)

    def _validate_label_ids(self) -> None:
        num_labels = int(getattr(self.model.config, "num_labels", 0) or 0)
        if num_labels <= 0:
            try:
                num_labels = int(getattr(self.model, "num_labels", 0) or 0)
            except Exception:
                num_labels = 0
        if num_labels <= 0:
            # Some custom heads do not expose num_labels. In that case the first
            # forward pass still validates indexes naturally, but standard HF
            # classifiers should be caught here.
            return

        ids = {"entailment": self.entailment_id}
        if self.contradiction_id is not None:
            ids["contradiction"] = self.contradiction_id

        for name, idx in ids.items():
            if idx < 0 or idx >= num_labels:
                raise ValueError(
                    f"NLI label id for {name}={idx} is outside model num_labels={num_labels}. "
                    "Check PREDICT_NLI_LABEL_MAP and the model config."
                )

    def _direction_score_from_probs(self, probs: torch.Tensor) -> float:
        entailment_prob = float(probs[self.entailment_id].item())
        if self.contradiction_id is not None:
            contradiction_prob = float(probs[self.contradiction_id].item())
            return _clamp01(max(entailment_prob - contradiction_prob, 0.0))
        return _clamp01(entailment_prob)

    def score_batch(self, left: list[str], right: list[str]) -> tuple[list[Optional[float]], list[str]]:
        if len(left) != len(right):
            raise ValueError(f"NLI scorer expects aligned inputs, got {len(left)} and {len(right)}")
        if len(left) == 0:
            return [], []

        pair_owner: list[int] = []
        premises: list[str] = []
        hypotheses: list[str] = []
        per_example_scores: list[list[float]] = [[] for _ in range(len(left))]

        for idx, (candidate, target) in enumerate(zip(left, right)):
            candidate = str(candidate or "").strip()
            target = str(target or "").strip()
            if not candidate or not target:
                per_example_scores[idx].append(_clamp01(float(self.cfg.empty_score)))
                continue

            premises.append(candidate)
            hypotheses.append(target)
            pair_owner.append(idx)
            if self.cfg.bidirectional:
                premises.append(target)
                hypotheses.append(candidate)
                pair_owner.append(idx)

        errors: list[str] = []

        def _score_range(start: int, end: int, allow_split: bool = True) -> None:
            try:
                encoded = self.tokenizer(
                    premises[start:end],
                    hypotheses[start:end],
                    padding=True,
                    truncation=True,
                    max_length=max(int(self.cfg.max_length), 8),
                    return_tensors="pt",
                )
                encoded = {k: v.to(self.device, non_blocking=(self.device.type == "cuda")) for k, v in encoded.items()}
                logits = self.model(**encoded).logits
                probs = torch.softmax(logits, dim=-1).detach().cpu()
                for offset, prob in enumerate(probs):
                    owner = pair_owner[start + offset]
                    per_example_scores[owner].append(self._direction_score_from_probs(prob))
            except Exception as e:
                if self.device.type == "cuda":
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass

                # A too-large NLI batch should not invalidate every example in the
                # batch. Split recursively instead of retrying every item singly;
                # single-item retry destroys GPU utilization after one oversized
                # batch or transient CUDA memory spike.
                if allow_split and end - start > 1:
                    mid = start + max((end - start) // 2, 1)
                    if len(errors) < 8:
                        errors.append(f"batch={start}:{end} err={e}; retrying split {start}:{mid} and {mid}:{end}")
                    _score_range(start, mid, allow_split=True)
                    _score_range(mid, end, allow_split=True)
                elif len(errors) < 8:
                    errors.append(f"batch={start}:{end} err={e}")

        batch_size = max(int(self.cfg.batch_size), 1)
        with torch.inference_mode():
            for start in range(0, len(premises), batch_size):
                end = min(start + batch_size, len(premises))
                _score_range(start, end, allow_split=True)

        final_scores: list[Optional[float]] = []
        for vals in per_example_scores:
            if not vals:
                final_scores.append(None)
            else:
                final_scores.append(_clamp01(sum(vals) / float(len(vals))))
        return final_scores, errors


def resolve_predict_text_reward_type_from_env() -> str:
    raw = _env_str_any(
        "predict_text_reward_type",
        "PREDICT_TEXT_REWARD_TYPE",
        "predict_aux_reward_type",
        "PREDICT_AUX_REWARD_TYPE",
        default="",
    ).strip()
    if raw:
        return _normalize_backend_name(raw)
    if env_flag("predict_similarity", default=False) or env_flag("PREDICT_SIMILARITY", default=False):
        return "embedding"
    return "none"


def build_predict_text_reward_from_env() -> tuple[str, Optional[BatchTextRewardScorer]]:
    backend = resolve_predict_text_reward_type_from_env()
    if backend == "none":
        return backend, None

    # NLI is expensive and easy to misconfigure. By default we fail fast for NLI
    # instead of silently switching to utility-only training. Other backends keep
    # the previous best-effort behavior unless PREDICT_TEXT_REWARD_STRICT is set.
    strict = _env_flag_any(
        "predict_text_reward_strict",
        "PREDICT_TEXT_REWARD_STRICT",
        default=(backend == "nli"),
    )

    try:
        if backend == "embedding":
            embedder = build_embedder_from_env()
            if embedder is None:
                if strict:
                    raise RuntimeError("embedding backend is selected but build_embedder_from_env() returned None")
                return backend, None
            return backend, EmbeddingTextRewardScorer(embedder)

        if backend == "bleu":
            cfg = BleuTextRewardConfig(
                max_order=_env_int_any("predict_bleu_max_order", "PREDICT_BLEU_MAX_ORDER", default=4),
                smooth=_env_flag_any("predict_bleu_smooth", "PREDICT_BLEU_SMOOTH", default=True),
            )
            return backend, BleuTextRewardScorer(cfg)

        if backend == "nli":
            cfg = LocalNLIConfig(
                model_path=_env_str_any("predict_nli_model_path", "PREDICT_NLI_MODEL_PATH", default=""),
                tokenizer_path=_env_str_any(
                    "predict_nli_tokenizer_path",
                    "PREDICT_NLI_TOKENIZER_PATH",
                    default="",
                ),
                device=_env_str_any("predict_nli_device", "PREDICT_NLI_DEVICE", default="auto"),
                batch_size=_env_int_any("predict_nli_batch_size", "PREDICT_NLI_BATCH_SIZE", default=0),
                max_length=_env_int_any("predict_nli_max_length", "PREDICT_NLI_MAX_LENGTH", default=512),
                bidirectional=_env_flag_any(
                    "predict_nli_bidirectional",
                    "PREDICT_NLI_BIDIRECTIONAL",
                    default=True,
                ),
                label_map=_env_str_any("predict_nli_label_map", "PREDICT_NLI_LABEL_MAP", default=""),
                local_files_only=_env_flag_any(
                    "predict_nli_local_files_only",
                    "PREDICT_NLI_LOCAL_FILES_ONLY",
                    default=True,
                ),
                trust_remote_code=_env_flag_any(
                    "predict_nli_trust_remote_code",
                    "PREDICT_NLI_TRUST_REMOTE_CODE",
                    default=False,
                ),
                cpu_threads=_env_int_any("predict_nli_cpu_threads", "PREDICT_NLI_CPU_THREADS", default=0),
                empty_score=_env_float_any(
                    "predict_nli_empty_score",
                    "PREDICT_NLI_EMPTY_SCORE",
                    default=0.0,
                ),
            )
            return backend, LocalNLITextRewardScorer(cfg)

        msg = f"unsupported backend='{backend}'"
        if strict:
            raise ValueError(msg)
        print(f"[predict_text_reward] WARN {msg}, disabling auxiliary text reward.")
        return "none", None
    except Exception as e:
        print(f"[predict_text_reward] ERROR failed to initialize backend='{backend}': {e}")
        if strict:
            raise RuntimeError(f"Failed to initialize predictor text reward backend '{backend}'.") from e
        return backend, None


def _normalize_backend_name(raw: str) -> str:
    value = str(raw).strip().lower()
    alias = {
        "": "none",
        "0": "none",
        "false": "none",
        "off": "none",
        "disable": "none",
        "disabled": "none",
        "none": "none",
        "embed": "embedding",
        "embedding": "embedding",
        "similarity": "embedding",
        "sim": "embedding",
        "bleu": "bleu",
        "bleu4": "bleu",
        "nli": "nli",
        "mnli": "nli",
    }
    return alias.get(value, value)


def _env_str_any(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        text = str(value)
        # Empty lower-case env vars are common when launcher scripts mirror both
        # cases. Treat them as unset so they do not mask an upper-case value or
        # the auto CUDA default.
        if text.strip() == "":
            continue
        return text
    return default


def _env_int_any(*names: str, default: int) -> int:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        try:
            return int(str(value).strip())
        except Exception:
            continue
    return default


def _env_float_any(*names: str, default: float) -> float:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        try:
            return float(str(value).strip())
        except Exception:
            continue
    return default


def _env_flag_any(*names: str, default: bool = False) -> bool:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        text = str(value).strip().lower()
        if text == "":
            continue
        return text in _BOOL_TRUE
    return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").strip().lower())


def _extract_ngrams(tokens: list[str], order: int) -> dict[tuple[str, ...], int]:
    counts: dict[tuple[str, ...], int] = {}
    if order <= 0:
        return counts
    upper = len(tokens) - order + 1
    for idx in range(max(upper, 0)):
        ng = tuple(tokens[idx : idx + order])
        counts[ng] = counts.get(ng, 0) + 1
    return counts


def _canonical_label_name(label: str) -> Optional[str]:
    name = str(label or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not name:
        return None
    if "contra" in name:
        return "contradiction"
    if "neutral" in name:
        return "neutral"
    if "non_entail" in name or "not_entail" in name or "nonentail" in name:
        return "non_entailment"
    if "entail" in name and "non" not in name and "not" not in name:
        return "entailment"
    return None


def _parse_label_map(raw: str) -> dict[str, int]:
    text = str(raw or "").strip()
    if not text:
        return {}

    items: list[tuple[str, str]] = []
    if text.startswith("{"):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("PREDICT_NLI_LABEL_MAP JSON must be an object like {'entailment': 2}")
        items = [(str(k), str(v)) for k, v in parsed.items()]
    else:
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" not in part:
                raise ValueError(
                    "PREDICT_NLI_LABEL_MAP must use 'label:id' items, e.g. 'contradiction:0,neutral:1,entailment:2'."
                )
            key, value = part.split(":", 1)
            items.append((key.strip(), value.strip()))

    out: dict[str, int] = {}
    for key, value in items:
        canonical = _canonical_label_name(key)
        if canonical is None:
            continue
        out[canonical] = int(value)
    return out
