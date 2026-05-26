// Integration tests for the spawn_subchat MCP tool plus unit tests for the
// supporting modules. Mirrors the layout of test/session_aware.test.ts.

import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ALL_TOOLS } from "../src/tools.js";
import { buildObjIndex } from "../src/obj_index.js";
import { type ContextPaths } from "../src/context.js";
import {
  DEFAULT_CONFIG,
  emitYaml,
  mergeConfig,
  parseMinimalYaml,
  parseProfile,
  assertValidSubagentName,
  type ConfigValue,
} from "../src/subchat_spawn.js";
import { ensureSubchatLine } from "../src/state_md.js";

const INITIAL_STATE_MD = `# Session state

## Input

*пусто*

## Scope

- OBJ001 [work]

## Problems

- P1 TestProblem (open)
  - P1.I01 ScopeBoundary (open) — is this one thing or two?
`;

const SAMPLE_PROFILE = `---
model: sonnet
output-format: stream-json
allowed-tools: [Read, Write, Edit, Bash, Glob, Grep, WebFetch]
max-turns: 5
---

# Protocolist

You are the protocolist subagent. Curate the narrative in protocol.md.

## Discipline
- Run Bash non-interactively.
- Always emit progress events.
`;

async function makeContextWithSession(): Promise<{
  ctx: ContextPaths;
  sessionId: string;
  sessionFolder: string;
  contextRoot: string;
  cleanup: () => Promise<void>;
}> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "cockpit-spawn-test-"));
  await fs.writeFile(
    path.join(root, "context.json"),
    JSON.stringify({ name: "test" }) + "\n",
  );
  await fs.mkdir(path.join(root, "objectives"), { recursive: true });
  await fs.mkdir(path.join(root, ".claude", "sessions"), { recursive: true });
  await fs.mkdir(path.join(root, ".claude", "cockpit", "subagents"), {
    recursive: true,
  });

  const sessionId = "test-sid";
  const sessionFolder = path.join(
    root,
    "journal",
    "2026",
    "05",
    "21",
    "1400_session",
  );
  await fs.mkdir(path.join(sessionFolder, "transcript"), { recursive: true });
  await fs.writeFile(path.join(sessionFolder, "state.md"), INITIAL_STATE_MD);
  await fs.writeFile(
    path.join(root, ".claude", "sessions", `${sessionId}.json`),
    JSON.stringify(
      { session_folder: sessionFolder, first_prompt_id: "p1" },
      null,
      2,
    ) + "\n",
  );

  return {
    ctx: {
      root,
      objectivesDir: path.join(root, "objectives"),
      sessionsDir: path.join(root, ".claude", "sessions"),
    },
    sessionId,
    sessionFolder,
    contextRoot: root,
    cleanup: async () => fs.rm(root, { recursive: true, force: true }),
  };
}

async function writeProfile(
  contextRoot: string,
  name: string,
  content: string,
): Promise<string> {
  const p = path.join(
    contextRoot,
    ".claude",
    "cockpit",
    "subagents",
    `${name}.md`,
  );
  await fs.writeFile(p, content);
  return p;
}

type LooseHandler = (
  args: Record<string, unknown>,
  deps: { ctx: ContextPaths; index: Awaited<ReturnType<typeof buildObjIndex>> },
) => Promise<unknown>;

function tool(name: string): { handler: LooseHandler } {
  const t = ALL_TOOLS.find((x) => x.name === name);
  if (!t) throw new Error(`tool not found in ALL_TOOLS: ${name}`);
  return { handler: t.handler as unknown as LooseHandler };
}

// ---------------------------------------------------------------------------
// YAML parse / emit unit tests
// ---------------------------------------------------------------------------

test("parseMinimalYaml: scalars, inline list, inline map, null, comments", () => {
  const text = [
    "model: sonnet",
    "max-turns: 5",
    "bare: false",
    "permission-mode: null",
    "allowed-tools: [Read, Write, Edit]",
    "env: {FOO: bar, BAZ: \"qux\"}",
    "# a comment",
    "",
    "  ",
  ].join("\n");
  const parsed = parseMinimalYaml(text);
  assert.equal(parsed["model"], "sonnet");
  assert.equal(parsed["max-turns"], 5);
  assert.equal(parsed["bare"], false);
  assert.equal(parsed["permission-mode"], null);
  assert.deepEqual(parsed["allowed-tools"], ["Read", "Write", "Edit"]);
  assert.deepEqual(parsed["env"], { FOO: "bar", BAZ: "qux" });
});

