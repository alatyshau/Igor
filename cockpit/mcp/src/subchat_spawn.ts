// spawn_subchat — materialize a subchat folder from a subagent profile.
//
// Reads the markdown profile at
// ``<ContextFolder>/.claude/agents/<name>.md`` (deployed by
// install.py — see cockpit/specs/deploy.md), splits its YAML frontmatter
// from the body, merges the frontmatter onto the schema defaults from
// cockpit/specs/subchat.md, and writes:
//
//   <SessionFolder>/subchats/<name>/
//     config.yaml         — all schema fields explicit, regenerated on every spawn
//     system_prompt.md    — profile body, regenerated on every spawn
//
// ``session.json`` and ``log/`` are left intact on re-spawn — they belong to
// the subchat runtime (cockpit/subchat/) and are MCP-untouched after the
// initial materialization.
//
// State-file integration (``state.md`` ``## Subchats``) is performed by the
// caller via ``ensureSubchatLine`` in ``state_md.ts``.
//
// YAML scope. The profile frontmatter and the emitted ``config.yaml`` use
// only the subset that ``cockpit/subchat/config.py`` understands: scalars
// (string / int / bool / null), inline lists ``[a, b, c]``, and inline maps
// ``{k: v}``. Block sequences and block maps are not supported — the schema
// is small and flat. Any frontmatter feature outside this subset surfaces
// as a parse error before we write anything.
//
// Defaults discipline. ``DEFAULT_CONFIG`` below mirrors ``DEFAULTS`` in
// ``cockpit/subchat/config.py`` field-for-field. The two must stay in sync.
// A dedup later (e.g., generate from a shared JSON spec at build time) is a
// follow-up; for now the surface is small enough that hand-sync is fine.
//
// Spec sources: cockpit/specs/mcp.md (``spawn_subchat`` row),
// cockpit/specs/subchat.md (``config.yaml`` schema, folder layout, overwrite
// semantics), cockpit/specs/deploy.md (profile location discipline).

import { promises as fs } from "node:fs";
import * as path from "node:path";
import { atomicWriteText, pathExists } from "./fs_atomic.js";

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

/** Subdir under the ContextFolder where install.py deploys subagent profiles.
 *  Canonical Claude Code path — auto-discovered on session start so native
 *  visibility works (@agent-mention, /agents). Cockpit subagents are still
 *  invoked through subchat (not Task) per ``instructions/igor.md`` rule;
 *  native discovery is for visibility, not invocation. See deploy.md §6. */
export const SUBAGENT_PROFILES_SUBDIR = path.join(".claude", "agents");

/** Subdir under a SessionFolder where MCP materializes per-subagent folders. */
export const SUBCHATS_SUBDIR = "subchats";

export const CONFIG_FILENAME = "config.yaml";
export const SYSTEM_PROMPT_FILENAME = "system_prompt.md";

// ---------------------------------------------------------------------------
// Subagent name validation
// ---------------------------------------------------------------------------

// Subagent name is used both as a filesystem segment (folder name) and as a
// profile-file basename. Path-separator characters, parent-dir refs, leading
// dots, and whitespace are forbidden. Empty names are forbidden.
//
// More permissive than ``assertValidSlug`` (which forbids hyphens) because
// subagent names are repo-controlled identifiers, not user-facing slugs —
// future names like ``code-reviewer`` should remain possible without
// re-litigating the slug rule.
const SUBAGENT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_\-]*$/;

export function assertValidSubagentName(name: string): void {
  if (!name) {
    throw new Error("subagent name is empty");
  }
  if (!SUBAGENT_NAME_RE.test(name)) {
    throw new Error(
      `invalid subagent name "${name}": must start with [A-Za-z0-9] and contain only [A-Za-z0-9_-]`,
    );
  }
}

// ---------------------------------------------------------------------------
// Config schema defaults — MUST mirror cockpit/subchat/config.py DEFAULTS.
// ---------------------------------------------------------------------------

export type ConfigValue =
  | string
  | number
  | boolean
  | null
  | ConfigValue[]
  | { [key: string]: ConfigValue };

/** Schema defaults from cockpit/specs/subchat.md §config.yaml schema.
 *
 *  Field order here is the emit order in config.yaml — kept human-readable
 *  and stable across regeneration so diffs stay reviewable. */
