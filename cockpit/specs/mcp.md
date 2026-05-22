# MCP Server — Spec

Specification of the cockpit's MCP server — the cognitive-offload layer for the agent.

## Purpose

The MCP encapsulates procedural complexity (code allocation, folder conventions, state machines, graph invariants, atomic file operations) so the agent calls a single named tool instead of carrying the recipe in its system prompt.

## Transport and lifecycle

- **Transport:** `stdio` (Claude Code spawns the MCP as a child process per session).
- **Scoping:** **per-context, per-session.** The MCP inherits `cwd` from the parent Claude Code process (the ContextFolder root) and `CLAUDE_CODE_SESSION_ID` as an env var (Claude Code injects it into every spawned subprocess automatically). To operate on the current session's `state.md`, MCP reads `./.claude/sessions/<CLAUDE_CODE_SESSION_ID>.json` (the SessionStateFile, maintained by the Stop hook — see `stop_hook.md`) lazily on first state.md operation and follows its `session_folder` field. The MCP reads and writes `./objectives/` and that session's `state.md`. Historical journal is not touched. If the SessionStateFile does not exist yet (pre-first-hook-fire), MCP returns a recoverable error — the operation succeeds once the hook has run.
- **Stack:** TypeScript with the official `@modelcontextprotocol/sdk`. Compiled with `tsc` to `dist/`, run by Node (v22+). Build is one-time at install; runtime is just `node dist/index.js`.

Per-context, per-session: each Claude Code chat in each Context has its own MCP instance, fully isolated. State the MCP holds in memory survives LLM context compaction — only the LLM context is compressed; the MCP process keeps running.

## Index build at startup

On startup, the MCP scans `objectives/` (all subfolders including `closed/`, `cancelled/`, `backlog/`) and constructs:

- a map of all Objectives by code and by slug;
- the Blocked-by dependency graph (DAG);
- the next available ObjectiveCode (`max + 1`).

The journal is not scanned at startup — no tool consumes that index.

## Tool surface

The selection criterion is **cognitive offload**: a tool earns its place if its absence would force the agent's system prompt to carry a non-trivial procedure (code allocation, folder conventions, state machines, multi-step file mutations, graph algorithms). A tool that would only save filesystem milliseconds — without removing anything from the agent's prompt — does not earn its place.

Three concrete categories qualify:

1. **Topology and code allocation** — creating / moving folders with named conventions and unique-code rules.
2. **Cross-entity invariants** — operations that need the indexed graph (Blocked-by cycle detection, cascade-on-cancel) or coordinated multi-file changes.
3. **Indexed queries** — answers that require a precomputed graph (`top_n`).

Things the agent does well by itself stay with the agent — freeform body text in `## WHAT` / `## WHY`, single-line `Цель`, `state.md` updates, `Выходы` line edits, loose-reference resolution, factual lookups. MCP does not wrap them: writing such wrappers would inflate the surface without reducing the prompt.

| Tool | Purpose |
|---|---|
| `objective_create` | Allocate the next `OBJxxx` code, create the ObjectiveFolder + initial `index.md` skeleton. |
| `objective_set_state` | Move an Objective between `draft` / `open` / `closed` / `canceled` / `backlog` (with the corresponding folder relocation). |
| `objective_set_blocked_by` | Replace the `Blocked by` list on an Objective; validates that each referenced code exists and that no cycle is introduced. |
| `sub_entity_create` | Allocate the next sub-entity code (`In` / `Sn` / `Tn`) under an Objective **or a Problem** and append the inline item. For Objective parents, edits `objectives/OBJxxx_<Slug>/index.md` `## Items`. For Problem parents, edits `state.md` of the current session under the relevant `P` bullet. |
| `sub_entity_set_state` | Move a sub-entity between states; cascades from a parent `cancel` automatically. Same dual-target as `sub_entity_create` (OBJ index.md or session `state.md`). |
| `rename_current_session` | Mutate the session slug: `mv` the SessionFolder, update the SessionStateFile, atomic. |

These map to the operations in `domain-model.md` §3. Each tool is an atomic filesystem transaction — no partial writes, no half-updated state.
