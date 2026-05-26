"""``config.yaml`` parsing for the subchat CLI.

The schema is fully under our control (the config.yaml is produced by MCP
``spawn_subchat`` from the subagent profile's frontmatter — see
``cockpit/specs/subchat.md``). It is small, flat, and uses only a known
subset of YAML syntax: scalars (string / int / bool / null), inline lists
(``[a, b, c]``), and inline maps (``{}`` / ``{k: v}``). A handwritten
parser for that subset removes a third-party dependency (PyYAML) for what
amounts to ~50 lines of code.

The parser is intentionally strict on the known schema (typed coercion per
field) and tolerant of trailing comments / blank lines / ``#``-comments.
Anything outside the recognised syntax raises ``ConfigError``.

The module also resolves path-valued fields to absolute paths anchored at
the subchat folder (NOT the SessionFolder, NOT claude's cwd) per the
"Path resolution" subsection of the spec — using ``abspath``, never
``realpath``, so that symlinked subchat folders are not collapsed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Schema defaults — applied to fields absent in the file.
#
# Mirrors the table in subchat.md "config.yaml schema". A field's default is
# documented in the spec; this dict is the single source for that default.
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "model": "sonnet",
    "system-prompt": "system_prompt.md",
    "append-system-prompt": None,
    "max-turns": None,
    "allowed-tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    "disallowed-tools": [],
    "permission-mode": None,
    "bare": False,
    "effort": "max",
    "cwd": "../..",
    "no-session-persistence": False,
    "env": {},
    "output-format": "stream-json",
}

# Path-valued fields — resolved against the subchat folder, not claude's cwd.
PATH_FIELDS: frozenset[str] = frozenset({
    "system-prompt",
    "append-system-prompt",
    "cwd",
})

# Field-type expectations. Used for both parse-time validation and defaults
# coercion. ``None`` is always permitted alongside the declared type.
_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "model": str,
    "system-prompt": str,
    "append-system-prompt": str,
    "max-turns": int,
    "allowed-tools": list,
    "disallowed-tools": list,
    "permission-mode": str,
    "bare": bool,
    "effort": str,
    "cwd": str,
    "no-session-persistence": bool,
    "env": dict,
    "output-format": str,
}


class ConfigError(Exception):
    """Raised when ``config.yaml`` cannot be parsed or violates the schema."""


@dataclass
class Config:
    """Validated, defaults-applied config for one subchat invocation.

    ``resolved_*`` fields hold absolute paths produced by resolving the
    on-disk relative values against the subchat folder.
    """

    raw: dict[str, Any]
    subchat_folder: str
    defaulted_fields: list[str] = field(default_factory=list)

    def get(self, key: str) -> Any:
        return self.raw.get(key, DEFAULTS.get(key))

    # Convenience accessors — keep call-sites readable.

    @property
    def resolved_system_prompt(self) -> str | None:
        v = self.get("system-prompt")
        return _resolve_path(self.subchat_folder, v) if v else None

    @property
    def resolved_append_system_prompt(self) -> str | None:
        v = self.get("append-system-prompt")
        return _resolve_path(self.subchat_folder, v) if v else None

    @property
    def resolved_cwd(self) -> str:
        v = self.get("cwd")
        # ``cwd`` always has a default ("../..") so this should never be None.
        return _resolve_path(self.subchat_folder, v)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load(path: str, subchat_folder: str) -> Config:
    """Load ``config.yaml`` from ``path``, apply defaults, validate types,
    and return a :class:`Config` bound to ``subchat_folder`` for path
    resolution.

    Raises :class:`ConfigError` on any parse or type failure.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(f"cannot read config.yaml at {path}: {e}") from e

    raw = _parse_minimal_yaml(text)
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml top level must be a mapping")

    # Type-check known fields, leave unknown fields as-is (forward-compat).
    for key, value in raw.items():
        expected = _FIELD_TYPES.get(key)
        if expected is None:
            continue  # unknown field — ignore for forward compat
        if value is None:
            continue  # null is always permitted; means "default / not set"
        if expected is int and isinstance(value, bool):
            # bool is a subclass of int in Python — reject explicitly.
            raise ConfigError(f"{key!r}: expected int, got bool")
        if not isinstance(value, expected):
            raise ConfigError(
                f"{key!r}: expected {expected}, got {type(value).__name__}"
            )

    defaulted = [k for k in DEFAULTS if k not in raw]
    return Config(raw=raw, subchat_folder=subchat_folder, defaulted_fields=defaulted)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_path(subchat_folder: str, value: str) -> str:
    """Resolve ``value`` against ``subchat_folder``.

    Uses ``abspath`` (NOT ``realpath``) per the spec — symlink resolution
    would surprise if a subchat folder is symlinked to a shared template.
    Absolute ``value`` is returned untouched (still passed through
    ``abspath`` to normalise ``..`` and ``.``).
    """
    return os.path.abspath(os.path.join(subchat_folder, value))