export const DEFAULT_CONFIG: Array<[string, ConfigValue]> = [
  ["model", "sonnet"],
  ["system-prompt", "system_prompt.md"],
  ["append-system-prompt", null],
  ["max-turns", null],
  ["allowed-tools", ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]],
  ["disallowed-tools", []],
  ["permission-mode", null],
  ["bare", false],
  ["effort", "max"],
  ["cwd", "../.."],
  ["no-session-persistence", false],
  ["env", {}],
  ["output-format", "stream-json"],
];

// ---------------------------------------------------------------------------
// Profile parsing — YAML frontmatter + markdown body
// ---------------------------------------------------------------------------

export interface ParsedProfile {
  frontmatter: { [key: string]: ConfigValue };
  body: string;
}

/** Split a profile file into ``{frontmatter, body}``.
 *
 *  The profile MUST start with a ``---`` line followed by YAML up to a
 *  closing ``---`` line. Missing frontmatter is a hard error: the schema
 *  requires explicit declarations (e.g., protocolist must set
 *  ``output-format: stream-json``); silently treating a no-frontmatter
 *  profile as all-defaults would hide misconfiguration.
 *
 *  Newline handling: both ``\n`` and ``\r\n`` are accepted; the emitted
 *  body is normalized to ``\n``.
 */
export function parseProfile(text: string): ParsedProfile {
  // Strip an optional leading BOM so the first-line check below works whether
  // the profile was written with or without one.
  const normalized = text.replace(/^﻿/, "").replace(/\r\n/g, "\n");
  const lines = normalized.split("\n");
  if (lines.length === 0 || lines[0] !== "---") {
    throw new Error(
      "subagent profile must start with a YAML frontmatter delimiter '---' on line 1",
    );
  }
  let closeIdx = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i] === "---") {
      closeIdx = i;
      break;
    }
  }
  if (closeIdx === -1) {
    throw new Error(
      "subagent profile frontmatter is not closed — expected a '---' line after the opening one",
    );
  }
  const yamlText = lines.slice(1, closeIdx).join("\n");
  // Body is everything after the closing ``---`` line. If the profile has a
  // single newline between ``---`` and the first body line, drop that leading
  // empty line so the body reads naturally.
  let bodyStart = closeIdx + 1;
  if (bodyStart < lines.length && lines[bodyStart] === "") {
    bodyStart += 1;
  }
  const body = lines.slice(bodyStart).join("\n");
  const frontmatter = parseMinimalYaml(yamlText);
  return { frontmatter, body };
}

// ---------------------------------------------------------------------------
// Minimal YAML parser — exactly the subset accepted by cockpit/subchat/config.py
// ---------------------------------------------------------------------------

/** Parse the YAML subset used by subagent frontmatter:
 *  top-level ``key: value`` lines with scalar / inline-list / inline-map
 *  values. Block sequences and block maps are not supported; comments
 *  (``#`` outside quotes) and blank lines are ignored.
 *
 *  This must stay aligned with cockpit/subchat/config.py — any feature added
 *  on one side must be mirrored on the other or the round-trip breaks.
 */
export function parseMinimalYaml(text: string): { [key: string]: ConfigValue } {
  const result: { [key: string]: ConfigValue } = {};
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i] ?? "";
    const stripped = stripYamlComment(raw).replace(/\s+$/, "");
    if (stripped.trim() === "") continue;
    if (stripped[0] === " " || stripped[0] === "\t") {
      throw new Error(
        `frontmatter line ${i + 1}: unexpected indentation (block structures not supported)`,
      );
    }
    const colonIdx = stripped.indexOf(":");
    if (colonIdx === -1) {
      throw new Error(`frontmatter line ${i + 1}: missing ':'`);
    }
    const key = stripped.slice(0, colonIdx).trim();
    const after = stripped.slice(colonIdx + 1).trim();
    if (key === "") {
      throw new Error(`frontmatter line ${i + 1}: empty key`);
    }
    result[key] = parseScalarOrInline(after, i + 1);
  }
  return result;
}

function stripYamlComment(line: string): string {
  let inSingle = false;
  let inDouble = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === "'" && !inDouble) inSingle = !inSingle;
    else if (ch === '"' && !inSingle) inDouble = !inDouble;
    else if (ch === "#" && !inSingle && !inDouble) return line.slice(0, i);
  }
  return line;
}

