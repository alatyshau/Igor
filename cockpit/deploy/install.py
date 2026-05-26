#!/usr/bin/env python3
"""Igor cockpit installer.

Deploys the cockpit (hooks + MCP + persona + subagent profiles) into a Context
folder so the user can open Claude Code there and have the full setup loaded.

Invocation:

    python3 /path/to/Igor.source.git/cockpit/deploy/install.py <context_folder>

The Igor.source.git repo's own absolute path is discovered from
``__file__``; no environment variables, no Duet dependency at install time.

Idempotent: re-running against an already-deployed Context preserves
user-owned keys / unrelated entries and only updates cockpit-owned bits.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Named constants — repo layout, file names, defaults
# ---------------------------------------------------------------------------

# Layout of the source repo, relative to this file (cockpit/deploy/install.py).
# Resolved at runtime via _SourcePaths.from_install_py(__file__).
REL_HOOKS_DIR = ("..", "hooks")
REL_MCP_ENTRYPOINT = ("..", "mcp", "dist", "src", "index.js")
REL_PERSONA_SRC = ("..", "..", "instructions", "igor.md")
REL_SUBAGENTS_SRC_DIR = ("..", "..", "instructions", "subagents")
REL_SKILLS_SRC_DIR = ("..", "..", "instructions", "skills")

HOOK_USER_PROMPT_SUBMIT_FILE = "user_prompt_submit.py"
HOOK_STOP_FILE = "stop.py"

# Layout of the deployed Context.
CONTEXT_JSON_NAME = "context.json"
SETTINGS_JSON_NAME = "settings.json"
MCP_JSON_NAME = ".mcp.json"
CLAUDE_DIR = ".claude"
SESSIONS_SUBDIR = "sessions"
OUTPUT_STYLES_SUBDIR = "output-styles"
SUBAGENTS_DEPLOY_SUBDIR = ("agents",)  # canonical Claude Code path.
SKILLS_DEPLOY_SUBDIR = ("skills",)     # canonical Claude Code path.
SKILL_MANIFEST_NAME = "SKILL.md"       # required entry-point file in each skill dir.
OBJECTIVES_DIR = "objectives"
JOURNAL_DIR = "journal"
PERSONA_DEPLOY_NAME = "igor.md"

# Persona / settings contract values.
OUTPUT_STYLE_NAME = "igor"
LOCALIZATION_PLACEHOLDER = "__LOCALIZATION_TEXT__"
LOCALIZATION_DEFAULT = (
    "English-speaking. Single-user context. Timezone: system default."
)
MCP_SERVER_NAME = "igor-cockpit"
PYTHON_COMMAND = "python3"
NODE_COMMAND = "node"

# cockpit_config defaults — written explicitly into context.json on scaffold,
# and backfilled into an existing context.json if the key is missing. Keeps
# the file self-documenting (user sees what they can override). Existing
# values are never overwritten; only absent keys are added.
#
# Static defaults are constants here; the dynamic ``skills`` default depends
# on what skills currently exist in the source tree, so it is computed at
# install time by ``_cockpit_config_defaults(source)``.
COCKPIT_CONFIG_DEFAULTS_STATIC: dict[str, Any] = {
    "localization": LOCALIZATION_DEFAULT,
}


def _discover_source_skills(skills_src_dir: Path) -> dict[str, Path]:
    """Return ``{skill_name: skill_dir}`` for every skill directly under
    ``skills_src_dir/<group>/<skill_name>/`` that contains a ``SKILL.md``.

    Source grouping (``modes/``, ``tools/``, ...) is org-convention only —
    Claude Code's runtime sees skills flat under ``.claude/skills/<name>/``.
    Group prefixes are dropped on deploy.
    """
    if not skills_src_dir.is_dir():
        return {}
    out: dict[str, Path] = {}
    for group_dir in sorted(skills_src_dir.iterdir()):
        if not group_dir.is_dir():
            continue
        for skill_dir in sorted(group_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            if not (skill_dir / SKILL_MANIFEST_NAME).is_file():
                continue
            out[skill_dir.name] = skill_dir
    return out


def _cockpit_config_defaults(source: "_SourcePaths") -> dict[str, Any]:
    """Compose the full default dict for ``cockpit_config`` at install time.

    Static keys (``localization``) live in ``COCKPIT_CONFIG_DEFAULTS_STATIC``.
    The ``skills`` default is the sorted list of skill names discovered in
    the source tree — so a freshly-scaffolded ``context.json`` lists every
    available skill explicitly (user can remove names to opt out).
    """
    skills = sorted(_discover_source_skills(source.skills_src_dir).keys())
    return {**COCKPIT_CONFIG_DEFAULTS_STATIC, "skills": skills}


# ---------------------------------------------------------------------------
# Source path discovery
# ---------------------------------------------------------------------------


class _SourcePaths:
    """Absolute paths into the source repo, derived from install.py location."""

    def __init__(
        self,
        user_prompt_submit_hook: Path,
        stop_hook: Path,
        mcp_entrypoint: Path,
        persona_src: Path,
        subagents_src_dir: Path,
        skills_src_dir: Path,
    ) -> None:
        self.user_prompt_submit_hook = user_prompt_submit_hook
        self.stop_hook = stop_hook
        self.mcp_entrypoint = mcp_entrypoint
        self.persona_src = persona_src
        self.subagents_src_dir = subagents_src_dir
        self.skills_src_dir = skills_src_dir

    @classmethod
    def from_install_py(cls, install_py: Path) -> "_SourcePaths":
        base = install_py.resolve().parent
        hooks_dir = base.joinpath(*REL_HOOKS_DIR).resolve()
        return cls(
            user_prompt_submit_hook=hooks_dir / HOOK_USER_PROMPT_SUBMIT_FILE,
            stop_hook=hooks_dir / HOOK_STOP_FILE,
            mcp_entrypoint=base.joinpath(*REL_MCP_ENTRYPOINT).resolve(),
            persona_src=base.joinpath(*REL_PERSONA_SRC).resolve(),
            subagents_src_dir=base.joinpath(*REL_SUBAGENTS_SRC_DIR).resolve(),
            skills_src_dir=base.joinpath(*REL_SKILLS_SRC_DIR).resolve(),
        )

    def verify_required(self) -> list[str]:
        """Return list of human-readable errors for missing required sources.

        ``subagents_src_dir`` is *not* required — empty / missing is a no-op.
        """
        problems: list[str] = []
        if not self.user_prompt_submit_hook.is_file():
            problems.append(
                f"hook source missing: {self.user_prompt_submit_hook}"
            )
        if not self.stop_hook.is_file():
            problems.append(f"hook source missing: {self.stop_hook}")
        if not self.mcp_entrypoint.is_file():
            problems.append(
                "MCP entrypoint missing: "
                f"{self.mcp_entrypoint} "
                "(run `npm run build` in cockpit/mcp/ first)"
            )
        if not self.persona_src.is_file():
            problems.append(f"persona source missing: {self.persona_src}")
        return problems


# ---------------------------------------------------------------------------
# Atomic JSON / text I/O
# ---------------------------------------------------------------------------


def _atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically.

    Writes a temp file in the same directory (same filesystem → ``os.replace``
    is atomic), then renames over the target.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the temp file on failure.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(target: Path, data: Any) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(target, text)


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    """Return parsed JSON object at ``path`` or ``{}`` if file is absent.

    Raises ``ValueError`` if the file exists but is not a JSON object.
    """
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must contain a JSON object, got {type(data).__name__}"
        )
    return data


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


class _Report:
    """Accumulates per-step status lines; printed at the end."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, line: str) -> None:
        self.lines.append(line)

    def render(self) -> str:
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# Step 1 — context.json
# ---------------------------------------------------------------------------


