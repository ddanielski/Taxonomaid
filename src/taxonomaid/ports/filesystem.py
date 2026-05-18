"""Filesystem port.

Centralised so we can substitute an in-memory or recording fake during
integration tests without monkeypatching the stdlib.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class FilesystemPort(Protocol):
    """Minimal filesystem surface used by services.

    Implementations must preserve mode and ownership where the underlying
    filesystem supports it.
    """

    def exists(self, path: Path) -> bool:
        """Return ``True`` if ``path`` exists."""
        ...

    def is_file(self, path: Path) -> bool:
        """Return ``True`` if ``path`` is a regular file."""
        ...

    def mkdir(self, path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
        """Create ``path`` and any missing parents."""
        ...

    def move(self, src: Path, dst: Path) -> None:
        """Move ``src`` to ``dst``, creating parents and preserving metadata."""
        ...

    def size(self, path: Path) -> int:
        """Return the size of ``path`` in bytes."""
        ...
