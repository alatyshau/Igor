#!/usr/bin/env python3
"""``fake_claude`` — deterministic stand-in for ``claude -p`` used by the
end-to-end test harness in this directory.

Why a fake. The full pipeline under test is:

    install.py  →  MCP spawn_subchat  →  subchat.py  →  claude -p  →  files

Running a real ``claude -p`` would (a) cost real API tokens per scenario,
(b) make tests non-deterministic, and (c) tie CI to network availability.
The fake replaces only the ``claude -p`` step. Every other component is the
real production code.

Contract. The fake reproduces the parts of ``claude -p
--output-format stream-json`` that subchat.py and the protocolist profile
care about:

  - reads its own argv (``-p``, ``--output-format``, ``--system-prompt-file``,
    ``--resume``, etc.) — only enough to extract ``--msg`` and confirm the
    profile path is a file (we do NOT parse the profile body; the test asserts
    plumbing, not curation);
  - emits stream-json events (``system/init``, ``assistant`` checkpoints with
    ``tool_use`` and ``text`` content blocks, terminal ``result``) on stdout,
    one JSON object per line;
  - emits configurable verbose chatter on stderr — used by the stderr-drain
    scenario to verify the T05d concurrent-drain fix in real piping;
  - performs the filesystem side effects (write chapter-file, rewrite
    ``transcript/index.json``, create / append to ``protocol.md``) that the
    *real* claude would have performed in response to the profile. This lets
    the harness assert on observable on-disk outputs without re-implementing
    chapter detection.

Scenario selection. Driven by the ``MOCK_CLAUDE_SCENARIO`` env var:

  - ``happy_ordinary``        — ``!протокол``: seal one chapter, append one section
  - ``happy_finish``          — ``!протокол финиш``: same plus Abstract + status flip
  - ``stderr_flood``          — emit > 100 KiB stderr before final event (T05d prereq)
  - ``stderr_flood_finish``   — like ``happy_finish`` plus stderr flood
  - ``no_op``                 — emit a ``no eligible content`` text + result, write nothing
  - ``exit_nonzero``          — emit error stderr, exit code 2

Defaults to ``happy_ordinary`` when the env var is unset; this is the most
useful baseline scenario for ad-hoc smoke runs.

Determinism. ``session_id`` defaults to ``mock-sid-0001`` so that
``session.json`` content is byte-stable across runs; override with
``MOCK_CLAUDE_SESSION_ID`` if a test needs a different value.

Where this fake lives. ``cockpit/e2e/`` — see ``cockpit/e2e/README``-section
in T05e ``done.md`` for the directory rationale.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENV_SCENARIO = "MOCK_CLAUDE_SCENARIO"
ENV_SESSION_ID = "MOCK_CLAUDE_SESSION_ID"

DEFAULT_SCENARIO = "happy_ordinary"
DEFAULT_SESSION_ID = "mock-sid-0001"

# > 100 KiB so the stderr-drain test exercises a buffer larger than any
# plausible OS pipe (~64 KiB on Linux). See T05d hard-prereq.
STDERR_FLOOD_BYTES = 128 * 1024

# Slug used by every "happy" scenario. Deterministic so assertions are stable.
CHAPTER_SLUG = "MockChapter"
CHAPTER_START_INDEX = 0  # zero-padded NNN; matches turns[0] in the fake transcript

# Pause between stream-json checkpoints. Small, so tests stay fast; non-zero,
# so subchat's stdout loop has to iterate (not consume the whole stream in one
# `for raw in proc.stdout`). 5 ms is well below CI patience and above zero.
INTER_EVENT_SLEEP_S = 0.005

# Status strings — must match the profile contract exactly.
STATUS_OPEN_RU = "в работе"
STATUS_DONE_RU = "завершена"


# ---------------------------------------------------------------------------
# argv parsing — only the subset we care about
# ---------------------------------------------------------------------------


def _parse_argv(argv: list[str]) -> dict:
    """Pick out the bits of the claude argv this fake reads.

    Real claude accepts dozens of flags. We only read what affects fake
    behavior. Unknown flags are ignored — the fake intentionally does NOT
    validate against the real claude argument grammar (would couple to a
    moving target outside our control).
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", dest="prompt_mode", action="store_true")
    parser.add_argument("--output-format", default="stream-json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--system-prompt-file", default=None)
    parser.add_argument("--append-system-prompt-file", default=None)
    parser.add_argument("--max-turns", default=None)
    parser.add_argument("--allowedTools", default=None)
    parser.add_argument("--disallowedTools", default=None)
    parser.add_argument("--permission-mode", default=None)
    parser.add_argument("--bare", action="store_true")
    parser.add_argument("--no-session-persistence", action="store_true")
    parser.add_argument("--resume", default=None)
    # Everything else (including the message after `-p`) lands in `remainder`.
    ns, remainder = parser.parse_known_args(argv[1:])  # skip "claude"
    # subchat constructs argv as ``["claude", "-p", msg, ...]`` so the message
    # is the first positional left over.
    msg = remainder[0] if remainder else ""
    return {
        "msg": msg,
        "output_format": ns.output_format,
        "system_prompt_file": ns.system_prompt_file,
        "resume": ns.resume,
        "model": ns.model,
    }