def _ensure_context_json(
    context_folder: Path, source: "_SourcePaths", report: _Report
) -> dict[str, Any]:
    """Create scaffold if absent; backfill ``cockpit_config`` defaults if present.

    Returns the dict — used by step 5 (persona) to read
    ``cockpit_config.localization`` and step 6.5 (skills) to read
    ``cockpit_config.skills``.

    Discipline:
      - Fresh scaffold writes every default key explicitly (computed via
        ``_cockpit_config_defaults(source)``, which discovers available
        skills from source).
      - Existing file: every absent key in ``cockpit_config`` is added with
        its default. Existing values are NEVER overwritten — that includes
        explicit ``null`` / empty strings / lists (user's choice; downstream
        handles malformed values defensively).
      - User-owned top-level keys (outside ``cockpit_config``) are never touched.
    """
    defaults = _cockpit_config_defaults(source)
    path = context_folder / CONTEXT_JSON_NAME
    fresh = not path.exists()
    if fresh:
        data: dict[str, Any] = {
            "name": context_folder.name,
            "cockpit_config": {},
        }
    else:
        data = _read_json_or_empty(path)

    cockpit_config = data.get("cockpit_config")
    if not isinstance(cockpit_config, dict):
        # Either missing or malformed (e.g., user set it to a list / null).
        # Replace with an empty dict so defaults can land. This is the only
        # case where we override a non-default user value, and only because
        # the spec requires ``cockpit_config`` to be a dict.
        cockpit_config = {}
        data["cockpit_config"] = cockpit_config

    backfilled: list[str] = []
    for key, default_value in defaults.items():
        if key not in cockpit_config:
            cockpit_config[key] = default_value
            backfilled.append(key)

    if fresh:
        _atomic_write_json(path, data)
        report.add(
            f"{CONTEXT_JSON_NAME}: created "
            f"(scaffold, defaults: {', '.join(defaults)})"
        )
    elif backfilled:
        _atomic_write_json(path, data)
        report.add(
            f"{CONTEXT_JSON_NAME}: existed, defaults backfilled "
            f"({', '.join(backfilled)})"
        )
    else:
        report.add(f"{CONTEXT_JSON_NAME}: existed, preserved")
    return data


