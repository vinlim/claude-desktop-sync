"""Putting bytes on disk: whole, private, and never half-visible.

Staging and committing are separate steps so a caller can run its last guard in between,
after the slow part (write and sync) and right before the rename.
"""
import os
from pathlib import Path
from typing import Optional

# The app ignores names that start with neither local_ nor deleted_, and promotes
# orphaned local_*.json.tmp files to live records, so a temp name must never look like one.
TEMP_PREFIX = ".sync-"
TEMP_SUFFIX = ".part"


def stage(destination: Path, data: bytes, mtime_ns: Optional[int] = None) -> Path:
    """Writes the bytes beside the destination under a temporary name and returns that path."""
    temporary = destination.parent / ("%s%d-%s%s" % (TEMP_PREFIX, os.getpid(), destination.name, TEMP_SUFFIX))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mtime_ns is not None:
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
    except BaseException:
        discard(temporary)
        raise
    return temporary


def commit_replace(temporary: Path, destination: Path) -> None:
    """Replaces whatever is at the destination."""
    try:
        os.replace(temporary, destination)
    except BaseException:
        discard(temporary)
        raise


def commit_create(temporary: Path, destination: Path) -> None:
    """Creates the destination or raises FileExistsError.

    A rename would silently replace a file that appeared since the caller looked.
    A hard link cannot: it fails if the name is taken.
    """
    try:
        os.link(temporary, destination)
    finally:
        discard(temporary)


def discard(temporary: Path) -> None:
    try:
        os.unlink(temporary)
    except OSError:
        pass


def write_atomic(destination: Path, data: bytes, mtime_ns: Optional[int] = None) -> None:
    commit_replace(stage(destination, data, mtime_ns), destination)


def create_exclusive(destination: Path, data: bytes, mtime_ns: Optional[int] = None) -> None:
    commit_create(stage(destination, data, mtime_ns), destination)
