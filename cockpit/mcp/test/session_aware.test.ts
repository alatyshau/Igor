// Integration tests for session-aware MCP tools.
//
// These tests cover:
// - ticket_create / ticket_set_state on Problem parents (require session_id)
// - rename_current_session (basic rename + multi-alias resume-case update)
// - loadSessionStateFile error path when SessionStateFile is absent
// - session_id field is declared on every tool's Zod shape (uniformity)

import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ALL_TOOLS } from "../src/tools.js";
import { buildObjIndex } from "../src/obj_index.js";
import { loadSessionStateFile, type ContextPaths } from "../src/context.js";

const INITIAL_STATE_MD = `# Session state

## Input

*пусто*

## Scope

*пусто*

## Problems

- P1 TestProblem (open)
`;

async function makeContextWithSession(): Promise<{
  ctx: ContextPaths;
  sessionId: string;
  sessionFolder: string;
  cleanup: () => Promise<void>;
}> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "cockpit-session-test-"));
  await fs.writeFile(
    path.join(root, "context.json"),
    JSON.stringify({ name: "test" }) + "\n",
  );
  await fs.mkdir(path.join(root, "objectives"), { recursive: true });
  await fs.mkdir(path.join(root, ".claude", "sessions"), { recursive: true });

  const sessionId = "test-sid";
  const sessionFolder = path.join(root, "journal", "2026", "05", "21", "1400_session");
  await fs.mkdir(path.join(sessionFolder, "transcript"), { recursive: true });
  await fs.writeFile(path.join(sessionFolder, "state.md"), INITIAL_STATE_MD);
  await fs.writeFile(
    path.join(sessionFolder, "transcript", "index.json"),
    JSON.stringify({ next_index: 0, turns: [], schema_version: 1 }),
  );
  await fs.writeFile(
    path.join(root, ".claude", "sessions", `${sessionId}.json`),
    JSON.stringify({ session_folder: sessionFolder, first_prompt_id: "p1" }, null, 2) + "\n",
  );

  return {
    ctx: {
      root,
      objectivesDir: path.join(root, "objectives"),
      sessionsDir: path.join(root, ".claude", "sessions"),
    },
    sessionId,
    sessionFolder,
    cleanup: async () => fs.rm(root, { recursive: true, force: true }),
  };
}

// Loose handler shape — same pattern as tools.integration.test.ts.
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
// Schema uniformity
// ---------------------------------------------------------------------------

test("every tool declares session_id in its shape", () => {
  for (const t of ALL_TOOLS) {
    const shape = t.shape as Record<string, unknown>;
    assert.ok(
      "session_id" in shape,
      `tool ${t.name} is missing the session_id field — see cockpit/specs/mcp.md`,
    );
  }
});

// ---------------------------------------------------------------------------
// Problem-parented ticket flow
// ---------------------------------------------------------------------------

test("ticket_create + ticket_set_state on Problem parent uses session_id", async () => {
  const { ctx, sessionId, sessionFolder, cleanup } = await makeContextWithSession();
  try {
    const index = await buildObjIndex(ctx);

    const created = (await tool("ticket_create").handler(
      {
        session_id: sessionId,
        parent: "P1",
        type: "I",
        slug: "ScopeBoundary",
        description: "is this one thing or two?",
        state: "open",
      },
      { ctx, index },
    )) as { parent: string; code: string; full_code: string };

    assert.equal(created.parent, "P1");
    assert.equal(created.code, "I01");
    assert.equal(created.full_code, "P1.I01");

    const stateMdAfterCreate = await fs.readFile(
      path.join(sessionFolder, "state.md"),
      "utf8",
    );
    assert.match(stateMdAfterCreate, /P1\.I01 ScopeBoundary \(open\)/);

    const transitioned = (await tool("ticket_set_state").handler(
      {
        session_id: sessionId,
        parent: "P1",
        code: "I01",
        new_state: "closed",
      },
      { ctx, index },
    )) as { parent: string; code: string; new_state: string };

    assert.equal(transitioned.new_state, "closed");
    const stateMdAfterTransition = await fs.readFile(
      path.join(sessionFolder, "state.md"),
      "utf8",
    );
    assert.match(stateMdAfterTransition, /P1\.I01 ScopeBoundary \(closed\)/);
  } finally {
    await cleanup();
  }
});