# ---------------------------------------------------------------------------
# Step 2 — .claude/ tree, step 7 — objectives/, journal/
# ---------------------------------------------------------------------------


def _ensure_dir(path: Path, label: str, report: _Report) -> None:
    # EAFP: pre-check exists() then mkdir(exist_ok=True). The race between
    # check and mkdir is now non-fatal — at worst we report "created" when
    # a concurrent install created it first. No FileExistsError.
    pre_existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if pre_existed:
        report.add(f"{label}: existed, skipped")
    else:
        report.add(f"{label}: created")


def _ensure_claude_tree(context_folder: Path, report: _Report) -> None:
    claude_root = context_folder / CLAUDE_DIR
    _ensure_dir(claude_root / SESSIONS_SUBDIR, f"{CLAUDE_DIR}/{SESSIONS_SUBDIR}/", report)
    _ensure_dir(
        claude_root / OUTPUT_STYLES_SUBDIR,
        f"{CLAUDE_DIR}/{OUTPUT_STYLES_SUBDIR}/",
        report,
    )
    _ensure_dir(
        claude_root.joinpath(*SUBAGENTS_DEPLOY_SUBDIR),
        f"{CLAUDE_DIR}/{'/'.join(SUBAGENTS_DEPLOY_SUBDIR)}/",
        report,
    )


def _ensure_top_level_dirs(context_folder: Path, report: _Report) -> None:
    _ensure_dir(context_folder / OBJECTIVES_DIR, f"{OBJECTIVES_DIR}/", report)
    _ensure_dir(context_folder / JOURNAL_DIR, f"{JOURNAL_DIR}/", report)


