# Architecture

How Igor.cockpit is realized as software — its purpose, the functional capabilities it delivers, the components that make it work, and how they cooperate. Component-level specs are in dedicated files; this document does not re-state their internals.

## 1. Purpose

Igor.cockpit is a Context-bound cognitive scaffolding for a Claude Code agent. It addresses two structural failures of the bare Claude Code experience:

1. **Sessions are ephemeral.** The agent has no durable record of what was discussed, decided, or produced across turns and across sessions. Re-opening a chat means re-discovering the state.
2. **Forward-pointing items fall on the floor.** Without entity discipline, open questions, proposals, and commitments evaporate when not acted on immediately.

The cockpit replaces both: every session is persisted to disk as readable artifacts, and every forward-pointing item becomes a tracked entity with a state machine and a durable home on disk.

## 2. Functional capabilities

The cockpit delivers four capabilities to a Context:

- **Session persistence.** Every turn of every session becomes a per-turn file on disk. The journal accumulates across sessions, indexed and searchable.
- **Entity tracking.** Objectives, Issues, Suggestions, Tasks, and Problems are first-class on-disk entities with state machines, cascade rules, and an indexed Blocked-by graph (see `domain-model.md`).
- **Cognitive offload.** The procedural complexity of entity operations (code allocation, state transitions, atomic multi-file changes, graph invariants) is hidden behind named tool calls, freeing the agent's prompt for substance.
- **Cross-session continuity.** Each Context's accumulated knowledge — protocols of past sessions, OBJ progress, design specs — is loaded by the agent on session start so work resumes where it left off.

## 3. Maturity axis

Artifacts produced during work travel through three locations as they mature. This is the load-bearing structural axis of the system, orthogonal to the *time axis* (Objective forward / Journal backward) captured in the domain model.

| Location | Maturity | Lifetime |
|---|---|---|
| **SessionFolder** (`journal/.../HHMM_<slug>/`) | scratch, drafts, transient generations | session-bound; lives forever in the journal |
| **ObjectiveFolder** (`objectives/OBJxxx_<Slug>/`) | unripe deliverables, in-progress materials | from Objective creation until closure |
| **Git repository** (`<repo>/specs/`, `<repo>/src/`, …) | ripe, public, permanent | indefinite |

Promotion between layers is explicit:

- *Session → OBJ*: the user (or the agent on a user signal) moves a scratch artifact from the SessionFolder into the active ObjectiveFolder when it ceases to be one-off.
- *OBJ → git*: on Objective closure, ripe artifacts are `mv`-ed from the ObjectiveFolder into one of the git repositories registered in `context.json.git_repos`. Default promotion target is `<repo>/specs/`. Recorded with the `promoted!` event.

External inputs (briefs, ТЗ, PDFs, datasets) enter at the leftmost edge and live wherever the consumer requires.

## 4. Components

Each runtime component has a single focused responsibility. Detailed specs live in dedicated files; this section explains what each component is for and why it exists.

### 4.1 Hooks

Two Claude Code hooks together maintain the SessionFolder + SessionStateFile and persist every turn to disk. Both call into a shared `session_bootstrap` module so bootstrap logic stays in lockstep.

**UserPromptSubmit hook (`user_prompt_submit.py`).** Fires when the user submits a prompt, *before* the agent begins its turn. Ensures the SessionStateFile and SessionFolder exist for the current `session_id` so the agent has a valid `state.md` to operate on from Turn 1. Idempotent: re-firing is a no-op. On resumed chats (Claude Code generates a new `session_id` for an existing logical chat), reads the JSONL transcript to find `first_prompt_id` and aliases the new SessionStateFile to the original SessionFolder rather than creating a duplicate.

**Stop hook (`stop.py`).** Fires when the agent finishes a turn. Walks the JSONL and writes per-turn `NNN_msg.md` files into the SessionFolder's `transcript/`, maintaining `transcript/index.json`. Also acts as the **bootstrap fallback** when UserPromptSubmit is not installed/failed, and backfills `first_prompt_id` into the SessionStateFile when UserPromptSubmit pre-created it without one (the JSONL was empty at UserPromptSubmit time).

