"""
Shared State Module — Inter-process communication via memory-mapped files.

Provides a mmap-backed key-value store for sharing state between
the main bot process and subprocesses (e.g., Monte Carlo workers).

All data is serialized as JSON. Size-limited to prevent unbounded growth.

Only use for small, frequently-read state (current prices, signals,
health status). Not a replacement for the SQLite database.
"""

import json
import logging
import mmap
import os
import tempfile
from typing import Any, Optional

logger = logging.getLogger("shared_state")


class SharedMemoryState:
    """Inter-process shared state using a memory-mapped file.

    Parameters
    ----------
    size : int
        Size of the mmap region in bytes. Default 1 MB.
    name : str, optional
        Optional name for the backing file. If not provided, a
        temporary file is created in /dev/shm (Linux) or /tmp.

    Notes
    -----
    - Only one process should write at a time.
    - Multiple processes can read concurrently.
    - No locking is implemented (use for single-writer scenarios).
    - Data is persisted to disk only if using a named file; temporary
      files are deleted on close.
    """

    def __init__(self, size: int = 1024 * 1024, name: Optional[str] = None) -> None:
        self._size = size
        self._name = name
        self._file: Optional[int] = None
        self._mmap: Optional[mmap.mmap] = None
        self._tmp_path: Optional[str] = None

    def open(self) -> None:
        """Open (or create) the memory-mapped file."""
        if self._mmap is not None:
            return

        if self._name:
            path = self._name
            if os.path.exists(path):
                file_size = os.path.getsize(path)
            else:
                file_size = 0
            fd = os.open(path, os.O_RDWR | os.O_CREAT)
        else:
            # Use /dev/shm on Linux for tmpfs (RAM-backed)
            tmp_dir = "/dev/shm" if os.path.exists("/dev/shm") else None
            with tempfile.NamedTemporaryFile(
                dir=tmp_dir, delete=False, suffix=".pele_shared"
            ) as f:
                self._tmp_path = f.name
                fd = os.open(self._tmp_path, os.O_RDWR | os.O_CREAT)
                file_size = 0

        self._file = fd

        # Ensure file is large enough for mmap
        if file_size < self._size:
            os.ftruncate(fd, self._size)

        self._mmap = mmap.mmap(fd, self._size, access=mmap.ACCESS_WRITE)
        logger.debug(
            "SharedMemoryState opened: size=%d path=%s",
            self._size, self._tmp_path or self._name or "anonymous",
        )

    def close(self) -> None:
        """Close the memory-mapped file and clean up."""
        if self._mmap:
            self._mmap.close()
            self._mmap = None
        if self._file is not None:
            os.close(self._file)
            self._file = None
        if self._tmp_path and os.path.exists(self._tmp_path):
            try:
                os.unlink(self._tmp_path)
                logger.debug("Removed shared state temp file: %s", self._tmp_path)
            except OSError:
                pass
            self._tmp_path = None

    def read(self) -> dict[str, Any]:
        """Read the current state from shared memory.

        Returns
        -------
        dict
            Deserialized state. Empty dict if no data or parse error.
        """
        if self._mmap is None:
            self.open()

        assert self._mmap is not None
        self._mmap.seek(0)
        raw = self._mmap.read(self._size).rstrip(b"\x00").strip()

        if not raw:
            return {}

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Shared state parse error: %s — returning empty", exc)
            return {}

    def write(self, data: dict[str, Any]) -> None:
        """Write state to shared memory.

        Parameters
        ----------
        data : dict
            Serializable state dictionary. Must fit within the mmap size.
        """
        if self._mmap is None:
            self.open()

        assert self._mmap is not None
        encoded = json.dumps(data, default=str).encode("utf-8")

        if len(encoded) > self._size:
            logger.warning(
                "Shared state data too large: %d > %d — truncating",
                len(encoded), self._size,
            )
            encoded = encoded[: self._size - 1]

        self._mmap.seek(0)
        self._mmap.write(encoded.ljust(self._size)[: self._size])

    def update(self, **kwargs: Any) -> None:
        """Merge keyword arguments into existing state.

        Reads current state, merges, and writes back.

        Parameters
        ----------
        **kwargs
            Key-value pairs to merge into the state.
        """
        current = self.read()
        current.update(kwargs)
        self.write(current)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a single value from shared state.

        Parameters
        ----------
        key : str
            State key.
        default : any
            Default value if key not found.

        Returns
        -------
        any
            The value for the key, or default.
        """
        return self.read().get(key, default)

    def __enter__(self) -> "SharedMemoryState":
        self.open()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