test("parseMinimalYaml: rejects indented (block-style) lines", () => {
  assert.throws(
    () => parseMinimalYaml("foo:\n  bar: baz"),
    /unexpected indentation/,
  );
});

test("emitYaml: roundtrips DEFAULT_CONFIG through parseMinimalYaml", () => {
  const text = emitYaml(DEFAULT_CONFIG);
  const parsed = parseMinimalYaml(text);
  for (const [k, v] of DEFAULT_CONFIG) {
    assert.deepEqual(parsed[k], v, `key ${k}`);
  }
});

test("emitYaml: emits one key per line in DEFAULT_CONFIG order", () => {
  const text = emitYaml(DEFAULT_CONFIG);
  const lines = text.trimEnd().split("\n");
  assert.equal(lines.length, DEFAULT_CONFIG.length);
  for (let i = 0; i < DEFAULT_CONFIG.length; i++) {
    assert.ok(
      lines[i]!.startsWith(`${DEFAULT_CONFIG[i]![0]}:`),
      `line ${i}: ${lines[i]} did not start with ${DEFAULT_CONFIG[i]![0]}:`,
    );
  }
});

test("emitYaml: special-character strings are quoted", () => {
  const text = emitYaml([
    ["a", "plain"],
    ["b", "has: colon"],
    ["c", "true"],
    ["d", "123"],
    ["e", ""],
    ["f", "with #hash"],
  ] as Array<[string, ConfigValue]>);
  const parsed = parseMinimalYaml(text);
  assert.equal(parsed["a"], "plain");
  assert.equal(parsed["b"], "has: colon");
  assert.equal(parsed["c"], "true"); // string "true", not boolean
  assert.equal(parsed["d"], "123"); // string "123", not integer
  assert.equal(parsed["e"], "");
  assert.equal(parsed["f"], "with #hash");
});

test("emitYaml: rejects string containing backslash, quote, or newline", () => {
  // The parser (stripQuotes) does not perform inverse unescape, so the emitter
  // must reject strings whose round-trip would silently corrupt. See
  // adversarial.md Finding 1.
  assert.throws(
    () =>
      emitYaml([["k", 'has "quote"']] as Array<[string, ConfigValue]>),
    /backslash\/quote\/newline/,
  );
  assert.throws(
    () =>
      emitYaml([["k", "has \\ backslash"]] as Array<[string, ConfigValue]>),
    /backslash\/quote\/newline/,
  );
  assert.throws(
    () =>
      emitYaml([["k", "has \n newline"]] as Array<[string, ConfigValue]>),
    /backslash\/quote\/newline/,
  );
  assert.throws(
    () =>
      emitYaml([["k", "has \r carriage"]] as Array<[string, ConfigValue]>),
    /backslash\/quote\/newline/,
  );
});

test("emitYaml: DEFAULT_CONFIG does not throw", () => {
  // Sanity check: no DEFAULT_CONFIG value contains a forbidden character.
  assert.doesNotThrow(() => emitYaml(DEFAULT_CONFIG));
});

// ---------------------------------------------------------------------------
// Profile parsing
// ---------------------------------------------------------------------------