**Why they exist.** Claude Code's internal JSONL is opaque and not optimized for agent reads. Without persistence, the agent cannot reliably consult its own past turns, and there is no durable record of work for later sessions, audits, or downstream tooling (chapterizers, protocol generators). Splitting bootstrap from persistence into a pre-turn hook unblocks Turn 1 entity tracking — without UserPromptSubmit, the agent's first turn cannot write `state.md` because the file does not yet exist.

**Spec:** [`stop_hook.md`](stop_hook.md). Shared bootstrap logic lives in `cockpit/hooks/session_bootstrap.py`.

### 4.2 MCP server

**Responsibility.** Mediate all entity operations on the cockpit's domain (Objectives, tickets, session slug).

**Why it exists.** Entity operations carry non-trivial procedural complexity — code allocation, folder conventions, atomic multi-file changes, Blocked-by graph maintenance, cascade rules. Encoding them in the agent's prompt would consume context budget on procedure rather than substance, and would not be atomic. The MCP encapsulates them as named tool calls; the agent reasons about *what* to do, not *how*.

**How.** A per-context, per-session stdio MCP server (TypeScript) running as a child of Claude Code. Builds an in-memory index of `objectives/` at startup; serves tool calls atomically against the index and the filesystem.

**Spec:** [`mcp.md`](mcp.md).

### 4.3 Deploy

**Responsibility.** Install MCP + hook + agent persona + subchat into a Context folder.

**Why it exists.** A Claude Code Context is just a folder until configured. Deploy turns it into a cockpit-managed Context: writes the right `settings.json`, places the hook script, registers the MCP server, deploys the persona file, makes the subchat CLI available.

**How.** A Python script (`install.py`) called once per Context. Idempotent — re-runs update settings without overwriting user keys.

**Spec:** [`deploy.md`](deploy.md).

### 4.4 Subchat

**Responsibility.** Run named subagents in headless `claude -p` mode under the current SessionFolder, with isolation and progress streaming.

**Why it exists.** When the main agent (Igor) delegates work to another agent — protocolist now, advisor/coder/reviewer in the future — every such use case needs the same plumbing: claude command construction, `session_id` management, per-subagent config and state on disk, isolation from Igor's `.claude/`, live progress to the parent. Subchat centralizes this so the main agent's prompt and downstream subagent profiles stay focused on substance.

