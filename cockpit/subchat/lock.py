"""``run.lock`` discipline for the subchat CLI.

Two ``subchat`` processes for the same ``<SessionFolder>/subchats/<name>/``
must never run concurrently — they would race on ``log/NN`` allocation,
``session.json`` writes, and any subagent-specific files (``protocol.md``,
``transcript/index.json``). The lock is the only inter-process coordination
in the subchat component; Stop hook + subchat never run at the same time
within a session (see ``cockpit/specs/architecture.md`` §5), so no
``fcntl``/``flock`` on shared files is needed.

The lock is a plain file created atomically via ``O_CREAT|O_EXCL``. Its
content is a single line ``pid=<PID> started_at=<ISO>`` for human-readable
diagnosis. Stale-lock recovery: if the existing lock's pid is not a live
process (``os.kill(pid, 0)`` raises ``ProcessLookupError``), we remove the
file and retry the atomic create exactly once. Cross-host scenarios are
out of scope per the spec.

Known limitation — SIGKILL window: if a subchat process is killed (SIGKILL,
not a regular exit) between the ``os.open(... O_CREAT|O_EXCL)`` syscall and
the pid-write that immediately follows (a few microseconds), the lock file
exists but is empty. The "unparseable-as-alive" policy then refuses to
break the lock until manual cleanup (``rm subchats/<name>/run.lock``).
Self-recovery would require either a 0-byte-as-stale heuristic (dangerous:
another in-flight subchat could be mid-write) or a write-before-create
trick (changes the atomicity guarantee). Both rejected in T05a; documented
here as accepted risk. Operationally: the recovery is one command and the
window is microseconds wide.
"""

from __future__ import annotations

import errno
import os
from datetime import datetime, timezone
from pathlib import Path

LOCK_FILENAME = "run.lock"


class LockBusy(Exception):
    """Raised when ``run.lock`` is held by a live process."""

    def __init__(self, lock_path: Path, holder_pid: int | None):
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        super().__init__(
            f"run.lock held by pid={holder_pid} at {lock_path} — see run.lock"
        )


class LockHandle:
    """RAII-style handle. Use as ``with lock(subchat_folder) as h: ...``.

    Removes the lock on context exit (normal and abnormal).
    """

    def __init__(self, path: Path):
        self.path = path
        self._owned = False

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if not self._owned:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Best-effort: a leftover lock is no worse than a crash without
            # ever-acquiring; stale-recovery handles the next invocation.
            pass
        self._owned = False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_lock_atomically(path: Path) -> None:
    """Create ``path`` atomically (``O_CREAT|O_EXCL``) and write a single
    ``pid=...`` line. Raises ``FileExistsError`` if the file already exists.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(path), flags, 0o644)
    try:
        payload = f"pid={os.getpid()} started_at={_iso_now()}\n"
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)


def _read_holder_pid(path: Path) -> int | None:
    """Parse ``pid=<N>`` out of an existing lock file. Returns ``None`` on
    any read or parse failure — caller treats unknown-holder as "alive" (we
    refuse rather than break a lock we don't understand).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for token in content.split():
        if token.startswith("pid="):
            try:
                return int(token[4:])
            except ValueError:
                return None
    return None


def _is_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` — POSIX trick for liveness.

    Semantics:
    - signal 0 sent successfully → process exists (we may or may not be
      allowed to signal it for real, but it exists).
    - ``ProcessLookupError`` (ESRCH) → process does not exist.
    - ``PermissionError`` (EPERM) → process exists but owned by someone
      else. Counts as alive — we do NOT break a lock owned by another user.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        # Defensive: unknown errno → don't presume dead.
        return e.errno != errno.ESRCH


def acquire(subchat_folder: Path, on_stale_recovered=None) -> LockHandle:
    """Acquire ``<subchat_folder>/run.lock`` atomically.

    Behaviour:
    - Success → returns an owned :class:`LockHandle` (caller uses it as a
      context manager or calls ``release()`` itself).
    - Lock exists and holder pid is alive → raises :class:`LockBusy`.
    - Lock exists but holder pid is not alive (stale) → removes the file
      and retries the atomic create exactly once. If that retry also loses
      a race (some third party grabbed the lock in between), raises
      :class:`LockBusy`.

    ``on_stale_recovered(pid)`` is invoked (if provided) when a stale lock
    is removed, so the caller can emit a ``[warn]`` event per spec.
    """
    path = subchat_folder / LOCK_FILENAME
    handle = LockHandle(path)
    try:
        _write_lock_atomically(path)
        handle._owned = True
        return handle
    except FileExistsError:
        pass

    holder = _read_holder_pid(path)
    if holder is None or _is_alive(holder):
        raise LockBusy(path, holder)

    # Stale — recover exactly once.
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    if on_stale_recovered is not None:
        try:
            on_stale_recovered(holder)
        except Exception:  # noqa: BLE001 — callback must not break recovery
            pass
    try:
        _write_lock_atomically(path)
        handle._owned = True
        return handle
    except FileExistsError:
        raise LockBusy(path, _read_holder_pid(path)) from None
