# Architecture

How Igor.cockpit is realized as software. This spec defines the components, their boundaries, and the way they are deployed into a context.

The domain model (entities, states, relationships) is in `domain-model.md`. The on-disk schemas are in `schemas/`. This document is about the **running system**: which process does what, how they communicate, and how the whole thing is installed into a Context.

## 1. The maturity axis

Artifacts produced during work travel through three locations as they mature. This is the load-bearing structural axis of the system, orthogonal to the *time axis* (Objective forward / Journal backward) captured in the domain model.

| Location | Maturity | Lifetime |
|---|---|---|
| **SessionFolder** (`journal/.../HHMM_<slug>/`) | scratch, drafts, transient generations | session-bound; lives forever in the journal |
| **ObjectiveFolder** (`objectives/OBJxxx_<Slug>/`) | unripe deliverables, in-progress materials | from Objective creation until closure |
| **Git repository** (`<repo>/specs/`, `<repo>/src/`, …) | ripe, public, permanent | indefinite |

Promotion between layers is explicit:
- *Session → OBJ*: the user (or the agent on a user signal) moves a scratch artifact from the SessionFolder into the active ObjectiveFolder when it ceases to be one-off.
- *OBJ → git*: on Objective closure, ripe artifacts are `mv`-ed from the ObjectiveFolder into the linked git repository (typically `<repo>/specs/`). Recorded with the `promoted!` event.

External inputs (briefs, ТЗ, PDFs, datasets) enter at the leftmost edge and live wherever the consumer requires — usually outside this maturity axis, referenced from `state.md` `## Input` if relevant to the current session.

## 2. Component layout

The cockpit ships as three components inside one source repo, plus a deploy step.

```
Igor.source.git/
  cockpit/
    specs/                         ← this directory: design specs (domain, schemas, architecture)
    mcp/                           ← MCP server (TypeScript, bun)
      package.json
      tsconfig.json
      src/
        index.ts                   ← entry point
        domain/                    ← typed model (Objective, SubEntity, Session, …)
        tools/                     ← MCP tool implementations
    hooks/                         ← Claude Code hooks (Python)
      stop.py                      ← single self-bootstrapping hook
      test_stop.py                 ← unit tests (stdlib unittest)
    deploy/                        ← install into a Context
      install.py
      settings.template.json
```

The split is deliberate:
- **MCP is the brain** — it owns the typed domain model, validates entities, performs filesystem operations, computes queries. State-machine-heavy work belongs in TypeScript where the compiler enforces the schema.
- **Hooks are dumb pipes** — they read Claude Code's payload, write a file, exit. No domain logic. Python keeps cold-start low (~30–60ms) and avoids any TS compile step on every turn.
- Shared library is **not** needed because hooks do not manipulate Objectives. They write transcripts and create/update the SessionStateFile. That is the entire surface.

## 3. MCP server

### Transport and lifecycle

- **Transport:** `stdio` (Claude Code spawns the MCP as a child process per session).
- **Scoping:** **per-context, per-session.** The MCP inherits `cwd` from the parent Claude Code process; that cwd is the ContextFolder root. The MCP reads `./objectives/`, `./journal/`, `./shared/` relative to it.
- **Stack:** TypeScript with the official `@modelcontextprotocol/sdk`. Run via `bun` for fast startup (~50ms).

Per-context, per-session means: each Claude Code chat in each Context has its own MCP instance, fully isolated. No coordination problems between contexts. State the MCP holds in memory survives LLM context compaction (only the LLM context is compressed, the MCP process keeps running).

### Index build at startup

On startup, the MCP scans `objectives/` (all subfolders including `closed/`, `cancelled/`, `backlog/`) and constructs:
- a map of all Objectives by code and by slug;
- the Blocked-by dependency graph (DAG);
- the next available ObjectiveCode (`max + 1`);
- a journal index (sessions and which Objectives they touched) by scanning recent `state.md` files.

For 200 sessions + 50 Objectives this build takes ~100–500 ms. Imperceptible on session start.

### Tool surface (initial)

MCP owns operations that the agent cannot do safely with `Edit` / `Write` alone. Three categories qualify:

1. **Topology** — creating / moving / deleting the folders themselves (code allocation, atomic folder relocation between state directories).
2. **Cross-entity invariants** — operations that need the indexed graph (Blocked-by references must point at existing OBJs, no cycles; cascade-on-cancel).
3. **Queries on the index** — answers that require the in-memory graph (`top_n`, search, orientation).

