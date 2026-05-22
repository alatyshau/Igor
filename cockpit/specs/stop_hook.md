# Stop Hook — Spec

Specification of the single Claude Code hook that persists session transcripts and maintains the SessionStateFile.

## Purpose

The Stop hook turns Claude Code's in-process JSONL into a durable, agent-readable on-disk record. On each fire it reconstructs the transcript including the first turn, writes per-turn files, maintains `index.json`, and self-bootstraps the SessionStateFile pointer.

The hook is **idempotent** — re-firing on an unchanged JSONL is a no-op. It performs no domain logic on Objectives or any other entities.

## Trigger

| Event | What the hook does |
|---|---|
| `Stop` | Reads `transcript_path` from the payload, parses the JSONL, builds/reconciles the SessionFolder, writes per-turn `NNN_msg.md` files, updates `transcript/index.json`. Self-bootstraps on first fire. |

`SubagentStop` events arrive on the same script invocation. The hook accepts only events where `hook_event_name == "Stop"`; anything else is a no-op.

Two cases the hook must handle correctly:

- **Ghost sessions.** Claude Code spawns transient `session_id`s on IDE restart that never receive any prompt. Stop only fires when there is chat content, so ghosts never get SessionFolders.
- **Resumed chats.** When a chat resumes, Claude Code assigns a *new* `session_id` but the first user prompt — and its `prompt_id` — stays the same. The hook stores that `first_prompt_id` in the SessionStateFile; on bootstrap it scans existing SessionFolders for a matching `turns[0].prompt_id` and reuses the original folder instead of creating a duplicate.

The hook does **not** fire on `claude -p` subprocesses in headless mode (project `settings.json` is not loaded). Useful side-effect: subagents invoked via `claude -p` do not recursively trigger Stop.

## Produced artifacts

The hook produces three kinds of on-disk artifacts. Their formats are this component's contract.

### Per-turn file

Path: `<SessionFolder>/transcript/NNN_msg.md`. Initial name uses `NNN_msg.md` (where `NNN` is the turn index, zero-padded 3 digits). After async indexing (by a separate skill, not this hook) the file may be renamed to `NNN_<slug>.md`.

File contents:

```markdown
## NNN <Title>                     ← H2; <Title> is added by the indexer

**User:**

<user message>

**Assistant:**

<assistant response>

**User (mid-flight):**

<message the user sent while the assistant was still working>

**Assistant:**

<continued response>
```

Notes:

- The file-level header is **H2** so that concatenating all turn files (`cat 000_*.md 001_*.md … > merged.md`) yields a clean H2-chaptered document.
- `User` / `Assistant` / `User (mid-flight)` are bold paragraph labels, not headers — keeps the visual hierarchy clean when merged.
- Before indexing the title is just `## NNN`; after indexing the title becomes `## NNN <Title>`.
- Multiple `**Assistant:**` / `**User (mid-flight):**` blocks may interleave within a single turn file when mid-flight messages arrive. They are emitted in chronological order, preserving the real conversation flow.

### `index.json`

Path: `<SessionFolder>/transcript/index.json`. Counter and per-turn metadata maintained by the hook.

Schema:

```json
{
  "next_index": 3,
  "turns": [
    {
      "index": 0,
      "slug": null,
      "file": "000_msg.md",
      "prompt_id": "<UUID of the turn-defining user prompt>",
      "started_at": "2026-05-21T14:18:41.743Z",
      "ended_at": "2026-05-21T14:18:53.094Z",
      "complete": true,
      "midflight_count": 0,
      "assistant_count": 1
    },
    ...
  ]
}
```

Per-turn fields:

- `prompt_id` — UUID of the user prompt that opens the turn. Used by the hook to detect resumed chats.
- `started_at` / `ended_at` — ISO timestamps of the first user message and the last assistant message in the turn.
- `complete` — `true` once the assistant has produced its final response for the turn.
- `midflight_count` — number of mid-flight user messages within the turn (each rendered as a `**User (mid-flight):**` block).
- `assistant_count` — number of assistant message blocks within the turn.
- `slug` — populated by an async indexer (separate from this hook); the hook leaves it alone once set.
- `file` — points at the on-disk file for the turn. Multiple turn entries may share a `file` value when a downstream component groups consecutive turns into chapter-files (see `instructions/specs/session_protocol_spec.md`).