test("ticket_create on Problem parent errors when SessionStateFile is missing", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "cockpit-no-session-"));
  try {
    await fs.writeFile(
      path.join(root, "context.json"),
      JSON.stringify({ name: "test" }) + "\n",
    );
    await fs.mkdir(path.join(root, "objectives"), { recursive: true });
    await fs.mkdir(path.join(root, ".claude", "sessions"), { recursive: true });
    const ctx: ContextPaths = {
      root,
      objectivesDir: path.join(root, "objectives"),
      sessionsDir: path.join(root, ".claude", "sessions"),
    };
    const index = await buildObjIndex(ctx);

    await assert.rejects(
      () =>
        tool("ticket_create").handler(
          {
            session_id: "nonexistent-sid",
            parent: "P1",
            type: "I",
            slug: "X",
            state: "open",
          },
          { ctx, index },
        ),
      /SessionStateFile not found/,
    );
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// rename_current_session — basic + multi-alias
// ---------------------------------------------------------------------------

test("rename_current_session moves folder and updates SessionStateFile", async () => {
  const { ctx, sessionId, sessionFolder, cleanup } = await makeContextWithSession();
  try {
    const index = await buildObjIndex(ctx);
    const result = (await tool("rename_current_session").handler(
      { session_id: sessionId, new_slug: "RenamedSlug" },
      { ctx, index },
    )) as { session_folder: string; previous: string; aliases_updated: number };

    const expectedNew = path.join(path.dirname(sessionFolder), "1400_RenamedSlug");
    assert.equal(result.session_folder, expectedNew);
    assert.equal(result.previous, sessionFolder);
    assert.equal(result.aliases_updated, 1);

    // Old folder gone, new folder exists with state.md
    await assert.rejects(() => fs.access(sessionFolder));
    await fs.access(path.join(expectedNew, "state.md"));

    // SessionStateFile points at the new folder
    const stateFile = await loadSessionStateFile(ctx, sessionId);
    assert.equal(stateFile.session_folder, expectedNew);
  } finally {
    await cleanup();
  }
});

test("rename_current_session updates every resumed-chat alias atomically", async () => {
  const { ctx, sessionId, sessionFolder, cleanup } = await makeContextWithSession();
  try {
    // Simulate a resumed chat: a second SessionStateFile pointing at the
    // same SessionFolder under a different session_id.
    const resumedSid = "resumed-sid-2";
    await fs.writeFile(
      path.join(ctx.sessionsDir, `${resumedSid}.json`),
      JSON.stringify(
        { session_folder: sessionFolder, first_prompt_id: "p1" },
        null,
        2,
      ) + "\n",
    );

    const index = await buildObjIndex(ctx);
    const result = (await tool("rename_current_session").handler(
      { session_id: sessionId, new_slug: "MultiAliasRename" },
      { ctx, index },
    )) as { session_folder: string; aliases_updated: number };

    assert.equal(result.aliases_updated, 2);

    // Both alias files now point at the new folder
    const original = await loadSessionStateFile(ctx, sessionId);
    const resumed = await loadSessionStateFile(ctx, resumedSid);
    assert.equal(original.session_folder, result.session_folder);
    assert.equal(resumed.session_folder, result.session_folder);
  } finally {
    await cleanup();
  }
});

// ---------------------------------------------------------------------------
// context.ts — explicit error when SessionStateFile is missing
// ---------------------------------------------------------------------------

test("loadSessionStateFile errors clearly when SessionStateFile is missing", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "cockpit-load-test-"));
  try {
    await fs.mkdir(path.join(root, ".claude", "sessions"), { recursive: true });
    const ctx: ContextPaths = {
      root,
      objectivesDir: path.join(root, "objectives"),
      sessionsDir: path.join(root, ".claude", "sessions"),
    };
    await assert.rejects(
      () => loadSessionStateFile(ctx, "absent-sid"),
      /SessionStateFile not found.*no hook has bootstrapped/,
    );
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("loadSessionStateFile errors when session_id is empty", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "cockpit-empty-sid-"));
  try {
    const ctx: ContextPaths = {
      root,
      objectivesDir: path.join(root, "objectives"),
      sessionsDir: path.join(root, ".claude", "sessions"),
    };
    await assert.rejects(
      () => loadSessionStateFile(ctx, ""),
      /session_id is required/,
    );
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