# ---------------------------------------------------------------------------
# Stream-json event emission
# ---------------------------------------------------------------------------


def _emit(event: dict) -> None:
    """Write one stream-json event as a single line to stdout, then flush."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _emit_init(session_id: str) -> None:
    _emit({"type": "system", "subtype": "init", "session_id": session_id})


def _emit_assistant_with_blocks(message_id: str, blocks: list[dict]) -> None:
    """Emit one ``assistant`` checkpoint event carrying ``blocks`` as the
    current message content. Each block must already include its ``index``.

    The real CLI emits *incremental* checkpoints (later events supersede
    earlier ones for the same index); to keep the fake simple we emit one
    checkpoint per content addition. The stream parser dedupes via
    ``(message_id, index)`` so this is correct per spec.
    """
    _emit(
        {
            "type": "assistant",
            "message": {"id": message_id, "content": blocks},
        }
    )
    time.sleep(INTER_EVENT_SLEEP_S)


def _emit_tool_use(message_id: str, index: int, tool_name: str) -> None:
    _emit_assistant_with_blocks(
        message_id,
        [
            {
                "type": "tool_use",
                "name": tool_name,
                "id": f"toolu_{message_id}_{index}",
                "input": {},
                "index": index,
            }
        ],
    )


def _emit_text(message_id: str, index: int, text: str) -> None:
    _emit_assistant_with_blocks(
        message_id,
        [{"type": "text", "text": text, "index": index}],
    )


def _emit_result(
    session_id: str,
    result_text: str,
    *,
    duration_ms: int = 1,
    total_cost_usd: float = 0.0001,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> None:
    _emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": duration_ms,
            "result": result_text,
            "session_id": session_id,
            "total_cost_usd": total_cost_usd,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    )


# ---------------------------------------------------------------------------
# Filesystem side effects (what the real model would have driven through
# Read/Write/Edit/Bash tool calls)
# ---------------------------------------------------------------------------


def _session_folder_from_cwd() -> Path:
    """The subchat CLI sets the child's cwd to the SessionFolder
    (``cwd: ../..`` in ``config.yaml`` resolved against the subchat folder).
    The fake just reads that — no need to receive an explicit path.
    """
    return Path(os.getcwd())


def _read_index_json(session_folder: Path) -> dict:
    p = session_folder / "transcript" / "index.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _atomic_write_text(path: Path, content: str) -> None:
    """Mirror subchat.py's atomic-write recipe — temp+rename in the same
    directory. Crashing the fake mid-scenario leaves either the old file
    or the new file, never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _seal_chapter(session_folder: Path, slug: str, start_index: int) -> tuple[str, list[int]]:
    """Materialize a chapter-file from every turn whose ``file`` field still
    points at its ``NNN_msg.md`` original, then rewrite ``index.json`` so
    every covered turn's ``file`` points at the new chapter file.

    Returns ``(chapter_filename, covered_indices)``. The covered range is
    derived from the in-flight index.json so the fake works against
    arbitrary harness-built transcripts (one turn or many).
    """
    transcript = session_folder / "transcript"
    index = _read_index_json(session_folder)
    turns: list[dict] = index["turns"]

    # Eligible turns = consecutive complete & still-unsealed turns starting
    # at ``start_index``. Stops at the first non-eligible turn (so the
    # right-edge gap policy is the harness's concern, not the fake's).
    covered: list[int] = []
    for t in turns:
        if t["index"] < start_index:
            continue
        if not t.get("complete"):
            break
        file_field = t.get("file", "")
        if not file_field.endswith("_msg.md"):
            # already sealed earlier (sealed runs idempotent)
            break
        covered.append(t["index"])

    if not covered:
        return "", []

    # Concatenate per-turn content, preserving each H2 heading.
    chunks: list[str] = []
    for idx in covered:
        per_turn = transcript / f"{idx:03d}_msg.md"
        if per_turn.exists():
            chunks.append(per_turn.read_text(encoding="utf-8"))
        else:
            # If the harness didn't pre-create one for this turn, fabricate
            # a minimal H2 so the seal still works.
            chunks.append(f"## {idx:03d} Stub\n\n*(no per-turn file)*\n")

    chapter_filename = f"{covered[0]:03d}_{slug}.md"
    chapter_path = transcript / chapter_filename
    _atomic_write_text(chapter_path, "\n".join(chunks))

    # Atomic index.json rewrite — repoint every covered turn at the chapter.
    for t in turns:
        if t["index"] in covered:
            t["file"] = chapter_filename
            if t.get("slug") is None:
                t["slug"] = slug
    _atomic_write_text(
        session_folder / "transcript" / "index.json",
        json.dumps(index, indent=2, ensure_ascii=False) + "\n",
    )

    # Best-effort delete of originals (step 3 of the seal commit). The seal
    # is already complete after the index rewrite — failure here is benign.
    for idx in covered:
        try:
            (transcript / f"{idx:03d}_msg.md").unlink()
        except FileNotFoundError:
            pass

    return chapter_filename, covered


