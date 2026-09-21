"""Two ways to put bytes on disk: whole, private, and never half-visible."""
import os
from pathlib import Path
from typing import Optional

# The app ignores names that start with neither local_ nor deleted_, and promotes
# orphaned local_*.json.tmp files to live records, so a temp name must never look like one.
TEMP_PREFIX = ".sync-"
TEMP_SUFFIX = ".part"


def write_atomic(destination: Path, data: bytes, mtime_ns: Optional[int] = None) -> None:
    """Replaces whatever is at the destination."""
    temporary = _write_temporary(destination, data, mtime_ns)
    try:
        os.replace(temporary, destination)
    except BaseException:
        _discard(temporary)
        raise


def create_exclusive(destination: Path, data: bytes, mtime_ns: Optional[int] = None) -> None:
    """Creates the destination or raises FileExistsError.

    A rename would silently replace a file that appeared since the caller looked.
    A hard link cannot: it fails if the name is taken.
    """
    temporary = _write_temporary(destination, data, mtime_ns)
    try:
        os.link(temporary, destination)
    finally:
        _discard(temporary)


def _write_temporary(destination: Path, data: bytes, mtime_ns: Optional[int]) -> Path:
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
        _discard(temporary)
        raise
    return temporary


def _discard(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
