"""Shared SessionFolder / SessionStateFile bootstrap logic.

Both the UserPromptSubmit hook (pre-turn) and the Stop hook (post-turn) need
to ensure the SessionStateFile and SessionFolder exist for a given
``session_id``. This module centralises that logic so the two hooks stay in
lockstep without duplicating code.

Bootstrap strategy:
- If SessionStateFile already exists for ``session_id`` → no-op, return its
  parsed contents.
- Else, if a ``first_prompt_id`` was provided (extracted from JSONL by the
  caller), scan existing SessionFolders for one whose ``turns[0].prompt_id``
  matches — this is the resumed-chat case where Claude Code generated a new
  ``session_id`` for an existing logical chat. On match: alias to that
  existing folder.
- Else: create a fresh SessionFolder named after ``first_prompt_at`` (or
  ``now()`` if unknown), scaffold it, and write a new SessionStateFile.

All filesystem writes are atomic (temp + ``os.replace``). The module is
``stdlib``-only.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SessionStateFile schema keys
STATE_KEY_FOLDER = "session_folder"
STATE_KEY_FIRST_PROMPT_ID = "first_prompt_id"

# Default placeholder slug used until the agent calls rename_current_session
DEFAULT_SLUG = "session"

# Initial state.md template (fresh session)
INITIAL_STATE_MD = """# Session state

## Input

*пусто*

## Scope

*пусто*

## Problems

*пусто*
"""

# transcript/index.json schema
INDEX_SCHEMA_VERSION = 1
INDEX_KEY_TURNS = "turns"
INDEX_KEY_NEXT = "next_index"
INDEX_KEY_VERSION = "schema_version"

INITIAL_INDEX: dict = {
    INDEX_KEY_NEXT: 0,
    INDEX_KEY_TURNS: [],
    INDEX_KEY_VERSION: INDEX_SCHEMA_VERSION,
}

# JSONL entry types used for first-prompt extraction
_ENTRY_USER = "user"
_BLOCK_TOOL_RESULT = "tool_result"

LOG_PREFIX = "[igor:session-bootstrap]"


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via temp + ``os.replace``."""
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


# ---------------------------------------------------------------------------
# Time / path helpers
# ---------------------------------------------------------------------------


def parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from JSONL, accepting both 'Z' and
    '+00:00' offset forms. Returns None on any failure."""
    if not ts or not isinstance(ts, str):
        return None
    text = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def compute_fresh_session_folder(context_root: Path, started_at: datetime) -> Path:
    """Compute the default SessionFolder path for a brand-new session,
    named after ``started_at``."""
    return (
        context_root
        / "journal"
        / started_at.strftime("%Y")
        / started_at.strftime("%m")
        / started_at.strftime("%d")
        / f"{started_at.strftime('%H%M')}_{DEFAULT_SLUG}"
    )


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------


def ensure_session_scaffold(session_folder: Path) -> bool:
    """Materialise the SessionFolder scaffold idempotently. Creates the
    folder, ``transcript/index.json``, and ``state.md`` if absent; never
    overwrites existing files.

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
            atomic_write_text(index_path, json.dumps(INITIAL_INDEX, indent=2) + "\n")
        except OSError as e:
            _log(f"failed to write {index_path}: {e}")
            return False

    state_md_path = session_folder / "state.md"
    if not state_md_path.exists():
        try:
            atomic_write_text(state_md_path, INITIAL_STATE_MD)
        except OSError as e:
            _log(f"failed to write {state_md_path}: {e}")
            return False

    return True


# ---------------------------------------------------------------------------
# Resumed-chat detection
# ---------------------------------------------------------------------------


