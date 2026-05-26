#!/usr/bin/env python3
"""``subchat`` — generic CLI wrapper for headless ``claude -p`` subagents.

Reads the per-subagent config + session state from
``<SessionFolder>/subchats/<subagent>/``, runs ``claude -p`` with the
appropriate flags / cwd / env, captures full output to ``log/NN/``, and
streams tagged progress events (``[start]``, ``[claude-spawn]``,
``[tool]``, ``[assistant]``, ``[heartbeat]``, ``[warn]``, ``[error]``,
``[complete]``) to its own stdout — those events are what the main agent
sees via the Monitor tool.

Source-of-truth spec: ``cockpit/specs/subchat.md``. Related: session
folder layout in ``cockpit/specs/schemas/session_folder.md``.

Usage:
    subchat --subagent <name> --msg "<text>" [--session-folder <path>]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make sibling modules importable when the file is invoked as a script
# (without a packaged install). Mirrors hooks/test_session_bootstrap.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config, ConfigError, DEFAULTS, load as load_config  # noqa: E402
from lock import LockBusy, acquire as acquire_lock  # noqa: E402
from stream_parser import StreamParseResult, parse_line  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOG_FOLDER = "log"
SESSION_JSON_NAME = "session.json"
CONFIG_FILENAME = "config.yaml"
MSG_TRUNCATE_LEN = 120  # max chars of --msg shown in [start] event
HEARTBEAT_INTERVAL_S = 10  # only used when output-format == "json"
LOG_NN_WIDTH = 2  # zero-pad width for ``log/NN/`` folder names
STDERR_TAIL_TRUNCATE_LEN = 200  # max chars of stderr tail in [warn] on non-zero exit

# Exit code conventions — propagate claude's code when it is non-zero;
# our own internal-failure code is a fixed value below.
EXIT_SUBCHAT_INTERNAL = 64
EXIT_LOCK_BUSY = 65


# ---------------------------------------------------------------------------
# Event emission (subchat -> its own stdout)
# ---------------------------------------------------------------------------


class Emitter:
    """Thread-safe single-line emitter for tagged progress events.

    The lock matters because heartbeat runs in a background thread.
    """

    def __init__(self, out=None):
        self._out = out if out is not None else sys.stdout
        self._lock = threading.Lock()
        self._last_emit_monotonic = time.monotonic()

    def emit(self, tag: str, payload: str) -> None:
        line = f"[{tag}] {payload}"
        with self._lock:
            self._last_emit_monotonic = time.monotonic()
            print(line, file=self._out, flush=True)

    def seconds_since_last(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_emit_monotonic


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


@dataclass
class Args:
    subagent: str
    msg: str
    session_folder: Path


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        prog="subchat",
        description="Run a headless claude -p subagent under a SessionFolder.",
    )
    parser.add_argument("--subagent", required=True, help="subagent profile name")
    parser.add_argument("--msg", required=True, help="message text to deliver")
    parser.add_argument(
        "--session-folder",
        default=None,
        help="absolute SessionFolder path; defaults to cwd",
    )
    ns = parser.parse_args(argv)
    sf = Path(os.path.abspath(ns.session_folder)) if ns.session_folder else Path.cwd()
    return Args(subagent=ns.subagent, msg=ns.msg, session_folder=sf)


# ---------------------------------------------------------------------------
# Time / path helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# log/NN allocation (caller must hold run.lock)
# ---------------------------------------------------------------------------


def allocate_run_folder(subchat_folder: Path) -> Path:
    """Allocate the next ``log/NN/`` folder under ``subchat_folder``.

    Caller MUST hold ``run.lock`` — without the lock this is racy. The
    spec explicitly forbids unlocked allocation.

    Strategy: list ``log/`` entries that look like zero-padded numeric
    folder names, pick ``max + 1``, create via ``os.makedirs(exist_ok=False)``.
    """
    log_root = subchat_folder / LOG_FOLDER
    log_root.mkdir(parents=True, exist_ok=True)
    max_n = 0
    for entry in log_root.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name.isdigit():
            try:
                n = int(name)
            except ValueError:
                continue
            if n > max_n:
                max_n = n
    next_n = max_n + 1
    new_path = log_root / f"{next_n:0{LOG_NN_WIDTH}d}"
    new_path.mkdir(parents=False, exist_ok=False)
    return new_path


# ---------------------------------------------------------------------------
# session.json
# ---------------------------------------------------------------------------


def read_session_id(subchat_folder: Path) -> str | None:
    """Return the persisted ``session_id`` or ``None`` if absent."""
    path = subchat_folder / SESSION_JSON_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sid = data.get("session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) else None


def write_session_id(subchat_folder: Path, session_id: str) -> None:
    """Atomically persist ``session_id`` to ``session.json`` if absent."""
    path = subchat_folder / SESSION_JSON_NAME
    if path.exists():
        return  # idempotent — never overwrite
    payload = {
        "session_id": session_id,
        "created": _iso_now(),
    }
    _atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


# ---------------------------------------------------------------------------
# claude command construction
# ---------------------------------------------------------------------------


def build_claude_argv(
    cfg: Config,
    msg: str,
    session_id: str | None,
) -> tuple[list[str], dict[str, str]]:
    """Build ``argv`` and the env-var overlay for invoking ``claude -p``.

    Returns ``(argv, env_overlay)``. ``env_overlay`` is overlaid onto
    ``os.environ`` by the caller before spawning.

    Flag policy:
    - ``null`` config values mean "do not pass this flag" — we never pass
      empty-string flags.
    - ``allowed-tools`` / ``disallowed-tools`` are passed as a single
      comma-separated argument per the CLI reference.
    - ``stream-json`` requires ``--verbose`` in headless mode (CLI quirk
      noted in ``ClaudeCLI_Reference.md``; not in the subchat spec — we
      add it unconditionally for stream-json and surface this in done.md).
    - ``effort`` is passed via ``CLAUDE_CODE_EFFORT_LEVEL`` env (higher
      priority than ``--effort`` per CLI reference).
    """
    argv: list[str] = ["claude", "-p", msg]

    output_format = cfg.get("output-format") or "stream-json"
    argv += ["--output-format", output_format]
    if output_format == "stream-json":
        argv.append("--verbose")  # required by claude for stream-json in -p

    model = cfg.get("model")
    if model:
        argv += ["--model", str(model)]

    sp = cfg.resolved_system_prompt
    asp = cfg.resolved_append_system_prompt
    if sp:
        argv += ["--system-prompt-file", sp]
    if asp:
        argv += ["--append-system-prompt-file", asp]

    max_turns = cfg.get("max-turns")
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]

    allowed = cfg.get("allowed-tools") or []
    if allowed:
        argv += ["--allowedTools", ",".join(str(x) for x in allowed)]
    disallowed = cfg.get("disallowed-tools") or []
    if disallowed:
        argv += ["--disallowedTools", ",".join(str(x) for x in disallowed)]

    perm = cfg.get("permission-mode")
    if perm:
        argv += ["--permission-mode", str(perm)]

    if cfg.get("bare"):
        argv.append("--bare")
    if cfg.get("no-session-persistence"):
        argv.append("--no-session-persistence")

    if session_id:
        argv += ["--resume", session_id]

    # Env overlay.
    env_overlay: dict[str, str] = {}
    effort = cfg.get("effort")
    if effort:
        env_overlay["CLAUDE_CODE_EFFORT_LEVEL"] = str(effort)
    extra_env = cfg.get("env") or {}
    if isinstance(extra_env, dict):
        for k, v in extra_env.items():
            env_overlay[str(k)] = str(v)

    return argv, env_overlay


def _short_flags(argv: list[str]) -> list[str]:
    """Return a short, human-readable list of flag tokens (no paths/secrets)
    for ``meta.json.flags`` and the ``[claude-spawn]`` event payload.
    """
    out: list[str] = []
    i = 0
    # Skip ``claude -p <msg>`` head.
    if argv[:2] == ["claude", "-p"]:
        i = 3
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--"):
            out.append(tok)
            # Don't include the following positional value if it's a
            # path / secret-ish; only short literals.
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                nxt = argv[i + 1]
                if "/" not in nxt and len(nxt) <= 40:
                    out.append(nxt)
                i += 2
            else:
                i += 1
        else:
            i += 1
    return out


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    """Result of one claude run; everything needed for meta.json + exit."""

    exit_code: int
    duration_ms: int
    session_id: str | None
    response_text: str | None
    output_json: Any  # list (stream-json) or dict (json) — saved to output.json
    raw_output: str | None  # set when output was malformed and we saved .raw
    usage: dict | None
    total_cost_usd: float | None
    flags_short: list[str]


def _start_heartbeat(emitter: Emitter, stop_event: threading.Event,
                     start_monotonic: float) -> threading.Thread:
    """Background thread that emits ``[heartbeat]`` every HEARTBEAT_INTERVAL_S
    seconds *only* when no other event has been emitted in that window.

    Used only with ``output-format: json`` — for stream-json, the per-block
    ``[tool]``/``[assistant]`` events already provide liveness.
    """
    # Poll at a fraction of the interval so we react promptly to stop_event
    # and still catch the window even when HEARTBEAT_INTERVAL_S is small
    # (tests inject sub-second intervals).
    poll = min(0.5, HEARTBEAT_INTERVAL_S / 2.0) if HEARTBEAT_INTERVAL_S > 0 else 0.5

    def loop() -> None:
        while not stop_event.wait(poll):
            if emitter.seconds_since_last() >= HEARTBEAT_INTERVAL_S:
                elapsed = int(time.monotonic() - start_monotonic)
                emitter.emit("heartbeat", f"elapsed={elapsed}")
    t = threading.Thread(target=loop, name="subchat-heartbeat", daemon=True)
    t.start()
    return t


# Max millis we wait for the stderr-drain thread to exit after stdout EOF.
# Stream is already at EOF by definition once `proc.wait()` returns, so the
# join should be instant; the timeout exists only to bound a pathological
# thread-state condition without hanging subchat.
STDERR_DRAIN_JOIN_TIMEOUT_S = 2.0


def _start_stderr_drain(proc: subprocess.Popen) -> tuple[threading.Thread, list[str]]:
    """Drain ``proc.stderr`` to a buffer in a daemon thread, concurrently
    with the main loop consuming ``proc.stdout``.

    Returns ``(thread, buf)``. The caller waits for the child via
    ``proc.wait()``, then ``thread.join(timeout=…)``; the accumulated text is
    ``"".join(buf)``.

    Why a thread (not ``stderr=subprocess.STDOUT``):
    Claude's ``--verbose`` (mandatory for ``output-format: stream-json``)
    writes diagnostic chatter to stderr. Merging it into stdout would feed
    that chatter to the stream-json parser, counted as ``malformed_lines``
    and surfaced as ``[warn] ignored N malformed stream lines`` — noisy and
    misleading. Keeping the streams separate but draining stderr live closes
    the deadlock window (≥pipe buffer of verbose chatter ⇒ child blocks
    writing stderr ⇒ subchat blocks reading stdout) without polluting the
    parser. Mirrors the heartbeat-thread pattern in ``_start_heartbeat``.

    Deadlock-avoidance is covered by
    ``test_subchat.py::TestStderrDrain::test_stderr_drain_avoids_deadlock``.
    """
    buf: list[str] = []

    def loop() -> None:
        try:
            if proc.stderr is None:
                return
            # Iterating a text-mode pipe yields lines until EOF; this is what
            # closes the loop naturally when claude exits.
            for chunk in proc.stderr:
                buf.append(chunk)
        except (OSError, ValueError):
            # ValueError can occur if the pipe is closed mid-read by the
            # child; treat as EOF.
            pass

    t = threading.Thread(target=loop, name="subchat-stderr-drain", daemon=True)
    t.start()
    return t, buf


def run_claude(
    cfg: Config,
    msg: str,
    session_id: str | None,
    emitter: Emitter,
    run_folder: Path,
    spawn=None,
) -> RunOutcome:
    """Spawn ``claude -p`` and collect output. ``spawn`` is parametrised
    so tests can inject a fake; when ``None``, looks up
    ``subprocess.Popen`` at call time so test-level ``mock.patch`` of
    ``subprocess.Popen`` takes effect.

    Writes ``prompt.md`` BEFORE spawn so a crash mid-run still leaves a
    replay-safe artifact.
    """
    if spawn is None:
        spawn = subprocess.Popen
    argv, env_overlay = build_claude_argv(cfg, msg, session_id)
    flags_short = _short_flags(argv)
    emitter.emit("claude-spawn", "flags=" + " ".join(flags_short))

    # prompt.md — written before spawn for replay safety.
    _atomic_write_text(run_folder / "prompt.md", msg)

    env = os.environ.copy()
    env.update(env_overlay)

    output_format = cfg.get("output-format") or "stream-json"

    start_monotonic = time.monotonic()
    stop_event = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    if output_format == "json":
        heartbeat_thread = _start_heartbeat(emitter, stop_event, start_monotonic)

    try:
        proc = spawn(
            argv,
            cwd=str(cfg.resolved_cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,  # line-buffered
            text=True,
        )
    except FileNotFoundError as e:
        stop_event.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=1.0)
        emitter.emit("error", f"claude binary not found: {e}")
        return RunOutcome(
            exit_code=EXIT_SUBCHAT_INTERNAL,
            duration_ms=int((time.monotonic() - start_monotonic) * 1000),
            session_id=None,
            response_text=None,
            output_json=None,
            raw_output=None,
            usage=None,
            total_cost_usd=None,
            flags_short=flags_short,
        )

    # Start the stderr-drain thread BEFORE iterating stdout so claude can
    # never block on stderr while subchat is reading stdout. See
    # `_start_stderr_drain` docstring for the deadlock rationale.
    stderr_thread, stderr_buf = _start_stderr_drain(proc)

    # Consume stdout. For stream-json, parse incrementally and surface
    # per-block progress. For json, accumulate the single blob.
    stream_state = StreamParseResult()
    raw_lines: list[str] = []
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            raw_lines.append(raw)
            if output_format == "stream-json":
                parse_line(
                    raw,
                    stream_state,
                    emit_tool=lambda name: emitter.emit("tool", name),
                    emit_assistant=lambda snip: emitter.emit("assistant", snip),
                )
        exit_code = proc.wait()
    finally:
        stop_event.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=1.0)
        # After `proc.wait()` returns, stderr is at EOF; the drain loop exits
        # cleanly on its own. The timeout bounds a pathological thread-state
        # condition (would never block subchat indefinitely).
        stderr_thread.join(timeout=STDERR_DRAIN_JOIN_TIMEOUT_S)
        # Close pipes — Popen leaves them open; the drain loop already saw
        # EOF on stderr, the main loop saw EOF on stdout, but the file
        # objects themselves linger and surface as ResourceWarning.
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    stderr_text = "".join(stderr_buf)

    duration_ms = int((time.monotonic() - start_monotonic) * 1000)

    # Parse & extract.
    output_json: Any = None
    raw_output: str | None = None
    response_text: str | None = None
    new_session_id: str | None = None
    usage: dict | None = None
    total_cost_usd: float | None = None

    if output_format == "stream-json":
        if stream_state.result_event is None:
            # Malformed stream — capture the raw lines for postmortem.
            raw_output = "".join(raw_lines)
            emitter.emit("error", "malformed output")
        else:
            output_json = stream_state.events
            response_text = stream_state.response_text
            new_session_id = stream_state.session_id
            res = stream_state.result_event
            usage = res.get("usage") if isinstance(res.get("usage"), dict) else None
            tcu = res.get("total_cost_usd")
            total_cost_usd = float(tcu) if isinstance(tcu, (int, float)) else None
        if stream_state.malformed_lines:
            emitter.emit(
                "warn",
                f"ignored {stream_state.malformed_lines} malformed stream lines",
            )
    else:  # json
        blob = "".join(raw_lines).strip()
        try:
            output_json = json.loads(blob) if blob else None
        except json.JSONDecodeError:
            raw_output = blob
            emitter.emit("error", "malformed output")
        if isinstance(output_json, dict):
            response_text = output_json.get("result") if isinstance(
                output_json.get("result"), str
            ) else None
            new_session_id = output_json.get("session_id") if isinstance(
                output_json.get("session_id"), str
            ) else None
            usage = output_json.get("usage") if isinstance(
                output_json.get("usage"), dict
            ) else None
            # TODO(T05d/e): `total_cost_usd` flat-at-top is the stream-json
            # shape (verified against real `stream.ndjson` logs). Per
            # `ClaudeCLI_Reference.md` §"Структура output (--output-format json)"
            # the `output-format: json` shape uses NESTED `cost.total_usd`
            # instead. This code path currently has no deployed consumer (all
            # v1 subagents declare `output-format: stream-json` per their
            # spec contract); validate against a real `claude -p
            # --output-format json` invocation before any json-format
            # subagent appears.
            tcu = output_json.get("total_cost_usd")
            total_cost_usd = float(tcu) if isinstance(tcu, (int, float)) else None

    if exit_code != 0:
        # Spec wants [error] exit=<code> + propagated code.
        emitter.emit("error", f"exit={exit_code}")
        if stderr_text.strip():
            # Surface a short stderr clue (truncated) without leaking secrets.
            emitter.emit("warn", "stderr=" + stderr_text.strip().splitlines()[-1][:STDERR_TAIL_TRUNCATE_LEN])

    return RunOutcome(
        exit_code=exit_code,
        duration_ms=duration_ms,
        session_id=new_session_id,
        response_text=response_text,
        output_json=output_json,
        raw_output=raw_output,
        usage=usage,
        total_cost_usd=total_cost_usd,
        flags_short=flags_short,
    )


def persist_run_artifacts(run_folder: Path, outcome: RunOutcome) -> None:
    """Write ``output.json`` / ``output.raw`` / ``response.md`` / ``meta.json``
    into ``run_folder`` based on ``outcome``.
    """
    # output.json: a JSON array of events (stream-json) or the single blob.
    # output.raw: present only when parsing failed.
    if outcome.raw_output is not None:
        _atomic_write_text(run_folder / "output.raw", outcome.raw_output)
    if outcome.output_json is not None:
        _atomic_write_text(
            run_folder / "output.json",
            json.dumps(outcome.output_json, indent=2, ensure_ascii=False) + "\n",
        )

    if outcome.response_text is not None:
        _atomic_write_text(run_folder / "response.md", outcome.response_text)

    meta = {
        "duration_ms": outcome.duration_ms,
        "exit_code": outcome.exit_code,
        "flags": outcome.flags_short,
        "tokens": outcome.usage,
        "cost_usd": outcome.total_cost_usd,
        "session_id": outcome.session_id,
        "completed_at": _iso_now(),
    }
    _atomic_write_text(
        run_folder / "meta.json",
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "..."


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    emitter = Emitter()

    subchat_folder = args.session_folder / "subchats" / args.subagent

    if not subchat_folder.is_dir():
        emitter.emit(
            "error",
            "subchat folder missing — run spawn_subchat first",
        )
        return EXIT_SUBCHAT_INTERNAL

    config_path = subchat_folder / CONFIG_FILENAME
    try:
        cfg = load_config(str(config_path), str(subchat_folder))
    except ConfigError as e:
        emitter.emit("error", f"config.yaml: {e}")
        return EXIT_SUBCHAT_INTERNAL

    for field_name in cfg.defaulted_fields:
        emitter.emit("warn", f"field defaulted: {field_name}={DEFAULTS[field_name]!r}")

    # Acquire run.lock with stale-recovery.
    try:
        lock_handle = acquire_lock(
            subchat_folder,
            on_stale_recovered=lambda pid:
                emitter.emit("warn", f"stale run.lock removed (pid={pid} not alive)"),
        )
    except LockBusy as e:
        emitter.emit("error", f"concurrent subchat run detected — see {e.lock_path}")
        return EXIT_LOCK_BUSY

    with lock_handle:
        try:
            run_folder = allocate_run_folder(subchat_folder)
        except OSError as e:
            emitter.emit("error", f"cannot allocate log folder: {e}")
            return EXIT_SUBCHAT_INTERNAL

        run_nn = run_folder.name
        emitter.emit(
            "start",
            f"subagent={args.subagent} run={run_nn} msg={_truncate(args.msg, MSG_TRUNCATE_LEN)}",
        )

        session_id = read_session_id(subchat_folder)
        outcome = run_claude(cfg, args.msg, session_id, emitter, run_folder)

        try:
            persist_run_artifacts(run_folder, outcome)
        except OSError as e:
            emitter.emit("error", f"failed to persist run artifacts: {e}")
            return EXIT_SUBCHAT_INTERNAL

        # First-run session.json: only on success and only if absent.
        if (
            outcome.exit_code == 0
            and outcome.session_id
            and session_id is None
        ):
            try:
                write_session_id(subchat_folder, outcome.session_id)
            except OSError as e:
                emitter.emit("warn", f"failed to write session.json: {e}")

        emitter.emit(
            "complete",
            f"run={run_nn} exit={outcome.exit_code} duration_ms={outcome.duration_ms}",
        )
        return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
