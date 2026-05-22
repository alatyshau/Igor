// Integration tests for all 6 MCP tools, exercised directly against a tmpdir context.

import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ALL_TOOLS } from "../src/tools.js";
import { buildObjIndex } from "../src/obj_index.js";
import type { ContextPaths } from "../src/context.js";

async function makeContext(): Promise<{ ctx: ContextPaths; cleanup: () => Promise<void> }> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "cockpit-test-"));
  await fs.writeFile(path.join(root, "context.json"), JSON.stringify({ name: "test" }) + "\n");
  await fs.mkdir(path.join(root, "objectives"), { recursive: true });
  await fs.mkdir(path.join(root, ".claude", "sessions"), { recursive: true });
  return {
    ctx: {
      root,
      objectivesDir: path.join(root, "objectives"),
      sessionsDir: path.join(root, ".claude", "sessions"),
    },
    cleanup: async () => fs.rm(root, { recursive: true, force: true }),
  };
}

// The tools' handlers each expect a different shape; for tests we use a uniform
// loose signature and rely on runtime validation inside each handler.
type LooseHandler = (
  args: Record<string, unknown>,
  deps: { ctx: ContextPaths; index: Awaited<ReturnType<typeof buildObjIndex>> },
) => Promise<unknown>;

function tool(name: string): { handler: LooseHandler } {
  const t = ALL_TOOLS.find((x) => x.name === name);
  if (!t) throw new Error(`tool not found in ALL_TOOLS: ${name}`);
  return { handler: t.handler as unknown as LooseHandler };
}

