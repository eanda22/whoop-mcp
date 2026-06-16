"""Local MCP server for personal WHOOP data."""

from typing import TYPE_CHECKING

__version__ = "0.1.0"
__all__ = ["WhoopClient", "__version__"]

if TYPE_CHECKING:
    from .client import WhoopClient


def __getattr__(name: str):
    if name == "WhoopClient":
        from .client import WhoopClient

        return WhoopClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