# ---------------------------------------------------------------------------
# Step 3 — settings.json (hooks merge)
# ---------------------------------------------------------------------------


def _is_our_hook_entry(entry: Any, hook_filename: str) -> bool:
    """Recognize a previously-deployed cockpit hook entry.

    Matches the wrapper shape ``{"hooks": [{"type": "command",
    "command": "python3", "args": ["…/<hook_filename>"]}]}``. The path may
    have changed across deploys (cockpit source moved) — recognition keys
    off basename of args[0], not absolute path, so cookie-cutter updates
    replace rather than duplicate.
    """
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if not isinstance(inner, list) or len(inner) != 1:
        return False
    h = inner[0]
    if not isinstance(h, dict):
        return False
    if h.get("type") != "command" or h.get("command") != PYTHON_COMMAND:
        return False
    args = h.get("args")
    if not isinstance(args, list) or not args:
        return False
    first = args[0]
    if not isinstance(first, str):
        return False
    # Tolerate both forward and back slashes — recognition is by basename.
    basename = first.replace("\\", "/").rsplit("/", 1)[-1]
    return basename == hook_filename


def _build_hook_entry(absolute_path: Path) -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": PYTHON_COMMAND,
                "args": [str(absolute_path)],
            }
        ]
    }


def _merge_hook_event(
    existing: list[Any],
    hook_filename: str,
    absolute_path: Path,
) -> tuple[list[Any], str]:
    """Merge our hook entry into the ``existing`` list for one event.

    Returns ``(new_list, status)`` where ``status`` is one of
    ``"added"``, ``"updated"``, ``"unchanged"``.
    """
    new_entry = _build_hook_entry(absolute_path)
    out: list[Any] = []
    matched = False
    status = "added"
    for entry in existing:
        if not matched and _is_our_hook_entry(entry, hook_filename):
            matched = True
            # Update only the path; preserve any sibling fields the user
            # may have added by overlaying onto a copy of the existing entry.
            updated = json.loads(json.dumps(entry))  # deep copy
            updated["hooks"][0]["args"][0] = str(absolute_path)
            # Defensive: also re-assert command + type in case earlier deploy
            # used a different shape we wouldn't recognize next time.
            updated["hooks"][0]["command"] = PYTHON_COMMAND
            updated["hooks"][0]["type"] = "command"
            if updated == entry:
                status = "unchanged"
            else:
                status = "updated"
            out.append(updated)
        else:
            out.append(entry)
    if not matched:
        out.append(new_entry)
        status = "added"
    return out, status


def _render_settings(
    context_folder: Path,
    source: _SourcePaths,
    report: _Report,
) -> None:
    path = context_folder / SETTINGS_JSON_NAME
    settings = _read_json_or_empty(path)

    # outputStyle — set only if absent, so the user can override.
    if "outputStyle" not in settings:
        settings["outputStyle"] = OUTPUT_STYLE_NAME

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    events = [
        ("UserPromptSubmit", HOOK_USER_PROMPT_SUBMIT_FILE, source.user_prompt_submit_hook),
        ("Stop", HOOK_STOP_FILE, source.stop_hook),
    ]
    statuses: list[tuple[str, str]] = []
    for event_name, hook_filename, abs_path in events:
        existing = hooks.get(event_name)
        if not isinstance(existing, list):
            existing = []
        merged, status = _merge_hook_event(existing, hook_filename, abs_path)
        hooks[event_name] = merged
        statuses.append((event_name, status))

    settings["hooks"] = hooks
    _atomic_write_json(path, settings)

    summary = ", ".join(f"{name}: {st}" for name, st in statuses)
    report.add(f"{SETTINGS_JSON_NAME}: hooks merged ({summary})")


# ---------------------------------------------------------------------------
# Step 4 — .mcp.json (MCP server merge)
# ---------------------------------------------------------------------------


