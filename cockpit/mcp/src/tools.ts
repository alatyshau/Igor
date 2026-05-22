// MCP tool definitions for the cockpit. See cockpit/specs/architecture.md §3.

import { promises as fs } from "node:fs";
import * as path from "node:path";
import { z, type ZodRawShape } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import {
  isValidObjectiveCode,
  parseSubEntityCode,
  formatSubEntityCode,
  nextSubEntityIndex,
  type SubEntityType,
} from "./code.js";
import { assertValidSlug } from "./slug.js";
import {
  isValidStateForType,
  objectiveSubdir,
  type ObjectiveState,
} from "./state.js";
import {
  buildObjIndex,
  nextObjectiveCode,
  wouldCreateCycle,
  type ObjIndex,
  type ObjMeta,
} from "./obj_index.js";
import {
  writeObjIndexMd,
  setObjectiveState,
  setBlockedBy,
  appendItem,
  setItemState,
  cascadeCancelOpenItems,
  parseItems,
} from "./obj_md.js";
import { atomicWriteText } from "./fs_atomic.js";
import {
  loadContext,
  loadSessionStateFile,
  currentSessionId,
  type ContextPaths,
} from "./context.js";
import {
  appendProblemSubItem,
  isValidProblemCode,
  nextProblemSubCode,
  readStateMd,
  setProblemSubItemState,
  writeStateMd,
} from "./state_md.js";

export interface ToolDeps {
  ctx: ContextPaths;
  index: ObjIndex;
}

interface ToolDef<S extends ZodRawShape> {
  name: string;
  description: string;
  shape: S;
  handler: (args: z.infer<z.ZodObject<S>>, deps: ToolDeps) => Promise<unknown>;
}

function makeTool<S extends ZodRawShape>(def: ToolDef<S>): ToolDef<S> {
  return def;
}

function ok(payload: unknown): CallToolResult {
  return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] };
}

function fail(message: string): CallToolResult {
  return { isError: true, content: [{ type: "text", text: message }] };
}

// ---- tools ----

const objectiveCreate = makeTool({
  name: "objective_create",
  description:
    "Allocate the next OBJxxx code and create its folder + index.md skeleton. " +
    "Returns the assigned code and folder path.",
  shape: {
    slug: z.string().describe("CamelCase or snake_case; no dashes or dots."),
    goal: z.string().describe("Цель — desired outcome, one paragraph."),
    outputs: z.array(z.string()).default([]).describe(
      "Выходы — verifiable deliverables, action + target each.",
    ),
    why: z.string().default("").describe("Motivation; one to a few paragraphs."),
    blocked_by: z.array(z.string()).default([]).describe(
      "OBJ codes that must reach a terminal state before this one can close.",
    ),
  },
  handler: async ({ slug, goal, outputs, why, blocked_by }, deps) => {
    assertValidSlug(slug);
    if (deps.index.bySlug.has(slug)) {
      throw new Error(`slug "${slug}" already in use by ${deps.index.bySlug.get(slug)}`);
    }
    for (const code of blocked_by) {
      if (!isValidObjectiveCode(code) || !deps.index.byCode.has(code)) {
        throw new Error(`blocked_by references unknown OBJ: ${code}`);
      }
    }

    const code = nextObjectiveCode(deps.index);
    const folderPath = path.join(deps.ctx.objectivesDir, `${code}_${slug}`);
    const indexPath = await writeObjIndexMd(folderPath, {
      code,
      slug,
      state: "open" as ObjectiveState,
      goal,
      outputs,
      why,
      blockedBy: blocked_by,
    });

    const meta: ObjMeta = {
      code,
      slug,
      state: "open",
      blockedBy: [...blocked_by],
      folderPath,
    };
    deps.index.byCode.set(code, meta);
    deps.index.bySlug.set(slug, code);

    return { code, folder_path: folderPath, index_path: indexPath };
  },
});

