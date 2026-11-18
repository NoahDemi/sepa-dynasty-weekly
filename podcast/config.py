"""Config + path helpers."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p) as f:
        cfg = yaml.safe_load(f)
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:  # dotenv is optional (CI injects env vars directly)
        pass
    return cfg


def snapshot_path(week: int) -> Path:
    return DATA / f"week_{week:02d}.json"


def facts_path(week: int) -> Path:
    return DATA / "facts" / f"week_{week:02d}.json"


def script_path(week: int) -> Path:
    return DATA / "scripts" / f"week_{week:02d}.json"
