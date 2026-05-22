#!/usr/bin/env python3
"""Unit tests for the Stop hook's pure logic.

Run from this directory:

    python3 -m unittest test_stop.py -v

These tests cover content rendering, turn detection, body construction, and
reconciliation decision logic. They do not exercise filesystem I/O or hook
plumbing.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Make sibling `stop` module importable without packaging.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stop  # noqa: E402  pylint: disable=wrong-import-position


# ---------------------------------------------------------------------------
# Helpers for building test fixtures
# ---------------------------------------------------------------------------


def user_entry(text: str, *, prompt_id: str = "p1", sidechain: bool = False) -> dict:
    return {
        "type": stop.ENTRY_USER,
        "isSidechain": sidechain,
        "promptId": prompt_id,
        "message": {"role": "user", "content": text},
    }


def user_tool_result_entry(*, sidechain: bool = False) -> dict:
    return {
        "type": stop.ENTRY_USER,
        "isSidechain": sidechain,
        "message": {
            "role": "user",
            "content": [{"type": stop.BLOCK_TOOL_RESULT, "tool_use_id": "x", "content": "ok"}],
        },
    }


def assistant_text_entry(text: str, *, sidechain: bool = False) -> dict:
    return {
        "type": stop.ENTRY_ASSISTANT,
        "isSidechain": sidechain,
        "message": {
            "role": "assistant",
            "content": [{"type": stop.BLOCK_TEXT, "text": text}],
        },
    }


def assistant_tool_use_entry(name: str, inp: dict | None = None, *, sidechain: bool = False) -> dict:
    block: dict = {"type": stop.BLOCK_TOOL_USE, "name": name}
    if inp is not None:
        block["input"] = inp
    return {
        "type": stop.ENTRY_ASSISTANT,
        "isSidechain": sidechain,
        "message": {"role": "assistant", "content": [block]},
    }


def midflight_attachment(text: str) -> dict:
    return {
        "type": stop.ENTRY_ATTACHMENT,
        "isSidechain": False,
        "attachment": {
            "type": stop.ATTACHMENT_QUEUED_COMMAND,
            "prompt": [{"type": stop.BLOCK_TEXT, "text": text}],
        },
    }


# ---------------------------------------------------------------------------
# Content rendering
# ---------------------------------------------------------------------------


class TestRenderContent(unittest.TestCase):

    def test_string_content(self):
        self.assertEqual(stop._render_content("  hello  "), "hello")

    def test_non_list_non_string(self):
        self.assertEqual(stop._render_content(42), "")
        self.assertEqual(stop._render_content(None), "")

    def test_single_text_block(self):
        content = [{"type": stop.BLOCK_TEXT, "text": "hello world"}]
        self.assertEqual(stop._render_content(content), "hello world")

    def test_two_text_blocks_joined_with_blank_line(self):
        content = [
            {"type": stop.BLOCK_TEXT, "text": "alpha"},
            {"type": stop.BLOCK_TEXT, "text": "beta"},
        ]
        self.assertEqual(stop._render_content(content), "alpha\n\nbeta")

    def test_text_block_with_empty_text_skipped(self):
        content = [
            {"type": stop.BLOCK_TEXT, "text": "   "},
            {"type": stop.BLOCK_TEXT, "text": "kept"},
        ]
        self.assertEqual(stop._render_content(content), "kept")

    def test_tool_use_with_input_renders_as_marker(self):
        content = [{"type": stop.BLOCK_TOOL_USE, "name": "Bash", "input": {"command": "ls -la"}}]
        out = stop._render_content(content)
        self.assertIn("[tool_use: Bash", out)
        self.assertIn("ls -la", out)

    def test_tool_use_long_input_truncated(self):
        big_value = "x" * 500
        content = [{"type": stop.BLOCK_TOOL_USE, "name": "Big", "input": {"k": big_value}}]
        out = stop._render_content(content)
        self.assertTrue(out.endswith("...]"))
        self.assertLess(len(out), 200)

    def test_tool_use_without_input(self):
        content = [{"type": stop.BLOCK_TOOL_USE, "name": "Probe"}]
        self.assertEqual(stop._render_content(content), "[tool_use: Probe]")

    def test_thinking_block_ignored(self):
        content = [
            {"type": "thinking", "text": "internal reasoning"},
            {"type": stop.BLOCK_TEXT, "text": "visible"},
        ]
        self.assertEqual(stop._render_content(content), "visible")

    def test_text_and_tool_use_mixed_preserves_order(self):
        content = [
            {"type": stop.BLOCK_TEXT, "text": "before"},
            {"type": stop.BLOCK_TOOL_USE, "name": "T", "input": {"k": "v"}},
            {"type": stop.BLOCK_TEXT, "text": "after"},
        ]
        out = stop._render_content(content)
        self.assertTrue(out.startswith("before"))
        self.assertTrue(out.endswith("after"))
        self.assertIn("[tool_use: T", out)


# ---------------------------------------------------------------------------
# Turn detection
# ---------------------------------------------------------------------------


class TestEntryClassification(unittest.TestCase):

    def test_real_user_prompt(self):
        self.assertTrue(stop._is_real_user_prompt(user_entry("hi")))

    def test_user_with_tool_result_is_not_prompt(self):
        self.assertFalse(stop._is_real_user_prompt(user_tool_result_entry()))

    def test_sidechain_user_is_not_prompt(self):
        self.assertFalse(stop._is_real_user_prompt(user_entry("sub", sidechain=True)))

    def test_assistant_is_not_user_prompt(self):
        self.assertFalse(stop._is_real_user_prompt(assistant_text_entry("hi")))

    def test_is_main_chain(self):
        self.assertTrue(stop._is_main_chain({"isSidechain": False}))
        self.assertFalse(stop._is_main_chain({"isSidechain": True}))
        # absence of field → not main (we want explicit False)
        self.assertFalse(stop._is_main_chain({}))


class TestQueuedCommandText(unittest.TestCase):

    def test_extracts_text_from_attachment(self):
        self.assertEqual(stop._queued_command_text(midflight_attachment("hello")), "hello")

    def test_returns_none_for_non_attachment(self):
        self.assertIsNone(stop._queued_command_text(user_entry("hi")))

    def test_returns_none_for_other_attachment_subtype(self):
        e = {
            "type": stop.ENTRY_ATTACHMENT,
            "isSidechain": False,
            "attachment": {"type": "skill_listing"},
        }
        self.assertIsNone(stop._queued_command_text(e))

    def test_sidechain_attachment_ignored(self):
        e = midflight_attachment("oops")
        e["isSidechain"] = True
        self.assertIsNone(stop._queued_command_text(e))

    def test_string_prompt_supported(self):
        e = {
            "type": stop.ENTRY_ATTACHMENT,
            "isSidechain": False,
            "attachment": {"type": stop.ATTACHMENT_QUEUED_COMMAND, "prompt": "  text  "},
        }
        self.assertEqual(stop._queued_command_text(e), "text")


# ---------------------------------------------------------------------------
# build_turns
# ---------------------------------------------------------------------------


class TestBuildTurns(unittest.TestCase):

    def test_empty_input(self):
        self.assertEqual(stop.build_turns([]), [])

    def test_single_turn(self):
        entries = [user_entry("hi", prompt_id="p1"), assistant_text_entry("hello")]
        turns = stop.build_turns(entries)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompt_id"], "p1")
        self.assertEqual(turns[0]["assistant_count"], 1)
        self.assertEqual(turns[0]["midflight_count"], 0)
        self.assertEqual(len(turns[0]["blocks"]), 2)
        self.assertEqual(turns[0]["blocks"][0]["role"], stop.ROLE_USER)
        self.assertEqual(turns[0]["blocks"][1]["role"], stop.ROLE_ASSISTANT)

    def test_two_turns_separated_by_next_user(self):
        entries = [
            user_entry("q1", prompt_id="p1"),
            assistant_text_entry("a1"),
            user_entry("q2", prompt_id="p2"),
            assistant_text_entry("a2"),
        ]
        turns = stop.build_turns(entries)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["prompt_id"], "p1")
        self.assertEqual(turns[1]["prompt_id"], "p2")

    def test_tool_result_user_does_not_start_new_turn(self):
        entries = [
            user_entry("q1", prompt_id="p1"),
            assistant_tool_use_entry("Bash", {"command": "ls"}),
            user_tool_result_entry(),
            assistant_text_entry("done"),
        ]
        turns = stop.build_turns(entries)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["assistant_count"], 2)

    def test_sidechain_assistant_ignored(self):
        entries = [
            user_entry("q", prompt_id="p1"),
            assistant_text_entry("sidechain", sidechain=True),
            assistant_text_entry("main"),
        ]
        turns = stop.build_turns(entries)
        self.assertEqual(turns[0]["assistant_count"], 1)
        self.assertEqual(turns[0]["blocks"][1]["text"], "main")

    def test_midflight_inserted_into_current_turn(self):
        entries = [
            user_entry("q", prompt_id="p1"),
            assistant_text_entry("partial"),
            midflight_attachment("hold on"),
            assistant_text_entry("ok, continuing"),
        ]
        turns = stop.build_turns(entries)
        self.assertEqual(len(turns), 1)
        roles = [b["role"] for b in turns[0]["blocks"]]
        self.assertEqual(roles, [stop.ROLE_USER, stop.ROLE_ASSISTANT, stop.ROLE_MIDFLIGHT, stop.ROLE_ASSISTANT])
        self.assertEqual(turns[0]["midflight_count"], 1)
        self.assertEqual(turns[0]["assistant_count"], 2)

    def test_attachment_before_any_user_is_skipped(self):
        entries = [
            midflight_attachment("orphan"),
            user_entry("q", prompt_id="p1"),
            assistant_text_entry("a"),
        ]
        turns = stop.build_turns(entries)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["midflight_count"], 0)

    def test_assistant_entry_with_empty_content_skipped(self):
        # an assistant entry that renders to "" must not become a block
        entry = {
            "type": stop.ENTRY_ASSISTANT,
            "isSidechain": False,
            "message": {"role": "assistant", "content": [{"type": "thinking", "text": "only thinking"}]},
        }
        entries = [user_entry("q", prompt_id="p1"), entry, assistant_text_entry("real")]
        turns = stop.build_turns(entries)
        self.assertEqual(turns[0]["assistant_count"], 1)

    def test_interrupted_turn_has_no_assistant(self):
        entries = [user_entry("q1", prompt_id="p1"), user_entry("q2", prompt_id="p2"), assistant_text_entry("a")]
        turns = stop.build_turns(entries)
        # turn 0 has no assistant, turn 1 has one
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["assistant_count"], 0)
        self.assertEqual(turns[1]["assistant_count"], 1)


# ---------------------------------------------------------------------------
# build_body
# ---------------------------------------------------------------------------


class TestBuildBody(unittest.TestCase):

    def test_empty_blocks_renders_placeholders(self):
        body = stop.build_body([])
        self.assertIn(stop.EMPTY_PROMPT_MARKER, body)
        self.assertIn(stop.INTERRUPTED_MARKER, body)
        self.assertIn("**User:**", body)
        self.assertIn("**Assistant:**", body)

    def test_simple_user_assistant(self):
        blocks = [
            {"role": stop.ROLE_USER, "text": "hi"},
            {"role": stop.ROLE_ASSISTANT, "text": "hello"},
        ]
        body = stop.build_body(blocks)
        self.assertIn("**User:**\n\nhi\n", body)
        self.assertIn("**Assistant:**\n\nhello\n", body)

    def test_no_assistant_appends_interrupted(self):
        blocks = [{"role": stop.ROLE_USER, "text": "hi"}]
        body = stop.build_body(blocks)
        self.assertIn(stop.INTERRUPTED_MARKER, body)

    def test_consecutive_assistants_merged(self):
        blocks = [
            {"role": stop.ROLE_USER, "text": "q"},
            {"role": stop.ROLE_ASSISTANT, "text": "part1"},
            {"role": stop.ROLE_ASSISTANT, "text": "part2"},
        ]
        body = stop.build_body(blocks)
        # only one **Assistant:** label
        self.assertEqual(body.count("**Assistant:**"), 1)
        self.assertIn("part1\n\npart2", body)

    def test_midflight_breaks_assistant_block(self):
        blocks = [
            {"role": stop.ROLE_USER, "text": "q"},
            {"role": stop.ROLE_ASSISTANT, "text": "before"},
            {"role": stop.ROLE_MIDFLIGHT, "text": "interject"},
            {"role": stop.ROLE_ASSISTANT, "text": "after"},
        ]
        body = stop.build_body(blocks)
        self.assertEqual(body.count("**Assistant:**"), 2)
        self.assertEqual(body.count("**User (mid-flight):**"), 1)
        # ordering check
        i_before = body.index("before")
        i_mid = body.index("interject")
        i_after = body.index("after")
        self.assertLess(i_before, i_mid)
        self.assertLess(i_mid, i_after)

    def test_empty_role_or_text_skipped(self):
        blocks = [
            {"role": stop.ROLE_USER, "text": "q"},
            {"role": "", "text": "junk"},
            {"role": stop.ROLE_ASSISTANT, "text": ""},
            {"role": stop.ROLE_ASSISTANT, "text": "real"},
        ]
        body = stop.build_body(blocks)
        self.assertNotIn("junk", body)
        self.assertIn("real", body)


# ---------------------------------------------------------------------------
# Reconciliation decision: _needs_action
# ---------------------------------------------------------------------------


class TestNeedsAction(unittest.TestCase):

    def setUp(self):
        # A tempdir we never actually write to; we only need the Path objects.
        # _needs_action calls .exists() and _file_is_interrupted; both safe on
        # non-existent paths (return False / False).
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _entry(self, **kw) -> dict:
        base = {"complete": True, "midflight_count": 0, "assistant_count": 1}
        base.update(kw)
        return base

    def _turn(self, **kw) -> dict:
        base = {"blocks": [], "midflight_count": 0, "assistant_count": 1}
        base.update(kw)
        return base

    def _file_with_body(self, name: str, body: str) -> Path:
        p = self.dir / name
        p.write_text(body, encoding="utf-8")
        return p

    def test_missing_file_triggers_action(self):
        path = self.dir / "missing.md"
        self.assertTrue(stop._needs_action(self._entry(), self._turn(), path))

    def test_missing_file_with_slug_set_does_NOT_trigger_action(self):
        """Indexer race-safety: if the file is missing but slug is set, the
        index.json view is stale (file already renamed by indexer). Do not
        resurrect a zombie file at the old path."""
        path = self.dir / "missing.md"
        entry = self._entry(slug="SomeSlug")
        self.assertFalse(stop._needs_action(entry, self._turn(), path))

    def test_complete_false_triggers_action(self):
        path = self._file_with_body("a.md", "## 000\n\n**User:**\n\nx\n\n**Assistant:**\n\ny\n")
        self.assertTrue(stop._needs_action(self._entry(complete=False), self._turn(), path))

    def test_legacy_interrupted_triggers_action(self):
        path = self._file_with_body(
            "a.md",
            f"## 000\n\n**User:**\n\nq\n\n**Assistant:**\n\n{stop.INTERRUPTED_MARKER}\n",
        )
        self.assertTrue(stop._needs_action(
            self._entry(complete=None, midflight_count=None, assistant_count=None),
            self._turn(),
            path,
        ))

    def test_midflight_grew_triggers_action(self):
        path = self._file_with_body("a.md", "## 000\nstub\n")
        self.assertTrue(stop._needs_action(
            self._entry(midflight_count=1),
            self._turn(midflight_count=2),
            path,
        ))

    def test_assistant_grew_triggers_action(self):
        path = self._file_with_body("a.md", "## 000\nstub\n")
        self.assertTrue(stop._needs_action(
            self._entry(assistant_count=1),
            self._turn(assistant_count=3),
            path,
        ))

    def test_legacy_midflight_unset_with_midflight_in_jsonl(self):
        path = self._file_with_body("a.md", "## 000\nstub\n")
        e = {"complete": True, "midflight_count": None, "assistant_count": 1}
        self.assertTrue(stop._needs_action(e, self._turn(midflight_count=1), path))

    def test_legacy_assistant_unset_with_assistant_in_jsonl(self):
        path = self._file_with_body("a.md", "## 000\nstub\n")
        e = {"complete": True, "midflight_count": 0, "assistant_count": None}
        self.assertTrue(stop._needs_action(e, self._turn(assistant_count=2), path))

    def test_no_action_when_everything_matches(self):
        path = self._file_with_body("a.md", "## 000\n\n**User:**\n\nq\n\n**Assistant:**\n\na\n")
        self.assertFalse(stop._needs_action(
            self._entry(complete=True, midflight_count=0, assistant_count=1),
            self._turn(midflight_count=0, assistant_count=1),
            path,
        ))


# ---------------------------------------------------------------------------
# Shared-file detection
# ---------------------------------------------------------------------------


class TestSharedFiles(unittest.TestCase):

    def test_no_shared_files_when_all_unique(self):
        written = [
            {"file": "000_a.md"},
            {"file": "001_b.md"},
            {"file": "002_c.md"},
        ]
        self.assertEqual(stop._compute_shared_files(written), set())

    def test_detects_shared_file(self):
        written = [
            {"file": "000-002_chapter.md"},
            {"file": "000-002_chapter.md"},
            {"file": "003_solo.md"},
        ]
        self.assertEqual(stop._compute_shared_files(written), {"000-002_chapter.md"})

    def test_missing_file_field_ignored(self):
        written = [{"file": None}, {"file": "a.md"}, {}]
        self.assertEqual(stop._compute_shared_files(written), set())


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


class TestParseIso(unittest.TestCase):

    def test_z_suffix(self):
        dt = stop._parse_iso("2026-05-21T14:18:41.743Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 5)
        self.assertEqual(dt.day, 21)
        self.assertEqual(dt.hour, 14)
        self.assertEqual(dt.minute, 18)

    def test_offset_form(self):
        dt = stop._parse_iso("2026-05-21T17:18:41+03:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 17)

    def test_invalid_returns_none(self):
        self.assertIsNone(stop._parse_iso("not a date"))
        self.assertIsNone(stop._parse_iso(""))
        self.assertIsNone(stop._parse_iso(None))


class TestComputeFreshSessionFolder(unittest.TestCase):

    def test_path_derived_from_timestamp(self):
        from datetime import datetime
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ts = datetime(2026, 5, 21, 14, 18, 41)
            folder = stop._compute_fresh_session_folder(root, ts)
            self.assertEqual(folder, root / "journal" / "2026" / "05" / "21" / "1418_session")


class TestEnsureSessionScaffold(unittest.TestCase):

    def test_creates_folder_and_initial_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "journal" / "2026" / "05" / "21" / "1418_session"
            self.assertTrue(stop._ensure_session_scaffold(folder))
            self.assertTrue((folder / "transcript").is_dir())
            self.assertTrue((folder / "transcript" / "index.json").is_file())
            self.assertTrue((folder / "state.md").is_file())
            # initial index.json content
            idx = json.loads((folder / "transcript" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(idx["next_index"], 0)
            self.assertEqual(idx["turns"], [])
            self.assertEqual(idx["schema_version"], stop.INDEX_SCHEMA_VERSION)

    def test_idempotent_does_not_overwrite_existing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "journal" / "2026" / "05" / "21" / "1418_session"
            self.assertTrue(stop._ensure_session_scaffold(folder))
            # mutate index.json
            idx_path = folder / "transcript" / "index.json"
            custom = {"next_index": 7, "turns": [{"index": 0}], "schema_version": 1}
            idx_path.write_text(json.dumps(custom), encoding="utf-8")
            # second call must leave custom content alone
            self.assertTrue(stop._ensure_session_scaffold(folder))
            self.assertEqual(json.loads(idx_path.read_text(encoding="utf-8")), custom)


class TestFindFolderByFirstPromptId(unittest.TestCase):
    """The scanner walks <context>/journal/YYYY/MM/DD/<session>/transcript/index.json
    and returns the first folder whose turns[0].prompt_id matches."""

    def _make_session(self, context_root: Path, hhmm: str, first_prompt_id: str | None, *, day: str = "21") -> Path:
        folder = context_root / "journal" / "2026" / "05" / day / f"{hhmm}_session"
        (folder / "transcript").mkdir(parents=True, exist_ok=True)
        turns = []
        if first_prompt_id is not None:
            turns.append({"index": 0, "slug": None, "file": "000_msg.md", "prompt_id": first_prompt_id})
        (folder / "transcript" / "index.json").write_text(
            json.dumps({"next_index": len(turns), "turns": turns, "schema_version": 1}),
            encoding="utf-8",
        )
        return folder

    def test_returns_match_when_present(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_session(root, "1418", "other-prompt")
            target = self._make_session(root, "1430", "target-prompt")
            self._make_session(root, "1445", "yet-other-prompt")
            result = stop._find_folder_by_first_prompt_id(root, "target-prompt")
            self.assertEqual(result, target)

    def test_returns_none_when_no_match(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_session(root, "1418", "other-prompt")
            result = stop._find_folder_by_first_prompt_id(root, "missing-prompt")
            self.assertIsNone(result)

    def test_returns_none_when_no_journal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)  # no journal/ at all
            result = stop._find_folder_by_first_prompt_id(root, "anything")
            self.assertIsNone(result)

    def test_skips_session_folders_with_no_turns(self):
        """A bootstrapped session that hasn't recorded any turn yet (empty turns
        list) must not be considered a match for anything."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_session(root, "1418", None)  # no first prompt
            self.assertIsNone(stop._find_folder_by_first_prompt_id(root, "any"))

    def test_skips_folders_with_corrupted_index(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._make_session(root, "1418", "ok")
            broken_folder = root / "journal" / "2026" / "05" / "21" / "1430_session"
            (broken_folder / "transcript").mkdir(parents=True, exist_ok=True)
            (broken_folder / "transcript" / "index.json").write_text("this is not json", encoding="utf-8")
            result = stop._find_folder_by_first_prompt_id(root, "ok")
            self.assertEqual(result, target)

    def test_walks_across_days(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_session(root, "1418", "p1", day="20")
            target = self._make_session(root, "1418", "p2", day="22")
            self.assertEqual(stop._find_folder_by_first_prompt_id(root, "p2"), target)


class TestBootstrapState(unittest.TestCase):

    def _turns(self, first_prompt_id: str, ts: str = "2026-05-21T14:18:41.743Z") -> list[dict]:
        return [{
            "prompt_id": first_prompt_id,
            "started_at": ts,
            "ended_at": ts,
            "blocks": [{"role": stop.ROLE_USER, "text": "hi"}],
            "assistant_count": 0,
            "midflight_count": 0,
        }]

    def _make_existing_session(self, context_root: Path, hhmm: str, first_prompt_id: str) -> Path:
        folder = context_root / "journal" / "2026" / "05" / "21" / f"{hhmm}_session"
        (folder / "transcript").mkdir(parents=True, exist_ok=True)
        (folder / "transcript" / "index.json").write_text(
            json.dumps({
                "next_index": 1,
                "turns": [{"index": 0, "slug": None, "file": "000_msg.md", "prompt_id": first_prompt_id}],
                "schema_version": 1,
            }),
            encoding="utf-8",
        )
        return folder

    def test_reuses_existing_folder_when_prompt_id_matches(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = self._make_existing_session(root, "1418", "shared-prompt-id")
            state_path = root / ".claude" / "sessions" / "new-session-id.json"
            result = stop._bootstrap_state(state_path, root, self._turns("shared-prompt-id"))
            self.assertIsNotNone(result)
            self.assertEqual(Path(result[stop.STATE_KEY_FOLDER]), existing)
            self.assertEqual(result[stop.STATE_KEY_FIRST_PROMPT_ID], "shared-prompt-id")
            # SessionStateFile was written to disk
            self.assertTrue(state_path.is_file())
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted, result)

    def test_creates_new_folder_when_no_match(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / ".claude" / "sessions" / "abc.json"
            result = stop._bootstrap_state(state_path, root, self._turns("fresh", ts="2026-05-21T14:30:00Z"))
            self.assertIsNotNone(result)
            self.assertEqual(
                Path(result[stop.STATE_KEY_FOLDER]),
                root / "journal" / "2026" / "05" / "21" / "1430_session",
            )
            # Scaffold materialized
            self.assertTrue((Path(result[stop.STATE_KEY_FOLDER]) / "transcript" / "index.json").is_file())

    def test_returns_none_when_no_turns(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / ".claude" / "sessions" / "abc.json"
            self.assertIsNone(stop._bootstrap_state(state_path, root, []))
            self.assertFalse(state_path.exists())

    def test_falls_back_to_now_when_timestamp_invalid(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / ".claude" / "sessions" / "abc.json"
            turns = self._turns("p1", ts="not-a-date")
            result = stop._bootstrap_state(state_path, root, turns)
            self.assertIsNotNone(result)
            # Folder name uses current time — just verify the journal path
            # structure was created somewhere.
            folder = Path(result[stop.STATE_KEY_FOLDER])
            self.assertEqual(folder.parent.parent.parent.parent, root / "journal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
