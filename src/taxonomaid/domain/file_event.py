"""Filesystem event domain types.

These mirror the subset of :mod:`watchfiles` events the dispatcher cares
about, but stay independent of the third-party ``watchfiles.Change``
enum so the rest of the package never depends on the watcher backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class FileEventKind(StrEnum):
    """Kinds of filesystem events the dispatcher reacts to.

    Phase 1 only handles ``ADDED``; ``MODIFIED`` is observed for
    debouncing but otherwise ignored, and ``DELETED`` is reserved for
    the destination feedback watcher in Phase 4.
    """

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class FileEvent:
    """A single filesystem event from one of the watched roots.

    Attributes:
        path: Absolute path that changed.
        kind: The kind of change.
        watch_root: The root directory under which the change was observed.
        destination_root: Root under which the dispatcher is allowed to
            place files for this event.
        unsorted_dir: Subdirectory under ``destination_root`` used as the
            tray for unresolved decisions.
    """

    path: Path
    kind: FileEventKind
    watch_root: Path
    destination_root: Path
    unsorted_dir: Path
