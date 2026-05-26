#!/usr/bin/env python3
"""Unit tests for the shared ``session_bootstrap`` module.

Run from this directory:

    python3 -m unittest test_session_bootstrap.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# Make sibling module importable without packaging.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_bootstrap as sb  # noqa: E402  pylint: disable=wrong-import-position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_session(
    context_root: Path, hhmm: str, first_prompt_id: str | None, day: str = "21"
) -> Path:
    folder = context_root / "journal" / "2026" / "05" / day / f"{hhmm}_session"
    (folder / "transcript").mkdir(parents=True, exist_ok=True)
    turns = []
    if first_prompt_id is not None:
        turns.append({
            "index": 0,
            "slug": None,
            "file": "000_msg.md",
            "prompt_id": first_prompt_id,
        })
    (folder / "transcript" / "index.json").write_text(
        json.dumps({"next_index": len(turns), "turns": turns, "schema_version": 1}),
        encoding="utf-8",
    )
    return folder


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# ensure_session_state — primary entry point
# ---------------------------------------------------------------------------


class TestEnsureSessionState(unittest.TestCase):

    def test_creates_fresh_session_when_no_prior_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ts = datetime(2026, 5, 21, 14, 30, 0)
            state = sb.ensure_session_state(
                root, session_id="sid-1", first_prompt_id="p1", first_prompt_at=ts
            )
            self.assertIsNotNone(state)
            self.assertEqual(state[sb.STATE_KEY_FIRST_PROMPT_ID], "p1")
            folder = Path(state[sb.STATE_KEY_FOLDER])
            self.assertEqual(
                folder, root / "journal" / "2026" / "05" / "21" / "1430_session"
            )
            self.assertTrue((folder / "state.md").is_file())
            self.assertTrue((folder / "transcript" / "index.json").is_file())
            # SessionStateFile persisted
            state_path = root / ".claude" / "sessions" / "sid-1.json"
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")), state
            )

    def test_idempotent_when_state_file_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Seed an existing SessionStateFile pointing at a custom folder.
            sessions_dir = root / ".claude" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            custom_folder = root / "journal" / "2026" / "05" / "21" / "0900_custom"
            (custom_folder / "transcript").mkdir(parents=True, exist_ok=True)
            (sessions_dir / "sid-2.json").write_text(
                json.dumps({sb.STATE_KEY_FOLDER: str(custom_folder)}) + "\n",
                encoding="utf-8",
            )
            # Calling ensure_session_state must return the existing pointer
            # unchanged — first_prompt_id supplied should NOT trigger a rewrite.
            state = sb.ensure_session_state(
                root, session_id="sid-2", first_prompt_id="newprompt"
            )
            self.assertEqual(Path(state[sb.STATE_KEY_FOLDER]), custom_folder)
            self.assertNotIn(sb.STATE_KEY_FIRST_PROMPT_ID, state)

    def test_aliases_existing_folder_on_resumed_chat(self):
        """Resumed chat: a new session_id pointing at the SAME logical chat
        (same first_prompt_id). Bootstrap must locate the existing folder via
        scan, not create a new one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = _seed_session(root, "1400", "shared-prompt")
            ts = datetime(2026, 5, 21, 18, 0, 0)
            state = sb.ensure_session_state(
                root,
                session_id="resumed-new-sid",
                first_prompt_id="shared-prompt",
                first_prompt_at=ts,
            )
            self.assertEqual(Path(state[sb.STATE_KEY_FOLDER]), existing)
            self.assertEqual(state[sb.STATE_KEY_FIRST_PROMPT_ID], "shared-prompt")

    def test_creates_fresh_folder_when_no_first_prompt_id_yet(self):
        """UserPromptSubmit hook case: JSONL is empty, no first_prompt_id to
        match on. Bootstrap creates a fresh folder and writes the
        SessionStateFile without first_prompt_id."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = sb.ensure_session_state(root, session_id="sid-3")
            self.assertIsNotNone(state)
            self.assertNotIn(sb.STATE_KEY_FIRST_PROMPT_ID, state)
            folder = Path(state[sb.STATE_KEY_FOLDER])
            self.assertTrue(folder.is_dir())
            self.assertTrue((folder / "state.md").is_file())

    def test_unreadable_state_file_triggers_rebootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_dir = root / ".claude" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            (sessions_dir / "sid-4.json").write_text("garbage{not json", encoding="utf-8")
            state = sb.ensure_session_state(root, session_id="sid-4", first_prompt_id="p4")
            self.assertIsNotNone(state)
            self.assertEqual(state[sb.STATE_KEY_FIRST_PROMPT_ID], "p4")


# ---------------------------------------------------------------------------
# fill_missing_first_prompt_id
# ---------------------------------------------------------------------------


class TestFillMissingFirstPromptId(unittest.TestCase):

    def test_writes_when_field_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp)
            (sessions_dir / "sid.json").write_text(
                json.dumps({sb.STATE_KEY_FOLDER: "/some/path"}) + "\n",
                encoding="utf-8",
            )
            result = sb.fill_missing_first_prompt_id(sessions_dir, "sid", "p1")
            self.assertTrue(result)
            data = json.loads((sessions_dir / "sid.json").read_text(encoding="utf-8"))
            self.assertEqual(data[sb.STATE_KEY_FIRST_PROMPT_ID], "p1")

    def test_noop_when_already_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp)
            initial = {
                sb.STATE_KEY_FOLDER: "/some/path",
                sb.STATE_KEY_FIRST_PROMPT_ID: "already-here",
            }
            (sessions_dir / "sid.json").write_text(json.dumps(initial) + "\n", encoding="utf-8")
            result = sb.fill_missing_first_prompt_id(sessions_dir, "sid", "different")
            self.assertFalse(result)
            data = json.loads((sessions_dir / "sid.json").read_text(encoding="utf-8"))
            self.assertEqual(data[sb.STATE_KEY_FIRST_PROMPT_ID], "already-here")

    def test_returns_false_when_state_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp)
            self.assertFalse(sb.fill_missing_first_prompt_id(sessions_dir, "missing", "p"))


# ---------------------------------------------------------------------------
# extract_first_prompt_from_jsonl
# ---------------------------------------------------------------------------


class TestExtractFirstPromptFromJsonl(unittest.TestCase):

    def test_extracts_first_real_user_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "x.jsonl"
            _write_jsonl(jsonl, [
                {
                    "type": "user",
                    "isSidechain": False,
                    "promptId": "first",
                    "timestamp": "2026-05-21T14:30:00Z",
                    "message": {"role": "user", "content": "hi"},
                },
                {
                    "type": "user",
                    "isSidechain": False,
                    "promptId": "second",
                    "timestamp": "2026-05-21T14:31:00Z",
                    "message": {"role": "user", "content": "again"},
                },
            ])
            pid, ts = sb.extract_first_prompt_from_jsonl(jsonl)
            self.assertEqual(pid, "first")
            self.assertEqual(ts.year, 2026)
            self.assertEqual(ts.month, 5)
            self.assertEqual(ts.day, 21)

    def test_skips_sidechain_user_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "x.jsonl"
            _write_jsonl(jsonl, [
                {
                    "type": "user",
                    "isSidechain": True,
                    "promptId": "side",
                    "message": {"role": "user", "content": "ignored"},
                },
                {
                    "type": "user",
                    "isSidechain": False,
                    "promptId": "real",
                    "timestamp": "2026-05-21T14:30:00Z",
                    "message": {"role": "user", "content": "real"},
                },
            ])
            pid, _ = sb.extract_first_prompt_from_jsonl(jsonl)
            self.assertEqual(pid, "real")

    def test_skips_tool_result_user_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "x.jsonl"
            _write_jsonl(jsonl, [
                {
                    "type": "user",
                    "isSidechain": False,
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}],
                    },
                },
                {
                    "type": "user",
                    "isSidechain": False,
                    "promptId": "real",
                    "message": {"role": "user", "content": "real prompt"},
                },
            ])
            pid, _ = sb.extract_first_prompt_from_jsonl(jsonl)
            self.assertEqual(pid, "real")

    def test_missing_jsonl_returns_none(self):
        pid, ts = sb.extract_first_prompt_from_jsonl(Path("/nonexistent/path.jsonl"))
        self.assertIsNone(pid)
        self.assertIsNone(ts)

    def test_empty_jsonl_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "empty.jsonl"
            jsonl.write_text("", encoding="utf-8")
            pid, ts = sb.extract_first_prompt_from_jsonl(jsonl)
            self.assertIsNone(pid)
            self.assertIsNone(ts)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWriteText(unittest.TestCase):

    def test_writes_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "out.txt"
            sb.atomic_write_text(path, "hello\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            path.write_text("old", encoding="utf-8")
            sb.atomic_write_text(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "new")

    def test_no_tmp_file_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            sb.atomic_write_text(path, "x")
            siblings = list(Path(tmp).iterdir())
            self.assertEqual([p.name for p in siblings], ["out.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