**How.** A Python CLI (`subchat.py`) invoked by Igor via Monitor. Reads the per-subagent config from `<SessionFolder>/subchats/<subagent>/config.yaml`, spawns `claude -p` with appropriate flags and cwd, streams progress events to stdout, captures full output to `log/NN/`. Subagent profiles are authored at `instructions/subagents/<name>.md` in the source repo; deploy copies them to `<ContextFolder>/.claude/cockpit/subagents/<name>.md` per-Context (not `.claude/agents/` — that path triggers Claude Code's Custom Agent auto-discovery and would let Task-tool calls bypass subchat); MCP `spawn_subchat` reads from there and materializes into the SessionFolder.

**Spec:** [`subchat.md`](subchat.md).

The persona file `instructions/igor.md` is a *product*, not a runtime component — see [`../../instructions/specs/igor_spec.md`](../../instructions/specs/igor_spec.md). Subagent profiles (e.g., `instructions/subagents/protocolist.md`) are also products with their own specs in `instructions/specs/`.

## 5. Interactions

How the components cooperate at runtime in a typical Context:

1. **Context creation.** User invokes `install.py` against a folder. Deploy writes `settings.json`, scaffolds `objectives/` and `journal/`, places the persona at `.claude/output-styles/igor.md`.

2. **Session start.** User opens Claude Code in the Context. Claude Code reads `settings.json`, spawns the MCP server (per-context), loads the persona. The agent is alive.

3. **Each turn.** User submits a message → Claude Code fires the **UserPromptSubmit** hook, which ensures the SessionStateFile + SessionFolder exist (creates them on the first turn of a brand-new chat, aliases to the original on a resumed chat). Agent receives the message and responds. On turn close, Claude Code fires the **Stop** hook, which writes the turn to `transcript/NNN_msg.md`, updates `index.json`, and backfills `first_prompt_id` into the SessionStateFile if absent.

4. **Entity operations.** Agent decides to create an Objective, transition a state, allocate a ticket code. It calls an MCP tool, passing its `CLAUDE_CODE_SESSION_ID` (from env) as the `session_id` argument. The tool atomically writes to `objectives/` or to the current session's `state.md`. The agent's prompt does not carry the procedural recipe.

5. **Subagent delegation.** On user commands like `!протокол`, the agent delegates work to a named subagent. First time: MCP `spawn_subchat(subagent=...)` materializes `<SessionFolder>/subchats/<name>/` with `config.yaml` + `system_prompt.md`. Then (and on subsequent invocations) the agent runs `subchat --subagent <name> --msg "<verbatim user command>"` via the Monitor mechanism. Subchat spawns `claude -p` in the SessionFolder (isolated from Igor's `.claude/`), streams progress events to stdout. The agent watches the stream and surfaces it to the user. The subagent's output (chapter-files, `protocol.md`, etc.) appears in the SessionFolder; OBJ-level state (`## PROGRESS` sections) is updated by the main agent later, on user request — not by the subagent.

6. **Session end.** User closes the chat. SessionFolder remains in the journal, including all subchat history under `subchats/`.

The components are **loosely coupled**: MCP knows entities and spawns subchats; the hook knows transcripts; deploy knows installation; subchat runs subagents. They share the on-disk layout described in `schemas/`; they do not call each other directly except via MCP's `spawn_subchat` triggering subchat folder creation.

There is no inter-process locking: Claude Code serializes turn processing within a session (the hook fires at turn boundaries; subagent subprocesses do not trigger hooks). The only coordination needed is content-level consistency between writers — handled by atomic writes (temp + rename) and the multi-entry → shared file pattern in `index.json` (see `stop_hook.md`).

## 6. Component layout

The cockpit ships as three runtime components inside one source repo, plus a deploy step.

```
Igor.source.git/
  cockpit/
    specs/                         ← design specs (this folder)
      architecture.md              ← this file
      mcp.md
      stop_hook.md
      deploy.md
      subchat.md
      domain-model.md
      schemas/                     ← on-disk folder layouts
    mcp/                           ← MCP implementation (TypeScript, Node v22+)
    hooks/                         ← Claude Code hooks (Python)
      session_bootstrap.py         ← shared SessionStateFile + SessionFolder bootstrap
      user_prompt_submit.py        ← pre-turn hook (ensures state.md before agent's Turn 1)
      stop.py                      ← post-turn hook (persist transcript + late-fill first_prompt_id)
    deploy/                        ← install.py + settings.template.json
    subchat/                       ← subchat CLI (Python)
  instructions/                    ← agent persona + skills (deployed, not part of runtime)
    igor.md                        ← persona (output style)
    specs/                         ← product specs for instructions/ artifacts
    skills/                        ← Anthropic-format skills
    subagents/                     ← invokable subagents
```

## 7. Out of scope (system-level)

- **Cross-context MCP / shared index.** Currently each Context's MCP is isolated. Global cross-context queries would require a daemon-mode MCP (HTTP/SSE).
- **External chat ingestion.** Imports from claude.ai, ChatGPT, Gemini land in SessionFolders manually; an importer that builds proper `transcript/NNN_msg.md` files from foreign formats is a future tool.
- **MCP tool API stability.** Tool names and signatures will firm up during the first implementation pass — `mcp.md` captures intent, not a frozen contract.