def _scaffold_protocol(session_folder: Path, session_slug: str) -> bool:
    """Create ``protocol.md`` with the spec scaffold if absent. Return True
    if a new file was written (used by the harness to assert idempotence).
    """
    p = session_folder / "protocol.md"
    if p.exists():
        return False
    scaffold = (
        f"# Protocol — {session_slug}\n"
        f"\n"
        f"**Status:** {STATUS_OPEN_RU}\n"
        f"\n"
        f"## Chapters\n"
        f"\n"
        f"<!-- chapters appended below by Stage 2 -->\n"
    )
    _atomic_write_text(p, scaffold)
    return True


def _append_section(
    session_folder: Path,
    chapter_slug: str,
    start_index: int,
    end_index: int,
) -> bool:
    """Append one chapter section to ``protocol.md`` with the spec H3 anchor.

    Returns True if a new section was appended, False if the anchor was
    already present (idempotent re-run).
    """
    p = session_folder / "protocol.md"
    text = p.read_text(encoding="utf-8")
    anchor_prefix = f"### {start_index:03d} — "
    if anchor_prefix in text:
        return False  # already appended — idempotent skip

    # Use en-dash (U+2013) in the (turns NNN–MMM) span per spec.
    header = (
        f"### {start_index:03d} — {chapter_slug}  "
        f"*(turns {start_index:03d}–{end_index:03d})*"
    )
    body_prose = (
        f"Mock narrative for chapter {chapter_slug}: turns "
        f"{start_index:03d}–{end_index:03d} sealed by the fake claude. "
        f"This text is generated by the end-to-end test harness; it stands "
        f"in for the curated prose a real protocolist run would produce."
    )
    new_section = f"{header}\n\n{body_prose}\n"
    # Append at end-of-file — the scaffold's ``## Chapters`` is the last
    # section, so appending equals "end of Chapters".
    if not text.endswith("\n"):
        text = text + "\n"
    _atomic_write_text(p, text + "\n" + new_section)
    return True


def _finalize_protocol(session_folder: Path) -> bool:
    """Insert ``## Abstract`` after the H1 + flip ``**Status:**`` to
    ``завершена``. Idempotent: returns False if already finalized.
    """
    p = session_folder / "protocol.md"
    text = p.read_text(encoding="utf-8")
    has_abstract = "\n## Abstract\n" in ("\n" + text)
    already_finalized = f"**Status:** {STATUS_DONE_RU}" in text
    if has_abstract and already_finalized:
        return False

    new_text = text.replace(
        f"**Status:** {STATUS_OPEN_RU}",
        f"**Status:** {STATUS_DONE_RU}",
    )
    abstract_section = (
        "\n## Abstract\n\n"
        "Mock abstract for the end-to-end test session. Substitute for the "
        "1-3 paragraph compressed-session summary a real finalization run "
        "would compose.\n"
    )
    # Insert Abstract before ``## Chapters``.
    new_text = new_text.replace(
        "\n## Chapters\n",
        abstract_section + "\n## Chapters\n",
        1,
    )
    _atomic_write_text(p, new_text)
    return True