const objectiveSetState = makeTool({
  name: "objective_set_state",
  description:
    "Move an Objective between states. Relocates the folder to the matching subdir " +
    "(active / closed / cancelled / backlog). On `canceled`, cascades open sub-entities to `canceled`.",
  shape: {
    code: z.string(),
    new_state: z.enum(["draft", "open", "closed", "canceled", "backlog"]),
  },
  handler: async ({ code, new_state }, deps) => {
    const meta = deps.index.byCode.get(code);
    if (!meta) throw new Error(`unknown OBJ: ${code}`);

    const targetSub = objectiveSubdir(new_state);
    const folderName = path.basename(meta.folderPath);
    const targetParent = targetSub
      ? path.join(deps.ctx.objectivesDir, targetSub)
      : deps.ctx.objectivesDir;
    await fs.mkdir(targetParent, { recursive: true });
    const targetFolder = path.join(targetParent, folderName);

    const indexPath = path.join(meta.folderPath, "index.md");
    let content = await fs.readFile(indexPath, "utf8");
    content = setObjectiveState(content, new_state);
    const cascaded: string[] = [];
    if (new_state === "canceled") {
      const result = cascadeCancelOpenItems(content);
      content = result.content;
      cascaded.push(...result.changed);
    }
    await atomicWriteText(indexPath, content);

    if (targetFolder !== meta.folderPath) {
      await fs.rename(meta.folderPath, targetFolder);
      meta.folderPath = targetFolder;
    }
    meta.state = new_state;

    return {
      code,
      new_state,
      folder_path: targetFolder,
      cascaded_sub_entities: cascaded,
    };
  },
});

const objectiveSetBlockedBy = makeTool({
  name: "objective_set_blocked_by",
  description:
    "Replace the Blocked-by list on an Objective. Validates each referenced code exists and that no cycle would be introduced.",
  shape: {
    code: z.string(),
    blocked_by: z.array(z.string()),
  },
  handler: async ({ code, blocked_by }, deps) => {
    const meta = deps.index.byCode.get(code);
    if (!meta) throw new Error(`unknown OBJ: ${code}`);

    for (const dep of blocked_by) {
      if (!isValidObjectiveCode(dep) || !deps.index.byCode.has(dep)) {
        throw new Error(`blocked_by references unknown OBJ: ${dep}`);
      }
      if (dep === code) {
        throw new Error(`Objective cannot block itself: ${code}`);
      }
    }
    if (wouldCreateCycle(deps.index, code, blocked_by)) {
      throw new Error(`cycle detected: setting blocked_by would create a circular dependency`);
    }

    const indexPath = path.join(meta.folderPath, "index.md");
    let content = await fs.readFile(indexPath, "utf8");
    content = setBlockedBy(content, blocked_by);
    await atomicWriteText(indexPath, content);

    meta.blockedBy = [...blocked_by];

    return { code, blocked_by };
  },
});

const subEntityCreate = makeTool({
  name: "sub_entity_create",
  description:
    "Create a sub-entity (Issue I / Suggestion S / Task T) under an Objective or a Problem. " +
    "For Objective parents (`OBJxxx`), appends to the OBJ's `## Items` in its `index.md`. " +
    "For Problem parents (`P1..P9`), appends a nested bullet under that Problem in the session's `state.md`.",
  shape: {
    parent: z.string().describe("OBJxxx code or P1..P9 Problem code"),
    type: z.enum(["I", "S", "T"]),
    slug: z.string(),
    description: z.string().default(""),
    state: z.string().default("open"),
  },
  handler: async ({ parent, type, slug, description, state }, deps) => {
    const t = type as SubEntityType;
    assertValidSlug(slug);
    if (!isValidStateForType(t, state)) {
      throw new Error(`invalid initial state ${state} for type ${t}`);
    }

    if (isValidObjectiveCode(parent)) {
      const meta = deps.index.byCode.get(parent);
      if (!meta) throw new Error(`unknown OBJ: ${parent}`);
      const indexPath = path.join(meta.folderPath, "index.md");
      let content = await fs.readFile(indexPath, "utf8");
      const existingCodes = parseItems(content).map((it) => it.code);
      const idx = nextSubEntityIndex(existingCodes, t);
      const code = formatSubEntityCode(t, idx);
      content = appendItem(content, { code, state, slug, text: description });
      await atomicWriteText(indexPath, content);
      return { parent, code, full_code: `${parent}.${code}` };
    }

    if (isValidProblemCode(parent)) {
      const sessionFile = await loadSessionStateFile(deps.ctx);
      const stateMd = await readStateMd(sessionFile.session_folder);
      const code = nextProblemSubCode(stateMd, parent, t);
      const next = appendProblemSubItem(stateMd, parent, {
        type: t,
        code,
        slug,
        state,
        text: description,
      });
      await writeStateMd(sessionFile.session_folder, next);
      return { parent, code, full_code: `${parent}.${code}` };
    }

    throw new Error(`parent ${parent} is neither a valid OBJ code nor a Problem code`);
  },
});

