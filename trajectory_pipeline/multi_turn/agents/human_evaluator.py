from __future__ import annotations

import json
from typing import Tuple


class HumanEvaluator:
    """Human approval gate."""

    def __init__(self, auto_approve: bool = False):
        self.auto_approve = auto_approve

    def evaluate(self, draft_case: dict) -> Tuple[bool, str]:
        """Approve automatically or prompt the user for pass/fail feedback."""

        if self.auto_approve:
            return True, "Auto-approved"

        print("\n" + "=" * 60)
        print("Draft case to evaluate:")
        print("=" * 60)
        print(json.dumps(draft_case, ensure_ascii=False, indent=2))
        print("=" * 60)

        while True:
            choice = input("\nChoose: [P]ass / [F]ail with feedback / [Q]uit: ").strip().upper()
            if choice == "P":
                return True, "Approved by human evaluator"
            if choice == "F":
                feedback = input("Enter suggestions for improvement: ").strip()
                return False, feedback
            if choice == "Q":
                raise KeyboardInterrupt("User aborted evaluation")
            print("Invalid choice, please retry")