def _render_mcp_json(
    context_folder: Path,
    source: _SourcePaths,
    report: _Report,
) -> None:
    path = context_folder / MCP_JSON_NAME
    data = _read_json_or_empty(path)

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    # Deep-copy overlay (mirror of _merge_hook_event): if a prior entry
    # exists, preserve user-added sibling fields (e.g., ``env``, ``cwd``)
    # and overwrite only the three cockpit-owned fields. If absent, create
    # the minimal entry.
    prev = servers.get(MCP_SERVER_NAME)
    owned = {
        "type": "stdio",
        "command": NODE_COMMAND,
        "args": [str(source.mcp_entrypoint)],
    }
    if isinstance(prev, dict):
        updated = json.loads(json.dumps(prev))  # deep copy
        updated["type"] = owned["type"]
        updated["command"] = owned["command"]
        updated["args"] = owned["args"]
        if updated == prev:
            status = "unchanged"
        else:
            status = "updated"
        servers[MCP_SERVER_NAME] = updated
    elif prev is None:
        servers[MCP_SERVER_NAME] = owned
        status = "registered"
    else:
        # prev was present but not a dict — replace wholesale, treat as update.
        servers[MCP_SERVER_NAME] = owned
        status = "updated"
    data["mcpServers"] = servers

    _atomic_write_json(path, data)
    report.add(f"{MCP_JSON_NAME}: {MCP_SERVER_NAME} {status}")


# ---------------------------------------------------------------------------
# Step 5 — persona deploy with localization substitution
# ---------------------------------------------------------------------------


def _resolve_localization(context_json: dict[str, Any]) -> tuple[str, bool]:
    """Return ``(text, is_customised)``.

    After step 1 the ``localization`` key is always present in ``cockpit_config``
    (backfilled with the default if absent). So the meaningful question shifts
    from «is the key set?» to «did the user override the default?». The boolean
    flag is True iff the value is a non-empty string that differs from
    ``LOCALIZATION_DEFAULT``. Malformed values (``null``, empty, non-string)
    fall back to the default and report as not-customised.
    """
    cockpit_config = context_json.get("cockpit_config")
    if isinstance(cockpit_config, dict):
        value = cockpit_config.get("localization")
        if isinstance(value, str) and value.strip():
            return value, value != LOCALIZATION_DEFAULT
    return LOCALIZATION_DEFAULT, False


def _deploy_persona(
    context_folder: Path,
    source: _SourcePaths,
    context_json: dict[str, Any],
    report: _Report,
) -> None:
    persona_text = source.persona_src.read_text(encoding="utf-8")
    if LOCALIZATION_PLACEHOLDER not in persona_text:
        # 3-whys: persona contract requires the placeholder; absence means
        # source drifted from spec and the deployed file would carry no
        # personalization — that's a silent failure mode. Fail loud.
        raise RuntimeError(
            f"persona source {source.persona_src} is missing "
            f"placeholder {LOCALIZATION_PLACEHOLDER!r}"
        )
    # Spec scopes the placeholder to a single occurrence inside ``## Identity``.
    # ``str.replace`` substitutes all occurrences — fail loud if igor.md grows
    # a second one (e.g. a docs section mentioning the placeholder by name),
    # rather than silently mangling it. Consistent with Decision 9.
    placeholder_count = persona_text.count(LOCALIZATION_PLACEHOLDER)
    if placeholder_count != 1:
        raise RuntimeError(
            f"persona source {source.persona_src} contains {placeholder_count} "
            f"occurrences of {LOCALIZATION_PLACEHOLDER!r}; expected exactly 1"
        )
    text, is_customised = _resolve_localization(context_json)
    rendered = persona_text.replace(LOCALIZATION_PLACEHOLDER, text)
    target = (
        context_folder
        / CLAUDE_DIR
        / OUTPUT_STYLES_SUBDIR
        / PERSONA_DEPLOY_NAME
    )
    _atomic_write_text(target, rendered)

    label = "explicit" if is_customised else "default"
    report.add(
        f"{CLAUDE_DIR}/{OUTPUT_STYLES_SUBDIR}/{PERSONA_DEPLOY_NAME}: "
        f"deployed (localization: {label})"
    )