const subEntitySetState = makeTool({
  name: "sub_entity_set_state",
  description:
    "Transition a sub-entity's state. Validates the target state against the entity type's " +
    "state machine (Issue → closed|canceled, Suggestion → confirmed|declined|canceled, Task → closed|canceled). " +
    "Works for both Objective-parented and Problem-parented sub-entities.",
  shape: {
    parent: z.string(),
    code: z.string(),
    new_state: z.string(),
  },
  handler: async ({ parent, code, new_state }, deps) => {
    const { type } = parseSubEntityCode(code);
    if (!isValidStateForType(type, new_state)) {
      throw new Error(`invalid state ${new_state} for type ${type}`);
    }

    if (isValidObjectiveCode(parent)) {
      const meta = deps.index.byCode.get(parent);
      if (!meta) throw new Error(`unknown OBJ: ${parent}`);
      const indexPath = path.join(meta.folderPath, "index.md");
      let content = await fs.readFile(indexPath, "utf8");
      content = setItemState(content, code, new_state);
      await atomicWriteText(indexPath, content);
      return { parent, code, new_state };
    }

    if (isValidProblemCode(parent)) {
      const sessionFile = await loadSessionStateFile(deps.ctx);
      const stateMd = await readStateMd(sessionFile.session_folder);
      const next = setProblemSubItemState(stateMd, parent, code, new_state);
      await writeStateMd(sessionFile.session_folder, next);
      return { parent, code, new_state };
    }

    throw new Error(`parent ${parent} is neither a valid OBJ code nor a Problem code`);
  },
});

const renameCurrentSession = makeTool({
  name: "rename_current_session",
  description:
    "Rename the current SessionFolder's slug. Moves the folder and updates the SessionStateFile atomically. " +
    "Preserves the `HHMM_` prefix.",
  shape: {
    new_slug: z.string(),
  },
  handler: async ({ new_slug }, deps) => {
    assertValidSlug(new_slug);
    const sessionId = currentSessionId();
    const sessionFile = await loadSessionStateFile(deps.ctx, sessionId);
    const oldFolder = sessionFile.session_folder;
    const dayDir = path.dirname(oldFolder);
    const oldName = path.basename(oldFolder);
    const hhmmMatch = /^(\d{4})_/.exec(oldName);
    if (!hhmmMatch) {
      throw new Error(`current SessionFolder name "${oldName}" does not start with HHMM_`);
    }
    const hhmm = hhmmMatch[1]!;
    const newFolder = path.join(dayDir, `${hhmm}_${new_slug}`);
    if (newFolder === oldFolder) {
      return { session_folder: oldFolder, note: "slug unchanged" };
    }
    await fs.rename(oldFolder, newFolder);
    const updated = { ...sessionFile, session_folder: newFolder };
    const stateFilePath = path.join(deps.ctx.sessionsDir, `${sessionId}.json`);
    await atomicWriteText(stateFilePath, JSON.stringify(updated, null, 2) + "\n");
    return { session_folder: newFolder, previous: oldFolder };
  },
});

export const ALL_TOOLS = [
  objectiveCreate,
  objectiveSetState,
  objectiveSetBlockedBy,
  subEntityCreate,
  subEntitySetState,
  renameCurrentSession,
];

export async function initializeDeps(): Promise<ToolDeps> {
  const ctx = await loadContext();
  const index = await buildObjIndex(ctx);
  return { ctx, index };
}

export function registerAllTools(server: McpServer, deps: ToolDeps): void {
  for (const tool of ALL_TOOLS) {
    server.tool(
      tool.name,
      tool.description,
      tool.shape as ZodRawShape,
      async (args: Record<string, unknown>): Promise<CallToolResult> => {
        try {
          // The MCP SDK has already validated args against the Zod shape.
          const result = await tool.handler(args as never, deps);
          return ok(result);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          return fail(message);
        }
      },
    );
  }
}
