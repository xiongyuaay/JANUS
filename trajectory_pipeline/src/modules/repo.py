from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .log import Log


class Repo:
    """Store approved cases as individual JSON files."""

    def __init__(self, storage_dir: str = "approved_cases"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.cases = self._load_cases()

    def _load_cases(self) -> List[dict]:
        cases: List[dict] = []
        for path in sorted(self.storage_dir.glob("case_*.json")):
            try:
                cases.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                Log.warning(f"Failed to read case file: {path}", "Repo")
        return cases

    def _next_id(self) -> int:
        ids = []
        for path in self.storage_dir.glob("case_*.json"):
            try:
                ids.append(int(path.stem.split("_")[1]))
            except Exception:
                continue
        return max(ids) + 1 if ids else 1

    def save_case(self, case: dict) -> int:
        """Persist a case and return its assigned numeric identifier."""

        case_id = self._next_id()
        case["case_id"] = case_id
        path = self.storage_dir / f"case_{case_id}.json"
        path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cases.append(case)
        Log.success(f"Case saved: {path}", "Repo")
        return case_id

    def get_all_cases(self) -> List[dict]:
        """Return the in-memory list of previously saved cases."""

        return self.cases
