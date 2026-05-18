"""Local filesystem adapter using :mod:`pathlib` and :mod:`shutil`."""

from __future__ import annotations

import shutil
from pathlib import Path

from taxonomaid.domain import FileSystemError


class LocalFilesystem:
    """Concrete :class:`taxonomaid.ports.FilesystemPort` over the local disk.

    Errors are normalised to :class:`taxonomaid.domain.FileSystemError` so
    services see a stable exception surface.
    """

    def exists(self, path: Path) -> bool:
        """Return whether ``path`` exists."""
        return path.exists()

    def is_file(self, path: Path) -> bool:
        """Return whether ``path`` is a regular file."""
        return path.is_file()

    def mkdir(self, path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
        """Create ``path`` (and parents) idempotently."""
        try:
            path.mkdir(parents=parents, exist_ok=exist_ok)
        except OSError as exc:
            msg = f"failed to create directory {path}: {exc}"
            raise FileSystemError(msg) from exc

    def move(self, src: Path, dst: Path) -> None:
        """Move ``src`` to ``dst``, creating parent directories as needed.

        Refuses to overwrite an existing destination - one of the
        project's hard safety guarantees ("zero destructive moves").
        Callers should pre-compute a collision-free name via
        :func:`taxonomaid.services.path_safety.collision_free_path` so
        this guard is rarely the one that trips.
        """
        if dst.exists():
            msg = f"refusing to overwrite existing destination {dst}"
            raise FileSystemError(msg)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as exc:
            msg = f"failed to move {src} -> {dst}: {exc}"
            raise FileSystemError(msg) from exc

    def size(self, path: Path) -> int:
        """Return the byte size of ``path``."""
        try:
            return path.stat().st_size
        except OSError as exc:
            msg = f"failed to stat {path}: {exc}"
            raise FileSystemError(msg) from exc