`next_index` is `len(turns)` — recomputed and written by the hook on each fire as turns are reconciled from the JSONL.

### SessionStateFile

Path: `<ContextFolder>/.claude/sessions/<session_id>.json`. A minimal pointer from a Claude Code `session_id` to the SessionFolder it owns.

Schema:

```json
{
  "session_folder": "/abs/path/to/journal/YYYY/MM/DD/HHMM_slug/",
  "first_prompt_id": "<UUID of turn 0>"
}
```

Fields:

- `session_folder` — absolute path to the SessionFolder for this `session_id`.
- `first_prompt_id` — `prompt_id` of the first user turn. On resumed chats: Claude Code assigns a new `session_id`, but the first prompt's UUID is stable. On bootstrap the hook scans existing SessionFolders' `index.json` for a matching `turns[0].prompt_id` and reuses the folder.

Derivable elsewhere (so not stored here):

- `session_id` is the filename itself.
- `started_at` lives in `index.json` as `turns[0].started_at`.

The MCP `rename_current_session` tool updates `session_folder` after `mv`-ing the folder.

## Lifecycle

1. **First fire (bootstrap).** No SessionStateFile exists yet. The hook reads the JSONL, takes `turns[0].prompt_id` as `first_prompt_id`, then:
   - scans `journal/YYYY/MM/DD/*/transcript/index.json` for a folder whose `turns[0].prompt_id` matches — if found, **reuse** (resumed-chat detection);
   - otherwise creates `journal/YYYY/MM/DD/HHMM_session/` using the timestamp of the first user prompt;
   - writes the minimal SessionStateFile.
2. **Subsequent fires.** Reads the SessionStateFile, walks the JSONL, computes the desired per-turn files, and reconciles.

## Reconciliation triggers

For each turn, the hook decides whether to (re)write the `NNN_msg.md` file. Action is triggered if any of:

- the file is missing **and** the index entry has no slug (i.e., the indexer hasn't claimed it — see *Concurrency model* below);
- the entry's `complete` flag is `false`;
- `midflight_count` grew (a mid-flight message arrived);
- `assistant_count` grew (additional assistant turn was appended);
- legacy `[interrupted — no assistant response]` marker present.

If none of these hold, the turn is skipped — keeping cold paths cheap.

## Mid-flight messages

Messages the user sends *while the assistant is working* arrive in the JSONL as `type:"attachment"` entries with `attachment.type:"queued_command"`. They belong to the same turn (no separate `prompt_id`) and are rendered chronologically inside the turn file under `**User (mid-flight):**` headers.

## Consistency invariants

The hook does not take any inter-process lock. Claude Code serializes turn processing within a session; the hook fires only at turn boundaries; skills running inside turns and `claude -p` subprocesses do not trigger hook re-entry. Concurrent writers to the same `index.json` therefore do not occur in current design.

Two content-level invariants the hook honors:

- **Atomic writes.** `index.json` and any per-turn file are written via temp + `os.replace`. No partial writes are ever observable.
- **Multi-entry → shared file pattern.** Downstream components (e.g., the `session-protocol` skill) may merge consecutive turn entries into a chapter-file by pointing multiple `index.json` entries at the same `file` value. The hook detects this and leaves the shared file untouched. A per-turn `NNN_msg.md` whose `index.json` entry has `slug` set, or whose `file` field points at a chapter-file rather than `NNN_msg.md`, is **not resurrected** — even if the file is missing on disk.

## Discipline

- The hook **reads its inputs from disk and the payload, writes files, exits**. No network calls, no agent invocations, no domain logic on Objectives.
- Cold-start budget: under 100 ms on a warm filesystem. `stdlib-only` Python keeps it light.
- **Safety check.** The hook refuses to run if `<cwd>/context.json` is absent — this prevents accidental folder creation when Claude Code is invoked outside a cockpit-managed context.
- Hooks never read or modify OBJ files. Anything touching the domain goes through MCP.
