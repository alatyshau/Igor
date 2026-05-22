// Startup OBJ index: scans `objectives/` (active + closed + cancelled + backlog),
// extracts code/slug from folder names, parses each index.md for State and Blocked-by.

import { promises as fs } from "node:fs";
import * as path from "node:path";
import { isValidObjectiveCode, codeToInt, intToCode } from "./code.js";
import { isValidObjectiveState, type ObjectiveState } from "./state.js";
import type { ContextPaths } from "./context.js";

const FOLDER_PATTERN = /^(OBJ[0-9A-Z]{3})_([A-Za-z][A-Za-z0-9_]*)$/;
const STATE_LINE = /^\*\*State:\*\*\s*(\S+)\s*$/m;
const BLOCKED_BY_LINE = /^\*\*Blocked by:\*\*\s*(.+?)\s*$/m;

export interface ObjMeta {
  code: string;
  slug: string;
  state: ObjectiveState;
  blockedBy: string[];
  folderPath: string;
}

export interface ObjIndex {
  byCode: Map<string, ObjMeta>;
  bySlug: Map<string, string>; // slug -> code
}

const STATE_SUBDIRS = ["", "closed", "cancelled", "backlog"];

export async function buildObjIndex(ctx: ContextPaths): Promise<ObjIndex> {
  const byCode = new Map<string, ObjMeta>();
  const bySlug = new Map<string, string>();

  for (const sub of STATE_SUBDIRS) {
    const dir = sub ? path.join(ctx.objectivesDir, sub) : ctx.objectivesDir;
    let entries: string[];
    try {
      entries = await fs.readdir(dir);
    } catch {
      continue; // dir doesn't exist yet
    }
    for (const name of entries) {
      const m = FOLDER_PATTERN.exec(name);
      if (!m) continue;
      const code = m[1]!;
      const slug = m[2]!;
      const folderPath = path.join(dir, name);
      const meta = await readObjMeta(folderPath, code, slug);
      if (!meta) continue;
      byCode.set(code, meta);
      bySlug.set(slug, code);
    }
  }

  return { byCode, bySlug };
}

async function readObjMeta(
  folderPath: string,
  code: string,
  slug: string,
): Promise<ObjMeta | null> {
  const indexPath = path.join(folderPath, "index.md");
  let content: string;
  try {
    content = await fs.readFile(indexPath, "utf8");
  } catch {
    return null;
  }

  const stateMatch = STATE_LINE.exec(content);
  if (!stateMatch || !isValidObjectiveState(stateMatch[1]!)) {
    return null;
  }
  const state = stateMatch[1] as ObjectiveState;

  let blockedBy: string[] = [];
  const blockedMatch = BLOCKED_BY_LINE.exec(content);
  if (blockedMatch) {
    blockedBy = blockedMatch[1]!
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0 && isValidObjectiveCode(s));
  }

  return { code, slug, state, blockedBy, folderPath };
}

export function nextObjectiveCode(index: ObjIndex): string {
  let max = -1;
  for (const code of index.byCode.keys()) {
    const n = codeToInt(code);
    if (n > max) max = n;
  }
  return intToCode(max + 1);
}

// Detect cycle: would adding `target -> deps` create a cycle?
// We're saying `target` is blocked by each of `deps`. A cycle exists if any dep
// (transitively) is blocked by target.
export function wouldCreateCycle(
  index: ObjIndex,
  target: string,
  deps: readonly string[],
): boolean {
  const visited = new Set<string>();
  const stack: string[] = [...deps];
  while (stack.length > 0) {
    const code = stack.pop()!;
    if (code === target) return true;
    if (visited.has(code)) continue;
    visited.add(code);
    const meta = index.byCode.get(code);
    if (!meta) continue;
    for (const d of meta.blockedBy) stack.push(d);
  }
  return false;
}
