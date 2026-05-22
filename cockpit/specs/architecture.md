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
- *OBJ → git*: on Objective closure, ripe artifacts are `mv`-ed from the ObjectiveFolder into one of the git repositories registered in `context.json.git_repos`. Local paths are provided by Duet (`mcp__duet__orientation` returns `workspace.git_folders` — name → absolute path). Default promotion target is `<repo>/specs/`. Recorded with the `promoted!` event.

External inputs (briefs, ТЗ, PDFs, datasets) enter at the leftmost edge and live wherever the consumer requires — usually outside this maturity axis, referenced from `state.md` `## Input` if relevant to the current session.

## 2. Component layout

The cockpit ships as three components inside one source repo, plus a deploy step.

```
Igor.source.git/
  cockpit/
    specs/                         ← design specs (domain-model, schemas, architecture)
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
  instructions/                    ← agent persona + skills (deployed but not part of cockpit runtime)
    igor.md                        ← persona (output style)
    skills/                        ← optional capabilities
    subagents/                     ← invokable subagents (e.g. transcript indexer)
```

The split is deliberate:
- **MCP is the cognitive-offload layer for the agent.** It encapsulates procedural complexity (code allocation, folder conventions, state machines, graph invariants, atomic file operations) so the agent calls a single named tool instead of carrying the recipe in its system prompt.
- **Hooks are dumb pipes** — they read Claude Code's payload, write a file, exit. No domain logic. Python is the implementation language.
- **`instructions/` is consumed by deploy, not by the runtime.** The deploy script copies / templates `igor.md` into the Context's `.claude/output-styles/`; skills and subagents are registered in `settings.json`. After deploy, the cockpit runtime (MCP + hook) does not read this directory.

## 3. MCP server

### Transport and lifecycle

- **Transport:** `stdio` (Claude Code spawns the MCP as a child process per session).
- **Scoping:** **per-context, per-session.** The MCP inherits `cwd` from the parent Claude Code process (the ContextFolder root) and `CLAUDE_CODE_SESSION_ID` as an env var (Claude Code injects it into every spawned subprocess automatically). To operate on the current session's `state.md`, MCP reads `./.claude/sessions/<CLAUDE_CODE_SESSION_ID>.json` (the SessionStateFile, maintained by the Stop hook) lazily on first state.md operation and follows its `session_folder` field. The MCP reads and writes `./objectives/` and that session's `state.md`. Historical journal is not touched. If the SessionStateFile does not exist yet (pre-first-hook-fire), MCP returns a recoverable error — the operation succeeds once the hook has run.
- **Stack:** TypeScript with the official `@modelcontextprotocol/sdk`. Run via `bun` for fast startup (~50ms).

Per-context, per-session: each Claude Code chat in each Context has its own MCP instance, fully isolated. State the MCP holds in memory survives LLM context compaction — only the LLM context is compressed; the MCP process keeps running.

### Index build at startup

On startup, the MCP scans `objectives/` (all subfolders including `closed/`, `cancelled/`, `backlog/`) and constructs:
- a map of all Objectives by code and by slug;
- the Blocked-by dependency graph (DAG);
- the next available ObjectiveCode (`max + 1`).

The journal is not scanned at startup — no tool consumes that index.

### Tool surface (initial)

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

## 4. Hooks

Hooks live as Python scripts (stdlib only). Claude Code spawns the hook on each event with a JSON payload on stdin.

### Events

The cockpit uses a single `Stop` hook. On each fire it has full JSONL access, reconstructs the transcript including the first turn, and writes / reconciles the SessionFolder. The hook is **idempotent** — re-firing on an unchanged JSONL is a no-op.

| Event | What the hook does |
|---|---|
| `Stop` | Reads `transcript_path` from the payload, parses the JSONL, builds/reconciles the SessionFolder, writes per-turn `NNN_msg.md` files, updates `transcript/index.json`. Self-bootstraps on first fire. |

`SubagentStop` events arrive on the same script invocation. The hook accepts only events where `hook_event_name == "Stop"`; anything else is a no-op.

Two cases the hook must handle correctly:

- **Ghost sessions.** Claude Code spawns transient `session_id`s on IDE restart that never receive any prompt. Stop only fires when there is chat content, so ghosts never get SessionFolders.
- **Resumed chats.** When a chat resumes, Claude Code assigns a *new* `session_id` but the first user prompt — and its `prompt_id` — stays the same. The hook stores that `first_prompt_id` in the SessionStateFile; on bootstrap it scans existing SessionFolders for a matching `turns[0].prompt_id` and reuses the original folder instead of creating a duplicate.

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
- **Race-safety with the indexer.** The transcript indexer (subagent at `instructions/subagents/transcript_indexer.md`, invoked via `claude -p`) renames `NNN_msg.md` → `NNN_<Slug>.md` and writes the slug into `index.json`. If the hook fires between the rename and the `index.json` commit, it sees `file: NNN_msg.md` missing but `slug: <Slug>` populated — this would otherwise resurrect a zombie file. The invariant: **if `slug` is set and the file at `entry.file` is missing, trust the slug and skip**.
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

1. **Validate / scaffold `context.json`.** If absent, write a minimal one (`name`, `cockpit_config`). If present, leave user-owned keys untouched.
2. **Create `.claude/` tree.** `<context_folder>/.claude/sessions/` and `<context_folder>/.claude/output-styles/`.
3. **Render `settings.json` from `settings.template.json`.** Substitute placeholders with absolute paths discovered relative to `install.py`'s own location (sibling `mcp/`, `hooks/`):

   ```json
   {
     "outputStyle": "igor",
     "hooks": {
       "Stop": [
         {"hooks": [{"type": "command", "command": "python3", "args": ["/…/cockpit/hooks/stop.py"]}]}
       ]
     },
     "mcpServers": {
       "igor": {
         "command": "bun",
         "args": ["run", "/…/cockpit/mcp/src/index.ts"]
       }
     }
   }
   ```

   If a `settings.json` already exists, merge `hooks` and `mcpServers` entries without overwriting unrelated keys.
4. **Deploy persona.** Read `instructions/igor.md` from source; prepend a localization paragraph rendered from `context.json.cockpit_config.localization` (single string with language, user name, timezone); write to `<context_folder>/.claude/output-styles/igor.md`. Source `igor.md` itself ships without personal data — public repo.
5. **Create empty `objectives/` and `journal/`** if absent.
6. **Report** what was created, merged, or skipped.

The Igor.source.git repo's own absolute path is discovered relative to where `install.py` lives — no environment variables, no Duet dependency at install time. Duet's role (per `mcp__duet__orientation`) is at runtime: the agent uses it to resolve `git_folders` paths for promotion.

### Per-context, per-Claude-Code-version

Settings files are scoped to one Context — different Contexts may have different versions of Igor installed if needed (e.g., during MCP migration). No global state on the user's machine; everything lives inside Context folders.

## 6. Out of scope (for now)

These belong to the design space but are deliberately deferred:

- **Autonomous protocol generation.** A separate agent that reads `transcript/` and writes narrative `protocol.md`. Not yet specified.
- **Cross-context MCP / shared index.** Currently each Context's MCP is isolated. Global cross-context queries would require a daemon-mode MCP (HTTP/SSE).
- **External chat ingestion.** Imports from `claude.ai`, ChatGPT, Gemini land in SessionFolders manually; an importer that builds proper `transcript/NNN_msg.md` files from foreign formats is a future tool.
- **MCP tool API stability.** Tool names and signatures will firm up during the first implementation pass — this document captures intent, not a frozen contract.
