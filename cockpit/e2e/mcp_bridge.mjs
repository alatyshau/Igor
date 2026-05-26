// Thin Node bridge that lets the Python e2e harness invoke the same
// spawn_subchat side-effect sequence the real MCP tool handler performs,
// without standing up the full stdio MCP server. See `spawn_subchat` in
// `cockpit/mcp/src/tools.ts:454` for the production handler.
//
// Side effects performed (in order):
//   1. materializeSubchat({contextRoot, sessionFolder, subagent})
//      → writes `<SessionFolder>/subchats/<name>/config.yaml` +
//        `system_prompt.md`.
//   2. readStateMd → ensureSubchatLine → writeStateMd (only if changed)
//      → appends `- <name> (active)` under `## Subchats` in `state.md`,
//        merge-preserving other sections. This is the spec-contracted
//        state.md integration (cockpit/specs/subchat.md §"state.md
//        integration"; cockpit/specs/schemas/session_folder.md §Subchats).
//
// Why exists: spinning up the full stdio MCP server for one tool call
// would require the harness to speak JSON-RPC over stdio and manage
// server lifecycle. Replicating the two side-effect calls directly is
// byte-identical and two orders of magnitude less plumbing. We do NOT
// replicate the session_id-resolution layer (`loadSessionStateFile`);
// that's tested by the MCP suite and the harness passes `sessionFolder`
// explicitly.
//
// Invocation:
//   node mcp_bridge.mjs '<JSON: {contextRoot, sessionFolder, subagent}>'
//
// On success, prints a single-line JSON
// `{subchat_folder, created, ignored_profile_keys, state_md_updated}`.
// `state_md_updated` is true iff `## Subchats` was edited (false on
// idempotent re-spawn).
// On failure, prints the error message on stderr and exits non-zero.

import { materializeSubchat } from "../mcp/dist/src/subchat_spawn.js";
import {
  ensureSubchatLine,
  readStateMd,
  writeStateMd,
} from "../mcp/dist/src/state_md.js";

const args = process.argv[2];
if (!args) {
  console.error(
    "mcp_bridge.mjs: missing JSON argument " +
      "(expected: {contextRoot, sessionFolder, subagent})",
  );
  process.exit(64);
}

let parsed;
try {
  parsed = JSON.parse(args);
} catch (e) {
  console.error(`mcp_bridge.mjs: invalid JSON arg: ${e?.message ?? e}`);
  process.exit(64);
}

try {
  const result = await materializeSubchat(parsed);

  // state.md integration — same logic as the production handler in
  // cockpit/mcp/src/tools.ts:486-490. Idempotent: writeStateMd is called
  // only if ensureSubchatLine produced a different string (typically on
  // first spawn of a given subagent; subsequent re-spawns find the line
  // already present and produce identical content).
  const stateMd = await readStateMd(parsed.sessionFolder);
  const next = ensureSubchatLine(stateMd, parsed.subagent);
  let stateMdUpdated = false;
  if (next !== stateMd) {
    await writeStateMd(parsed.sessionFolder, next);
    stateMdUpdated = true;
  }

  // Re-key to snake_case so the Python side reads it like the real MCP tool
  // response shape (cockpit/specs/mcp.md `spawn_subchat` row).
  process.stdout.write(
    JSON.stringify({
      subchat_folder: result.subchatFolder,
      created: result.created,
      ignored_profile_keys: result.ignoredProfileKeys,
      state_md_updated: stateMdUpdated,
    }) + "\n",
  );
} catch (e) {
  console.error(e?.message ?? String(e));
  process.exit(1);
}
