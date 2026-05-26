#!/usr/bin/env python3
"""Unit tests for ``config.py``.

Run from this directory:
    python3 -m unittest test_config.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfgmod  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


class TestMinimalYAMLParser(unittest.TestCase):

    def test_full_spec_example_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "protocolist"
            sub.mkdir(parents=True)
            _write(sub / "config.yaml", """\
                model: sonnet
                system-prompt: system_prompt.md
                append-system-prompt: null
                max-turns: null
                allowed-tools: [Read, Write, Edit, Bash, Glob, Grep]
                disallowed-tools: []
                permission-mode: null
                bare: false
                effort: max
                cwd: ../..
                no-session-persistence: false
                env: {}
                output-format: stream-json
            """)
            c = cfgmod.load(str(sub / "config.yaml"), str(sub))
            self.assertEqual(c.get("model"), "sonnet")
            self.assertEqual(
                c.get("allowed-tools"),
                ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            )
            self.assertEqual(c.get("disallowed-tools"), [])
            self.assertIsNone(c.get("max-turns"))
            self.assertIsNone(c.get("append-system-prompt"))
            self.assertIsNone(c.get("permission-mode"))
            self.assertFalse(c.get("bare"))
            self.assertEqual(c.get("env"), {})
            # No fields defaulted: every field of the spec table is present.
            self.assertEqual(c.defaulted_fields, [])

    def test_defaults_applied_for_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "x"
            sub.mkdir(parents=True)
            _write(sub / "config.yaml", "model: opus\n")
            c = cfgmod.load(str(sub / "config.yaml"), str(sub))
            self.assertEqual(c.get("model"), "opus")
            self.assertEqual(c.get("system-prompt"), "system_prompt.md")
            self.assertEqual(c.get("effort"), "max")
            self.assertEqual(c.get("cwd"), "../..")
            self.assertIn("system-prompt", c.defaulted_fields)
            self.assertIn("cwd", c.defaulted_fields)
            self.assertNotIn("model", c.defaulted_fields)

    def test_handles_block_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "x"
            sub.mkdir(parents=True)
            _write(sub / "config.yaml", """\
                allowed-tools:
                  - Read
                  - Bash
                model: sonnet
            """)
            c = cfgmod.load(str(sub / "config.yaml"), str(sub))
            self.assertEqual(c.get("allowed-tools"), ["Read", "Bash"])
            self.assertEqual(c.get("model"), "sonnet")

    def test_handles_block_map_for_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "x"
            sub.mkdir(parents=True)
            _write(sub / "config.yaml", """\
                env:
                  DEBUG: "true"
                  FOO: bar
            """)
            c = cfgmod.load(str(sub / "config.yaml"), str(sub))
            self.assertEqual(c.get("env"), {"DEBUG": "true", "FOO": "bar"})

    def test_int_and_bool_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "x"
            sub.mkdir(parents=True)
            _write(sub / "config.yaml", """\
                max-turns: 7
                bare: true
                no-session-persistence: false
            """)
            c = cfgmod.load(str(sub / "config.yaml"), str(sub))
            self.assertEqual(c.get("max-turns"), 7)
            self.assertIs(c.get("bare"), True)
            self.assertIs(c.get("no-session-persistence"), False)

    def test_inline_comments_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "x"
            sub.mkdir(parents=True)
            _write(sub / "config.yaml", """\
                model: sonnet  # claude model
                effort: max   # opus only
            """)
            c = cfgmod.load(str(sub / "config.yaml"), str(sub))
            self.assertEqual(c.get("model"), "sonnet")
            self.assertEqual(c.get("effort"), "max")

    def test_quoted_string_with_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "x"
            sub.mkdir(parents=True)
            # The # inside quotes must NOT be treated as a comment.
            _write(sub / "config.yaml", """\
                model: "sonnet#nightly"
            """)
            c = cfgmod.load(str(sub / "config.yaml"), str(sub))
            self.assertEqual(c.get("model"), "sonnet#nightly")

    def test_type_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "x"
            sub.mkdir(parents=True)
            _write(sub / "config.yaml", """\
                max-turns: notanumber
            """)
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(str(sub / "config.yaml"), str(sub))

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(str(Path(tmp) / "nope.yaml"), str(tmp))

    def test_top_level_indent_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "subchats" / "x"
            sub.mkdir(parents=True)
            (sub / "config.yaml").write_text("  model: sonnet\n", encoding="utf-8")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(str(sub / "config.yaml"), str(sub))


class TestPathResolution(unittest.TestCase):

    def test_resolves_relative_against_subchat_folder_not_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "journal" / "2026" / "05" / "24" / "1430_session" / \
                "subchats" / "protocolist"
            sub.mkdir(parents=True)
            _write(sub / "config.yaml", """\
                system-prompt: system_prompt.md
                cwd: ../..
            """)
            c = cfgmod.load(str(sub / "config.yaml"), str(sub))
            # Make sure we run from a path unrelated to the subchat folder.
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                # system-prompt is anchored at the subchat folder.
                self.assertEqual(
                    c.resolved_system_prompt,
                    str(sub / "system_prompt.md"),
                )
                # cwd "../.." from subchats/protocolist/ → SessionFolder.
                self.assertEqual(
                    c.resolved_cwd,
                    str(sub.parent.parent),
                )
            finally:
                os.chdir(old_cwd)

    def test_does_not_collapse_symlinks_in_subchat_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real" / "subchats" / "x"
            real.mkdir(parents=True)
            link = Path(tmp) / "link"
            os.symlink(str(Path(tmp) / "real"), str(link))
            # Use the symlinked path as the subchat folder anchor.
            anchor = str(link / "subchats" / "x")
            _write(real / "config.yaml", "system-prompt: system_prompt.md\n")
            c = cfgmod.load(str(real / "config.yaml"), anchor)
            # abspath normalises ``..``/``.`` but does NOT resolve symlinks,
            # so the link path is preserved in the resolved value.
            self.assertTrue(c.resolved_system_prompt.startswith(anchor))


if __name__ == "__main__":
    unittest.main()
