#!/usr/bin/env python3
"""Igor cockpit hook: Stop.

Runs each time Claude Code finishes generating an assistant response on the
main chain. Self-bootstraps and reconciles ``<SessionFolder>/transcript/``
against the Claude Code transcript JSONL referenced by ``transcript_path``
in the hook payload.

On every fire:

1. If the SessionStateFile for ``session_id`` does not exist, the hook
   bootstraps a session: it reads the JSONL, finds the first real user
   prompt, derives the session start time and ``first_prompt_id``, attempts
   to discover an existing session folder with the same ``first_prompt_id``
   (so that resumed chats with a fresh ``session_id`` still reuse their
   folder), and otherwise computes a new folder path
   ``journal/YYYY/MM/DD/HHMM_session/``.
2. If the session folder + ``transcript/index.json`` do not yet exist,
   the hook creates the scaffold (folder, ``transcript/``,
   ``transcript/index.json``, ``state.md``).
3. The hook walks the JSONL and ensures one per-turn file in
   ``transcript/`` for every turn, with chronological block ordering
   (user / mid-flight / assistant blocks, merged when consecutive).

Guarantees:
- atomic writes (temp + ``os.replace``);
- single-writer lock per session (``.claude/sessions/<session_id>.lock``);
- idempotent: re-fires on unchanged JSONL are no-ops;
- chapter-safe: files shared by multiple ``index.json`` entries are left
  untouched (owned by a downstream merger);
- event-filtered: only ``hook_event_name == "Stop"`` is processed.

Errors are logged to stderr; the process always exits 0.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# JSONL entry types
ENTRY_USER = "user"
ENTRY_ASSISTANT = "assistant"
ENTRY_ATTACHMENT = "attachment"

# Anthropic content block types
BLOCK_TEXT = "text"
BLOCK_TOOL_USE = "tool_use"
BLOCK_TOOL_RESULT = "tool_result"

# Attachment subtypes
ATTACHMENT_QUEUED_COMMAND = "queued_command"

# Roles used in the rendered turn file
ROLE_USER = "user"
ROLE_MIDFLIGHT = "midflight"
ROLE_ASSISTANT = "assistant"

# Hook events
HOOK_EVENT_STOP = "Stop"

# Markers in turn files
INTERRUPTED_MARKER = "[interrupted - no assistant response]"
EMPTY_PROMPT_MARKER = "(empty)"

# index.json schema
INDEX_SCHEMA_VERSION = 1
INDEX_KEY_TURNS = "turns"
INDEX_KEY_NEXT = "next_index"
INDEX_KEY_VERSION = "schema_version"

# SessionStateFile schema
STATE_KEY_FOLDER = "session_folder"
STATE_KEY_FIRST_PROMPT_ID = "first_prompt_id"

# Lock acquisition
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_INTERVAL_SECONDS = 0.1

# Logging
LOG_PREFIX = "[igor:stop]"

# Default placeholder slug in fresh session folder names (HHMM_<slug>)
DEFAULT_SLUG = "session"

# Initial state.md content for a fresh session folder
INITIAL_STATE_MD = """# Session state

## Input

*пусто*

## Scope

*пусто*

## Problems

