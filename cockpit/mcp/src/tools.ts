// MCP tool definitions for the cockpit. See cockpit/specs/architecture.md §3
// and cockpit/specs/mcp.md §Transport and lifecycle for the session_id
// parameter contract.

import { promises as fs } from "node:fs";
import * as path from "node:path";
import { z, type ZodRawShape } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import {
  isValidObjectiveCode,
  parseTicketCode,
  formatTicketCode,
  nextTicketIndex,
  type TicketType,
} from "./code.js";
import { assertValidSlug } from "./slug.js";
import {
  isValidInitialObjectiveState,
  isValidStateForType,
  objectiveSubdir,
  VALID_INITIAL_OBJECTIVE_STATES,
  type InitialObjectiveState,
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
  type ContextPaths,
} from "./context.js";
import {
  appendProblemTicket,
  ensureSubchatLine,
  isValidProblemCode,
  nextProblemTicketCode,
  readStateMd,
  setProblemTicketState,
  writeStateMd,
} from "./state_md.js";
import { materializeSubchat } from "./subchat_spawn.js";

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

/**
 * Shared Zod field for the session_id parameter on every tool. The agent
 * sources this from its own CLAUDE_CODE_SESSION_ID env var and passes it
 * uniformly to every MCP call. Tools that need to resolve the current
 * SessionFolder consume it; tools that operate only on Objectives accept
 * it for shape uniformity and ignore it.
 *
 * Background: MCP's stdio transport bakes env at spawn time, but session_id
 * is per-chat and shifts on resume. process.env is therefore the wrong
 * transport for session_id — see cockpit/specs/mcp.md.
 */
const SESSION_ID_FIELD = z
  .string()
  .min(1)
  .describe(
    "Caller's CLAUDE_CODE_SESSION_ID. Required uniformly on every tool. " +
      "Session-aware tools (rename_current_session, ticket_* on Problem parents) " +
      "use it to resolve the SessionStateFile; other tools accept it for shape uniformity.",
  );

// ---- tools ----

const objectiveCreate = makeTool({
  name: "objective_create",
  description:
    "Allocate the next OBJxxx code and create its folder + index.md skeleton " +
    "directly in the subfolder matching `initial_state` (active for draft/open, " +
    "`backlog/` for backlog). Returns the assigned code and folder path.",
  shape: {
    session_id: SESSION_ID_FIELD,
    slug: z.string().describe("CamelCase or snake_case; no dashes or dots."),
    goal: z.string().describe("Цель — desired outcome, one paragraph."),
    outputs: z.array(z.string()).default([]).describe(
      "Выходы — verifiable deliverables, action + target each.",
    ),
    why: z.string().default("").describe("Motivation; one to a few paragraphs."),
    blocked_by: z.array(z.string()).default([]).describe(
      "OBJ codes that must reach a terminal state before this one can close.",
    ),
    initial_state: z
      .enum(VALID_INITIAL_OBJECTIVE_STATES)
      .default("open")
      .describe(
        "Initial state: `draft` (parked), `open` (active), or `backlog` (deferred). " +
          "Terminal states (`closed`, `canceled`) are not valid initial states — " +
          "they can only be reached via transitions.",
      ),
  },
  handler: async ({ slug, goal, outputs, why, blocked_by, initial_state }, deps) => {
    // Robust to direct invocation that bypasses Zod's `.default("open")`.
    const requested = initial_state ?? "open";
    assertValidSlug(slug);
    if (!isValidInitialObjectiveState(requested)) {
      throw new Error(
        `invalid initial_state "${requested}"; expected one of: ${VALID_INITIAL_OBJECTIVE_STATES.join(", ")}`,
      );
    }
    if (deps.index.bySlug.has(slug)) {
      throw new Error(`slug "${slug}" already in use by ${deps.index.bySlug.get(slug)}`);
    }
    for (const code of blocked_by) {
      if (!isValidObjectiveCode(code) || !deps.index.byCode.has(code)) {
        throw new Error(`blocked_by references unknown OBJ: ${code}`);
      }
    }

    const code = nextObjectiveCode(deps.index);
    const state = requested as InitialObjectiveState;
    const subdir = objectiveSubdir(state);
    const parentDir = subdir
      ? path.join(deps.ctx.objectivesDir, subdir)
      : deps.ctx.objectivesDir;
    await fs.mkdir(parentDir, { recursive: true });
    const folderPath = path.join(parentDir, `${code}_${slug}`);
    const indexPath = await writeObjIndexMd(folderPath, {
      code,
      slug,
      state: state as ObjectiveState,
      goal,
      outputs,
      why,
      blockedBy: blocked_by,
    });

    const meta: ObjMeta = {
      code,
      slug,
      state,
      blockedBy: [...blocked_by],
      folderPath,
    };
    deps.index.byCode.set(code, meta);
    deps.index.bySlug.set(slug, code);

    return { code, folder_path: folderPath, index_path: indexPath, state };
  },
});

