#!/usr/bin/env python3
"""Igor cockpit hook: UserPromptSubmit.

Fires when the user submits a prompt to Claude Code, before the agent begins
its turn. Ensures the SessionFolder + SessionStateFile for the current
``session_id`` exist so the agent has a valid ``state.md`` to operate on from
Turn 1.

The hook is **idempotent** — re-firing or firing after the Stop hook has
already bootstrapped this session is a no-op.

On resumed chats (Claude Code generates a new ``session_id`` for an existing
logical chat), the JSONL transcript already contains the chat's first user
prompt. The hook reads ``turns[0].prompt_id`` from the JSONL and asks the
shared bootstrap helper to alias the new SessionStateFile to the existing
SessionFolder, so resumed chats land in their original journal entry rather
than creating a duplicate.

Errors are logged to stderr; the process always exits 0 (a hook failure must
never block the user's turn).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make sibling ``session_bootstrap`` importable without packaging.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_bootstrap as sb  # noqa: E402  pylint: disable=wrong-import-position


HOOK_EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
LOG_PREFIX = "[igor:user-prompt-submit]"


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        _log(f"failed to parse payload: {e}")
        return 0

    event_name = payload.get("hook_event_name")
    if event_name and event_name != HOOK_EVENT_USER_PROMPT_SUBMIT:
        return 0

    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    transcript_path = payload.get("transcript_path")
    if not session_id or not cwd:
        _log("missing session_id or cwd in payload; skipping")
        return 0

    context_root = Path(cwd)
    # Safety check: refuse to operate unless ``cwd`` is an actual cockpit
    # context. Without this, a stray hook fire from an unrelated cwd would
    # scaffold a ``journal/`` subtree there.
    if not (context_root / "context.json").is_file():
        return 0

    # Probe the JSONL for an existing first user prompt — this is what makes
    # resumed-chat detection work in the pre-turn hook. On the first turn of
    # a brand-new chat the JSONL is empty (or contains only the prompt that
    # is being submitted right now without a recorded timestamp); both
    # outcomes map cleanly to bootstrap-fresh.
    first_prompt_id = None
    first_prompt_at = None
    if transcript_path:
        first_prompt_id, first_prompt_at = sb.extract_first_prompt_from_jsonl(
            Path(transcript_path)
        )

    try:
        sb.ensure_session_state(
            context_root, session_id, first_prompt_id, first_prompt_at
        )
    except Exception as e:  # pylint: disable=broad-except
        _log(f"unexpected error: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