# ---------------------------------------------------------------------------
# Minimal YAML parser
# ---------------------------------------------------------------------------
#
# Supports exactly the subset used by ``config.yaml``:
#   - top-level mapping: ``key: value`` per line
#   - scalars: bare strings, quoted strings, ints, ``true``/``false``,
#     ``null`` / ``~`` / empty
#   - inline lists: ``[a, b, c]`` (scalars only, no nesting)
#   - inline maps:  ``{}`` or ``{k: v, k2: v2}`` (scalars only, no nesting)
#   - block lists under a key (``key:`` then ``  - item`` lines)
#   - block mappings under a key (``key:`` then ``  k: v`` lines)
#   - blank lines and ``#`` line / inline comments
#
# Anything outside this subset raises ``ConfigError``.


def _parse_minimal_yaml(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    result: dict[str, Any] = {}

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            i += 1
            continue
        # Top-level lines have zero indent.
        if line[0] in (" ", "\t"):
            raise ConfigError(
                f"unexpected indentation at top level (line {i + 1}): {raw_line!r}"
            )
        if ":" not in line:
            raise ConfigError(f"line {i + 1}: missing ':' in {raw_line!r}")
        key, _, after = line.partition(":")
        key = key.strip()
        after = after.strip()

        if after == "":
            # Possible block list or block map under this key. Look ahead.
            block, consumed = _parse_block(lines, i + 1)
            if block is None:
                # Empty value — treat as None.
                result[key] = None
            else:
                result[key] = block
            i += 1 + consumed
            continue

        result[key] = _parse_scalar_or_inline(after, line_no=i + 1)
        i += 1
    return result


def _strip_comment(line: str) -> str:
    """Strip a trailing ``#`` comment, respecting quotes."""
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:idx]
    return line


def _parse_block(lines: list[str], start: int) -> tuple[Any, int]:
    """Parse a block list (``- item``) or block map (``  k: v``) starting at
    ``start``. Returns ``(value, consumed)``. If no block content follows
    (next non-blank line has zero indent), returns ``(None, 0)``.
    """
    # Skip blank / comment-only lines.
    j = start
    while j < len(lines) and not _strip_comment(lines[j]).strip():
        j += 1
    if j >= len(lines):
        return None, j - start
    first = lines[j]
    leading = len(first) - len(first.lstrip(" "))
    if leading == 0:
        return None, 0  # not a block

    indent_str = first[:leading]
    is_list = first.lstrip().startswith("- ") or first.lstrip() == "-"

    if is_list:
        items: list[Any] = []
        while j < len(lines):
            current = lines[j]
            stripped_line = _strip_comment(current).rstrip()
            if not stripped_line.strip():
                j += 1
                continue
            if not current.startswith(indent_str):
                break
            content = current[len(indent_str):]
            if not content.startswith("-"):
                break
            after_dash = content[1:].lstrip()
            items.append(_parse_scalar_or_inline(after_dash, line_no=j + 1))
            j += 1
        return items, j - start

    # Block map.
    mapping: dict[str, Any] = {}
    while j < len(lines):
        current = lines[j]
        stripped_line = _strip_comment(current).rstrip()
        if not stripped_line.strip():
            j += 1
            continue
        if not current.startswith(indent_str):
            break
        content = stripped_line[len(indent_str):]
        if ":" not in content:
            raise ConfigError(f"line {j + 1}: expected 'k: v' in block map")
        k, _, v = content.partition(":")
        mapping[k.strip()] = _parse_scalar_or_inline(v.strip(), line_no=j + 1)
        j += 1
    return mapping, j - start


def _parse_scalar_or_inline(text: str, line_no: int) -> Any:
    """Parse a scalar, inline list, or inline map from ``text``."""
    if text == "" or text.lower() in ("null", "~"):
        return None
    if text.startswith("[") and text.endswith("]"):
        body = text[1:-1].strip()
        if not body:
            return []
        return [_parse_scalar_or_inline(_strip_quotes(p.strip()), line_no)
                for p in _split_inline(body, line_no)]
    if text.startswith("{") and text.endswith("}"):
        body = text[1:-1].strip()
        if not body:
            return {}
        out: dict[str, Any] = {}
        for entry in _split_inline(body, line_no):
            if ":" not in entry:
                raise ConfigError(f"line {line_no}: bad inline map entry: {entry!r}")
            k, _, v = entry.partition(":")
            out[_strip_quotes(k.strip())] = _parse_scalar_or_inline(
                v.strip(), line_no
            )
        return out
    return _parse_bare_scalar(text)


def _parse_bare_scalar(text: str) -> Any:
    """Parse a bare scalar — int, bool, or string."""
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    # Try int.
    try:
        # Reject floats / leading zeros / signs explicitly? — keep simple:
        # accept what ``int`` accepts.
        if text.lstrip("-").isdigit():
            return int(text)
    except ValueError:
        pass
    return _strip_quotes(text)


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _split_inline(body: str, line_no: int) -> list[str]:
    """Split a comma-separated inline list/map body, respecting quotes.

    The parser does NOT support nested ``[...]`` or ``{...}`` inside an
    inline container — the schema does not require them. A balanced-bracket
    check here would over-engineer the case.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    for ch in body:
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == "," and not in_single and not in_double:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if in_single or in_double:
        raise ConfigError(f"line {line_no}: unterminated quote in inline value")
    last = "".join(buf).strip()
    if last:
        parts.append(last)
    return parts
