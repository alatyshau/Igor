#!/usr/bin/env python3
"""End-to-end pipeline tests for the Igor cockpit's subagent stack.

What is exercised:

    install.py  ──▶  MCP materializeSubchat  ──▶  subchat.py  ──▶  fake_claude
       (real)              (real, via Node bridge)     (real)         (mock)

What is NOT exercised:
    - real ``claude`` API calls — covered by ``manual_checklist.md``;
    - the protocolist model's curation quality — same checklist;
    - the Claude Code IDE / Stop hook / UserPromptSubmit hook — out of scope
      for T05e (the harness pre-builds a fake transcript on disk, see
      ``_seed_transcript``).

Every component on the in-scope arrow above is the real production code.
The only fake is the ``claude`` binary itself — wired in via a PATH shim so
``subchat.py`` runs through the entire spawn / pipe / parse / persist
chain. See ``fake_claude.py`` docstring for the contract.

Run:
    python3 -m unittest test_e2e.py -v

Requirements (asserted at suite startup):
    - ``node`` v22+ on PATH (for ``mcp_bridge.mjs``);
    - ``cockpit/mcp/dist/src/subchat_spawn.js`` exists
      (run ``npm run build`` in ``cockpit/mcp/`` if absent);
    - ``cockpit/subchat/subchat.py`` exists.

Coverage matrix (see done.md):
    - happy path ordinary  (1)
    - happy path finish    (2)
    - stderr-drain         (6)   ← T05d hard-prereq verification in piping
    - concurrent refuse    (5)
Deferred (with rationale in done.md):
    - no-op (3), lower-threshold (4), crash recovery (7), missing-profile (8).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — paths to production artifacts and the harness's own pieces
# ---------------------------------------------------------------------------

E2E_DIR = Path(__file__).resolve().parent
COCKPIT_DIR = E2E_DIR.parent
SOURCE_REPO = COCKPIT_DIR.parent  # Igor.source.git/

INSTALL_PY = COCKPIT_DIR / "deploy" / "install.py"
SUBCHAT_PY = COCKPIT_DIR / "subchat" / "subchat.py"
MCP_DIST = COCKPIT_DIR / "mcp" / "dist" / "src" / "subchat_spawn.js"

FAKE_CLAUDE = E2E_DIR / "fake_claude.py"
MCP_BRIDGE = E2E_DIR / "mcp_bridge.mjs"

# Per-scenario temp folders live under this directory so all artifacts stay
# inside the repo tree (easier to inspect on failure, cleanly gitignored).
# Cleaned up by ``_Fixture.cleanup``; safe to ``rm -rf`` between runs.
TMP_ROOT = E2E_DIR / "tmp"

PROTOCOLIST_PROFILE_SRC = (
    SOURCE_REPO / "instructions" / "subagents" / "protocolist.md"
)

# subchat exit codes (mirrored from cockpit/subchat/subchat.py).
EXIT_LOCK_BUSY = 65

# Bound the stderr-drain scenario test wall-time. If the drain is broken
# the subchat hangs indefinitely; this timeout converts that to a loud test
# failure instead of a stalled CI runner.
STDERR_DRAIN_TIMEOUT_S = 10.0

# Per-scenario wall-time bound. The fake never sleeps non-trivially; if
# subchat takes >5s something has gone wrong (a real hang or a deadlock).
SCENARIO_TIMEOUT_S = 30.0

DEFAULT_SESSION_ID_PREFIX = "e2e-sid"


# ---------------------------------------------------------------------------
# Preflight — fail loudly if the production artifacts aren't where we expect
# ---------------------------------------------------------------------------


def _preflight_or_skip() -> None:
    missing: list[str] = []
    if not INSTALL_PY.is_file():
        missing.append(f"install.py missing: {INSTALL_PY}")
    if not SUBCHAT_PY.is_file():
        missing.append(f"subchat.py missing: {SUBCHAT_PY}")
    if not MCP_DIST.is_file():
        missing.append(
            f"MCP dist missing: {MCP_DIST} "
            "(run `npm run build` in cockpit/mcp/)"
        )
    if not PROTOCOLIST_PROFILE_SRC.is_file():
        missing.append(
            f"protocolist profile missing: {PROTOCOLIST_PROFILE_SRC}"
        )
    if not FAKE_CLAUDE.is_file():
        missing.append(f"fake_claude.py missing: {FAKE_CLAUDE}")
    if not MCP_BRIDGE.is_file():
        missing.append(f"mcp_bridge.mjs missing: {MCP_BRIDGE}")
    if shutil.which("node") is None:
        missing.append("`node` not on PATH (required for mcp_bridge.mjs)")
    # MCP source uses `node:` prefix imports (Node 14.18+) and top-level
    # await (Node 14.8+). Production target is Node 22 per cockpit/specs/mcp.md
    # §Transport and lifecycle, but the e2e bridge does not exercise any
    # 22-specific surface; presence of `node` plus dist artifacts is enough
    # for these scenarios.
    if missing:
        raise unittest.SkipTest(
            "e2e prerequisites missing:\n  - " + "\n  - ".join(missing)
        )


# ---------------------------------------------------------------------------
# Test fixture — temp Context + Session + transcript + PATH shim
# ---------------------------------------------------------------------------


class _Fixture:
    """Owns one temp Context folder + the PATH shim that routes ``claude``
    to ``fake_claude.py``. Use as a context manager; ``cleanup()`` is
    idempotent.
    """

    def __init__(self, scenario: str, session_id: str | None = None) -> None:
        self.scenario = scenario
        self.session_id = session_id or f"{DEFAULT_SESSION_ID_PREFIX}-{scenario}"
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="cockpit-e2e-", dir=str(TMP_ROOT)))
        self.context_root = self.tmp / "Context"
        self.context_root.mkdir()
        self.session_folder = (
            self.context_root
            / "journal"
            / "2026"
            / "05"
            / "25"
            / "1400_TestSession"
        )
        self.shim_dir = self.tmp / "bin"
        self.shim_dir.mkdir()
        self._installed = False

    def __enter__(self) -> "_Fixture":
        return self

    def __exit__(self, *args) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        try:
            shutil.rmtree(self.tmp)
        except FileNotFoundError:
            pass

    # -- deploy ----------------------------------------------------------

    def install(self) -> subprocess.CompletedProcess:
        """Run real ``install.py`` against ``context_root``. Returns the
        CompletedProcess so tests can assert on stdout (the deploy report).
        """
        cp = subprocess.run(
            [sys.executable, str(INSTALL_PY), str(self.context_root)],
            capture_output=True,
            text=True,
            timeout=SCENARIO_TIMEOUT_S,
        )
        self._installed = True
        return cp

    def assert_install_artifacts(self) -> None:
        """Spot-check that install.py produced the cockpit contract files
        in the right places. Mirrors the manual checklist § Setup.
        """
        c = self.context_root
        assert (c / "context.json").is_file(), "context.json missing"
        assert (c / "settings.json").is_file(), "settings.json missing"
        assert (c / ".mcp.json").is_file(), ".mcp.json missing"
        assert (c / ".claude" / "output-styles" / "igor.md").is_file(), \
            "deployed persona missing"
        assert (
            c / ".claude" / "agents" / "protocolist.md"
        ).is_file(), "deployed protocolist profile missing"

    # -- transcript seeding ----------------------------------------------

    def seed_transcript(self, n_turns: int = 12) -> None:
        """Materialize a SessionFolder + state.md + ``transcript/`` with
        ``n_turns`` per-turn files and a valid ``index.json``. Simulates
        what UserPromptSubmit + Stop hooks would have produced after a
        ``n_turns``-turn chat.

        Each ``NNN_msg.md`` is ~3 KiB so 12 turns ≈ 36 KiB — over the
        protocolist's 30 KB lower threshold (ordinary command actually
        does work) but well below 80 KB (single batch).
        """
        sf = self.session_folder
        (sf / "transcript").mkdir(parents=True)
        # state.md must contain the mandatory sections so the MCP's
        # ensureSubchatLine merge can place the ## Subchats entry.
        (sf / "state.md").write_text(
            textwrap.dedent("""\
                # Session state

                ## Input

                *пусто*

                ## Scope

                - OBJ001 [work]

                ## Problems

                *пусто*
            """),
            encoding="utf-8",
        )

        turns: list[dict] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        # ~3 KiB of plausible per-turn content
        filler = "User context line repeated for size. " * 60  # ~2.4 KiB

        for i in range(n_turns):
            content = textwrap.dedent(f"""\
                ## {i:03d}

                **User:**

                Turn {i}: please continue with the demonstration. {filler}

                **Assistant:**

                Acknowledged turn {i}. {filler}
            """)
            (sf / "transcript" / f"{i:03d}_msg.md").write_text(
                content, encoding="utf-8"
            )
            turns.append({
                "index": i,
                "slug": None,
                "file": f"{i:03d}_msg.md",
                "prompt_id": f"prompt-{i:03d}",
                "started_at": now_iso,
                "ended_at": now_iso,
                "complete": True,
                "midflight_count": 0,
                "assistant_count": 1,
            })

        (sf / "transcript" / "index.json").write_text(
            json.dumps({"next_index": n_turns, "turns": turns}, indent=2) + "\n",
            encoding="utf-8",
        )

        # NB: we intentionally do NOT seed `.claude/sessions/<id>.json` here —
        # the bridge accepts `sessionFolder` explicitly (see `mcp_bridge.mjs`),
        # so the SessionStateFile lookup path is bypassed in e2e. If a future
        # scenario needs to exercise that lookup (e.g., spawn_subchat called
        # without an explicit folder), seed the mapping at that point.

    # -- MCP bridge ------------------------------------------------------

    def spawn_subchat(self, subagent: str = "protocolist") -> dict:
        """Call ``materializeSubchat`` via the Node bridge with the same
        argument shape the MCP tool handler uses. Returns the parsed JSON
        response.
        """
        payload = {
            "contextRoot": str(self.context_root),
            "sessionFolder": str(self.session_folder),
            "subagent": subagent,
        }
        cp = subprocess.run(
            ["node", str(MCP_BRIDGE), json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=SCENARIO_TIMEOUT_S,
        )
        if cp.returncode != 0:
            raise RuntimeError(
                f"mcp_bridge.mjs failed (exit={cp.returncode}): "
                f"stderr={cp.stderr!r} stdout={cp.stdout!r}"
            )
        return json.loads(cp.stdout.strip())

    # -- PATH shim -------------------------------------------------------

    def write_claude_shim(self) -> Path:
        """Drop a ``claude`` shell shim into ``shim_dir`` that exec's
        ``fake_claude.py``. Returns the shim path; tests prepend
        ``shim_dir`` to PATH when invoking subchat.

        Symlinking would be simpler, but a wrapper script lets us forward
        scenario / session_id env vars without leaking the caller's full
        environment to the fake. (We let env propagate via os.environ on
        the subprocess call, but the shim makes the redirection explicit
        and easy to debug.)
        """
        shim = self.shim_dir / "claude"
        shim.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            exec {sys.executable} "{FAKE_CLAUDE}" "$@"
        """), encoding="utf-8")
        os.chmod(shim, shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return shim

    # -- run subchat -----------------------------------------------------

    def run_subchat(
        self,
        *,
        msg: str,
        timeout: float = SCENARIO_TIMEOUT_S,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run the real subchat.py with the PATH shim wired in. Returns
        the CompletedProcess; tests assert on stdout (the [start]/[tool]/
        [assistant]/[complete] event stream) and the on-disk side effects.
        """
        env = os.environ.copy()
        env["PATH"] = f"{self.shim_dir}{os.pathsep}{env.get('PATH', '')}"
        env["MOCK_CLAUDE_SCENARIO"] = self.scenario
        env["MOCK_CLAUDE_SESSION_ID"] = self.session_id
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                sys.executable,
                str(SUBCHAT_PY),
                "--subagent",
                "protocolist",
                "--msg",
                msg,
                "--session-folder",
                str(self.session_folder),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Helpers — assertions on stdout event stream and on-disk artifacts
# ---------------------------------------------------------------------------


def _assert_event_tags(stdout: str, required_tags: list[str], testcase) -> None:
    """Each tag must appear at least once on a line prefixed with that tag.
    Mirrors the streaming contract in cockpit/specs/subchat.md §Stdout streaming.
    """
    lines = stdout.splitlines()
    for tag in required_tags:
        prefix = f"[{tag}]"
        if not any(line.startswith(prefix) for line in lines):
            testcase.fail(
                f"required tag {tag!r} not seen on stdout.\n"
                f"--- stdout:\n{stdout}\n--- end stdout"
            )


def _assert_chapter_sealed(session_folder: Path, testcase) -> tuple[str, list[int]]:
    """Assert that some chapter-file appears in ``transcript/`` and that
    ``index.json`` has at least one entry pointing at it. Returns
    ``(chapter_filename, covered_indices)``.
    """
    transcript = session_folder / "transcript"
    chapters = sorted(
        p.name for p in transcript.glob("*.md")
        if not p.name.endswith("_msg.md") and "_" in p.name
    )
    testcase.assertTrue(
        chapters,
        f"no chapter-file found in {transcript}; "
        f"contents: {[p.name for p in transcript.iterdir()]}",
    )
    chapter = chapters[0]
    index = json.loads((transcript / "index.json").read_text(encoding="utf-8"))
    covered = [t["index"] for t in index["turns"] if t["file"] == chapter]
    testcase.assertTrue(
        covered,
        f"chapter-file {chapter} not referenced in index.json"
    )
    return chapter, covered


def _assert_protocol_scaffold(session_folder: Path, testcase) -> str:
    """Assert ``protocol.md`` exists with the spec scaffold. Returns text."""
    p = session_folder / "protocol.md"
    testcase.assertTrue(p.is_file(), f"protocol.md missing at {p}")
    text = p.read_text(encoding="utf-8")
    testcase.assertIn("# Protocol — ", text)
    testcase.assertIn("**Status:**", text)
    testcase.assertIn("## Chapters", text)
    return text


def _assert_subchat_log_artifacts(session_folder: Path, testcase) -> Path:
    """Assert the per-run log/NN/ folder exists with prompt.md / output.json /
    response.md / meta.json. Returns the log/NN/ Path.
    """
    log_root = session_folder / "subchats" / "protocolist" / "log"
    testcase.assertTrue(log_root.is_dir(), f"log root missing: {log_root}")
    runs = sorted(p for p in log_root.iterdir() if p.is_dir() and p.name.isdigit())
    testcase.assertTrue(runs, f"no run folder in {log_root}")
    run = runs[-1]
    for f in ("prompt.md", "output.json", "response.md", "meta.json"):
        testcase.assertTrue((run / f).is_file(), f"missing log artifact: {run / f}")
    return run


# ---------------------------------------------------------------------------
# Scenario tests
# ---------------------------------------------------------------------------


class TestE2E(unittest.TestCase):
    """End-to-end scenarios. Each test sets up its own temp Context and
    runs the full deploy→spawn→run chain; teardown removes the temp tree.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _preflight_or_skip()

    # -- scenario 1 — happy path ordinary -------------------------------

    def test_happy_ordinary_seals_chapter_and_appends_section(self):
        """!протокол on a 12-turn transcript → one chapter sealed, scaffold
        + one section in protocol.md, all event tags emitted, status = open.
        """
        with _Fixture(scenario="happy_ordinary") as fx:
            install = fx.install()
            self.assertEqual(install.returncode, 0,
                             msg=f"install.py failed: {install.stderr}")
            fx.assert_install_artifacts()

            fx.seed_transcript(n_turns=12)
            mcp_result = fx.spawn_subchat()
            self.assertTrue(mcp_result["created"],
                            f"materializeSubchat reported created=False on first spawn: {mcp_result}")
            self.assertTrue(
                (fx.session_folder / "subchats" / "protocolist" / "config.yaml").is_file()
            )
            self.assertTrue(
                (fx.session_folder / "subchats" / "protocolist" / "system_prompt.md").is_file()
            )

            # state.md integration — the MCP spawn_subchat handler also runs
            # ensureSubchatLine; our bridge mirrors that. Verify the
            # `## Subchats` section has the active-line per spec
            # (cockpit/specs/subchat.md §"state.md integration"; schema in
            # cockpit/specs/schemas/session_folder.md §Subchats).
            self.assertTrue(
                mcp_result.get("state_md_updated"),
                f"bridge reported state.md NOT updated on first spawn: {mcp_result}",
            )
            state_md_text = (fx.session_folder / "state.md").read_text(encoding="utf-8")
            self.assertIn("## Subchats", state_md_text,
                          msg=f"## Subchats header missing.\n{state_md_text}")
            self.assertIn("- protocolist (active)", state_md_text,
                          msg=f"protocolist active line missing.\n{state_md_text}")
            # Merge-preserving: other sections must still be present byte-for-byte.
            self.assertIn("## Scope", state_md_text)
            self.assertIn("- OBJ001 [work]", state_md_text)
            self.assertIn("## Problems", state_md_text)

            fx.write_claude_shim()
            run = fx.run_subchat(msg="!протокол")
            self.assertEqual(
                run.returncode, 0,
                msg=f"subchat exited non-zero. "
                    f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}",
            )

            # Event stream — [start] / [claude-spawn] / [tool] / [assistant] /
            # [complete] must all appear (this is the subchat streaming contract).
            _assert_event_tags(
                run.stdout,
                ["start", "claude-spawn", "tool", "assistant", "complete"],
                self,
            )

            # Filesystem side effects.
            chapter, covered = _assert_chapter_sealed(fx.session_folder, self)
            self.assertTrue(chapter.endswith("_MockChapter.md"))
            self.assertGreater(len(covered), 0)
            text = _assert_protocol_scaffold(fx.session_folder, self)
            self.assertIn("**Status:** в работе", text,
                          msg="ordinary run must leave status open")
            self.assertIn("### 000 — MockChapter", text,
                          msg="chapter section missing in protocol.md")

            # Per-run log artifacts.
            run_folder = _assert_subchat_log_artifacts(fx.session_folder, self)
            meta = json.loads((run_folder / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["exit_code"], 0)
            self.assertIsNotNone(meta["session_id"])

            # session.json materialized after first run.
            sj = fx.session_folder / "subchats" / "protocolist" / "session.json"
            self.assertTrue(sj.is_file(), "session.json missing after first run")

    # -- scenario 2 — happy path finish ---------------------------------

    def test_happy_finish_writes_abstract_and_flips_status(self):
        """!протокол финиш → chapter sealed, section appended, Abstract
        inserted, status = завершена.
        """
        with _Fixture(scenario="happy_finish") as fx:
            install = fx.install()
            self.assertEqual(install.returncode, 0, msg=install.stderr)
            fx.assert_install_artifacts()

            fx.seed_transcript(n_turns=8)
            fx.spawn_subchat()
            fx.write_claude_shim()
            run = fx.run_subchat(msg="!протокол финиш")
            self.assertEqual(run.returncode, 0,
                             msg=f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}")

            _assert_event_tags(
                run.stdout,
                ["start", "claude-spawn", "tool", "assistant", "complete"],
                self,
            )

            text = _assert_protocol_scaffold(fx.session_folder, self)
            self.assertIn("**Status:** завершена", text,
                          msg="status not flipped on finish run")
            self.assertIn("## Abstract", text,
                          msg="Abstract not inserted on finish run")
            # Anchor ordering: Abstract must come BEFORE Chapters.
            self.assertLess(
                text.index("## Abstract"), text.index("## Chapters"),
                "Abstract must precede ## Chapters in the document",
            )

    # -- scenario 6 — stderr-drain integration --------------------------

    def test_stderr_flood_does_not_deadlock(self):
        """T05d hard-prereq verification: > 100 KiB stderr from the child
        must not deadlock subchat's stdout consumer. Bounded by a 10s
        timeout — a regression would hang indefinitely on a real pipe.

        This mirrors test_subchat.py::TestStderrDrain but exercises the
        full subchat CLI invoked as a subprocess, with our fake_claude
        producing the stderr flood. The child is a real OS process; the
        pipes are real OS pipes; the deadlock is real if the drain is
        broken.
        """
        with _Fixture(scenario="stderr_flood") as fx:
            install = fx.install()
            self.assertEqual(install.returncode, 0, msg=install.stderr)
            fx.seed_transcript(n_turns=12)
            fx.spawn_subchat()
            fx.write_claude_shim()

            # Wrap in a thread so we can hard-kill on timeout.
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            t0 = time.monotonic()
            fut = ex.submit(
                fx.run_subchat,
                msg="!протокол",
                timeout=STDERR_DRAIN_TIMEOUT_S,
            )
            try:
                run = fut.result(timeout=STDERR_DRAIN_TIMEOUT_S + 2.0)
            except concurrent.futures.TimeoutError:
                ex.shutdown(wait=False)
                self.fail(
                    f"subchat hung — stderr-drain deadlock not prevented "
                    f"(>{STDERR_DRAIN_TIMEOUT_S}s)"
                )
            except subprocess.TimeoutExpired as e:
                ex.shutdown(wait=False)
                self.fail(
                    f"subchat invocation timed out — drain regression. "
                    f"partial stdout:\n{e.stdout!r}"
                )
            finally:
                ex.shutdown(wait=False)
            elapsed = time.monotonic() - t0

            self.assertEqual(
                run.returncode, 0,
                msg=f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}",
            )
            self.assertLess(
                elapsed, STDERR_DRAIN_TIMEOUT_S,
                msg=f"elapsed {elapsed:.2f}s exceeds drain timeout — slow "
                    f"but not technically deadlocked",
            )
            # The chapter must still seal correctly under stderr load.
            _assert_chapter_sealed(fx.session_folder, self)
            _assert_protocol_scaffold(fx.session_folder, self)
            # And all standard tags still emit.
            _assert_event_tags(
                run.stdout,
                ["start", "claude-spawn", "tool", "complete"],
                self,
            )

    # -- scenario 5 — concurrent run refused ----------------------------

    def test_concurrent_run_refuses_with_lock_busy(self):
        """A pre-existing run.lock owned by an *alive* pid must cause subchat
        to refuse with EXIT_LOCK_BUSY and emit the spec-mandated [error] line.

        Implementation note: rather than race two real subchat processes
        (flaky), we plant a lock referencing the harness's own pid (which
        is definitely alive) and verify a single subchat invocation
        refuses correctly. This isolates the lock-busy code path from
        scheduling noise.
        """
        with _Fixture(scenario="happy_ordinary") as fx:
            install = fx.install()
            self.assertEqual(install.returncode, 0, msg=install.stderr)
            fx.seed_transcript(n_turns=12)
            fx.spawn_subchat()

            # Plant a live-pid lock so acquire() raises LockBusy.
            lock_path = (
                fx.session_folder
                / "subchats"
                / "protocolist"
                / "run.lock"
            )
            lock_path.write_text(
                f"pid={os.getpid()} started_at="
                f"{datetime.now(timezone.utc).isoformat()}\n",
                encoding="utf-8",
            )

            fx.write_claude_shim()
            run = fx.run_subchat(msg="!протокол")
            self.assertEqual(
                run.returncode, EXIT_LOCK_BUSY,
                msg=f"expected exit {EXIT_LOCK_BUSY}, got {run.returncode}. "
                    f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}",
            )
            self.assertIn(
                "concurrent subchat run detected", run.stdout,
                msg=f"expected [error] line not emitted. stdout:\n{run.stdout}",
            )

            # No log/NN/ folder was allocated — spec: "does NOT allocate log/NN".
            log_root = fx.session_folder / "subchats" / "protocolist" / "log"
            if log_root.exists():
                runs = [p for p in log_root.iterdir()
                        if p.is_dir() and p.name.isdigit()]
                self.assertEqual(
                    runs, [],
                    "log/NN/ allocated even though lock was busy — spec violation",
                )

            # Cleanup the planted lock so the temp tree teardown is hygenic.
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
