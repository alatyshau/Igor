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

### 4.1 Stop hook

**Responsibility.** Persist every Claude Code turn to disk and maintain the transcript index.

**Why it exists.** Claude Code's internal JSONL is opaque and not optimized for agent reads. Without persistence, the agent cannot reliably consult its own past turns, and there is no durable record of work for later sessions, audits, or downstream tooling (chapterizers, protocol generators).

**How.** A single `Stop` hook fires on each turn close, reads `transcript_path` from the payload, writes `NNN_msg.md` files into the SessionFolder's `transcript/`, and maintains `transcript/index.json` + the SessionStateFile.

**Spec:** [`stop_hook.md`](stop_hook.md).

### 4.2 MCP server

**Responsibility.** Mediate all entity operations on the cockpit's domain (Objectives, sub-entities, session slug).

**Why it exists.** Entity operations carry non-trivial procedural complexity — code allocation, folder conventions, atomic multi-file changes, Blocked-by graph maintenance, cascade rules. Encoding them in the agent's prompt would consume context budget on procedure rather than substance, and would not be atomic. The MCP encapsulates them as named tool calls; the agent reasons about *what* to do, not *how*.

**How.** A per-context, per-session stdio MCP server (TypeScript) running as a child of Claude Code. Builds an in-memory index of `objectives/` at startup; serves tool calls atomically against the index and the filesystem.

**Spec:** [`mcp.md`](mcp.md).

### 4.3 Deploy

**Responsibility.** Install MCP + hook + agent persona into a Context folder.

**Why it exists.** A Claude Code Context is just a folder until configured. Deploy turns it into a cockpit-managed Context: writes the right `settings.json`, places the hook script, registers the MCP server, deploys the persona file.

**How.** A Python script (`install.py`) called once per Context. Idempotent — re-runs update settings without overwriting user keys.

**Spec:** [`deploy.md`](deploy.md).

The persona file `instructions/igor.md` is a *product*, not a runtime component — see [`../../instructions/specs/igor_spec.md`](../../instructions/specs/igor_spec.md).

## 5. Interactions

How the components cooperate at runtime in a typical Context:

1. **Context creation.** User invokes `install.py` against a folder. Deploy writes `settings.json`, scaffolds `objectives/` and `journal/`, places the persona at `.claude/output-styles/igor.md`.

2. **Session start.** User opens Claude Code in the Context. Claude Code reads `settings.json`, spawns the MCP server (per-session), loads the persona. The agent is alive.

3. **Each turn.** User sends a message; agent responds. On turn close, Claude Code fires the Stop hook, which writes the turn to `transcript/NNN_msg.md` and updates `index.json`.

4. **Entity operations.** Agent decides to create an Objective, transition a state, allocate a sub-entity code. It calls an MCP tool. The tool atomically writes to `objectives/`. The agent's prompt does not carry the procedural recipe.

5. **Skill-driven downstream work.** Agent invokes a skill (e.g., `!протокол`). The skill consumes Stop hook outputs (`transcript/`) and produces its own artifacts (chapter-files, `protocol.md`). Some skills may also update agent-managed sections (e.g., `## PROGRESS` in OBJ index files) as a secondary path — the agent is the primary writer of those sections; the skill backs the agent up when needed. Each skill operates within boundaries declared in its own spec; none effects OBJ state transitions directly.

6. **Session end.** User closes the chat. SessionFolder remains in the journal. The next session opens with `state.md` scope and PROGRESS sections of in-scope OBJ already loaded.

The components are **loosely coupled**: MCP knows entities, the hook knows transcripts, deploy knows installation. They share the on-disk layout described in `schemas/`; they do not call each other directly.

There is no inter-process locking: Claude Code serializes turn processing within a session (the hook fires at turn boundaries; skills run inside turns; `claude -p` subprocesses do not trigger hooks). The only coordination needed is content-level consistency between writers — handled by atomic writes (temp + rename) and the multi-entry → shared file pattern in `index.json` (see `stop_hook.md`).

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
      domain-model.md
      schemas/                     ← on-disk folder layouts
    mcp/                           ← MCP implementation (TypeScript, Node v22+)
    hooks/                         ← Claude Code hooks (Python)
    deploy/                        ← install.py + settings.template.json
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
