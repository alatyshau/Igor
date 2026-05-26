# MCP Server — Spec

Specification of the cockpit's MCP server — the cognitive-offload layer for the agent.

## Purpose

The MCP encapsulates procedural complexity (code allocation, folder conventions, state machines, graph invariants, atomic file operations) so the agent calls a single named tool instead of carrying the recipe in its system prompt.

## Transport and lifecycle

- **Transport:** `stdio` (Claude Code spawns the MCP as a child process per chat).
- **Scoping:** **per-context.** The MCP inherits `cwd` from the parent Claude Code process (the ContextFolder root) and operates on `./objectives/`, the current session's `state.md` and `subchats/`, and `./.claude/agents/` (read-only — canonical Claude Code path where `install.py` deploys subagent profiles; same source serves both native discovery and MCP `spawn_subchat`, see `deploy.md`). Historical journal is not touched.
- **`session_id` is a tool argument, not env.** Every tool's Zod shape requires `session_id` as a mandatory parameter. The agent reads its own `CLAUDE_CODE_SESSION_ID` env var once at session start and threads it through every MCP call. The MCP uses it to locate the SessionStateFile (`./.claude/sessions/<session_id>.json`) when an operation needs to resolve the current `SessionFolder`; tools that operate only on Objectives accept it for shape uniformity and ignore it. **Why not env?** The MCP server is a long-lived stdio subprocess; process env is baked at spawn time. Claude Code does not propagate `CLAUDE_CODE_SESSION_ID` into spawned MCP subprocesses for exactly this reason — passing it via env would freeze the value to the spawning chat and break on resume or any chat sharing the process. If the SessionStateFile does not yet exist for the supplied `session_id` (the UserPromptSubmit hook is not installed and Stop has not fired yet), the MCP returns a clear recoverable error — `SessionStateFile not found … no hook has bootstrapped this session yet`.
- **Stack:** TypeScript with the official `@modelcontextprotocol/sdk`. Compiled with `tsc` to `dist/` (entrypoint: `dist/src/index.js`, per `package.json` `main`), run by Node (v22+). Build is one-time at install; runtime is just `node dist/src/index.js`.

Per-context: each Context has its own MCP instance bound to that ContextFolder. State the MCP holds in memory survives LLM context compaction — only the LLM context is compressed; the MCP process keeps running. Multiple Claude Code chats in the same Context may share one MCP process — this is fine because session_id flows through tool arguments, not process state.

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

Every tool below takes `session_id` (string, required) as its first parameter — see *Transport and lifecycle* for the rationale. Tools that need to read or write the current session's `state.md` or `SessionFolder` use it; tools that operate only on Objectives ignore it.

| Tool | Purpose | Uses `session_id`? |
|---|---|---|
| `objective_create` | Allocate the next `OBJxxx` code, create the ObjectiveFolder + initial `index.md` skeleton. | No (shape-only) |
| `objective_set_state` | Move an Objective between `draft` / `open` / `closed` / `canceled` / `backlog` (with the corresponding folder relocation). | No (shape-only) |
| `objective_set_blocked_by` | Replace the `Blocked by` list on an Objective; validates that each referenced code exists and that no cycle is introduced. | No (shape-only) |
| `ticket_create` | Allocate the next ticket code (`In` / `Sn` / `Tn`) under an Objective **or a Problem** and append the inline item. For Objective parents, edits `objectives/OBJxxx_<Slug>/index.md` `## Items`. For Problem parents, edits `state.md` of the session pointed at by `session_id`. | Yes (Problem parent only) |
| `ticket_set_state` | Move a ticket between states; cascades from a parent `cancel` automatically. Same dual-target as `ticket_create` (OBJ index.md or session `state.md`). | Yes (Problem parent only) |
| `rename_current_session` | Mutate the session slug: `mv` the SessionFolder, then update **every** SessionStateFile alias whose `session_folder` equals the old path (resumed chats produce multiple aliases pointing at one folder — see [`stop_hook.md`](stop_hook.md) §SessionStateFile), atomic across all of them. | Yes |
| `spawn_subchat` | Materialize a subchat in the current SessionFolder (resolved via `session_id`). Reads the subagent profile from `<ContextFolder>/.claude/agents/<name>.md` (deployed by `install.py` — see `deploy.md`), creates `<SessionFolder>/subchats/<name>/` with `config.yaml` (generated from profile frontmatter + defaults) and `system_prompt.md` (profile body), ensures the subagent line `- <name> (active)` is present in `state.md` `## Subchats` (no-op if already present, never duplicated). **Overwrite semantics on re-spawn:** `config.yaml` and `system_prompt.md` are regenerated from the current profile; `session.json` and `log/` are left intact. These two regenerated files are MCP-owned, not for hand-editing. **Errors:** if the profile file is missing from `.claude/agents/`, returns a recoverable error pointing the caller at `deploy.md` (resolution: re-run `install.py`). Full subchat behavior contract in [`subchat.md`](subchat.md). | Yes |

These map to the operations in `domain-model.md` §3 and (for `spawn_subchat`) [`subchat.md`](subchat.md). Each tool is an atomic filesystem transaction — no partial writes, no half-updated state.
