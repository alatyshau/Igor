# Stop Hook — Spec

Specification of the single Claude Code hook that persists session transcripts and maintains the SessionStateFile.

## Purpose

The Stop hook turns Claude Code's in-process JSONL into a durable, agent-readable on-disk record. On each fire it reconstructs the transcript including the first turn, writes per-turn files, and maintains `index.json`.

The hook also acts as the **bootstrap fallback** for the SessionStateFile: if no other component has created it yet (typically the UserPromptSubmit hook would, see *Bootstrap responsibility* below), Stop creates it on its first fire. If the SessionStateFile was pre-created by UserPromptSubmit *before* any user prompt was visible in the JSONL (so `first_prompt_id` is absent), Stop backfills it.

Bootstrap logic — both branches — is shared with `user_prompt_submit.py` via the `session_bootstrap` module so the two hooks stay in lockstep.

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

Path: `<SessionFolder>/transcript/NNN_msg.md`. Initial name uses `NNN_msg.md` (where `NNN` is the turn index, zero-padded 3 digits). A downstream component may later rename the file to `NNN_<slug>.md` (per-turn slug) or repoint multiple entries at a shared `NNN_<ChapterSlug>.md` (chapter-file). Per-turn slugging is currently performed by the `protocolist` subagent's Stage 1 in the same pass as chapter sealing — a separate slugging component is not shipped. Per-field ownership of `index.json` allows either model (one combined subagent or two separate ones); see *Field-level ownership* below.

File contents:

```markdown
## NNN <Title>                     ← H2; <Title> is added by the downstream slugging writer (see Per-turn file note above)

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
- `slug` — populated by a downstream slugging writer (currently `protocolist` Stage 1; see *Field-level ownership* below). The hook leaves it alone once set.
- `file` — points at the on-disk file for the turn. Multiple turn entries may share a `file` value when a downstream component groups consecutive turns into chapter-files (see `instructions/specs/protocolist_spec.md`).

`next_index` is `len(turns)` — recomputed and written by the hook on each fire as turns are reconciled from the JSONL.

#### Field-level ownership

`index.json` has multiple writers. Without explicit per-field ownership they would silently overwrite each other. The rule:

| Field | Writer | Mutability |
|---|---|---|
| `next_index` | Stop hook only | Recomputed every fire; never written by anyone else. |
| `turns[i].index` | Stop hook only | Set on first append; immutable. |
| `turns[i].prompt_id` | Stop hook only | Set on first append; immutable. Used for resumed-chat detection. |
| `turns[i].started_at` / `ended_at` | Stop hook only | Hook recomputes from the JSONL each fire. |
| `turns[i].complete` | Stop hook only | Hook flips `false → true` once the assistant finishes the turn. |
| `turns[i].midflight_count` / `assistant_count` | Stop hook only | Hook recomputes from the JSONL each fire. |
| `turns[i].slug` | Downstream slugging writer (currently the `protocolist` subagent's Stage 1, in the same pass as chapter sealing — a separate slugging component is not shipped) | Hook leaves it alone once set to a non-null value. Hook never resets a non-null `slug` to `null`. |
| `turns[i].file` | Initially Stop hook (`NNN_msg.md`); subsequently the `protocolist` subagent during chapter seal | Once `file` no longer points at the canonical `NNN_msg.md`, the hook treats the entry as **owned downstream** and will not resurrect the original file. See *Consistency invariants*. |

Writer discipline:

- Every writer must read-modify-write `index.json` atomically (temp + `os.replace`).
- The Stop hook recomputes its owned fields from the JSONL on every fire; any drift in those fields is healed by the next fire, so downstream writers must not depend on those fields holding values across runs.
- Downstream writers must touch only `slug` and/or `file`. Writing any other field is a contract violation and may be silently overwritten by the next hook fire.
- A new field added to `index.json` requires updating this table — without an explicit owner the field is undefined and may be dropped by any writer.

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

- `session_folder` — absolute path to the SessionFolder for this `session_id`. Always present.
- `first_prompt_id` — `prompt_id` of the first user turn. On resumed chats: Claude Code assigns a new `session_id`, but the first prompt's UUID is stable. On bootstrap the writing hook scans existing SessionFolders' `index.json` for a matching `turns[0].prompt_id` and reuses the folder. **May be absent** when the UserPromptSubmit hook bootstrapped the file before any user prompt was visible in the JSONL — in that case the Stop hook backfills it on its first fire (see *Bootstrap responsibility* below).

Derivable elsewhere (so not stored here):

- `session_id` is the filename itself.
- `started_at` lives in `index.json` as `turns[0].started_at`.

The MCP `rename_current_session` tool updates `session_folder` after `mv`-ing the folder. Because resumed chats produce **multiple SessionStateFile aliases** with the same `first_prompt_id` and `session_folder`, the tool must locate **every** alias (scan `<ContextFolder>/.claude/sessions/*.json` for files whose `first_prompt_id` matches the current session's, or equivalently whose `session_folder` matches the pre-rename path) and update all of them in one atomic pass. Leaving stale aliases would mean later resumes silently land on a non-existent path.

Alias bootstrap repair: on every fire, after locating its target SessionFolder, the hook compares its own SessionStateFile `session_folder` field against reality (does the path exist?). If the path is missing while the SessionFolder it should point at can be reached by `first_prompt_id` scan, the hook self-heals — rewrites the alias to the live path. This covers cases where a rename happened while this `session_id` was offline.

## Bootstrap responsibility

Two hooks may create the SessionStateFile + SessionFolder; they share `session_bootstrap.py`. The intent is that **UserPromptSubmit always wins** (it fires before the agent sees the message, so `state.md` is ready for Turn 1), and Stop is the **fallback** for when UserPromptSubmit is not registered, failed, or fired before the JSONL had any turns visible.

Both hooks call `session_bootstrap.ensure_session_state(context_root, session_id, first_prompt_id?, first_prompt_at?)`. It is idempotent: if the SessionStateFile already exists, it returns the parsed content untouched. Otherwise:

- if `first_prompt_id` is supplied, scan existing SessionFolders for a match (`first_prompt_id` survives resume; new `session_id` aliases the existing folder);
- otherwise create a fresh `journal/YYYY/MM/DD/HHMM_session/` using `first_prompt_at` (or `now()` when unknown).

Folder name HHMM may differ by seconds depending on which hook bootstrapped — irrelevant, the folder name is a label, not a key.

### Late-fill of `first_prompt_id`

When UserPromptSubmit bootstraps a brand-new chat, the JSONL is empty at hook-fire time and the written SessionStateFile has no `first_prompt_id`. On its first fire, after locating turns, Stop calls `session_bootstrap.fill_missing_first_prompt_id(sessions_dir, session_id, turns[0].prompt_id)` to atomically backfill the marker. Subsequent fires are no-ops on this branch (the field is already set).

## Lifecycle

1. **First fire (bootstrap or reconcile).**
   - If no SessionStateFile exists yet, Stop runs the bootstrap fallback through `session_bootstrap.ensure_session_state` (extracting `first_prompt_id` and `first_prompt_at` from the JSONL it just parsed). This handles the case where UserPromptSubmit is not registered.
   - If the SessionStateFile already exists (UserPromptSubmit got there first) but lacks `first_prompt_id`, Stop backfills it (see *Late-fill of `first_prompt_id`* above).
   - In both branches the folder scaffold (`state.md`, `transcript/index.json`) is materialised before transcript reconciliation proceeds.
2. **Subsequent fires.** Reads the SessionStateFile, walks the JSONL, computes the desired per-turn files, and reconciles.

## Reconciliation triggers

For each turn, the hook decides whether to (re)write the `NNN_msg.md` file. Action is triggered if any of:

- the file is missing **and** the index entry has no slug (i.e., no downstream slugging writer has claimed it — see *Consistency invariants* below);
- the entry's `complete` flag is `false`;
- `midflight_count` grew (a mid-flight message arrived);
- `assistant_count` grew (additional assistant turn was appended);
- legacy `[interrupted — no assistant response]` marker present.

If none of these hold, the turn is skipped — keeping cold paths cheap.

## Mid-flight messages

Messages the user sends *while the assistant is working* arrive in the JSONL as `type:"attachment"` entries with `attachment.type:"queued_command"`. They belong to the same turn (no separate `prompt_id`) and are rendered chronologically inside the turn file under `**User (mid-flight):**` headers.

## Consistency invariants

The hook does not take any inter-process lock. Claude Code serializes turn processing within a session; the hook fires only at turn boundaries; tool calls running inside turns and `claude -p` subprocesses (subchat-spawned subagents) do not trigger hook re-entry. Concurrent writers to the same `index.json` therefore do not occur in current design.

Two content-level invariants the hook honors:

- **Atomic writes.** `index.json` and any per-turn file are written via temp + `os.replace`. No partial writes are ever observable.
- **Multi-entry → shared file pattern.** Downstream components (e.g., the `protocolist` subagent) may merge consecutive turn entries into a chapter-file by pointing multiple `index.json` entries at the same `file` value. The hook detects this and leaves the shared file untouched. A per-turn `NNN_msg.md` whose `index.json` entry has `slug` set, or whose `file` field points at a chapter-file rather than `NNN_msg.md`, is **not resurrected** — even if the file is missing on disk.

  Required seal sequence for downstream chapter writers (full procedure and crash-recovery in [`schemas/session_protocol.md`](schemas/session_protocol.md) §Multi-file commit order): **(1)** write chapter-file (temp + atomic rename), **(2)** atomically rewrite `index.json` so every covered turn entry's `file` points at the chapter-file, **(3)** *optionally* delete the original `NNN_msg.md` files in the covered range. Step (3) is optional — once step (2) commits, the hook treats those entries as downstream-owned regardless of whether originals still exist on disk. Reversing the order (deleting originals before the index commit) would leave a window in which the hook resurrects them on the next fire.

## Discipline

- The hook **reads its inputs from disk and the payload, writes files, exits**. No network calls, no agent invocations, no domain logic on Objectives.
- Cold-start budget: under 100 ms on a warm filesystem. `stdlib-only` Python keeps it light.
- **Safety check.** The hook refuses to run if `<cwd>/context.json` is absent — this prevents accidental folder creation when Claude Code is invoked outside a cockpit-managed context.
- Hooks never read or modify OBJ files. Anything touching the domain goes through MCP.
