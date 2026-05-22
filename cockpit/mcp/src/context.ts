// Resolves the ContextFolder (process cwd) and the current SessionFolder (via
// CLAUDE_CODE_SESSION_ID + SessionStateFile).
//
// See cockpit/specs/architecture.md §3 and cockpit/specs/schemas/session_folder.md.

import { promises as fs } from "node:fs";
import * as path from "node:path";
import { pathExists } from "./fs_atomic.js";

export interface ContextPaths {
  root: string; // ContextFolder root (== cwd)
  objectivesDir: string; // <root>/objectives/
  sessionsDir: string; // <root>/.claude/sessions/
}

const CONTEXT_MANIFEST = "context.json";

export async function loadContext(root: string = process.cwd()): Promise<ContextPaths> {
  const manifest = path.join(root, CONTEXT_MANIFEST);
  if (!(await pathExists(manifest))) {
    throw new Error(
      `not a Cockpit context: ${manifest} is missing. Refusing to operate.`,
    );
  }
  return {
    root,
    objectivesDir: path.join(root, "objectives"),
    sessionsDir: path.join(root, ".claude", "sessions"),
  };
}

export interface SessionStateFile {
  session_folder: string;
  first_prompt_id: string;
}

const SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID";

export function currentSessionId(): string {
  const id = process.env[SESSION_ID_ENV];
  if (!id) {
    throw new Error(
      `${SESSION_ID_ENV} env var is not set — cannot resolve current session.`,
    );
  }
  return id;
}

export async function loadSessionStateFile(
  ctx: ContextPaths,
  sessionId: string = currentSessionId(),
): Promise<SessionStateFile> {
  const filePath = path.join(ctx.sessionsDir, `${sessionId}.json`);
  if (!(await pathExists(filePath))) {
    throw new Error(
      `SessionStateFile not found at ${filePath} — the Stop hook has not run yet for this session.`,
    );
  }
  const raw = await fs.readFile(filePath, "utf8");
  const parsed = JSON.parse(raw) as Partial<SessionStateFile>;
  if (!parsed.session_folder || !parsed.first_prompt_id) {
    throw new Error(`SessionStateFile ${filePath} is malformed (missing required fields).`);
  }
  return {
    session_folder: parsed.session_folder,
    first_prompt_id: parsed.first_prompt_id,
  };
}