Anything else — freeform body text in `## WHAT` / `## WHY`, single-line `Цель`, `state.md` updates, `Выходы` line edits — the agent writes directly via the standard `Edit` tool. MCP does not wrap text-writing for its own sake.

Names below are indicative; final naming gets fixed during implementation.

| Tool | Purpose |
|---|---|
| `objective_create` | Allocate the next `OBJxxx` code, create the ObjectiveFolder + initial `index.md` skeleton. |
| `objective_set_state` | Move an Objective between `draft` / `open` / `closed` / `canceled` / `backlog` (with the corresponding folder relocation). |
| `objective_set_blocked_by` | Replace the `Blocked by` list on an Objective; validates that each referenced code exists and that no cycle is introduced. |
| `sub_entity_create` | Allocate the next sub-entity code (`In` / `Sn` / `Tn`) under an Objective or Problem and append the inline item to its parent. |
| `sub_entity_set_state` | Move a sub-entity between states; cascades from a parent `cancel` automatically. |
| `rename_current_session` | Mutate the session slug: `mv` the SessionFolder, update the SessionStateFile, atomic. |
| `top_n` | Compute the critical path — the top N ready-to-work Objectives (open, all `Blocked by` resolved). Backs the `!топN` command. |
| `objective_search` | Lookup by code, slug, or loose reference. |
| `orientation_cockpit` | Return cockpit-level orientation (active OBJs, today's session, MCP version). Complements Duet's `orientation`. |

These map to the operations in `domain-model.md` §3. Each tool is an atomic filesystem transaction — no partial writes, no half-updated state.

The earlier draft included `objective_update_field`, `state_md_update`, and `triage_propose`. All three were removed: the first two wrap freeform text the agent can `Edit` directly; the third is read-only reasoning that lives in the chat, not at the MCP boundary.

## 4. Hooks

Hooks live as Python scripts (stdlib only). Claude Code spawns the hook on each event with a JSON payload on stdin.

### One hook only

The cockpit ships **a single Stop hook**. `SessionStart` and `UserPromptSubmit` were eliminated after experimentation revealed two problems:

- **Ghost sessions.** Claude Code spawns transient `session_id`s on VS Code restart that never receive prompts. A `SessionStart` hook eagerly created empty SessionFolders for these ghosts — pollution.
- **Resumed-chat misattribution.** A resumed chat gets a *new* `session_id` but reuses the original first prompt. `SessionStart` had no way to recognize "this is the same conversation continuing" and would create a duplicate folder.

Stop alone is sufficient: it has full JSONL access, can reconstruct the entire transcript including the first turn, can detect resumed chats via `first_prompt_id` matching, and never fires for ghost sessions. The hook is **idempotent** — re-firing on an unchanged JSONL is a no-op.

| Event | What the hook does |
|---|---|
| `Stop` | Reads `transcript_path` from the payload, parses the JSONL, builds/reconciles the SessionFolder, writes per-turn `NNN_msg.md` files, updates `transcript/index.json`. Self-bootstraps on first fire. |

`SubagentStop` events fire on the same channel and are filtered by `hook_event_name == "Stop"`. `SessionEnd` is unused — autonomous protocol generation, when added, will run detached, not as a hook.

### Lifecycle

1. **First fire (bootstrap).** No SessionStateFile exists yet. The hook reads the JSONL, takes `turns[0].prompt_id` as `first_prompt_id`, then:
   - scans `journal/YYYY/MM/DD/*/transcript/index.json` for a folder whose `turns[0].prompt_id` matches — if found, **reuse** (resumed-chat detection);
   - otherwise creates `journal/YYYY/MM/DD/HHMM_session/` using the timestamp of the first user prompt.
   - Writes the minimal SessionStateFile `{session_folder, first_prompt_id}` to `.claude/sessions/<session_id>.json`.
2. **Subsequent fires.** Reads the SessionStateFile, walks the JSONL, computes the desired per-turn files, and reconciles. Per-turn timestamps (`started_at` / `ended_at`) and counters (`midflight_count`, `assistant_count`) live in `index.json`.

### Reconciliation triggers

For each turn, the hook decides whether to (re)write the `NNN_msg.md` file. Action is triggered if any of:

- the file is missing **and** the index entry has no slug (i.e. the indexer hasn't claimed it — see *race-safety* below);
- the entry's `complete` flag is `false`;
- `midflight_count` grew (a mid-flight message arrived);
- `assistant_count` grew (additional assistant turn was appended);
- legacy `[interrupted — no assistant response]` marker present.

If none of these hold, the turn is skipped — keeping cold paths cheap.

### Mid-flight messages

Messages the user sends *while the assistant is working* arrive in the JSONL as `type:"attachment"` entries with `attachment.type:"queued_command"`. They belong to the same turn (no separate `prompt_id`) and are rendered chronologically inside the turn file under `**User (mid-flight):**` headers.

### Concurrency model

- **File lock per session.** Each fire acquires `fcntl.flock` on `.claude/sessions/<session_id>.lock` with a 30 s timeout. Concurrent fires for the same session serialize cleanly. Lock files are 0 bytes and harmless.
- **Atomic writes.** All file writes go through `temp file + os.replace` — no partial writes are ever observable.
- **Race-safety with the indexer.** The transcript indexer (see §3 in `domain-model.md` and the agent file in `instructions/subagents/`) renames `NNN_msg.md` → `NNN_<Slug>.md` and writes the slug into `index.json`. If the hook fires between the rename and the index.json commit, it sees `file: NNN_msg.md` missing but `slug: <Slug>` populated — this would otherwise resurrect a zombie file. The invariant: **if `slug` is set and the file at `entry.file` is missing, trust the slug and skip**.
- **Chapter-safe.** When `index.json` has multiple entries pointing at the same file (future chapter-merge), the hook detects shared files and leaves them untouched.

### Discipline

- The hook **reads its inputs from disk and the payload, writes files, exits**. No network calls, no agent invocations, no domain logic on Objectives.
- Cold-start budget: under 100 ms on a warm filesystem. `stdlib-only` Python keeps it light.
- **Safety check.** The hook refuses to run if `<cwd>/context.json` is absent — this prevents accidental folder creation when Claude Code is invoked outside a cockpit-managed context.
- Hooks never read or modify OBJ files. Anything touching the domain goes through MCP.
- Hooks do **not** fire on `claude -p` subprocesses in headless mode (project settings.json is not loaded). Useful side-effect: the indexer (which runs via `claude -p`) does not recursively trigger the Stop hook.

## 5. Deploy

The cockpit is installed into a Context by running:

```
python /path/to/Igor.source.git/cockpit/deploy/install.py <context_folder_path>
```

### What `install.py` does

1. Validates that `<context_folder>/context.json` exists (or creates a minimal one).
2. Creates `<context_folder>/.claude/` (and `sessions/` underneath).
3. Reads `settings.template.json` and substitutes placeholders with the absolute paths to:
   - MCP launch command (`bun run /…/Igor.source.git/cockpit/mcp/src/index.ts`);
   - the Stop hook (`python3 /…/Igor.source.git/cockpit/hooks/stop.py`).
4. Writes the result to `<context_folder>/.claude/settings.json`. If a `settings.json` already exists, merges hook and MCP entries without overwriting unrelated keys.
5. Creates empty `<context_folder>/objectives/` and `<context_folder>/journal/` if absent.
6. Reports what it did (created vs merged vs skipped).

The Igor.source.git repo's own absolute path is discovered relative to where `install.py` lives, so the script knows where its sibling `mcp/` and `hooks/` directories are.

### Per-context, per-Claude-Code-version

Settings files are scoped to one Context — different Contexts may have different versions of Igor installed if needed (e.g., during MCP migration). No global state on the user's machine; everything lives inside Context folders.

## 6. Out of scope (for now)

These belong to the design space but are deliberately deferred:

- **Autonomous protocol generation.** A separate agent that reads `transcript/` and writes `protocol.md` is planned but not specified here. When it lands, its spec joins `cockpit/specs/`.
- **Cross-context MCP / shared index.** Currently each Context's MCP is isolated. If global queries across all contexts become useful, a daemon-mode MCP (HTTP/SSE) is the path; for now, stdio per-context is enough.
- **External chat ingestion.** Imports from `claude.ai`, ChatGPT, Gemini land in SessionFolders manually for now; an importer that builds proper `transcript/NNN_msg.md` files from foreign formats is a future tool.
- **MCP tool API stability.** Tool names and signatures will firm up during the first implementation pass; this document captures intent, not yet a frozen contract.
