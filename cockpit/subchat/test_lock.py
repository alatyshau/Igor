#!/usr/bin/env python3
"""Unit tests for ``lock.py``.

Run from this directory:
    python3 -m unittest test_lock.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lock as lockmod  # noqa: E402


class TestAcquireRelease(unittest.TestCase):

    def test_acquire_creates_run_lock_with_pid_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp)
            h = lockmod.acquire(sub)
            self.assertTrue((sub / "run.lock").is_file())
            content = (sub / "run.lock").read_text()
            self.assertIn(f"pid={os.getpid()}", content)
            self.assertIn("started_at=", content)
            h.release()
            self.assertFalse((sub / "run.lock").exists())

    def test_context_manager_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp)
            with lockmod.acquire(sub):
                self.assertTrue((sub / "run.lock").is_file())
            self.assertFalse((sub / "run.lock").exists())

    def test_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp)
            h = lockmod.acquire(sub)
            h.release()
            h.release()  # no-op, no error
            self.assertFalse((sub / "run.lock").exists())

    def test_concurrent_acquire_raises_lockbusy_when_holder_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp)
            # Hold the lock with our own (live) pid.
            h1 = lockmod.acquire(sub)
            try:
                with self.assertRaises(lockmod.LockBusy) as ctx:
                    lockmod.acquire(sub)
                self.assertEqual(ctx.exception.holder_pid, os.getpid())
            finally:
                h1.release()


class TestStaleRecovery(unittest.TestCase):

    def test_stale_lock_with_dead_pid_recovered_and_callback_invoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp)
            # Use a pid that is overwhelmingly unlikely to be alive.
            dead_pid = 999_999
            (sub / "run.lock").write_text(
                f"pid={dead_pid} started_at=2000-01-01T00:00:00+00:00\n",
                encoding="utf-8",
            )
            recovered = []
            h = lockmod.acquire(sub, on_stale_recovered=recovered.append)
            self.assertEqual(recovered, [dead_pid])
            self.assertTrue((sub / "run.lock").is_file())
            self.assertIn(f"pid={os.getpid()}", (sub / "run.lock").read_text())
            h.release()

    def test_unparseable_lock_treated_as_alive_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp)
            # No pid= token → unknown holder → must refuse rather than break it.
            (sub / "run.lock").write_text("garbage\n", encoding="utf-8")
            with self.assertRaises(lockmod.LockBusy):
                lockmod.acquire(sub)
            # Lock untouched.
            self.assertTrue((sub / "run.lock").is_file())

    def test_stale_recovery_only_one_retry(self):
        # If somebody races and recreates the lock between unlink and our
        # retry, we surface LockBusy rather than loop.
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp)
            dead_pid = 999_999
            (sub / "run.lock").write_text(
                f"pid={dead_pid} started_at=2000-01-01T00:00:00+00:00\n",
                encoding="utf-8",
            )

            real_open = os.open

            attempts = {"n": 0}

            def fake_open(path, flags, mode=0o777):
                # Simulate: first O_CREAT|O_EXCL fails with EEXIST (the
                # existing stale lock), second attempt also fails (race).
                if (flags & os.O_EXCL) and (flags & os.O_CREAT):
                    attempts["n"] += 1
                    if attempts["n"] <= 2:
                        # Recreate the lock between calls to simulate a racer
                        # who grabbed it after we unlinked the stale one.
                        if attempts["n"] == 2:
                            Path(path).write_text(
                                "pid=999998 started_at=2000-01-01T00:00:00+00:00\n",
                                encoding="utf-8",
                            )
                        raise FileExistsError(17, "exists", str(path))
                return real_open(path, flags, mode)

            with mock.patch("os.open", side_effect=fake_open):
                with self.assertRaises(lockmod.LockBusy):
                    lockmod.acquire(sub)


class TestPidHelpers(unittest.TestCase):

    def test_is_alive_self(self):
        self.assertTrue(lockmod._is_alive(os.getpid()))

    def test_is_alive_unlikely_pid(self):
        # 999_999 — very unlikely to exist on a typical dev machine.
        self.assertFalse(lockmod._is_alive(999_999))

    def test_read_holder_pid_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "run.lock"
            p.write_text("pid=12345 started_at=2026-05-24T00:00:00+00:00\n")
            self.assertEqual(lockmod._read_holder_pid(p), 12345)

    def test_read_holder_pid_returns_none_on_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "run.lock"
            p.write_text("garbage\n")
            self.assertIsNone(lockmod._read_holder_pid(p))


if __name__ == "__main__":
    unittest.main()
