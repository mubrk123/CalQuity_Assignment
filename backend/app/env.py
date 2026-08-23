"""Load backend/.env into the process environment."""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path = ENV_FILE) -> list[str]:
    """Read KEY=VALUE lines from `path`. Returns the names that were set."""
    if not path.exists():
        return []

    applied: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied


# Existing environment variables always win over .env.
LOADED = load_env()