def _session_slug_from_cwd() -> str:
    """The SessionFolder's name is ``HHMM_<slug>``; return the slug portion.
    Defensive fallback returns the whole folder name if the prefix split
    doesn't match.
    """
    name = Path(os.getcwd()).name
    if "_" in name:
        prefix, _, slug = name.partition("_")
        if prefix.isdigit() and len(prefix) == 4 and slug:
            return slug
    return name


# ---------------------------------------------------------------------------
# Scenario runners
# ---------------------------------------------------------------------------


def _scenario_happy_ordinary(session_id: str) -> int:
    """!протокол happy path. Seal one chapter from whatever per-turn files
    the harness pre-populated, scaffold/append protocol.md, exit 0.
    """
    _emit_init(session_id)
    session_folder = _session_folder_from_cwd()

    # Stage 1: read transcripts (= [tool] Read events), then seal.
    _emit_tool_use("msg_stage1", 0, "Read")
    _emit_text("msg_stage1", 1, "gathered batch from msg-files")

    chapter_filename, covered = _seal_chapter(
        session_folder, CHAPTER_SLUG, CHAPTER_START_INDEX
    )
    if not chapter_filename:
        # Harness mis-set up; treat as no-op so test failure surfaces with a
        # clean exit rather than as a side-effect race.
        _emit_text("msg_stage1", 2, "no eligible content")
        _emit_result(session_id, "no eligible content", duration_ms=1)
        return 0

    _emit_tool_use("msg_stage1", 2, "Bash")
    _emit_text(
        "msg_stage1",
        3,
        f"sealed chapter {chapter_filename} "
        f"(turns {covered[0]:03d}–{covered[-1]:03d})",
    )

    # Stage 2: scaffold + append.
    _scaffold_protocol(session_folder, _session_slug_from_cwd())
    _emit_tool_use("msg_stage2", 0, "Write")
    appended = _append_section(
        session_folder, CHAPTER_SLUG, covered[0], covered[-1]
    )
    _emit_tool_use("msg_stage2", 1, "Edit")
    if appended:
        _emit_text(
            "msg_stage2",
            2,
            f"appended section {covered[0]:03d} — {CHAPTER_SLUG}",
        )

    summary = (
        f"done — sealed 1 chapter, appended 1 section, finalized no."
    )
    _emit_result(session_id, summary, duration_ms=42)
    return 0


def _scenario_happy_finish(session_id: str) -> int:
    _emit_init(session_id)
    session_folder = _session_folder_from_cwd()

    # Cleanup: read protocol.md to see current state.
    _emit_tool_use("msg_cleanup", 0, "Read")

    chapter_filename, covered = _seal_chapter(
        session_folder, CHAPTER_SLUG, CHAPTER_START_INDEX
    )
    if chapter_filename:
        _emit_tool_use("msg_stage1", 0, "Bash")
        _emit_text(
            "msg_stage1",
            1,
            f"sealed chapter {chapter_filename}",
        )

    # Stage 2 + Finalization.
    _scaffold_protocol(session_folder, _session_slug_from_cwd())
    if chapter_filename:
        _append_section(session_folder, CHAPTER_SLUG, covered[0], covered[-1])
        _emit_text(
            "msg_stage2",
            0,
            f"appended section {covered[0]:03d}",
        )
    flipped = _finalize_protocol(session_folder)
    _emit_tool_use("msg_finalize", 0, "Edit")
    if flipped:
        _emit_text("msg_finalize", 1, "finalized — status flipped to завершена")
    else:
        _emit_text("msg_finalize", 1, "already finalized — no changes")

    seal_count = 1 if chapter_filename else 0
    _emit_result(
        session_id,
        f"done — sealed {seal_count} chapter, "
        f"finalized yes.",
        duration_ms=84,
    )
    return 0