*пусто*
"""

# Initial transcript/index.json content
INITIAL_INDEX: dict = {
    INDEX_KEY_NEXT: 0,
    INDEX_KEY_TURNS: [],
    INDEX_KEY_VERSION: INDEX_SCHEMA_VERSION,
}

# Turn file label per role
_ROLE_LABEL = {
    ROLE_USER: "**User:**",
    ROLE_MIDFLIGHT: "**User (mid-flight):**",
    ROLE_ASSISTANT: "**Assistant:**",
}

# Regex: capture the first markdown H2 line of a turn file
_H2_LINE_RE = re.compile(r"^(##\s+\S[^\n]*\n)")


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Atomic write + file lock
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _file_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout
        acquired = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)
        if not acquired:
            raise TimeoutError(f"failed to acquire {lock_path} within {timeout}s")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Content rendering
# ---------------------------------------------------------------------------


def _render_tool_use_block(block: dict) -> str:
    tool_name = block.get("name", "?")
    inp = block.get("input")
    if isinstance(inp, dict) and inp:
        try:
            inp_str = json.dumps(inp, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            inp_str = str(inp)
        inp_str = inp_str.replace("\n", " ")
        if len(inp_str) > 120:
            inp_str = inp_str[:117] + "..."
        return f"[tool_use: {tool_name} {inp_str}]"
    return f"[tool_use: {tool_name}]"


def _render_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == BLOCK_TEXT:
            text = block.get("text")
            if isinstance(text, str):
                stripped = text.strip()
                if stripped:
                    parts.append(stripped)
        elif btype == BLOCK_TOOL_USE:
            parts.append(_render_tool_use_block(block))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Turn detection
# ---------------------------------------------------------------------------


def _is_main_chain(entry: dict) -> bool:
    return entry.get("isSidechain") is False


def _is_real_user_prompt(entry: dict) -> bool:
    if entry.get("type") != ENTRY_USER or not _is_main_chain(entry):
        return False
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == BLOCK_TOOL_RESULT:
                return False
    return True


def _queued_command_text(entry: dict) -> str | None:
    if entry.get("type") != ENTRY_ATTACHMENT or not _is_main_chain(entry):
        return None
    att = entry.get("attachment") or {}
    if att.get("type") != ATTACHMENT_QUEUED_COMMAND:
        return None
    prompt = att.get("prompt")
    if isinstance(prompt, list):
        parts: list[str] = []
        for block in prompt:
            if isinstance(block, dict) and block.get("type") == BLOCK_TEXT:
                text = block.get("text")
                if isinstance(text, str):
                    stripped = text.strip()
                    if stripped:
                        parts.append(stripped)
        if parts:
            return "\n\n".join(parts)
    elif isinstance(prompt, str):
        return prompt.strip() or None
    return None


def _load_entries(jsonl_path: Path) -> list[dict]:
    entries: list[dict] = []
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except FileNotFoundError:
        _log(f"transcript JSONL not found: {jsonl_path}")
    except OSError as e:
        _log(f"failed to read JSONL {jsonl_path}: {e}")
    return entries


def build_turns(entries: list[dict]) -> list[dict]:
    """Group JSONL entries into turns. See module docstring."""
    turns: list[dict] = []
    current: dict | None = None
    for e in entries:
        etype = e.get("type")
        if etype == ENTRY_USER and _is_real_user_prompt(e):
            if current is not None:
                turns.append(current)
            user_text = _render_content((e.get("message") or {}).get("content"))
            current = {
                "prompt_id": e.get("promptId"),
                "started_at": e.get("timestamp"),
                "ended_at": None,
                "blocks": [{"role": ROLE_USER, "text": user_text}],
            }
        elif etype == ENTRY_ASSISTANT and _is_main_chain(e) and current is not None:
            rendered = _render_content((e.get("message") or {}).get("content"))
            if rendered:
                current["blocks"].append({"role": ROLE_ASSISTANT, "text": rendered})
            ts = e.get("timestamp")
            if ts:
                current["ended_at"] = ts
        elif etype == ENTRY_ATTACHMENT and current is not None:
            qtext = _queued_command_text(e)
            if qtext:
                current["blocks"].append({"role": ROLE_MIDFLIGHT, "text": qtext})
    if current is not None:
        turns.append(current)
    for t in turns:
        blocks = t["blocks"]
        t["assistant_count"] = sum(1 for b in blocks if b["role"] == ROLE_ASSISTANT)
        t["midflight_count"] = sum(1 for b in blocks if b["role"] == ROLE_MIDFLIGHT)
    return turns


# ---------------------------------------------------------------------------
# Body construction
# ---------------------------------------------------------------------------


def build_body(blocks: list[dict]) -> str:
    if not blocks:
        return (
            f"{_ROLE_LABEL[ROLE_USER]}\n\n{EMPTY_PROMPT_MARKER}\n\n"
            f"{_ROLE_LABEL[ROLE_ASSISTANT]}\n\n{INTERRUPTED_MARKER}\n"
        )

    merged: list[tuple[str, str]] = []
    for b in blocks:
        role = b.get("role")
        text = (b.get("text") or "").strip()
        if not role or not text:
            continue
        if merged and merged[-1][0] == role:
            prev_role, prev_text = merged[-1]
            merged[-1] = (role, prev_text + "\n\n" + text)
        else:
            merged.append((role, text))

    has_assistant = any(r == ROLE_ASSISTANT for r, _ in merged)
    if not has_assistant:
        merged.append((ROLE_ASSISTANT, INTERRUPTED_MARKER))

    return "\n".join(f"{_ROLE_LABEL[role]}\n\n{text}\n" for role, text in merged)


def _read_existing_header(turn_file: Path, fallback_index: int) -> str:
    try:
        original = turn_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        original = ""
    m = _H2_LINE_RE.match(original)
    if m:
        return m.group(1)
    return f"## {fallback_index:03d}\n"


def _file_is_interrupted(turn_file: Path) -> bool:
    try:
        return INTERRUPTED_MARKER in turn_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    # Accept both "Z" and "+00:00" forms.
    text = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _find_folder_by_first_prompt_id(context_root: Path, first_prompt_id: str) -> Path | None:
    """Walk ``<context_root>/journal/`` looking for a session folder whose
    ``transcript/index.json`` has ``turns[0].prompt_id == first_prompt_id``.

    Returns the folder path on first match, or None. The walk is bounded by
    the journal structure (year/month/day/session) — no deeper traversal.
    """
    journal_root = context_root / "journal"
    if not journal_root.is_dir():
        return None
    try:
        for year_dir in sorted(journal_root.iterdir()):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir()):
                    if not day_dir.is_dir():
                        continue
                    for session_dir in sorted(day_dir.iterdir()):
                        if not session_dir.is_dir():
                            continue
                        index_path = session_dir / "transcript" / "index.json"
                        try:
                            idx = json.loads(index_path.read_text(encoding="utf-8"))
                        except (FileNotFoundError, OSError, json.JSONDecodeError):
                            continue
                        turns = idx.get(INDEX_KEY_TURNS) or []
                        if turns and turns[0].get("prompt_id") == first_prompt_id:
                            return session_dir
    except OSError as e:
        _log(f"journal scan failed under {journal_root}: {e}")
    return None


def _compute_fresh_session_folder(context_root: Path, started_at: datetime) -> Path:
    """Compute the default session folder for a new session, named after the
    actual session start timestamp (not the time stop.py runs)."""
    return (
        context_root
        / "journal"
        / started_at.strftime("%Y")
        / started_at.strftime("%m")
        / started_at.strftime("%d")
        / f"{started_at.strftime('%H%M')}_{DEFAULT_SLUG}"
    )


def _ensure_session_scaffold(session_folder: Path) -> bool:
    """Create the session folder + transcript/ + initial files if absent.
    Returns True on success.
    """
    transcript_folder = session_folder / "transcript"
    try:
        transcript_folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log(f"failed to create {transcript_folder}: {e}")
        return False

    index_path = transcript_folder / "index.json"
    if not index_path.exists():
        try:
            _atomic_write_text(index_path, json.dumps(INITIAL_INDEX, indent=2) + "\n")
        except OSError as e:
            _log(f"failed to write {index_path}: {e}")
            return False

    state_md_path = session_folder / "state.md"
    if not state_md_path.exists():
        try:
            _atomic_write_text(state_md_path, INITIAL_STATE_MD)
        except OSError as e:
            _log(f"failed to write {state_md_path}: {e}")
            return False

    return True


def _bootstrap_state(
    state_path: Path,
    context_root: Path,
    turns: list[dict],
) -> dict | None:
    """Create a fresh SessionStateFile from JSONL turns.

    Looks up an existing folder by ``first_prompt_id`` first (handles the
    case where Claude Code generated a new ``session_id`` for a chat that
    was already captured under a different ``session_id``). Otherwise
    derives the folder name from the first user prompt's timestamp.
    """
    if not turns:
        return None
    first = turns[0]
    first_prompt_id = first.get("prompt_id")
    started_at = _parse_iso(first.get("started_at")) or datetime.now()

    folder: Path | None = None
    if first_prompt_id:
        folder = _find_folder_by_first_prompt_id(context_root, first_prompt_id)
    if folder is None:
        folder = _compute_fresh_session_folder(context_root, started_at)

    if not _ensure_session_scaffold(folder):
        return None

    state = {
        STATE_KEY_FOLDER: str(folder),
        STATE_KEY_FIRST_PROMPT_ID: first_prompt_id,
    }
    try:
        _atomic_write_text(
            state_path,
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        )
    except OSError as e:
        _log(f"failed to write {state_path}: {e}")
        return None
    return state


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _needs_action(entry: dict, turn: dict, turn_file: Path) -> bool:
    complete = entry.get("complete")
    recorded_midflight = entry.get("midflight_count")
    recorded_assistant = entry.get("assistant_count")
    cur_midflight = turn.get("midflight_count", 0)
    cur_assistant = turn.get("assistant_count", 0)

    if not turn_file.exists():
        # Slug is set only after the indexer has owned the file (renamed it).
        # If the file at ``entry.file`` is missing but ``slug`` is set, the
        # index.json view is stale (a concurrent indexer just renamed the
        # file but its update hasn't reached us yet). Do NOT recreate at
        # the old path — that would resurrect a zombie file.
        if entry.get("slug") is not None:
            return False
        return True
    if complete is False:
        return True
    if complete is None and _file_is_interrupted(turn_file):
        return True
    if recorded_midflight is None and cur_midflight > 0:
        return True
    if isinstance(recorded_midflight, int) and recorded_midflight < cur_midflight:
        return True
    if recorded_assistant is None and cur_assistant > 0:
        return True
    if isinstance(recorded_assistant, int) and recorded_assistant < cur_assistant:
        return True
    return False


def _reconcile_existing(
    written: list[dict],
    turns: list[dict],
    transcript_folder: Path,
    shared_files: set[str],
) -> bool:
    changed = False
    for i, entry in enumerate(written):
        if i >= len(turns):
            break
        file_name = entry.get("file")
        if not file_name or file_name in shared_files:
            continue

        turn = turns[i]
        turn_file = transcript_folder / file_name

        if not _needs_action(entry, turn, turn_file):
            continue

        blocks = turn.get("blocks") or []
        if turn_file.exists():
            header = _read_existing_header(turn_file, i)
        else:
            slug = entry.get("slug")
            header = f"## {i:03d} ({slug})\n" if slug else f"## {i:03d}\n"

        try:
            _atomic_write_text(turn_file, header + "\n" + build_body(blocks))
        except OSError as e:
            _log(f"failed to write {turn_file}: {e}")
            continue

        entry["complete"] = turn.get("assistant_count", 0) > 0
        entry["midflight_count"] = turn.get("midflight_count", 0)
        entry["assistant_count"] = turn.get("assistant_count", 0)
        if turn.get("started_at"):
            entry["started_at"] = turn["started_at"]
        if turn.get("ended_at"):
            entry["ended_at"] = turn["ended_at"]
        changed = True
    return changed


def _append_new_turns(
    written: list[dict],
    turns: list[dict],
    transcript_folder: Path,
) -> bool:
    changed = False
    n_written = len(written)
    for i in range(n_written, len(turns)):
        turn = turns[i]
        turn_file_name = f"{i:03d}_msg.md"
        turn_file = transcript_folder / turn_file_name
        header = f"## {i:03d}\n\n"

        try:
            _atomic_write_text(turn_file, header + build_body(turn.get("blocks") or []))
        except OSError as e:
            _log(f"failed to write {turn_file}: {e}")
            continue

        written.append({
            "index": i,
            "slug": None,
            "file": turn_file_name,
            "prompt_id": turn.get("prompt_id"),
            "started_at": turn.get("started_at"),
            "ended_at": turn.get("ended_at"),
            "complete": turn.get("assistant_count", 0) > 0,
            "midflight_count": turn.get("midflight_count", 0),
            "assistant_count": turn.get("assistant_count", 0),
        })
        changed = True
    return changed


def _compute_shared_files(written: list[dict]) -> set[str]:
    counts: dict[str, int] = {}
    for entry in written:
        fn = entry.get("file")
        if fn:
            counts[fn] = counts.get(fn, 0) + 1
    return {fn for fn, c in counts.items() if c > 1}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        _log(f"failed to read {path}: {e}")
        return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        _log(f"failed to read payload: {e}")
        return 0

    event_name = payload.get("hook_event_name")
    if event_name and event_name != HOOK_EVENT_STOP:
        return 0

    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    transcript_path = payload.get("transcript_path")
    if not session_id or not cwd or not transcript_path:
        _log("missing session_id, cwd, or transcript_path")
        return 0

    context_root = Path(cwd)
    # Safety check: refuse to operate unless `cwd` is an actual cockpit
    # context (has context.json at its root). Without this, a stray hook
    # fire from any unrelated cwd would scaffold a journal/ subtree there.
    if not (context_root / "context.json").is_file():
        _log(f"cwd is not a cockpit context (no context.json): {context_root}; skipping")
        return 0
    sessions_dir = context_root / ".claude" / "sessions"
    state_path = sessions_dir / f"{session_id}.json"
    lock_path = sessions_dir / f"{session_id}.lock"

    try:
        with _file_lock(lock_path):
            entries = _load_entries(Path(transcript_path))
            turns = build_turns(entries)
            if not turns:
                # nothing to record yet (e.g., a ghost stop with empty JSONL)
                return 0

            state = _read_json(state_path) if state_path.exists() else None
            if state is None:
                state = _bootstrap_state(state_path, context_root, turns)
                if state is None:
                    return 0

            session_folder = Path(state.get(STATE_KEY_FOLDER, ""))
            transcript_folder = session_folder / "transcript"
            index_path = transcript_folder / "index.json"

            if not index_path.exists():
                # SessionStateFile pointed to a folder whose scaffold is gone;
                # rebuild from the same path.
                if not _ensure_session_scaffold(session_folder):
                    return 0

            index = _read_json(index_path)
            if index is None:
                return 0

            written: list[dict] = index.get(INDEX_KEY_TURNS, [])
            shared = _compute_shared_files(written)

            c1 = _reconcile_existing(written, turns, transcript_folder, shared)
            c2 = _append_new_turns(written, turns, transcript_folder)
            if not (c1 or c2):
                return 0

            index[INDEX_KEY_TURNS] = written
            index[INDEX_KEY_NEXT] = len(written)
            index.setdefault(INDEX_KEY_VERSION, INDEX_SCHEMA_VERSION)
            try:
                _atomic_write_text(
                    index_path,
                    json.dumps(index, indent=2, ensure_ascii=False) + "\n",
                )
            except OSError as e:
                _log(f"failed to update {index_path}: {e}")
    except TimeoutError as e:
        _log(str(e))
    except Exception as e:
        _log(f"unexpected error: {e!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
