"""Lifetime locks on immutable public manifests; no extra on-disk state."""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import weakref
from pathlib import Path

try:
    import fcntl
except ImportError:  # Full host retention must fail closed without flock.
    fcntl = None

RELEASE_ID = re.compile(r"^[0-9a-f]{32}$")


class PublicReleaseError(ValueError):
    def __init__(self, kind="invalid_release"):
        self.kind = kind
        super().__init__(kind)


def release_directory(path: Path) -> Path:
    """Reject aliases, symlink releases and anything outside sibling releases."""
    try:
        path = Path(path).absolute()
        resolved = path.resolve(strict=True)
        if (
            not RELEASE_ID.fullmatch(path.name) or path.is_symlink() or path.parent.is_symlink()
            or resolved.parent.name != "releases"
            or path.parent.resolve(strict=True) != resolved.parent
            or not resolved.is_dir()
        ):
            raise PublicReleaseError()
        return resolved
    except OSError:
        raise PublicReleaseError() from None


def current_release(data_dir: Path) -> Path:
    try:
        data_dir = Path(data_dir)
        root = data_dir.parent.resolve(strict=True)
        releases = (root / "releases").resolve(strict=True)
        if releases.parent != root:
            raise PublicReleaseError()
        target = release_directory(data_dir.resolve(strict=True))
        if target.parent != releases:
            raise PublicReleaseError()
        marker = target / ".public_release.json"
        if not stat.S_ISREG(marker.lstat().st_mode):
            raise PublicReleaseError()
        return target
    except (OSError, PublicReleaseError):
        raise PublicReleaseError("current_unavailable") from None


class PublicReleaseLease:
    def __init__(self, release_dir, fd, *, locked):
        self.release_dir, self._fd = release_dir, fd
        self._identity = os.fstat(fd)
        self._locked = locked
        self._read_lock = threading.Lock()

    @classmethod
    def acquire(cls, release_dir: Path):
        path = release_directory(release_dir)
        fd = None
        try:
            fd = os.open(
                path / ".public_release.json",
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PublicReleaseError()
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError:
                    raise PublicReleaseError("reader_lock_busy") from None
            lease = cls(path, fd, locked=fcntl is not None)
            lease.validate()
            return lease
        except (OSError, ValueError) as exc:
            if fd is not None:
                os.close(fd)
            if isinstance(exc, PublicReleaseError):
                raise
            raise PublicReleaseError() from None

    @property
    def held(self):
        return self._fd is not None and self._locked

    def validate(self):
        """A lock on an unlinked or replaced inode does not protect this path."""
        try:
            if self._fd is None or release_directory(self.release_dir) != self.release_dir:
                raise PublicReleaseError()
            current = (self.release_dir / ".public_release.json").lstat()
            original = self._identity
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
                or (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns)
            ):
                raise PublicReleaseError()
            # pread avoids shared file-offset races between concurrent probes.
            if hasattr(os, "pread"):
                raw = os.pread(self._fd, current.st_size, 0)
            else:
                with self._read_lock:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    raw = os.read(self._fd, current.st_size)
            marker = json.loads(raw)
            if not isinstance(marker, dict) or marker.get("id") != self.release_dir.name:
                raise PublicReleaseError()
            after = (self.release_dir / ".public_release.json").lstat()
            if (
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            ):
                raise PublicReleaseError()
            return marker
        except (OSError, ValueError, TypeError):
            raise PublicReleaseError() from None

    def close(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)


def _finish_store(connection, lease):
    try:
        connection.close()
    finally:
        lease.close()


def attach_public_lease(store, lease=None):
    existing = getattr(store, "_public_release_lease", None)
    if existing is not None:
        return existing
    lease = lease or PublicReleaseLease.acquire(store.data_dir)
    store._public_release_lease = lease
    # Neither callback nor arguments retain the store or its repository.
    store._public_release_finalizer = weakref.finalize(store, _finish_store, store.db, lease)
    return lease


def open_public_repository(release_dir):
    from app.tickflow.repository import DataStore, KlineRepository

    lease = PublicReleaseLease.acquire(release_dir)
    store = None
    try:
        store = DataStore(lease.release_dir, read_only=True)
        attach_public_lease(store, lease)
        repo = KlineRepository(store)
        repo._refresh_instruments()
        repo._refresh_index_instruments()
        return repo
    except BaseException:
        if store is not None:
            store.db.close()
        lease.close()
        raise
