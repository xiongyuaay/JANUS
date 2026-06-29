"""Progress reporting helpers for the unified eval runner.

Each benchmark adapter uses these to emit human-readable progress to stderr.
Falls back to plain prints when tqdm is unavailable so the eval works in any
environment.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, TypeVar

T = TypeVar("T")
_QUIET = False

try:
    from tqdm import tqdm as _tqdm  # type: ignore

    _HAS_TQDM = True
except ImportError:  # pragma: no cover - tqdm is in the project deps but be safe.
    _tqdm = None  # type: ignore
    _HAS_TQDM = False


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def set_quiet(value: bool) -> None:
    global _QUIET
    _QUIET = bool(value)


def is_quiet() -> bool:
    return _QUIET


def banner(message: str) -> None:
    """Single-line section header to stderr (does not interfere with stdout JSON)."""
    if _QUIET:
        return
    sys.stderr.write(f"\n[{_ts()}] {message}\n")
    sys.stderr.flush()


def info(message: str) -> None:
    if _QUIET:
        return
    sys.stderr.write(f"[{_ts()}] {message}\n")
    sys.stderr.flush()


def error(message: str) -> None:
    sys.stderr.write(f"[{_ts()}] {message}\n")
    sys.stderr.flush()


def progress_iter(
    iterable: Iterable[T],
    *,
    total: int | None = None,
    desc: str = "",
    leave: bool = True,
) -> Iterator[T]:
    """Wrap an iterable with a progress bar (tqdm if installed)."""
    if _HAS_TQDM:
        return _tqdm(iterable, total=total, desc=desc, leave=leave, file=sys.stderr, dynamic_ncols=True)
    return _SimpleProgress(iterable, total=total, desc=desc)


class ProgressTracker:
    """Manual progress counter for cases where iter-wrapping is awkward
    (e.g. ThreadPool as_completed loops, or callbacks from external benchmarks)."""

    def __init__(self, *, total: int | None, desc: str = "") -> None:
        self.desc = desc
        self.total = total
        self._bar = None
        if _HAS_TQDM:
            self._bar = _tqdm(total=total, desc=desc, leave=True, file=sys.stderr, dynamic_ncols=True)
        else:
            self._n = 0
            self._t0 = time.time()
            self._postfix = ""

    def update(self, n: int = 1) -> None:
        if self._bar is not None:
            self._bar.update(n)
        else:
            self._n += n
            self._render()

    def set_postfix(self, **kwargs: Any) -> None:
        if self._bar is not None:
            self._bar.set_postfix(**kwargs)
        else:
            self._postfix = " ".join(f"{k}={v}" for k, v in kwargs.items())
            self._render()

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
        else:
            sys.stderr.write("\n")
            sys.stderr.flush()

    # context manager sugar
    def __enter__(self) -> "ProgressTracker":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # fallback rendering
    def _render(self) -> None:  # pragma: no cover - fallback path
        elapsed = time.time() - self._t0
        total = self.total if self.total is not None else "?"
        line = f"\r[{self.desc}] {self._n}/{total} ({elapsed:.1f}s) {self._postfix}".rstrip()
        sys.stderr.write(line)
        sys.stderr.flush()


class _SimpleProgress:  # pragma: no cover - fallback path
    def __init__(self, iterable: Iterable[Any], *, total: int | None, desc: str) -> None:
        self._it = iter(iterable)
        self._total = total
        self._desc = desc
        self._n = 0
        self._t0 = time.time()

    def __iter__(self) -> "_SimpleProgress":
        return self

    def __next__(self) -> Any:
        try:
            value = next(self._it)
        except StopIteration:
            self._render(final=True)
            raise
        self._n += 1
        step = max(1, (self._total or 100) // 20)
        if self._n == 1 or self._n % step == 0:
            self._render()
        return value

    def _render(self, final: bool = False) -> None:
        elapsed = time.time() - self._t0
        total = self._total if self._total is not None else "?"
        suffix = "\n" if final else ""
        sys.stderr.write(f"\r[{self._desc}] {self._n}/{total} ({elapsed:.1f}s){suffix}")
        sys.stderr.flush()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Time a stage and emit start/end banners to stderr."""
    banner(f"=== {name} starting ===")
    t0 = time.time()
    status = "completed"
    try:
        yield
    except Exception:
        status = "failed"
        raise
    finally:
        info(f"=== {name} {status} in {time.time() - t0:.1f}s ===")
