// Resolves the ContextFolder (process cwd) and loads a session's SessionStateFile.
//
// Design note (see cockpit/specs/mcp.md §Transport and lifecycle): the MCP
// server runs as a long-lived stdio subprocess and cannot reliably read
// session_id from process.env — Claude Code does not propagate the per-chat
// CLAUDE_CODE_SESSION_ID into MCP subprocess env (it would be frozen at
// spawn time, incorrect for any subsequent chat sharing the process). Every
// session-aware tool therefore requires the caller (the agent) to pass
// session_id explicitly as a tool argument; the agent reads it from its
// own env once at session start and threads it through.

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
  first_prompt_id?: string;
}

/**
 * Load the SessionStateFile for an explicitly provided session_id.
 *
 * The caller is responsible for sourcing session_id from somewhere reliable
 * (the agent passes its own CLAUDE_CODE_SESSION_ID via a tool argument).
 * Reading session_id from process.env is unsafe in this transport — see the
 * design note at the top of this module.
 *
 * Throws a recoverable error if the SessionStateFile does not exist yet
 * (pre-first-hook-fire) or is malformed. ``first_prompt_id`` may be absent
 * when the UserPromptSubmit hook bootstrapped the file before any JSONL turn
 * existed; the Stop hook backfills it on the first subsequent fire.
 */
export async function loadSessionStateFile(
  ctx: ContextPaths,
  sessionId: string,
): Promise<SessionStateFile> {
  if (!sessionId) {
    throw new Error("loadSessionStateFile: session_id is required");
  }
  const filePath = path.join(ctx.sessionsDir, `${sessionId}.json`);
  if (!(await pathExists(filePath))) {
    throw new Error(
      `SessionStateFile not found at ${filePath} — no hook has bootstrapped this session yet.`,
    );
  }
  const raw = await fs.readFile(filePath, "utf8");
  const parsed = JSON.parse(raw) as Partial<SessionStateFile>;
  if (!parsed.session_folder) {
    throw new Error(`SessionStateFile ${filePath} is malformed (missing session_folder).`);
  }
  const result: SessionStateFile = { session_folder: parsed.session_folder };
  if (parsed.first_prompt_id) {
    result.first_prompt_id = parsed.first_prompt_id;
  }
  return result;
}
