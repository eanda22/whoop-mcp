"""Environment configuration. Loads `.env` once on import."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"Missing required env var {name}. "
            f"Copy .env.example to .env and fill in values."
        )
    return value


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    db_path: Path
    tokens_path: Path

    def __repr__(self) -> str:
        return (
            f"Config(client_id={self.client_id!r}, client_secret='***', "
            f"redirect_uri={self.redirect_uri!r}, db_path={self.db_path!r}, "
            f"tokens_path={self.tokens_path!r})"
        )


def load_config() -> Config:
    return Config(
        client_id=_required("WHOOP_CLIENT_ID"),
        client_secret=_required("WHOOP_CLIENT_SECRET"),
        redirect_uri=_required("WHOOP_REDIRECT_URI"),
        db_path=Path(os.environ.get("WHOOP_DB_PATH", "./whoop.db")),
        tokens_path=Path(os.environ.get("WHOOP_TOKENS_PATH", "./.whoop_tokens.json")),
    )