function parseScalarOrInline(text: string, lineNo: number): ConfigValue {
  if (text === "" || text === "null" || text === "Null" || text === "NULL" || text === "~") {
    return null;
  }
  if (text.startsWith("[") && text.endsWith("]")) {
    const body = text.slice(1, -1).trim();
    if (body === "") return [];
    return splitInline(body, lineNo).map((p) =>
      parseScalarOrInline(stripQuotes(p.trim()), lineNo),
    );
  }
  if (text.startsWith("{") && text.endsWith("}")) {
    const body = text.slice(1, -1).trim();
    const obj: { [key: string]: ConfigValue } = {};
    if (body === "") return obj;
    for (const entry of splitInline(body, lineNo)) {
      const colonIdx = entry.indexOf(":");
      if (colonIdx === -1) {
        throw new Error(`line ${lineNo}: bad inline map entry: ${entry}`);
      }
      const k = stripQuotes(entry.slice(0, colonIdx).trim());
      const v = entry.slice(colonIdx + 1).trim();
      obj[k] = parseScalarOrInline(v, lineNo);
    }
    return obj;
  }
  return parseBareScalar(text);
}

function parseBareScalar(text: string): ConfigValue {
  if (text === "true" || text === "True" || text === "TRUE") return true;
  if (text === "false" || text === "False" || text === "FALSE") return false;
  // Integer (optionally signed). Reject floats / hex for simplicity — the
  // schema does not need them.
  if (/^-?\d+$/.test(text)) {
    const n = Number.parseInt(text, 10);
    if (Number.isFinite(n)) return n;
  }
  return stripQuotes(text);
}

function stripQuotes(text: string): string {
  if (
    text.length >= 2 &&
    text[0] === text[text.length - 1] &&
    (text[0] === "'" || text[0] === '"')
  ) {
    return text.slice(1, -1);
  }
  return text;
}

function splitInline(body: string, lineNo: number): string[] {
  const parts: string[] = [];
  let buf = "";
  let inSingle = false;
  let inDouble = false;
  for (const ch of body) {
    if (ch === "'" && !inDouble) {
      inSingle = !inSingle;
      buf += ch;
    } else if (ch === '"' && !inSingle) {
      inDouble = !inDouble;
      buf += ch;
    } else if (ch === "," && !inSingle && !inDouble) {
      parts.push(buf.trim());
      buf = "";
    } else {
      buf += ch;
    }
  }
  if (inSingle || inDouble) {
    throw new Error(`line ${lineNo}: unterminated quote in inline value`);
  }
  if (buf.trim() !== "") parts.push(buf.trim());
  return parts;
}

// ---------------------------------------------------------------------------
// Config merge + YAML emit
// ---------------------------------------------------------------------------

/** Merge ``frontmatter`` onto ``DEFAULT_CONFIG``: every default key is
 *  emitted; if the frontmatter overrides it, the override wins. Unknown
 *  keys present in the frontmatter are dropped — the on-disk schema is
 *  fixed by ``cockpit/specs/subchat.md`` and surfacing arbitrary frontmatter
 *  fields would create a silent extension surface. (The list of dropped
 *  keys is returned so the caller can surface a warning if desired.)
 */
export function mergeConfig(frontmatter: {
  [key: string]: ConfigValue;
}): { merged: Array<[string, ConfigValue]>; ignoredKeys: string[] } {
  const knownKeys = new Set(DEFAULT_CONFIG.map(([k]) => k));
  const merged: Array<[string, ConfigValue]> = DEFAULT_CONFIG.map(
    ([k, def]) => {
      if (Object.prototype.hasOwnProperty.call(frontmatter, k)) {
        return [k, frontmatter[k] as ConfigValue];
      }
      return [k, def];
    },
  );
  const ignoredKeys: string[] = [];
  for (const k of Object.keys(frontmatter)) {
    if (!knownKeys.has(k)) ignoredKeys.push(k);
  }
  return { merged, ignoredKeys };
}

/** Emit a YAML document from a list of ``[key, value]`` pairs in the
 *  inline subset (one key per line; lists and maps inline). Output is
 *  deterministic so re-spawning the same profile yields a byte-identical
 *  ``config.yaml``. */
export function emitYaml(entries: Array<[string, ConfigValue]>): string {
  const lines: string[] = [];
  for (const [k, v] of entries) {
    lines.push(`${k}: ${emitValue(v)}`);
  }
  return lines.join("\n") + "\n";
}

