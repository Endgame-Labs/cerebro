"""A file a tool may return and evidence may carry: an image, a PDF, any bytes with a media type.

It is plain data. The agent run turns it into what its loop needs, so no loop type reaches a tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["File"]


@dataclass
class File:
    """A named file: its media type, and its bytes or a path to them."""

    name: str
    media_type: str
    data: bytes | None = None
    path: Path | None = None