def _scenario_stderr_flood(session_id: str, *, finish: bool = False) -> int:
    """Run a happy scenario AND flood stderr > 100 KiB before the final
    ``result`` event. This is the T05d hard-prereq verification at the
    real-piping level (the unit test in cockpit/subchat/test_subchat.py
    does the same against the subchat plumbing; this one verifies the
    integration sees no deadlock).

    Order matters: emit one stdout event so the parent has something to
    consume, THEN flood stderr (which would block the child if subchat's
    drain thread were absent / broken), THEN emit the terminal result.
    """
    _emit_init(session_id)
    _emit_tool_use("msg_stderr", 0, "Read")

    # Real claude's --verbose writes diagnostic chatter to stderr. Mimic
    # that with a multi-line flood; > 100 KiB total. Use single-line writes
    # so the OS pipe buffer fills predictably.
    line = "X" * 256 + "\n"
    written = 0
    while written < STDERR_FLOOD_BYTES:
        sys.stderr.write(line)
        written += len(line)
    sys.stderr.flush()

    # Now do the real work and emit the result. If subchat's stderr-drain
    # thread is missing or broken, the previous stderr.write calls would have
    # blocked us at ~64 KiB and we'd never reach this line.
    session_folder = _session_folder_from_cwd()
    chapter_filename, covered = _seal_chapter(
        session_folder, CHAPTER_SLUG, CHAPTER_START_INDEX
    )
    if chapter_filename:
        _scaffold_protocol(session_folder, _session_slug_from_cwd())
        _append_section(session_folder, CHAPTER_SLUG, covered[0], covered[-1])
        if finish:
            _finalize_protocol(session_folder)

    _emit_text(
        "msg_stderr",
        1,
        "stderr flood survived; chapter sealed under load",
    )
    _emit_result(session_id, "done — stderr-drain integration verified",
                 duration_ms=99)
    return 0


def _scenario_no_op(session_id: str) -> int:
    """``!протокол`` with < 30 KB eligible — refuse to do anything. The
    profile's contract: emit a short text + exit 0 (NO transcript writes).
    """
    _emit_init(session_id)
    _emit_text("msg_noop", 0, "no eligible content (< 30 KB)")
    _emit_result(session_id, "no eligible content", duration_ms=1)
    return 0


def _scenario_exit_nonzero(session_id: str) -> int:
    """Emit an error and exit non-zero. Used to verify subchat's [error]/
    [warn] exit propagation.
    """
    _emit_init(session_id)
    sys.stderr.write("simulated claude failure: invalid model\n")
    sys.stderr.flush()
    # No `result` event — subchat will surface ``[error] malformed output``.
    return 2


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


SCENARIOS = {
    "happy_ordinary": lambda sid: _scenario_happy_ordinary(sid),
    "happy_finish": lambda sid: _scenario_happy_finish(sid),
    "stderr_flood": lambda sid: _scenario_stderr_flood(sid, finish=False),
    "stderr_flood_finish": lambda sid: _scenario_stderr_flood(sid, finish=True),
    "no_op": lambda sid: _scenario_no_op(sid),
    "exit_nonzero": lambda sid: _scenario_exit_nonzero(sid),
}


def main(argv: list[str]) -> int:
    parsed = _parse_argv(argv)

    # Defensive sanity — the harness always passes these; failing fast helps
    # diagnose misconfigured tests.
    if parsed["output_format"] != "stream-json":
        sys.stderr.write(
            f"fake_claude: only output-format=stream-json is implemented "
            f"(got {parsed['output_format']!r})\n"
        )
        return 64
    sp = parsed.get("system_prompt_file")
    if sp and not Path(sp).is_file():
        sys.stderr.write(
            f"fake_claude: --system-prompt-file does not exist: {sp}\n"
        )
        return 64

    scenario = os.environ.get(ENV_SCENARIO, DEFAULT_SCENARIO)
    if scenario not in SCENARIOS:
        sys.stderr.write(
            f"fake_claude: unknown scenario {scenario!r}; "
            f"set {ENV_SCENARIO}=<one of: {', '.join(sorted(SCENARIOS))}>\n"
        )
        return 64

    session_id = os.environ.get(ENV_SESSION_ID, DEFAULT_SESSION_ID)
    return SCENARIOS[scenario](session_id)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
