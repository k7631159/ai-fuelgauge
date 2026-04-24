"""Tests for the tray's single-instance advisory lock."""
import os

import pytest

# Tray requires optional deps — skip the whole module if unavailable
pytest.importorskip("pystray")
pytest.importorskip("PIL")


def test_lock_acquire_and_release(tmp_path, monkeypatch):
    """First acquire returns an fd; after release, a second acquire succeeds."""
    import tray

    monkeypatch.setattr(tray, "_LOCK_FILE", tmp_path / "test.lock")

    fd = tray._acquire_single_instance_lock()
    assert fd is not None, "Acquiring a fresh lock should succeed"

    os.close(fd)

    fd2 = tray._acquire_single_instance_lock()
    assert fd2 is not None, "After releasing (close), reacquire should succeed"
    os.close(fd2)


def test_lock_file_parent_created(tmp_path, monkeypatch):
    """The lock dir is auto-created if the parent path doesn't exist yet."""
    import tray

    lock = tmp_path / "nested" / "subdir" / "test.lock"
    monkeypatch.setattr(tray, "_LOCK_FILE", lock)

    fd = tray._acquire_single_instance_lock()
    try:
        assert fd is not None
        assert lock.exists()
        assert lock.parent.is_dir()
    finally:
        if fd is not None:
            os.close(fd)


def test_lock_returns_fd_integer(tmp_path, monkeypatch):
    """The acquire function returns an OS-level fd (int), not a file object."""
    import tray

    lock = tmp_path / "fd.lock"
    monkeypatch.setattr(tray, "_LOCK_FILE", lock)

    fd = tray._acquire_single_instance_lock()
    try:
        assert fd is not None
        assert isinstance(fd, int)
        # fd >= 0 — valid descriptors are non-negative on every OS
        assert fd >= 0
    finally:
        if fd is not None:
            os.close(fd)