# ---------------------------------------------------------------------------
# Step 6 — subagent profiles (copy from instructions/subagents/)
# ---------------------------------------------------------------------------


def _deploy_subagents(
    context_folder: Path,
    source: _SourcePaths,
    report: _Report,
) -> None:
    target_dir = context_folder.joinpath(CLAUDE_DIR, *SUBAGENTS_DEPLOY_SUBDIR)
    label = f"{CLAUDE_DIR}/{'/'.join(SUBAGENTS_DEPLOY_SUBDIR)}/"

    if not source.subagents_src_dir.is_dir():
        report.add(f"{label} empty (source dir absent: {source.subagents_src_dir})")
        return

    profiles = sorted(p for p in source.subagents_src_dir.glob("*.md") if p.is_file())
    if not profiles:
        report.add(f"{label} empty (no profiles in source)")
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for src in profiles:
        dst = target_dir / src.name
        # Atomic copy: write to temp in same dir, then replace.
        _atomic_write_text(dst, src.read_text(encoding="utf-8"))
        names.append(src.name)

    # Prune: deployed dir mirrors source. Any *.md present in target but not
    # in source set is a ghost from a prior deploy (deleted or renamed
    # upstream) — remove it. Deployed folder is the MCP contract surface
    # (cockpit/specs/mcp.md), so a ghost is a stale spawn_subchat target.
    source_names = {p.name for p in profiles}
    pruned: list[str] = []
    for existing in sorted(target_dir.glob("*.md")):
        if existing.is_file() and existing.name not in source_names:
            try:
                existing.unlink()
            except FileNotFoundError:
                pass
            pruned.append(existing.name)

    summary = f"{label} deployed {len(names)} profile(s): {', '.join(names)}"
    if pruned:
        summary += f"; pruned {len(pruned)}: {', '.join(pruned)}"
    report.add(summary)


# ---------------------------------------------------------------------------
# Step 6.5 — skills (recursive copy from instructions/skills/<group>/<name>/)
# ---------------------------------------------------------------------------


def _resolve_skills_to_deploy(
    context_json: dict[str, Any],
    available: dict[str, Path],
    report: _Report,
    label: str,
) -> list[str]:
    """Resolve the list of skill names the user opted to deploy.

    Reads ``cockpit_config.skills``: if a list of strings, treat that as the
    whitelist; if missing / wrong type, fall back to all available source
    skills (defensive — ``_ensure_context_json`` would have backfilled, so
    this branch is for hand-edited corner cases). Unknown names in the list
    produce a warn line and are skipped.
    """
    cockpit_config = context_json.get("cockpit_config")
    raw: Any = None
    if isinstance(cockpit_config, dict):
        raw = cockpit_config.get("skills")
    if not isinstance(raw, list):
        return sorted(available.keys())
    requested = [s for s in raw if isinstance(s, str)]
    unknown = [s for s in requested if s not in available]
    if unknown:
        report.add(
            f"{label} warning — unknown skill(s) in cockpit_config.skills: "
            f"{', '.join(unknown)}"
        )
    return [s for s in requested if s in available]