test("objective_create: allocates code, creates folder + index.md", async () => {
  const { ctx, cleanup } = await makeContext();
  try {
    const index = await buildObjIndex(ctx);
    const result = (await tool("objective_create").handler(
      {
        slug: "FirstObj",
        goal: "First test goal",
        outputs: ["Do thing A.", "Do thing B."],
        why: "Because reasons.",
        blocked_by: [],
      },
      { ctx, index },
    )) as { code: string; folder_path: string; index_path: string };

    assert.equal(result.code, "OBJ000");
    assert.match(result.folder_path, /OBJ000_FirstObj$/);

    const indexMd = await fs.readFile(result.index_path, "utf8");
    assert.match(indexMd, /^# OBJ000 FirstObj$/m);
    assert.match(indexMd, /^\*\*State:\*\* open$/m);
    assert.match(indexMd, /^\*\*Цель:\*\* First test goal$/m);
    assert.match(indexMd, /^- Do thing A\.$/m);
    assert.match(indexMd, /^## WHY$/m);
    assert.match(indexMd, /^Because reasons\.$/m);
  } finally {
    await cleanup();
  }
});

test("objective_create: rejects duplicate slug", async () => {
  const { ctx, cleanup } = await makeContext();
  try {
    const index = await buildObjIndex(ctx);
    await tool("objective_create").handler(
      { slug: "Dup", goal: "g", outputs: [], why: "", blocked_by: [] },
      { ctx, index },
    );
    await assert.rejects(
      () =>
        tool("objective_create").handler(
          { slug: "Dup", goal: "g2", outputs: [], why: "", blocked_by: [] },
          { ctx, index },
        ),
      /already in use/,
    );
  } finally {
    await cleanup();
  }
});

test("objective_create: rejects invalid slug", async () => {
  const { ctx, cleanup } = await makeContext();
  try {
    const index = await buildObjIndex(ctx);
    await assert.rejects(
      () =>
        tool("objective_create").handler(
          { slug: "with-dash", goal: "g", outputs: [], why: "", blocked_by: [] },
          { ctx, index },
        ),
      /invalid slug/,
    );
  } finally {
    await cleanup();
  }
});

test("objective_set_state: moves folder + updates State line; cascade on canceled", async () => {
  const { ctx, cleanup } = await makeContext();
  try {
    const index = await buildObjIndex(ctx);
    const created = (await tool("objective_create").handler(
      { slug: "ToClose", goal: "g", outputs: [], why: "", blocked_by: [] },
      { ctx, index },
    )) as { code: string };

    // add a sub-entity to verify cascade
    await tool("sub_entity_create").handler(
      { parent: created.code, type: "I", slug: "ChildIssue", description: "", state: "open" },
      { ctx, index },
    );

    const result = (await tool("objective_set_state").handler(
      { code: created.code, new_state: "canceled" },
      { ctx, index },
    )) as { folder_path: string; cascaded_sub_entities: string[] };

    assert.match(result.folder_path, /cancelled\/OBJ000_ToClose$/);
    assert.deepEqual(result.cascaded_sub_entities, ["I01"]);

    const content = await fs.readFile(path.join(result.folder_path, "index.md"), "utf8");
    assert.match(content, /^\*\*State:\*\* canceled$/m);
    assert.match(content, /\[I01 canceled\]/);
  } finally {
    await cleanup();
  }
});

test("objective_set_blocked_by: applies + detects cycle", async () => {
  const { ctx, cleanup } = await makeContext();
  try {
    const index = await buildObjIndex(ctx);
    const create = (slug: string) =>
      tool("objective_create").handler(
        { slug, goal: "g", outputs: [], why: "", blocked_by: [] },
        { ctx, index },
      ) as Promise<{ code: string }>;

    const a = await create("A");
    const b = await create("B");

    await tool("objective_set_blocked_by").handler(
      { code: a.code, blocked_by: [b.code] },
      { ctx, index },
    );

    const aContent = await fs.readFile(path.join(ctx.objectivesDir, `${a.code}_A`, "index.md"), "utf8");
    assert.match(aContent, new RegExp(`\\*\\*Blocked by:\\*\\* ${b.code}`));

    // Setting B -> A should be rejected as a cycle (A is already blocked by B).
    await assert.rejects(
      () =>
        tool("objective_set_blocked_by").handler(
          { code: b.code, blocked_by: [a.code] },
          { ctx, index },
        ),
      /cycle/,
    );
  } finally {
    await cleanup();
  }
});

test("sub_entity_create + sub_entity_set_state on OBJ parent", async () => {
  const { ctx, cleanup } = await makeContext();
  try {
    const index = await buildObjIndex(ctx);
    const obj = (await tool("objective_create").handler(
      { slug: "Parent", goal: "g", outputs: [], why: "", blocked_by: [] },
      { ctx, index },
    )) as { code: string };

    const sub = (await tool("sub_entity_create").handler(
      { parent: obj.code, type: "T", slug: "DoStuff", description: "Do the thing.", state: "open" },
      { ctx, index },
    )) as { code: string };

    assert.equal(sub.code, "T01");

    await tool("sub_entity_set_state").handler(
      { parent: obj.code, code: "T01", new_state: "closed" },
      { ctx, index },
    );

    const content = await fs.readFile(
      path.join(ctx.objectivesDir, `${obj.code}_Parent`, "index.md"),
      "utf8",
    );
    assert.match(content, /\[T01 closed\] DoStuff — Do the thing\./);
  } finally {
    await cleanup();
  }
});

test("sub_entity_set_state: rejects invalid state for type", async () => {
  const { ctx, cleanup } = await makeContext();
  try {
    const index = await buildObjIndex(ctx);
    const obj = (await tool("objective_create").handler(
      { slug: "Validate", goal: "g", outputs: [], why: "", blocked_by: [] },
      { ctx, index },
    )) as { code: string };

    await tool("sub_entity_create").handler(
      { parent: obj.code, type: "I", slug: "AnIssue", description: "", state: "open" },
      { ctx, index },
    );

    await assert.rejects(
      () =>
        tool("sub_entity_set_state").handler(
          { parent: obj.code, code: "I01", new_state: "confirmed" },
          { ctx, index },
        ),
      /invalid state confirmed for type I/,
    );
  } finally {
    await cleanup();
  }
});
