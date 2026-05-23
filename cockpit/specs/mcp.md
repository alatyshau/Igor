# MCP Server — Spec

Specification of the cockpit's MCP server — the cognitive-offload layer for the agent.

## Purpose

The MCP encapsulates procedural complexity (code allocation, folder conventions, state machines, graph invariants, atomic file operations) so the agent calls a single named tool instead of carrying the recipe in its system prompt.

## Transport and lifecycle

- **Transport:** `stdio` (Claude Code spawns the MCP as a child process per session).
- **Scoping:** **per-context, per-session.** The MCP inherits `cwd` from the parent Claude Code process (the ContextFolder root) and `CLAUDE_CODE_SESSION_ID` as an env var (Claude Code injects it into every spawned subprocess automatically). To operate on the current session's `state.md`, MCP reads `./.claude/sessions/<CLAUDE_CODE_SESSION_ID>.json` (the SessionStateFile, maintained by the Stop hook — see `stop_hook.md`) lazily on first state.md operation and follows its `session_folder` field. The MCP reads and writes `./objectives/`, that session's `state.md`, and that session's `subchats/` (for `spawn_subchat`). It reads `./.claude/cockpit/subagents/` for subagent profiles (this path is cockpit-owned and explicitly *not* `.claude/agents/` — see `deploy.md` for the reason). Historical journal is not touched. If the SessionStateFile does not exist yet (pre-first-hook-fire), MCP returns a recoverable error — the operation succeeds once the hook has run.
- **Stack:** TypeScript with the official `@modelcontextprotocol/sdk`. Compiled with `tsc` to `dist/` (entrypoint: `dist/src/index.js`, per `package.json` `main`), run by Node (v22+). Build is one-time at install; runtime is just `node dist/src/index.js`.

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
| `rename_current_session` | Mutate the session slug: `mv` the SessionFolder, update **every** SessionStateFile alias whose `first_prompt_id` matches (resumed chats produce multiple aliases pointing at one folder — see [`stop_hook.md`](stop_hook.md) §SessionStateFile), atomic across all of them. |
| `spawn_subchat` | Materialize a subchat in the current SessionFolder. Reads the subagent profile from `<ContextFolder>/.claude/cockpit/subagents/<name>.md` (deployed by `install.py` — see `deploy.md`), creates `<SessionFolder>/subchats/<name>/` with `config.yaml` (generated from profile frontmatter + defaults) and `system_prompt.md` (profile body), ensures the subagent line `- <name> (active)` is present in `state.md` `## Subchats` (no-op if already present, never duplicated). **Overwrite semantics on re-spawn:** `config.yaml` and `system_prompt.md` are regenerated from the current profile; `session.json` and `log/` are left intact. These two regenerated files are MCP-owned, not for hand-editing. **Errors:** if the profile file is missing from `.claude/cockpit/subagents/`, returns a recoverable error pointing the caller at `deploy.md` (resolution: re-run `install.py`). Full subchat behavior contract in [`subchat.md`](subchat.md). |

These map to the operations in `domain-model.md` §3 and (for `spawn_subchat`) [`subchat.md`](subchat.md). Each tool is an atomic filesystem transaction — no partial writes, no half-updated state.
