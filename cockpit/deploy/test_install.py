#!/usr/bin/env python3
"""Tests for cockpit/deploy/install.py.

All tests build a fake source-tree mirroring the real ``Igor.source.git``
layout in a ``tempfile.TemporaryDirectory``, then invoke ``install(...)``
against a separate Context tmpdir. No real paths touched.

Run with::

    python3 -m unittest cockpit/deploy/test_install.py
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import install as inst


# ---------------------------------------------------------------------------
# Fake source-tree builder
# ---------------------------------------------------------------------------


PERSONA_TEMPLATE = """---
name: igor
description: test persona
---

## Identity

__LOCALIZATION_TEXT__

## Body

Stuff.
"""


def _build_fake_source(root: Path, *, with_subagents: list[str] | None = None) -> Path:
    """Build a fake Igor.source.git tree under ``root``.

    Returns the absolute path of the fake ``install.py`` (which is what the
    real ``install()`` uses to discover source-paths via ``__file__``).
    """
    # cockpit/deploy/install.py
    deploy_dir = root / "cockpit" / "deploy"
    deploy_dir.mkdir(parents=True)
    fake_install = deploy_dir / "install.py"
    fake_install.write_text("# fake install.py marker\n", encoding="utf-8")

    # cockpit/hooks/{user_prompt_submit,stop}.py
    hooks_dir = root / "cockpit" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "user_prompt_submit.py").write_text("# ups\n", encoding="utf-8")
    (hooks_dir / "stop.py").write_text("# stop\n", encoding="utf-8")
    # An untracked sibling helper — must NOT be auto-registered.
    (hooks_dir / "session_bootstrap.py").write_text("# helper\n", encoding="utf-8")

    # cockpit/mcp/dist/src/index.js
    mcp_dir = root / "cockpit" / "mcp" / "dist" / "src"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "index.js").write_text("// mcp entry\n", encoding="utf-8")

    # instructions/igor.md
    instr_dir = root / "instructions"
    instr_dir.mkdir(parents=True)
    (instr_dir / "igor.md").write_text(PERSONA_TEMPLATE, encoding="utf-8")

    # instructions/subagents/
    subagents_dir = instr_dir / "subagents"
    subagents_dir.mkdir()
    for name in with_subagents or []:
        (subagents_dir / name).write_text(
            f"# subagent profile {name}\n", encoding="utf-8"
        )

    return fake_install


# ---------------------------------------------------------------------------
# Helper assertions
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class FreshDeployTests(unittest.TestCase):
    """Step-by-step coverage of a fresh deploy into an empty Context."""

    def setUp(self) -> None:
        self._src_td = tempfile.TemporaryDirectory()
        self._ctx_td = tempfile.TemporaryDirectory()
        self.addCleanup(self._src_td.cleanup)
        self.addCleanup(self._ctx_td.cleanup)
        # Resolve to follow symlinks (macOS /var → /private/var) so test
        # expectations match the `.resolve()`-d paths the installer writes.
        self.src_root = Path(self._src_td.name).resolve()
        self.ctx = Path(self._ctx_td.name).resolve()
        self.fake_install = _build_fake_source(self.src_root)

    def test_fresh_deploy_creates_full_layout(self) -> None:
        report = inst.install(self.ctx, self.fake_install)

        # Step 1: context.json scaffold.
        cj = _read_json(self.ctx / "context.json")
        self.assertEqual(cj["name"], self.ctx.name)
        self.assertEqual(cj["cockpit_config"], {})

        # Step 2: .claude/ tree.
        self.assertTrue((self.ctx / ".claude" / "sessions").is_dir())
        self.assertTrue((self.ctx / ".claude" / "output-styles").is_dir())
        self.assertTrue((self.ctx / ".claude" / "cockpit" / "subagents").is_dir())

        # Step 3: settings.json with two hook entries, both abs paths.
        settings = _read_json(self.ctx / "settings.json")
        self.assertEqual(settings["outputStyle"], "igor")
        ups = settings["hooks"]["UserPromptSubmit"]
        stop = settings["hooks"]["Stop"]
        self.assertEqual(len(ups), 1)
        self.assertEqual(len(stop), 1)
        self.assertEqual(
            ups[0]["hooks"][0]["args"][0],
            str(self.src_root / "cockpit" / "hooks" / "user_prompt_submit.py"),
        )
        self.assertEqual(
            stop[0]["hooks"][0]["args"][0],
            str(self.src_root / "cockpit" / "hooks" / "stop.py"),
        )
        self.assertEqual(ups[0]["hooks"][0]["command"], "python3")
        self.assertEqual(stop[0]["hooks"][0]["command"], "python3")

        # Step 4: .mcp.json with igor-cockpit pointing at dist/src/index.js.
        mcp = _read_json(self.ctx / ".mcp.json")
        self.assertEqual(
            mcp["mcpServers"]["igor-cockpit"],
            {
                "type": "stdio",
                "command": "node",
                "args": [str(self.src_root / "cockpit" / "mcp" / "dist" / "src" / "index.js")],
            },
        )

        # Step 5: persona with default localization (cockpit_config empty).
        persona = (self.ctx / ".claude" / "output-styles" / "igor.md").read_text(encoding="utf-8")
        self.assertNotIn("__LOCALIZATION_TEXT__", persona)
        self.assertIn(inst.LOCALIZATION_DEFAULT, persona)

        # Step 6: subagents/ deployed empty (source dir exists but empty).
        deployed_profiles = list(
            (self.ctx / ".claude" / "cockpit" / "subagents").iterdir()
        )
        self.assertEqual(deployed_profiles, [])

        # Step 7: objectives/, journal/.
        self.assertTrue((self.ctx / "objectives").is_dir())
        self.assertTrue((self.ctx / "journal").is_dir())

        # Step 8: report mentions every step.
        text = report.render()
        for needle in [
            "context.json: created",
            ".claude/sessions/: created",
            ".claude/output-styles/: created",
            ".claude/cockpit/subagents/: created",
            "settings.json:",
            ".mcp.json:",
            "igor.md: deployed",
            "objectives/: created",
            "journal/: created",
        ]:
            self.assertIn(needle, text, f"missing in report: {needle}")


class IdempotencyTests(unittest.TestCase):
    """Re-running install on a deployed Context must not duplicate / clobber."""

    def setUp(self) -> None:
        self._src_td = tempfile.TemporaryDirectory()
        self._ctx_td = tempfile.TemporaryDirectory()
        self.addCleanup(self._src_td.cleanup)
        self.addCleanup(self._ctx_td.cleanup)
        self.src_root = Path(self._src_td.name).resolve()
        self.ctx = Path(self._ctx_td.name).resolve()
        self.fake_install = _build_fake_source(self.src_root)

    def test_re_deploy_does_not_duplicate_hook_entries(self) -> None:
        inst.install(self.ctx, self.fake_install)
        inst.install(self.ctx, self.fake_install)
        settings = _read_json(self.ctx / "settings.json")
        self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)

    def test_re_deploy_preserves_unrelated_settings_keys(self) -> None:
        # First deploy, then user adds custom keys.
        inst.install(self.ctx, self.fake_install)
        settings_path = self.ctx / "settings.json"
        s = _read_json(settings_path)
        s["customKey"] = "user-owned"
        s["hooks"]["OtherEvent"] = [{"matcher": "x", "hooks": [{"type": "command"}]}]
        settings_path.write_text(json.dumps(s, indent=2), encoding="utf-8")

        # Re-deploy.
        inst.install(self.ctx, self.fake_install)
        after = _read_json(settings_path)
        self.assertEqual(after["customKey"], "user-owned")
        self.assertEqual(
            after["hooks"]["OtherEvent"],
            [{"matcher": "x", "hooks": [{"type": "command"}]}],
        )

    def test_cookie_cutter_old_hook_path_is_updated_not_appended(self) -> None:
        """If a previous install wrote a different absolute path, refresh it."""
        # Simulate a stale prior deploy under a different source root.
        stale_path = "/old/path/cockpit/hooks/user_prompt_submit.py"
        settings_path = self.ctx / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(
                {
                    "outputStyle": "igor",
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "python3",
                                        "args": [stale_path],
                                    }
                                ]
                            }
                        ]
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        inst.install(self.ctx, self.fake_install)
        settings = _read_json(settings_path)
        ups = settings["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(ups), 1, "stale entry must be updated, not duplicated")
        self.assertEqual(
            ups[0]["hooks"][0]["args"][0],
            str(self.src_root / "cockpit" / "hooks" / "user_prompt_submit.py"),
        )


class ContextJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._src_td = tempfile.TemporaryDirectory()
        self._ctx_td = tempfile.TemporaryDirectory()
        self.addCleanup(self._src_td.cleanup)
        self.addCleanup(self._ctx_td.cleanup)
        self.src_root = Path(self._src_td.name).resolve()
        self.ctx = Path(self._ctx_td.name).resolve()
        self.fake_install = _build_fake_source(self.src_root)

    def test_preserves_custom_keys(self) -> None:
        path = self.ctx / "context.json"
        path.write_text(
            json.dumps(
                {
                    "name": "MyContext",
                    "custom_field": {"deep": [1, 2, 3]},
                    "cockpit_config": {"localization": "Russian. Tz: UTC+3."},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        inst.install(self.ctx, self.fake_install)
        after = _read_json(path)
        self.assertEqual(after["name"], "MyContext")
        self.assertEqual(after["custom_field"], {"deep": [1, 2, 3]})
        self.assertEqual(
            after["cockpit_config"]["localization"], "Russian. Tz: UTC+3."
        )

    def test_localization_present_substituted_into_persona(self) -> None:
        path = self.ctx / "context.json"
        path.write_text(
            json.dumps(
                {
                    "name": "MyContext",
                    "cockpit_config": {"localization": "Russian. Tz: UTC+3."},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        inst.install(self.ctx, self.fake_install)
        persona = (self.ctx / ".claude" / "output-styles" / "igor.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Russian. Tz: UTC+3.", persona)
        self.assertNotIn(inst.LOCALIZATION_DEFAULT, persona)
        self.assertNotIn("__LOCALIZATION_TEXT__", persona)

    def test_localization_absent_uses_neutral_default(self) -> None:
        # Fresh deploy → scaffold has empty cockpit_config.
        inst.install(self.ctx, self.fake_install)
        persona = (self.ctx / ".claude" / "output-styles" / "igor.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(inst.LOCALIZATION_DEFAULT, persona)


class McpJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._src_td = tempfile.TemporaryDirectory()
        self._ctx_td = tempfile.TemporaryDirectory()
        self.addCleanup(self._src_td.cleanup)
        self.addCleanup(self._ctx_td.cleanup)
        self.src_root = Path(self._src_td.name).resolve()
        self.ctx = Path(self._ctx_td.name).resolve()
        self.fake_install = _build_fake_source(self.src_root)

    def test_preserves_existing_mcp_server(self) -> None:
        path = self.ctx / ".mcp.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "other": {
                            "type": "stdio",
                            "command": "node",
                            "args": ["/elsewhere/server.js"],
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        inst.install(self.ctx, self.fake_install)
        after = _read_json(path)
        self.assertEqual(
            after["mcpServers"]["other"],
            {"type": "stdio", "command": "node", "args": ["/elsewhere/server.js"]},
        )
        self.assertEqual(
            after["mcpServers"]["igor-cockpit"]["args"][0],
            str(self.src_root / "cockpit" / "mcp" / "dist" / "src" / "index.js"),
        )

    def test_overwrites_stale_igor_cockpit_entry(self) -> None:
        path = self.ctx / ".mcp.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "igor-cockpit": {
                            "type": "stdio",
                            "command": "node",
                            "args": ["/old/path/index.js"],
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        inst.install(self.ctx, self.fake_install)
        after = _read_json(path)
        self.assertEqual(
            after["mcpServers"]["igor-cockpit"]["args"][0],
            str(self.src_root / "cockpit" / "mcp" / "dist" / "src" / "index.js"),
        )

    def test_preserves_user_env_on_igor_cockpit_entry(self) -> None:
        """Sibling fields (env, etc.) on a prior igor-cockpit entry must survive re-deploy."""
        path = self.ctx / ".mcp.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "igor-cockpit": {
                            "type": "stdio",
                            "command": "old-command",
                            "args": ["/old/path/index.js"],
                            "env": {"DEBUG": "1"},
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        inst.install(self.ctx, self.fake_install)
        after = _read_json(path)
        entry = after["mcpServers"]["igor-cockpit"]
        # env preserved.
        self.assertEqual(entry.get("env"), {"DEBUG": "1"})
        # Owned fields updated.
        self.assertEqual(entry["command"], "node")
        self.assertEqual(
            entry["args"][0],
            str(self.src_root / "cockpit" / "mcp" / "dist" / "src" / "index.js"),
        )
        self.assertEqual(entry["type"], "stdio")


class SubagentTests(unittest.TestCase):
    def test_empty_source_subagents_is_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            fake_install = _build_fake_source(src_root)  # no subagents
            report = inst.install(ctx, fake_install)
            target = ctx / ".claude" / "cockpit" / "subagents"
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertIn("no profiles in source", report.render())

    def test_single_profile_copied(self) -> None:
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            fake_install = _build_fake_source(src_root, with_subagents=["protocolist.md"])
            inst.install(ctx, fake_install)
            deployed = ctx / ".claude" / "cockpit" / "subagents" / "protocolist.md"
            self.assertTrue(deployed.is_file())
            self.assertIn("subagent profile protocolist.md", deployed.read_text(encoding="utf-8"))

    def test_profiles_overwritten_on_redeploy(self) -> None:
        """Source-of-truth: re-deploy must overwrite stale deployed profiles."""
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            fake_install = _build_fake_source(src_root, with_subagents=["x.md"])
            inst.install(ctx, fake_install)
            # Tamper with deployed copy.
            dst = ctx / ".claude" / "cockpit" / "subagents" / "x.md"
            dst.write_text("# tampered\n", encoding="utf-8")
            inst.install(ctx, fake_install)
            self.assertIn("subagent profile x.md", dst.read_text(encoding="utf-8"))

    def test_subagents_not_deployed_to_dot_claude_agents(self) -> None:
        """Path discipline: never write to .claude/agents/ (reserved)."""
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            fake_install = _build_fake_source(src_root, with_subagents=["x.md"])
            inst.install(ctx, fake_install)
            self.assertFalse((ctx / ".claude" / "agents").exists())

    def test_pruning_removes_source_absent_profiles(self) -> None:
        """Deployed dir mirrors source: profile removed upstream must not persist."""
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            # First deploy: two profiles in source.
            fake_install = _build_fake_source(src_root, with_subagents=["a.md", "b.md"])
            inst.install(ctx, fake_install)
            deployed = ctx / ".claude" / "cockpit" / "subagents"
            self.assertTrue((deployed / "a.md").is_file())
            self.assertTrue((deployed / "b.md").is_file())

            # Remove b.md from source, re-deploy.
            (src_root / "instructions" / "subagents" / "b.md").unlink()
            report = inst.install(ctx, fake_install)

            self.assertTrue((deployed / "a.md").is_file())
            self.assertFalse(
                (deployed / "b.md").exists(),
                "b.md should be pruned after upstream deletion",
            )
            text = report.render()
            self.assertIn("pruned", text)
            self.assertIn("b.md", text)


class TopLevelDirTests(unittest.TestCase):
    def test_existing_objectives_journal_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            fake_install = _build_fake_source(src_root)
            (ctx / "objectives").mkdir()
            (ctx / "objectives" / "OBJ001.md").write_text("preserved", encoding="utf-8")
            (ctx / "journal").mkdir()
            (ctx / "journal" / "entry.md").write_text("preserved", encoding="utf-8")

            inst.install(ctx, fake_install)

            self.assertEqual(
                (ctx / "objectives" / "OBJ001.md").read_text(encoding="utf-8"),
                "preserved",
            )
            self.assertEqual(
                (ctx / "journal" / "entry.md").read_text(encoding="utf-8"),
                "preserved",
            )


class ErrorPathTests(unittest.TestCase):
    def test_missing_context_folder_raises(self) -> None:
        with tempfile.TemporaryDirectory() as src_td:
            src_root = Path(src_td)
            fake_install = _build_fake_source(src_root)
            with self.assertRaises(FileNotFoundError):
                inst.install(Path("/definitely/not/a/real/path/xyzzy"), fake_install)

    def test_missing_mcp_entrypoint_raises_with_hint(self) -> None:
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            fake_install = _build_fake_source(src_root)
            # Remove the built MCP entry.
            (src_root / "cockpit" / "mcp" / "dist" / "src" / "index.js").unlink()
            with self.assertRaises(FileNotFoundError) as cm:
                inst.install(ctx, fake_install)
            self.assertIn("npm run build", str(cm.exception))

    def test_persona_without_placeholder_raises(self) -> None:
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            fake_install = _build_fake_source(src_root)
            # Replace persona with one missing the placeholder.
            (src_root / "instructions" / "igor.md").write_text(
                "## Identity\n\nno placeholder here\n", encoding="utf-8"
            )
            with self.assertRaises(RuntimeError) as cm:
                inst.install(ctx, fake_install)
            self.assertIn("__LOCALIZATION_TEXT__", str(cm.exception))

    def test_persona_with_multiple_placeholders_raises(self) -> None:
        """Multi-placeholder igor.md must fail loud, not silently substitute all."""
        with tempfile.TemporaryDirectory() as src_td, tempfile.TemporaryDirectory() as ctx_td:
            src_root = Path(src_td)
            ctx = Path(ctx_td)
            fake_install = _build_fake_source(src_root)
            # Persona with two placeholders — Identity section + a docs section.
            (src_root / "instructions" / "igor.md").write_text(
                "## Identity\n\n__LOCALIZATION_TEXT__\n\n"
                "## Docs\n\nThe __LOCALIZATION_TEXT__ slot is filled from config.\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as cm:
                inst.install(ctx, fake_install)
            self.assertIn("expected exactly 1", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