test("parseProfile: extracts frontmatter and body", () => {
  const p = parseProfile(SAMPLE_PROFILE);
  assert.equal(p.frontmatter["model"], "sonnet");
  assert.equal(p.frontmatter["output-format"], "stream-json");
  assert.equal(p.frontmatter["max-turns"], 5);
  assert.deepEqual(p.frontmatter["allowed-tools"], [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "WebFetch",
  ]);
  assert.match(p.body, /^# Protocolist/);
  assert.match(p.body, /Curate the narrative/);
});

test("parseProfile: rejects profile without frontmatter delimiters", () => {
  assert.throws(
    () => parseProfile("# No frontmatter here\nbody"),
    /must start with a YAML frontmatter delimiter/,
  );
});

test("parseProfile: rejects unterminated frontmatter", () => {
  assert.throws(
    () => parseProfile("---\nmodel: sonnet\nbody without closing"),
    /not closed/,
  );
});

test("parseProfile: handles CRLF line endings", () => {
  const p = parseProfile("---\r\nmodel: sonnet\r\n---\r\nbody\r\n");
  assert.equal(p.frontmatter["model"], "sonnet");
  assert.match(p.body, /body/);
});

test("parseProfile: handles empty body", () => {
  const p = parseProfile("---\nmodel: sonnet\n---\n");
  assert.equal(p.body, "");
});

// ---------------------------------------------------------------------------
// mergeConfig
// ---------------------------------------------------------------------------

test("mergeConfig: frontmatter overrides defaults; unknown keys reported", () => {
  const { merged, ignoredKeys } = mergeConfig({
    "model": "opus",
    "max-turns": 10,
    "custom-key": "ignored",
  });
  const asMap = new Map(merged);
  assert.equal(asMap.get("model"), "opus");
  assert.equal(asMap.get("max-turns"), 10);
  // unchanged defaults
  assert.equal(asMap.get("cwd"), "../..");
  assert.deepEqual(ignoredKeys, ["custom-key"]);
});

test("mergeConfig: preserves DEFAULT_CONFIG order in merged output", () => {
  const { merged } = mergeConfig({});
  assert.deepEqual(
    merged.map((e) => e[0]),
    DEFAULT_CONFIG.map((e) => e[0]),
  );
});

// ---------------------------------------------------------------------------
// assertValidSubagentName
// ---------------------------------------------------------------------------

test("assertValidSubagentName: accepts protocolist, kebab-case, snake_case", () => {
  assertValidSubagentName("protocolist");
  assertValidSubagentName("code-reviewer");
  assertValidSubagentName("snake_case");
  assertValidSubagentName("Mixed123");
});

test("assertValidSubagentName: rejects empty, path separators, dots, leading hyphen", () => {
  assert.throws(() => assertValidSubagentName(""), /empty/);
  assert.throws(() => assertValidSubagentName("../escape"), /invalid subagent name/);
  assert.throws(() => assertValidSubagentName("a/b"), /invalid subagent name/);
  assert.throws(() => assertValidSubagentName(".hidden"), /invalid subagent name/);
  assert.throws(() => assertValidSubagentName("-leading"), /invalid subagent name/);
  assert.throws(() => assertValidSubagentName("with space"), /invalid subagent name/);
});

// ---------------------------------------------------------------------------
// ensureSubchatLine — state.md merge-preserving
// ---------------------------------------------------------------------------

test("ensureSubchatLine: creates ## Subchats section when absent", () => {
  const next = ensureSubchatLine(INITIAL_STATE_MD, "protocolist");
  assert.match(next, /^## Subchats$/m);
  assert.match(next, /^- protocolist \(active\)$/m);
  // other sections preserved verbatim
  assert.match(next, /^## Input$/m);
  assert.match(next, /^## Scope$/m);
  assert.match(next, /^## Problems$/m);
  assert.match(next, /^- OBJ001 \[work\]$/m);
  assert.match(next, /^- P1 TestProblem \(open\)$/m);
  assert.match(next, /^ {2}- P1\.I01 ScopeBoundary \(open\)/m);
});

test("ensureSubchatLine: idempotent when line already active", () => {
  const once = ensureSubchatLine(INITIAL_STATE_MD, "protocolist");
  const twice = ensureSubchatLine(once, "protocolist");
  assert.equal(twice, once);
});

test("ensureSubchatLine: re-spawn reactivates a terminated entry", () => {
  const withTerminated = INITIAL_STATE_MD + "\n## Subchats\n- protocolist (terminated)\n";
  const reactivated = ensureSubchatLine(withTerminated, "protocolist");
  assert.match(reactivated, /^- protocolist \(active\)$/m);
  assert.doesNotMatch(reactivated, /protocolist \(terminated\)/);
});

test("ensureSubchatLine: appends a second subagent without disturbing the first", () => {
  const first = ensureSubchatLine(INITIAL_STATE_MD, "protocolist");
  const second = ensureSubchatLine(first, "advisor");
  assert.match(second, /^- protocolist \(active\)$/m);
  assert.match(second, /^- advisor \(active\)$/m);
});

test("ensureSubchatLine: leaves Problems/Scope sections byte-for-byte intact", () => {
  const next = ensureSubchatLine(INITIAL_STATE_MD, "protocolist");
  // Everything before the newly-appended ## Subchats heading must match the
  // original content modulo a normalized trailing-newline run.
  const idx = next.indexOf("## Subchats");
  assert.ok(idx > 0);
  const head = next.slice(0, idx).replace(/\n+$/, "");
  const origNormalized = INITIAL_STATE_MD.replace(/\n+$/, "");
  assert.equal(head, origNormalized);
});

// ---------------------------------------------------------------------------
// spawn_subchat tool — happy path
// ---------------------------------------------------------------------------

test("spawn_subchat: happy path creates folder + config + prompt + state.md entry", async () => {
  const { ctx, sessionId, sessionFolder, contextRoot, cleanup } =
    await makeContextWithSession();
  try {
    await writeProfile(contextRoot, "protocolist", SAMPLE_PROFILE);
    const index = await buildObjIndex(ctx);

    const result = (await tool("spawn_subchat").handler(
      { session_id: sessionId, subagent: "protocolist" },
      { ctx, index },
    )) as {
      subagent: string;
      subchat_folder: string;
      created: boolean;
      ignored_profile_keys: string[];
    };

    assert.equal(result.subagent, "protocolist");
    assert.equal(result.created, true);
    assert.equal(
      result.subchat_folder,
      path.join(sessionFolder, "subchats", "protocolist"),
    );
    assert.deepEqual(result.ignored_profile_keys, []);

    // config.yaml exists and contains the override values
    const configText = await fs.readFile(
      path.join(result.subchat_folder, "config.yaml"),
      "utf8",
    );
    assert.match(configText, /^model: sonnet$/m);
    assert.match(configText, /^max-turns: 5$/m);
    assert.match(configText, /^output-format: stream-json$/m);
    assert.match(
      configText,
      /^allowed-tools: \[Read, Write, Edit, Bash, Glob, Grep, WebFetch\]$/m,
    );
    // defaults preserved
    assert.match(configText, /^cwd: "\.\.\/\.\."$/m);
    assert.match(configText, /^bare: false$/m);

    // system_prompt.md exists and equals profile body
    const promptText = await fs.readFile(
      path.join(result.subchat_folder, "system_prompt.md"),
      "utf8",
    );
    assert.match(promptText, /^# Protocolist/);
    assert.match(promptText, /Curate the narrative/);

    // state.md updated with active entry
    const stateMd = await fs.readFile(
      path.join(sessionFolder, "state.md"),
      "utf8",
    );
    assert.match(stateMd, /^## Subchats$/m);
    assert.match(stateMd, /^- protocolist \(active\)$/m);
    // pre-existing sections still there
    assert.match(stateMd, /^## Problems$/m);
    assert.match(stateMd, /^- P1 TestProblem \(open\)$/m);
  } finally {
    await cleanup();
  }
});

// ---------------------------------------------------------------------------
// spawn_subchat tool — idempotent re-spawn
// ---------------------------------------------------------------------------

test("spawn_subchat: re-spawn regenerates config + prompt; leaves session.json and log/ intact", async () => {
  const { ctx, sessionId, sessionFolder, contextRoot, cleanup } =
    await makeContextWithSession();
  try {
    await writeProfile(contextRoot, "protocolist", SAMPLE_PROFILE);
    const index = await buildObjIndex(ctx);

    // First spawn
    const r1 = (await tool("spawn_subchat").handler(
      { session_id: sessionId, subagent: "protocolist" },
      { ctx, index },
    )) as { subchat_folder: string; created: boolean };
    assert.equal(r1.created, true);

    // Simulate a prior subchat run: drop session.json and a log/01/ folder.
    const sessionJson = path.join(r1.subchat_folder, "session.json");
    await fs.writeFile(
      sessionJson,
      JSON.stringify(
        { session_id: "abc-123", created: "2026-05-24T10:00:00Z" },
        null,
        2,
      ),
    );
    const logRunDir = path.join(r1.subchat_folder, "log", "01");
    await fs.mkdir(logRunDir, { recursive: true });
    await fs.writeFile(path.join(logRunDir, "prompt.md"), "first message");

    // Modify the profile and re-spawn — config.yaml + system_prompt.md must
    // regenerate while session.json + log/ stay untouched.
    const modified = SAMPLE_PROFILE
      .replace("max-turns: 5", "max-turns: 9")
      .replace("# Protocolist", "# Protocolist v2");
    await writeProfile(contextRoot, "protocolist", modified);

    const r2 = (await tool("spawn_subchat").handler(
      { session_id: sessionId, subagent: "protocolist" },
      { ctx, index },
    )) as { subchat_folder: string; created: boolean };
    assert.equal(r2.created, false);
    assert.equal(r2.subchat_folder, r1.subchat_folder);

    // config.yaml regenerated
    const configText = await fs.readFile(
      path.join(r1.subchat_folder, "config.yaml"),
      "utf8",
    );
    assert.match(configText, /^max-turns: 9$/m);
    // system_prompt.md regenerated
    const promptText = await fs.readFile(
      path.join(r1.subchat_folder, "system_prompt.md"),
      "utf8",
    );
    assert.match(promptText, /^# Protocolist v2/);

    // session.json + log/ unchanged
    const sessionJsonText = await fs.readFile(sessionJson, "utf8");
    assert.match(sessionJsonText, /"session_id": "abc-123"/);
    const logPrompt = await fs.readFile(
      path.join(logRunDir, "prompt.md"),
      "utf8",
    );
    assert.equal(logPrompt, "first message");

    // state.md still has exactly one active entry (no duplication)
    const stateMd = await fs.readFile(
      path.join(sessionFolder, "state.md"),
      "utf8",
    );
    const matches = stateMd.match(/^- protocolist \(active\)$/gm) ?? [];
    assert.equal(matches.length, 1);
  } finally {
    await cleanup();
  }
});

// ---------------------------------------------------------------------------
// spawn_subchat tool — errors
// ---------------------------------------------------------------------------

test("spawn_subchat: missing profile returns a recoverable error pointing at deploy.md", async () => {
  const { ctx, sessionId, cleanup } = await makeContextWithSession();
  try {
    const index = await buildObjIndex(ctx);
    await assert.rejects(
      () =>
        tool("spawn_subchat").handler(
          { session_id: sessionId, subagent: "protocolist" },
          { ctx, index },
        ),
      (err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        return (
          /subagent profile not found/.test(msg) &&
          /\.claude\/cockpit\/subagents\/protocolist\.md/.test(msg) &&
          /install\.py/.test(msg) &&
          /deploy\.md/.test(msg)
        );
      },
    );
  } finally {
    await cleanup();
  }
});

test("spawn_subchat: rejects invalid subagent name before touching filesystem", async () => {
  const { ctx, sessionId, sessionFolder, cleanup } =
    await makeContextWithSession();
  try {
    const index = await buildObjIndex(ctx);
    await assert.rejects(
      () =>
        tool("spawn_subchat").handler(
          { session_id: sessionId, subagent: "../escape" },
          { ctx, index },
        ),
      /invalid subagent name/,
    );
    // No subchats/ folder should have been created
    await assert.rejects(() =>
      fs.access(path.join(sessionFolder, "subchats")),
    );
  } finally {
    await cleanup();
  }
});

test("spawn_subchat: rejects a profile without frontmatter", async () => {
  const { ctx, sessionId, contextRoot, cleanup } = await makeContextWithSession();
  try {
    await writeProfile(contextRoot, "broken", "# no frontmatter\nbody only\n");
    const index = await buildObjIndex(ctx);
    await assert.rejects(
      () =>
        tool("spawn_subchat").handler(
          { session_id: sessionId, subagent: "broken" },
          { ctx, index },
        ),
      /YAML frontmatter delimiter/,
    );
  } finally {
    await cleanup();
  }
});

// ---------------------------------------------------------------------------
// spawn_subchat tool — merge-preserving state.md
// ---------------------------------------------------------------------------

test("spawn_subchat: preserves all other state.md sections verbatim", async () => {
  const { ctx, sessionId, sessionFolder, contextRoot, cleanup } =
    await makeContextWithSession();
  try {
    await writeProfile(contextRoot, "protocolist", SAMPLE_PROFILE);
    const index = await buildObjIndex(ctx);

    const before = await fs.readFile(
      path.join(sessionFolder, "state.md"),
      "utf8",
    );
    await tool("spawn_subchat").handler(
      { session_id: sessionId, subagent: "protocolist" },
      { ctx, index },
    );
    const after = await fs.readFile(
      path.join(sessionFolder, "state.md"),
      "utf8",
    );

    // The Input/Scope/Problems section content must remain byte-identical
    // up to where the new ## Subchats heading is appended.
    const subchatsIdx = after.indexOf("## Subchats");
    assert.ok(subchatsIdx > 0);
    const head = after.slice(0, subchatsIdx).replace(/\n+$/, "");
    const origNormalized = before.replace(/\n+$/, "");
    assert.equal(head, origNormalized);
  } finally {
    await cleanup();
  }
});

// ---------------------------------------------------------------------------
// spawn_subchat tool — appears in ALL_TOOLS with session_id declared
// ---------------------------------------------------------------------------

test("spawn_subchat is registered and declares session_id", () => {
  const t = ALL_TOOLS.find((x) => x.name === "spawn_subchat");
  assert.ok(t, "spawn_subchat not registered in ALL_TOOLS");
  const shape = t!.shape as Record<string, unknown>;
  assert.ok("session_id" in shape);
  assert.ok("subagent" in shape);
});