def find_folder_by_first_prompt_id(
    context_root: Path, first_prompt_id: str
) -> Path | None:
    """Walk ``<context_root>/journal/`` looking for a SessionFolder whose
    ``transcript/index.json`` has ``turns[0].prompt_id == first_prompt_id``.
    Returns the folder path on first match, or None.
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


# ---------------------------------------------------------------------------
# JSONL helper — extract first real user prompt
# ---------------------------------------------------------------------------


def extract_first_prompt_from_jsonl(
    jsonl_path: Path,
) -> tuple[str | None, datetime | None]:
    """Return ``(first_prompt_id, first_prompt_at)`` from the JSONL transcript
    at ``jsonl_path``, or ``(None, None)`` if no real user prompt has been
    recorded yet (e.g., the file does not exist or contains only sidechain /
    tool-result entries).
    """
    if not jsonl_path.exists():
        return None, None
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
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != _ENTRY_USER:
                    continue
                if obj.get("isSidechain") is True:
                    continue
                msg = obj.get("message") or {}
                content = msg.get("content")
                # Skip tool_result-only user entries (they aren't real prompts).
                if isinstance(content, list):
                    blocks = [b for b in content if isinstance(b, dict)]
                    if blocks and all(b.get("type") == _BLOCK_TOOL_RESULT for b in blocks):
                        continue
                first_prompt_id = obj.get("promptId")
                first_prompt_at = parse_iso(obj.get("timestamp"))
                return first_prompt_id, first_prompt_at
    except OSError as e:
        _log(f"failed to read JSONL {jsonl_path}: {e}")
    return None, None


# ---------------------------------------------------------------------------
# Public entry point — used by both hooks
# ---------------------------------------------------------------------------


def ensure_session_state(
    context_root: Path,
    session_id: str,
    first_prompt_id: str | None = None,
    first_prompt_at: datetime | None = None,
) -> dict | None:
    """Ensure SessionStateFile for ``session_id`` exists and the
    SessionFolder it points at is scaffolded.

    Idempotent: if the SessionStateFile already exists, returns its parsed
    contents without modification.

    On a fresh session:
    - If ``first_prompt_id`` is given, scan existing SessionFolders for a
      match (resumed-chat detection); on match, alias to that folder.
    - Otherwise (or no match): create a fresh folder using
      ``first_prompt_at`` (or ``now()`` if absent) as the HHMM stamp.

    The written SessionStateFile contains ``session_folder`` always and
    ``first_prompt_id`` only when known. The Stop hook backfills
    ``first_prompt_id`` on its first fire if missing (see
    ``fill_missing_first_prompt_id``).

    Returns the state dict on success, or None on failure.
    """
    sessions_dir = context_root / ".claude" / "sessions"
    state_path = sessions_dir / f"{session_id}.json"

    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log(
                f"existing SessionStateFile {state_path} unreadable ({e});"
                f" re-bootstrapping"
            )

    folder: Path | None = None
    if first_prompt_id:
        folder = find_folder_by_first_prompt_id(context_root, first_prompt_id)
    if folder is None:
        started = first_prompt_at or datetime.now()
        folder = compute_fresh_session_folder(context_root, started)

    if not ensure_session_scaffold(folder):
        return None

    state: dict = {STATE_KEY_FOLDER: str(folder)}
    if first_prompt_id:
        state[STATE_KEY_FIRST_PROMPT_ID] = first_prompt_id

    try:
        atomic_write_text(
            state_path,
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        )
    except OSError as e:
        _log(f"failed to write {state_path}: {e}")
        return None
    return state


def fill_missing_first_prompt_id(
    sessions_dir: Path, session_id: str, first_prompt_id: str
) -> bool:
    """Write ``first_prompt_id`` into the SessionStateFile if it is currently
    absent or null. Atomic, idempotent.

    Returns True if the file was updated, False otherwise (already set,
    missing, or write failed). Used by the Stop hook to backfill the marker
    when UserPromptSubmit bootstrapped the SessionStateFile before any user
    prompt was visible in the JSONL.
    """
    state_path = sessions_dir / f"{session_id}.json"
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log(f"cannot read {state_path}: {e}")
        return False
    if state.get(STATE_KEY_FIRST_PROMPT_ID):
        return False
    state[STATE_KEY_FIRST_PROMPT_ID] = first_prompt_id
    try:
        atomic_write_text(
            state_path,
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        )
    except OSError as e:
        _log(f"failed to update {state_path}: {e}")
        return False
    return True
