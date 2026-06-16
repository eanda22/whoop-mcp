"""Token persistence — single source of truth for WHOOP OAuth tokens.

WHOOP refresh tokens ROTATE. Every refresh response carries a new refresh
token and invalidates the old one. All persistence goes through this module
so the rotation invariant lives in exactly one place.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field


class Tokens(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: datetime  # UTC
    token_type: str = "bearer"
    scope: str | None = None

    def is_expired(self, skew_seconds: int = 60) -> bool:
        now = datetime.now(tz=timezone.utc)
        return (self.expires_at - now).total_seconds() <= skew_seconds


def load(path: Path) -> Tokens | None:
    if not path.exists():
        return None
    return Tokens.model_validate_json(path.read_text())


def save(tokens: Tokens, path: Path) -> None:
    """Atomic write with mode 0600. Survives crashes mid-refresh."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = tokens.model_dump_json(indent=2).encode("utf-8")

    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(
        tmp,
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