def _atomic_copy_tree(src_dir: Path, dst_dir: Path) -> None:
    """Recursively copy ``src_dir`` contents into ``dst_dir``.

    Per-file atomic writes (temp + os.replace). Directories created
    eagerly. Subtree existing in target but not in source is removed
    in the per-skill prune pass driven by the caller — this function
    only mirrors source onto target.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src_path in sorted(src_dir.rglob("*")):
        rel = src_path.relative_to(src_dir)
        dst_path = dst_dir / rel
        if src_path.is_dir():
            dst_path.mkdir(parents=True, exist_ok=True)
        elif src_path.is_file():
            _atomic_write_text(dst_path, src_path.read_text(encoding="utf-8"))


def _deploy_skills(
    context_folder: Path,
    source: _SourcePaths,
    context_json: dict[str, Any],
    report: _Report,
) -> None:
    target_root = context_folder.joinpath(CLAUDE_DIR, *SKILLS_DEPLOY_SUBDIR)
    label = f"{CLAUDE_DIR}/{'/'.join(SKILLS_DEPLOY_SUBDIR)}/"

    available = _discover_source_skills(source.skills_src_dir)
    to_deploy = _resolve_skills_to_deploy(context_json, available, report, label)

    if not to_deploy and not target_root.exists():
        if not available:
            report.add(f"{label} empty (no skills in source)")
        else:
            report.add(f"{label} empty (cockpit_config.skills opted out of all)")
        return

    target_root.mkdir(parents=True, exist_ok=True)
    deployed: list[str] = []
    for name in to_deploy:
        skill_src_dir = available[name]
        skill_dst_dir = target_root / name
        _atomic_copy_tree(skill_src_dir, skill_dst_dir)
        deployed.append(name)

    # Prune: target/<name> not in `to_deploy` is a ghost (user removed it
    # from list, or source dropped the skill).
    pruned: list[str] = []
    if target_root.is_dir():
        keep = set(to_deploy)
        for child in sorted(target_root.iterdir()):
            if child.is_dir() and child.name not in keep:
                _rmtree(child)
                pruned.append(child.name)

    summary = (
        f"{label} deployed {len(deployed)} skill(s): {', '.join(deployed)}"
        if deployed
        else f"{label} empty (cockpit_config.skills opted out of all)"
    )
    if pruned:
        summary += f"; pruned {len(pruned)}: {', '.join(pruned)}"
    report.add(summary)


def _rmtree(path: Path) -> None:
    """Best-effort recursive removal — silently swallows FileNotFoundError."""
    for child in sorted(path.rglob("*"), reverse=True):
        try:
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        except FileNotFoundError:
            pass
    try:
        path.rmdir()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def install(context_folder: Path, install_py: Path) -> _Report:
    """Run all 8 deploy steps against ``context_folder``.

    ``install_py`` is the absolute path of this script — used to discover
    source paths. Injected for testability.
    """
    if not context_folder.exists():
        raise FileNotFoundError(f"context folder does not exist: {context_folder}")
    if not context_folder.is_dir():
        raise NotADirectoryError(f"not a directory: {context_folder}")

    source = _SourcePaths.from_install_py(install_py)
    problems = source.verify_required()
    if problems:
        raise FileNotFoundError("required source(s) missing:\n  - " + "\n  - ".join(problems))

    report = _Report()

    # Step 1 — context.json must exist before step 5 reads localization and
    # step 6.5 reads cockpit_config.skills.
    context_json = _ensure_context_json(context_folder, source, report)
    # Step 2 — .claude/ tree.
    _ensure_claude_tree(context_folder, report)
    # Step 3 — settings.json.
    _render_settings(context_folder, source, report)
    # Step 4 — .mcp.json.
    _render_mcp_json(context_folder, source, report)
    # Step 5 — persona.
    _deploy_persona(context_folder, source, context_json, report)
    # Step 6 — subagent profiles.
    _deploy_subagents(context_folder, source, report)
    # Step 6.5 — skills (gated by cockpit_config.skills).
    _deploy_skills(context_folder, source, context_json, report)
    # Step 7 — objectives/ and journal/.
    _ensure_top_level_dirs(context_folder, report)
    # Step 8 — report (printed by main()).

    return report


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Deploy Igor cockpit into a Context folder.",
    )
    parser.add_argument(
        "context_folder",
        type=Path,
        help="Absolute path to the Context folder to install into.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = install(args.context_folder.resolve(), Path(__file__))
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as exc:
        print(f"install: error: {exc}", file=sys.stderr)
        return 1
    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