function emitValue(v: ConfigValue): string {
  if (v === null) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) {
      throw new Error(`cannot emit non-finite number: ${v}`);
    }
    return String(v);
  }
  if (typeof v === "string") return emitString(v);
  if (Array.isArray(v)) {
    return `[${v.map((item) => emitValue(item)).join(", ")}]`;
  }
  // Plain object — inline map.
  const entries = Object.entries(v);
  if (entries.length === 0) return "{}";
  return `{${entries.map(([k, val]) => `${emitString(k)}: ${emitValue(val)}`).join(", ")}}`;
}

// Bare strings are safe iff they don't collide with reserved literals
// (true/false/null/~), don't look like a number, and don't contain YAML
// metacharacters that would change the parse (``: # , [ ] { } " ' \``, or
// leading whitespace). Otherwise emit a double-quoted string with minimal
// escaping (``\`` and ``"``).
const YAML_BARE_SAFE = /^[A-Za-z_][A-Za-z0-9_\-./]*$/;
const YAML_RESERVED_LITERALS = new Set([
  "true",
  "True",
  "TRUE",
  "false",
  "False",
  "FALSE",
  "null",
  "Null",
  "NULL",
  "~",
  "",
]);

function emitString(s: string): string {
  if (
    YAML_BARE_SAFE.test(s) &&
    !YAML_RESERVED_LITERALS.has(s) &&
    !/^-?\d+$/.test(s)
  ) {
    return s;
  }
  if (/[\\"\n\r]/.test(s)) {
    throw new Error(
      `cannot emit string containing backslash/quote/newline: ${JSON.stringify(s)}`,
    );
  }
  return `"${s}"`;
}

// ---------------------------------------------------------------------------
// Materialization
// ---------------------------------------------------------------------------

export interface SpawnResult {
  /** Absolute path of the subchat folder, ``<SessionFolder>/subchats/<name>/``. */
  subchatFolder: string;
  /** True iff this is the first spawn (subchat folder did not pre-exist). */
  created: boolean;
  /** Frontmatter keys present in the profile that aren't in the schema. */
  ignoredProfileKeys: string[];
}

/** Materialize the subchat folder. Idempotent: re-spawn regenerates
 *  ``config.yaml`` and ``system_prompt.md`` from the current profile but
 *  preserves ``session.json`` and ``log/`` untouched.
 *
 *  Caller is responsible for invoking ``ensureSubchatLine`` on the session's
 *  ``state.md`` — keeping that step in the tool handler keeps this function
 *  free of context/session coupling and testable in isolation. */
export async function materializeSubchat(args: {
  contextRoot: string;
  sessionFolder: string;
  subagent: string;
}): Promise<SpawnResult> {
  const { contextRoot, sessionFolder, subagent } = args;
  assertValidSubagentName(subagent);

  const profilePath = path.join(
    contextRoot,
    SUBAGENT_PROFILES_SUBDIR,
    `${subagent}.md`,
  );
  if (!(await pathExists(profilePath))) {
    // Recoverable error: install.py hasn't deployed (or has been re-run with
    // a stale source). Point the caller at deploy.md, which documents the fix.
    throw new Error(
      `subagent profile not found at ${path.join(SUBAGENT_PROFILES_SUBDIR, `${subagent}.md`)} — ` +
        `re-run install.py (see cockpit/specs/deploy.md §Deploy subagent profiles)`,
    );
  }

  const profileText = await fs.readFile(profilePath, "utf8");
  const { frontmatter, body } = parseProfile(profileText);
  const { merged, ignoredKeys } = mergeConfig(frontmatter);

  const subchatFolder = path.join(sessionFolder, SUBCHATS_SUBDIR, subagent);
  const created = !(await pathExists(subchatFolder));
  await fs.mkdir(subchatFolder, { recursive: true });

  // Overwrite semantics: config.yaml + system_prompt.md are MCP-owned and
  // regenerated every spawn. session.json and log/ belong to the subchat
  // runtime — we explicitly do not touch them on re-spawn.
  const configPath = path.join(subchatFolder, CONFIG_FILENAME);
  const promptPath = path.join(subchatFolder, SYSTEM_PROMPT_FILENAME);

  await atomicWriteText(configPath, emitYaml(merged));
  await atomicWriteText(promptPath, body.endsWith("\n") ? body : body + "\n");

  return { subchatFolder, created, ignoredProfileKeys: ignoredKeys };
}
