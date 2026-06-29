"""vLLM OpenAI-compatible embedding client used for summary similarity rewards."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import numpy as np
import requests

from .vllm_judge import env_flag, env_str


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


def _env_str_any(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        return str(value)
    return default


@dataclass
class VLLMEmbeddingConfig:
    base_urls: tuple[str, ...]
    model: str = ""
    request_timeout_s: int = 180
    max_retries: int = 3
    max_concurrency: int = 256
    batch_size: int = 32
    api_key: str = ""
    encoding_format: str = "float"


class VLLMTextEmbedder:
    def __init__(self, cfg: VLLMEmbeddingConfig):
        self.cfg = cfg
        self.base_urls = tuple(self._normalize_base_url(url) for url in cfg.base_urls if str(url).strip())
        if not self.base_urls:
            raise ValueError("No embedding base URLs provided.")
        self.endpoints = [f"{base_url}/embeddings" for base_url in self.base_urls]
        self._session_local = threading.local()
        self._headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            self._headers["Authorization"] = f"Bearer {cfg.api_key}"
        self._model = cfg.model.strip() if cfg.model else ""
        if not self._model:
            self._model = self._auto_detect_model() or ""

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        url = str(url).strip().rstrip("/")
        if not url:
            return ""
        if url.endswith("/v1/embeddings"):
            return url[: -len("/embeddings")]
        if url.endswith("/v1"):
            return url
        return f"{url}/v1"

    def _get_session(self) -> requests.Session:
        sess = getattr(self._session_local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._session_local.session = sess
        return sess

    def _auto_detect_model(self) -> Optional[str]:
        for base_url in self.base_urls:
            url = f"{base_url}/models"
            try:
                resp = self._get_session().get(url, headers=self._headers, timeout=10)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                models = data.get("data", [])
                if not models:
                    continue
                mid = models[0].get("id")
                if mid:
                    print(f"[vllm_embedding] Auto-detected model id='{mid}' from {url}")
                    return str(mid)
            except Exception:
                continue
        return None

    def _post_embeddings_with_retries(
        self,
        endpoint: str,
        texts: list[str],
    ) -> tuple[Optional[list[Optional[list[float]]]], Optional[str]]:
        payload = {
            "input": texts,
            "encoding_format": self.cfg.encoding_format,
        }
        if self._model:
            payload["model"] = self._model

        last_err: Optional[str] = None
        for attempt in range(1, max(int(self.cfg.max_retries), 1) + 1):
            try:
                resp = self._get_session().post(
                    endpoint,
                    headers=self._headers,
                    data=json.dumps(payload),
                    timeout=self.cfg.request_timeout_s,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code} body={resp.text[:300]}")
                data = resp.json().get("data", [])
                if len(data) != len(texts):
                    raise RuntimeError(
                        f"Unexpected embedding response length: got {len(data)} for {len(texts)} inputs."
                    )
                ordered = sorted(data, key=lambda x: int(x.get("index", 0)))
                embeddings: list[Optional[list[float]]] = []
                for item in ordered:
                    emb = item.get("embedding")
                    if emb is None:
                        embeddings.append(None)
                    else:
                        embeddings.append([float(x) for x in emb])
                return embeddings, None
            except Exception as e:
                last_err = f"attempt={attempt}/{self.cfg.max_retries} endpoint={endpoint} err={e}"
                if attempt < self.cfg.max_retries:
                    time.sleep(min(2 ** (attempt - 1), 8))
        return None, last_err

    def embed_texts(self, texts: list[str]) -> tuple[list[Optional[list[float]]], list[str]]:
        if len(texts) == 0:
            return [], []

        batch_size = max(int(self.cfg.batch_size), 1)
        chunks: list[tuple[int, list[str]]] = []
        for start in range(0, len(texts), batch_size):
            chunks.append((start, texts[start : start + batch_size]))

        max_workers = min(max(len(chunks), 1), max(int(self.cfg.max_concurrency), 1))
        results: list[Optional[list[float]]] = [None] * len(texts)
        errors: list[str] = []

        def _task(chunk_idx: int, start: int, chunk_texts: list[str]):
            endpoint = self.endpoints[chunk_idx % len(self.endpoints)]
            embeddings, err = self._post_embeddings_with_retries(endpoint, chunk_texts)
            return start, embeddings, err

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(_task, idx, start, chunk_texts) for idx, (start, chunk_texts) in enumerate(chunks)]
            for fut in as_completed(futs):
                start, embeddings, err = fut.result()
                if embeddings is not None:
                    for offset, emb in enumerate(embeddings):
                        results[start + offset] = emb
                elif err is not None and len(errors) < 8:
                    errors.append(err)

        return results, errors

    def similarity_batch(self, left: list[str], right: list[str]) -> tuple[list[Optional[float]], list[str]]:
        if len(left) != len(right):
            raise ValueError(f"similarity_batch expects aligned lists, got {len(left)} and {len(right)}")
        if len(left) == 0:
            return [], []

        unique_texts: list[str] = []
        text_to_index: dict[str, int] = {}
        for text in list(left) + list(right):
            if text not in text_to_index:
                text_to_index[text] = len(unique_texts)
                unique_texts.append(text)

        embeddings, errors = self.embed_texts(unique_texts)
        similarities: list[Optional[float]] = []
        for a, b in zip(left, right):
            emb_a = embeddings[text_to_index[a]]
            emb_b = embeddings[text_to_index[b]]
            if emb_a is None or emb_b is None:
                similarities.append(None)
                continue
            vec_a = np.asarray(emb_a, dtype=np.float32)
            vec_b = np.asarray(emb_b, dtype=np.float32)
            denom = float(np.linalg.norm(vec_a) * np.linalg.norm(vec_b))
            if denom <= 0:
                similarities.append(None)
                continue
            score = float(np.dot(vec_a, vec_b) / denom)
            score = (score + 1.0) * 0.5
            similarities.append(max(min(score, 1.0), 0.0))
        return similarities, errors


def build_embedder_from_env() -> Optional[VLLMTextEmbedder]:
    enabled = env_flag("predict_similarity", default=False) or env_flag("PREDICT_SIMILARITY", default=False)
    if not enabled:
        return None

    base_url_raw = (
        env_str("vllm_embedding_base_url", default="")
        or env_str("VLLM_EMBEDDING_BASE_URL", default="")
        or env_str("embedding_base_url", default="")
        or env_str("EMBEDDING_BASE_URL", default="")
    )
    if not base_url_raw:
        print("[vllm_embedding] WARN similarity reward enabled but no embedding base URL provided. Skip.")
        return None

    base_urls = tuple([part.strip() for part in base_url_raw.split(",") if part.strip()])
    api_key = (
        env_str("vllm_embedding_api_key", default="")
        or env_str("VLLM_EMBEDDING_API_KEY", default="")
        or env_str("embedding_api_key", default="")
        or env_str("EMBEDDING_API_KEY", default="")
    )

    cfg = VLLMEmbeddingConfig(
        base_urls=base_urls,
        model=(
            env_str("vllm_embedding_model", default="")
            or env_str("VLLM_EMBEDDING_MODEL", default="")
            or env_str("embedding_model", default="")
            or env_str("EMBEDDING_MODEL", default="")
        ),
        request_timeout_s=_env_int_any("vllm_embedding_timeout_s", "VLLM_EMBEDDING_TIMEOUT_S", default=180),
        max_retries=_env_int_any("vllm_embedding_max_retries", "VLLM_EMBEDDING_MAX_RETRIES", default=3),
        max_concurrency=_env_int_any(
            "vllm_embedding_max_concurrency", "VLLM_EMBEDDING_MAX_CONCURRENCY", default=256
        ),
        batch_size=_env_int_any("vllm_embedding_batch_size", "VLLM_EMBEDDING_BATCH_SIZE", default=32),
        api_key=api_key,
        encoding_format=(
            _env_str_any("vllm_embedding_encoding_format", "VLLM_EMBEDDING_ENCODING_FORMAT", default="float")
            or "float"
        ),
    )

    try:
        return VLLMTextEmbedder(cfg)
    except Exception as e:
        print(f"[vllm_embedding] ERROR failed to init VLLMTextEmbedder: {e}")
        return None