const objectiveSetState = makeTool({
  name: "objective_set_state",
  description:
    "Move an Objective between states. Relocates the folder to the matching subdir " +
    "(active / closed / cancelled / backlog). On `canceled`, cascades open tickets to `canceled`.",
  shape: {
    session_id: SESSION_ID_FIELD,
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
      cascaded_tickets: cascaded,
    };
  },
});

const objectiveSetBlockedBy = makeTool({
  name: "objective_set_blocked_by",
  description:
    "Replace the Blocked-by list on an Objective. Validates each referenced code exists and that no cycle would be introduced.",
  shape: {
    session_id: SESSION_ID_FIELD,
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

const ticketCreate = makeTool({
  name: "ticket_create",
  description:
    "Create a ticket (Issue I / Suggestion S / Task T) under an Objective or a Problem. " +
    "For Objective parents (`OBJxxx`), appends to the OBJ's `## Items` in its `index.md`. " +
    "For Problem parents (`P1..P9`), appends a nested bullet under that Problem in the " +
    "session's `state.md` — `session_id` is required to locate the right session.",
  shape: {
    session_id: SESSION_ID_FIELD,
    parent: z.string().describe("OBJxxx code or P1..P9 Problem code"),
    type: z.enum(["I", "S", "T"]),
    slug: z.string(),
    description: z.string().default(""),
    state: z.string().default("open"),
  },
  handler: async ({ session_id, parent, type, slug, description, state }, deps) => {
    const t = type as TicketType;
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
      const idx = nextTicketIndex(existingCodes, t);
      const code = formatTicketCode(t, idx);
      content = appendItem(content, { code, state, slug, text: description });
      await atomicWriteText(indexPath, content);
      return { parent, code, full_code: `${parent}.${code}` };
    }

    if (isValidProblemCode(parent)) {
      const sessionFile = await loadSessionStateFile(deps.ctx, session_id);
      const stateMd = await readStateMd(sessionFile.session_folder);
      const code = nextProblemTicketCode(stateMd, parent, t);
      const next = appendProblemTicket(stateMd, parent, {
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

const ticketSetState = makeTool({
  name: "ticket_set_state",
  description:
    "Transition a ticket's state. Validates the target state against the entity type's " +
    "state machine (Issue → closed|canceled, Suggestion → confirmed|canceled, Task → closed|canceled). " +
    "Works for both Objective-parented and Problem-parented tickets — for the latter, " +
    "`session_id` is required to locate the right session.",
  shape: {
    session_id: SESSION_ID_FIELD,
    parent: z.string(),
    code: z.string(),
    new_state: z.string(),
  },
  handler: async ({ session_id, parent, code, new_state }, deps) => {
    const { type } = parseTicketCode(code);
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
      const sessionFile = await loadSessionStateFile(deps.ctx, session_id);
      const stateMd = await readStateMd(sessionFile.session_folder);
      const next = setProblemTicketState(stateMd, parent, code, new_state);
      await writeStateMd(sessionFile.session_folder, next);
      return { parent, code, new_state };
    }

    throw new Error(`parent ${parent} is neither a valid OBJ code nor a Problem code`);
  },
});

const renameCurrentSession = makeTool({
  name: "rename_current_session",
  description:
    "Rename the current SessionFolder's slug. Moves the folder and updates every " +
    "SessionStateFile alias whose `session_folder` matches the old path (resumed " +
    "chats produce multiple aliases pointing at one folder). Atomic across all " +
    "aliases. Preserves the `HHMM_` prefix.",
  shape: {
    session_id: SESSION_ID_FIELD,
    new_slug: z.string(),
  },
  handler: async ({ session_id, new_slug }, deps) => {
    assertValidSlug(new_slug);
    const sessionFile = await loadSessionStateFile(deps.ctx, session_id);
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

    // Locate every SessionStateFile alias pointing at the old folder before
    // the rename — resumed chats produce multiple aliases pointing at one
    // folder, and leaving stale aliases would mean later resumes silently
    // land on a non-existent path.
    const aliases = await findAliasesByFolder(deps.ctx.sessionsDir, oldFolder);
    if (aliases.length === 0) {
      // Shouldn't happen — at minimum the caller's own SessionStateFile
      // points at oldFolder — but be defensive.
      aliases.push(path.join(deps.ctx.sessionsDir, `${session_id}.json`));
    }

    await fs.rename(oldFolder, newFolder);
    for (const aliasPath of aliases) {
      try {
        const raw = await fs.readFile(aliasPath, "utf8");
        const parsed = JSON.parse(raw);
        parsed.session_folder = newFolder;
        await atomicWriteText(aliasPath, JSON.stringify(parsed, null, 2) + "\n");
      } catch (e) {
        // One bad alias shouldn't fail the rename; log via thrown context.
        // The folder is already moved, so subsequent reads against this alias
        // would fail clearly with "folder does not exist".
        console.error(`[rename_current_session] failed to update alias ${aliasPath}: ${e}`);
      }
    }

    return {
      session_folder: newFolder,
      previous: oldFolder,
      aliases_updated: aliases.length,
    };
  },
});

/**
 * Find every SessionStateFile in ``sessionsDir`` whose ``session_folder``
 * field equals ``folderPath``. Used by rename_current_session to update all
 * aliases produced by resumed chats in one atomic sweep.
 */
async function findAliasesByFolder(
  sessionsDir: string,
  folderPath: string,
): Promise<string[]> {
  const result: string[] = [];
  let entries: string[];
  try {
    entries = await fs.readdir(sessionsDir);
  } catch {
    return result;
  }
  for (const name of entries) {
    if (!name.endsWith(".json")) continue;
    const full = path.join(sessionsDir, name);
    try {
      const raw = await fs.readFile(full, "utf8");
      const parsed = JSON.parse(raw);
      if (parsed && parsed.session_folder === folderPath) {
        result.push(full);
      }
    } catch {
      // skip unreadable / malformed alias files
    }
  }
  return result;
}

const spawnSubchat = makeTool({
  name: "spawn_subchat",
  description:
    "Materialize a subchat in the current SessionFolder (resolved via session_id). " +
    "Reads the subagent profile from `<ContextFolder>/.claude/cockpit/subagents/<name>.md` " +
    "(deployed by install.py — see cockpit/specs/deploy.md), creates " +
    "`<SessionFolder>/subchats/<name>/` with `config.yaml` (defaults + profile " +
    "frontmatter overrides) and `system_prompt.md` (profile body), and ensures the " +
    "subagent line `- <name> (active)` is present in `state.md` `## Subchats`. " +
    "Overwrite semantics on re-spawn: `config.yaml` and `system_prompt.md` are " +
    "regenerated; `session.json` and `log/` are left intact. Errors: missing profile " +
    "returns a recoverable error pointing the caller at deploy.md.",
  shape: {
    session_id: SESSION_ID_FIELD,
    subagent: z
      .string()
      .min(1)
      .describe(
        "Subagent profile name. Matches the basename of " +
          "`<ContextFolder>/.claude/cockpit/subagents/<name>.md`. " +
          "Must contain only [A-Za-z0-9_-] and not start with a hyphen.",
      ),
  },
  handler: async ({ session_id, subagent }, deps) => {
    const sessionFile = await loadSessionStateFile(deps.ctx, session_id);
    const result = await materializeSubchat({
      contextRoot: deps.ctx.root,
      sessionFolder: sessionFile.session_folder,
      subagent,
    });

    // Update state.md ## Subchats — merge-preserving (other sections untouched).
    const stateMd = await readStateMd(sessionFile.session_folder);
    const next = ensureSubchatLine(stateMd, subagent);
    if (next !== stateMd) {
      await writeStateMd(sessionFile.session_folder, next);
    }

    return {
      subagent,
      subchat_folder: result.subchatFolder,
      created: result.created,
      ignored_profile_keys: result.ignoredProfileKeys,
    };
  },
});

export const ALL_TOOLS = [
  objectiveCreate,
  objectiveSetState,
  objectiveSetBlockedBy,
  ticketCreate,
  ticketSetState,
  renameCurrentSession,
  spawnSubchat,
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
