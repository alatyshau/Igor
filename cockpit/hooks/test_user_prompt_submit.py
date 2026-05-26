#!/usr/bin/env python3
"""Integration tests for the UserPromptSubmit hook entry point.

Spawn the hook script as a subprocess with a JSON payload on stdin and assert
on the resulting filesystem state. This mirrors how Claude Code invokes the
hook in production.

Run from this directory:

    python3 -m unittest test_user_prompt_submit.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_SCRIPT = Path(__file__).resolve().parent / "user_prompt_submit.py"


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


def _make_context_root(tmp: str) -> Path:
    root = Path(tmp)
    (root / "context.json").write_text(json.dumps({"name": "test"}) + "\n", encoding="utf-8")
    return root


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


class TestUserPromptSubmitHook(unittest.TestCase):

    def test_bootstraps_fresh_session_when_state_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context_root(tmp)
            result = _run_hook({
                "hook_event_name": "UserPromptSubmit",
                "session_id": "sid-fresh",
                "cwd": str(root),
                "transcript_path": "",
                "prompt": "hello",
            })
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state_path = root / ".claude" / "sessions" / "sid-fresh.json"
            self.assertTrue(state_path.is_file())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            folder = Path(state["session_folder"])
            self.assertTrue((folder / "state.md").is_file())
            self.assertTrue((folder / "transcript" / "index.json").is_file())
            # first_prompt_id is intentionally absent — JSONL was empty
            self.assertNotIn("first_prompt_id", state)

    def test_idempotent_on_existing_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context_root(tmp)
            sessions_dir = root / ".claude" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            preset_folder = root / "journal" / "2026" / "05" / "21" / "0900_preset"
            (preset_folder / "transcript").mkdir(parents=True, exist_ok=True)
            (sessions_dir / "sid-existing.json").write_text(
                json.dumps({
                    "session_folder": str(preset_folder),
                    "first_prompt_id": "p-existing",
                }) + "\n",
                encoding="utf-8",
            )
            result = _run_hook({
                "hook_event_name": "UserPromptSubmit",
                "session_id": "sid-existing",
                "cwd": str(root),
            })
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state = json.loads((sessions_dir / "sid-existing.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(state["session_folder"]), preset_folder)
            self.assertEqual(state["first_prompt_id"], "p-existing")

    def test_resume_detection_aliases_to_existing_folder(self):
        """When JSONL already contains a user prompt (resumed chat), the hook
        must find the original SessionFolder via prompt_id scan and write a
        SessionStateFile alias pointing at it — not create a duplicate."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context_root(tmp)
            # Seed an existing SessionFolder with a known first_prompt_id
            original_folder = root / "journal" / "2026" / "05" / "21" / "1400_original"
            (original_folder / "transcript").mkdir(parents=True, exist_ok=True)
            (original_folder / "transcript" / "index.json").write_text(
                json.dumps({
                    "next_index": 1,
                    "turns": [{
                        "index": 0,
                        "slug": None,
                        "file": "000_msg.md",
                        "prompt_id": "p-resumed",
                    }],
                    "schema_version": 1,
                }),
                encoding="utf-8",
            )
            # JSONL contains the resumed chat with same first prompt_id
            jsonl = Path(tmp) / "transcript.jsonl"
            _write_jsonl(jsonl, [{
                "type": "user",
                "isSidechain": False,
                "promptId": "p-resumed",
                "timestamp": "2026-05-21T14:00:00Z",
                "message": {"role": "user", "content": "resumed prompt"},
            }])
            result = _run_hook({
                "hook_event_name": "UserPromptSubmit",
                "session_id": "sid-resumed-new",
                "cwd": str(root),
                "transcript_path": str(jsonl),
            })
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state = json.loads(
                (root / ".claude" / "sessions" / "sid-resumed-new.json").read_text(encoding="utf-8")
            )
            # The alias must point at the ORIGINAL folder, not create a new one
            self.assertEqual(Path(state["session_folder"]), original_folder)
            self.assertEqual(state["first_prompt_id"], "p-resumed")

    def test_skips_when_cwd_is_not_a_cockpit_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No context.json — must refuse to bootstrap
            result = _run_hook({
                "hook_event_name": "UserPromptSubmit",
                "session_id": "sid-strange",
                "cwd": tmp,
            })
            self.assertEqual(result.returncode, 0)
            self.assertFalse((Path(tmp) / "journal").exists())
            self.assertFalse((Path(tmp) / ".claude" / "sessions").exists())

    def test_skips_when_event_name_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context_root(tmp)
            result = _run_hook({
                "hook_event_name": "Stop",  # wrong event
                "session_id": "sid-wrong",
                "cwd": str(root),
            })
            self.assertEqual(result.returncode, 0)
            self.assertFalse((root / ".claude" / "sessions" / "sid-wrong.json").exists())

    def test_skips_when_session_id_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context_root(tmp)
            result = _run_hook({
                "hook_event_name": "UserPromptSubmit",
                "cwd": str(root),
            })
            self.assertEqual(result.returncode, 0)
            self.assertFalse((root / ".claude" / "sessions").exists())

    def test_malformed_payload_exits_zero(self):
        """A hook failure must never block the user's turn — always exit 0."""
        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="not json",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
