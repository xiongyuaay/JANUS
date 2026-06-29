from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional


class Log:
    """Minimal colored logger."""

    DEBUG_DIR: Optional[Path] = None

    COLORS = {
        "RED": "\033[91m",
        "GREEN": "\033[92m",
        "YELLOW": "\033[93m",
        "BLUE": "\033[94m",
        "MAGENTA": "\033[95m",
        "CYAN": "\033[96m",
        "WHITE": "\033[97m",
        "RESET": "\033[0m",
        "BOLD": "\033[1m",
    }

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%H:%M:%S")

    @classmethod
    def info(cls, msg: str, agent: str = "System") -> None:
        print(
            f"{cls.COLORS['CYAN']}[{cls._timestamp()}]{cls.COLORS['RESET']} "
            f"{cls.COLORS['BOLD']}[{agent}]{cls.COLORS['RESET']} {msg}"
        )

    @classmethod
    def success(cls, msg: str, agent: str = "System") -> None:
        print(f"{cls.COLORS['GREEN']}[{cls._timestamp()}] [OK] [{agent}]{cls.COLORS['RESET']} {msg}")

    @classmethod
    def warning(cls, msg: str, agent: str = "System") -> None:
        print(f"{cls.COLORS['YELLOW']}[{cls._timestamp()}] [WARN] [{agent}]{cls.COLORS['RESET']} {msg}")

    @classmethod
    def error(cls, msg: str, agent: str = "System") -> None:
        print(f"{cls.COLORS['RED']}[{cls._timestamp()}] [ERROR] [{agent}]{cls.COLORS['RESET']} {msg}")

    @classmethod
    def section(cls, title: str) -> None:
        print(f"\n{cls.COLORS['BLUE']}{'-' * 60}")
        print(f"  {title}")
        print(f"{'-' * 60}{cls.COLORS['RESET']}")

    @classmethod
    def step(cls, step_num: int, total: int, msg: str) -> None:
        print(f"\n{cls.COLORS['MAGENTA']}{'=' * 60}")
        print(f"Step {step_num}/{total}: {msg}")
        print(f"{'=' * 60}{cls.COLORS['RESET']}\n")

    @classmethod
    def json_preview(cls, data: dict, max_length: int = 500) -> None:
        preview = json.dumps(data, ensure_ascii=False, indent=2)
        if len(preview) > max_length:
            preview = preview[:max_length] + "\n... [truncated]"
        print(f"{cls.COLORS['WHITE']}{preview}{cls.COLORS['RESET']}")

    @classmethod
    def set_debug_dir(cls, path: Path) -> None:
        """Configure a directory for debug artifacts."""

        path.mkdir(parents=True, exist_ok=True)
        cls.DEBUG_DIR = path

    @classmethod
    def dump_text(cls, filename: str, content: str, agent: str = "System") -> Optional[Path]:
        """Write debug text content to the configured directory."""

        if cls.DEBUG_DIR is None:
            return None
        path = cls.DEBUG_DIR / filename
        path.write_text(content, encoding="utf-8")
        cls.warning(f"Debug output written to {path}", agent)
        return path
