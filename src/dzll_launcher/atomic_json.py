from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: str | os.PathLike[str], data: object, **json_options) -> None:
    """Serialize JSON completely, then atomically replace the destination."""
    payload = json.dumps(data, **json_options).encode("utf-8")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    fd = -1
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            written = handle.write(payload)
            if written != len(payload):
                raise OSError(
                    f"short JSON write for {destination}: "
                    f"wrote {written!r} of {len(payload)} bytes"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory_best_effort(destination.parent)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _fsync_directory_best_effort(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = -1
    try:
        fd = os.open(directory, flags)
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
