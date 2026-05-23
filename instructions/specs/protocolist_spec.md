# `protocolist` subagent — Spec

Normative contract for the `protocolist` subagent: the conveyor that distills a session's raw transcript into chaptered transcript and curated `protocol.md`. This spec is the **source of truth for the conveyor behaviour**; the deployable subagent profile [`instructions/subagents/protocolist.md`](../subagents/protocolist.md) is an executable inscription of this contract, not the contract itself. If the two diverge, the spec wins.

This file is for humans, review agents, and the implementer of `protocolist.md`. The runtime audience of the profile is `claude -p` in headless mode with the profile body as full system prompt — the harness defaults are replaced, so every behavioural rule must be stated explicitly in the profile, and the spec must give the implementer enough constraint that two independent implementations cannot diverge in observable behaviour.

## Purpose

`protocolist` performs a two-stage transformation inside one SessionFolder:

```
msg-files → chapter-files → protocol.md
```

**Stage 1** groups consecutive per-turn files (`NNN_msg.md`) into semantic chapter-files (`NNN_<ChapterSlug>.md`), sealing covered entries in `transcript/index.json`.

**Stage 2** appends a curated narrative section for each sealed chapter to `<SessionFolder>/protocol.md`.

The subagent operates only on files inside its SessionFolder. It never touches `objectives/`, never edits OBJ index files, never modifies `## PROGRESS`. Continuity hand-off from `protocol.md` to `## PROGRESS` is the main agent's (Igor's) job — see [`instructions/igor.md`](../igor.md) §On Recognizing a Milestone.

## Out of scope

- **Auto-trigger.** The subagent runs only when invoked by the user via `!протокол` / `!протокол финиш`. Scheduled or turn-count-triggered runs are forbidden.
- **OBJ mutations.** Reading OBJ files is forbidden; writing them is forbidden. The conveyor is session-local.
- **Closure flow.** No path that effects `open → closed` on any Objective.
- **Network I/O.** No external calls, no MCP usage. Only filesystem and the model itself via `claude -p`.

## Conveyor

```
msg-files                    chapter-files                  protocol.md
NNN_msg.md           Stage 1     NNN_<ChapterSlug>.md   Stage 2   curated narrative
NNN_msg.md           ──────▶                            ──────▶   section per chapter
NNN_msg.md
```

Both stages run in a single invocation. Stage 1 may seal zero, one, or many chapters; Stage 2 appends exactly one protocol section per chapter sealed in Stage 1 *plus* any sealed chapters whose protocol sections are missing (the cleanup pass — see §Recovery).

File-level content schemas for chapter-files and `protocol.md` are in [`cockpit/specs/schemas/session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md). This spec specifies the **behaviour** of the conveyor.

---

## Stage 1 — msg-files → chapter-files

### Input

- `<SessionFolder>/transcript/index.json` — the authoritative list of turns and their state (see [`cockpit/specs/stop_hook.md`](../../cockpit/specs/stop_hook.md) §`index.json`).
- `<SessionFolder>/transcript/NNN_msg.md` — per-turn files for unprocessed turns.

### Output

- Zero or more `<SessionFolder>/transcript/NNN_<ChapterSlug>.md` chapter-files.
- Updated `<SessionFolder>/transcript/index.json` — covered turn entries have their `file` field repointed to the chapter-file. Format per [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §1.

### Invariants

- **Coverage is contiguous.** A chapter-file covers a contiguous range of turn indices `[NNN, NNN+k-1]`. Holes within a chapter are forbidden.
- **Coverage is partitioning.** Once an entry's `file` is repointed to a chapter-file, no later chapter may also claim it. Each turn entry belongs to at most one chapter.
- **Only `index.json.turns[i].file` is mutated by this subagent.** All other fields are owned by the Stop hook or the slugging subagent (see [`stop_hook.md`](../../cockpit/specs/stop_hook.md) §Field-level ownership).
- **Atomic writes.** Every file write (chapter-file, `index.json`, `protocol.md`) goes through `temp + os.replace`. Partial writes are never observable.

### Right-edge gap

Defines which turns are eligible for this run.

- A turn entry `turns[i]` is **complete** iff `turns[i].complete == true` (the assistant finished its turn — see [`stop_hook.md`](../../cockpit/specs/stop_hook.md) §`index.json`).
- A turn entry is **unprocessed** iff `turns[i].file == "NNN_msg.md"` (i.e., still points at the per-turn file, not at a sealed chapter-file).
- `max_complete_index` = the largest `i` for which `turns[i].complete == true`.
- `R` is the **right-edge gap** (default `R = 1` for `!протокол`, `R = 0` for `!протокол финиш`). The gap excludes the most recent complete turn from ordinary runs because mid-flight messages may yet be appended to it, growing `assistant_count` and changing the file content; sealing such a turn into a chapter would later require resealing.

A turn `turns[i]` is **eligible** for this run iff:

- `turns[i].complete == true`, **and**
- `i <= max_complete_index - R`, **and**
- `turns[i].file == "NNN_msg.md"` (i.e., still unprocessed).

If the latest turn is incomplete (`turns[len-1].complete == false`):

- `!протокол` (ordinary) — proceeds; `max_complete_index` simply excludes that turn.
- `!протокол финиш` — refuses with `[error] latest turn incomplete — rerun after Stop hook closes it`, exits non-zero. The user re-runs after the assistant completes a turn boundary.

### Batch boundary

Eligible turns are grouped into batches by a size budget on the raw msg-file bytes:

- **Lower threshold** — if total size of unprocessed eligible turns is **< 30 KB**, the run is a **no-op** (return cleanly with `[complete] no eligible content`). This avoids processing tiny tails that have not accumulated enough content for meaningful chapters.
- **Target batch size** — **50–80 KB** of unprocessed eligible content per invocation. The model receives this batch in one prompt.
- **Override** — `!протокол финиш` disables the lower threshold; any unprocessed eligible content is processed, including amounts below 30 KB.

Within a batch, the model decides chapter boundaries (no fixed turn-count quota) — see §Procedure step 3.

### Procedure

For one invocation of `!протокол` (or `!протокол финиш` — differences are inlined where they appear):

1. **Acquire lock.** Subchat has already acquired `<SessionFolder>/subchats/protocolist/run.lock` (see [`subchat.md`](../../cockpit/specs/subchat.md) §Concurrency). The subagent inherits the lock for the duration of its run.
2. **Cleanup pass.** Read `index.json` and scan for partial-seal states left by an earlier crashed run (see §Recovery). Repair each before proceeding to fresh sealing.
3. **Compute eligible turns.** Walk `index.json.turns`, apply the §Right-edge gap rule, collect the unprocessed eligible range. If total size < 30 KB and command is `!протокол` ordinary — exit no-op. Otherwise gather up to target batch size (or all of the eligible content for `финиш`).
4. **Chapter detection (LLM pass).** Read the batch of `NNN_msg.md` files. Issue one model prompt that returns a JSON array of chapter proposals:
   ```json
   [
     {"start_index": 12, "end_index": 17, "slug": "InterviewWHATWHY"},
     {"start_index": 18, "end_index": 23, "slug": "DesignDecomposition"}
   ]
   ```
   Each proposal covers a contiguous, non-overlapping range; ranges together cover the batch (no gaps); slugs follow SessionFolder slug rules (camelCase or snake_case, no dashes/dots).
5. **Validation.** Reject the model's proposal and refuse the run with `[error] invalid chapter proposal` if any of: ranges overlap, ranges have gaps, ranges extend outside the batch, slugs violate the rules, slug duplicates an already-sealed chapter slug in `transcript/`. No retry within one invocation — the user re-issues `!протокол` after inspecting `log/NN/`.
6. **Per-chapter seal.** For each validated proposal, in `start_index` order, execute the multi-file commit order from [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §Multi-file commit order:
   - (1) write chapter-file (temp + `os.replace`);
   - (2) atomically rewrite `index.json` to repoint covered entries' `file` field — this is the **seal commit**;
   - (3) *(optional)* delete original `NNN_msg.md` files.
7. **Hand off to Stage 2.** Each successfully sealed chapter is queued for Stage 2 narrative append.

### Streaming

The subagent must perform frequent observable actions during Stage 1 — at minimum: one `Read` per `NNN_msg.md` file in the batch (so subchat emits `[tool] Read` events), one assistant text chunk acknowledging the batch before issuing the model call (so subchat emits `[assistant]`), and one short text statement on completion of each chapter seal. Long silent stretches (> ~20s) where no tool call or assistant text appears are a defect.

---

## Stage 2 — chapter-files → protocol

### Input

- One or more sealed chapter-files from Stage 1 (queued during this invocation, plus any sealed chapter-files from earlier runs whose protocol sections are missing — see §Recovery).
- `<SessionFolder>/protocol.md` (may not exist on first invocation).

### Output

- One curated narrative section per chapter, appended to `protocol.md` `## Chapters`, in `start_index` order. Format per [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §2.
- On `!протокол финиш`: Abstract section written immediately after the H1 title and before `## Chapters`; `**Status:**` flipped to `завершена`.

### Invariants

- **Scaffold once.** If `protocol.md` does not exist, create the scaffold per [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §2 with `**Status:** в работе` and an empty `## Chapters`. Never rewrite the scaffold.
- **Append-only chapters.** Chapter sections are appended; existing chapter sections are not edited or reordered. (Editing the scaffold's `**Status:**` line during finalization is the single exception.)
- **One chapter section per sealed chapter.** Idempotence anchor: a chapter is considered already appended iff `## Chapters` contains a `### NNN — <ChapterSlug>` header matching the chapter's `start_index`. Stage 2 skips chapters that are already appended; this makes repeated `!протокол` calls safe (see [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §Idempotence).
- **Atomic writes.** Every write to `protocol.md` is temp + `os.replace`.

### Editorial bar

- **Final formulations only.** Intermediate proposals, cancelled paths, breadcrumbs "as we got here" — drop entirely. Density principle: dense on substance, drop everything else.
- **Key user prompts in full.** Where a user prompt is load-bearing (key inputs, decisions, rationale that the chapter pins down), quote it verbatim as a block quote with `> **Пользователь —** *(turn KKK)*` attribution. Only technical fixes — typos, formatting. Routine exchanges (acknowledgments, short clarifications) are not quoted.
- **Plain prose paragraphs.** No bullet lists, no event-log shape, no `[T07 ...]` code prefixes in the body. The chapter must read cold to a reader returning two weeks later.
- **No templated phrasing.** The model generates the narrative each chapter; no fixed opening / closing formulas.

### Procedure

For each chapter queued by Stage 1 (or recovered by the cleanup pass), in `start_index` order:

1. Read the chapter-file (`NNN_<ChapterSlug>.md`) and any context the model needs (the chapter-file is self-contained — it carries the full turn content).
2. Issue one model prompt to compose the chapter section. The prompt enforces the §Editorial bar.
3. Read current `protocol.md`. If absent, write the scaffold first (atomic).
4. Verify the chapter section is not already present (idempotence check by `### NNN — <ChapterSlug>` header). If present — skip; this is a benign re-run.
5. Append the new chapter section to `## Chapters`, preserving everything else in `protocol.md` verbatim. Atomic write.
6. Emit a short text statement for streaming.

### Finalization (`!протокол финиш`)

After all chapters are appended, the run is in finish mode:

1. Read current `protocol.md`.
2. If `**Status:**` is already `завершена` *and* Abstract exists *and* no new chapters were appended this run — no-op. Idempotent finish.
3. Otherwise, draft the Abstract (one to three paragraphs of compressed session essence) via one model prompt, insert it after the H1 title and before `## Chapters`, flip `**Status:**` to `завершена`. Atomic write.

After finish, ordinary `!протокол` calls against this `protocol.md` refuse with `[error] protocol finalized — explicit reopen required` (see [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §Idempotence). Reopen is manual: the user flips status back to `в работе` and removes Abstract.

---

## Commands

The subagent recognizes exactly two `--msg` values from subchat:

| Command | Effect |
|---|---|
| `!протокол` | Ordinary run. Stage 1 + Stage 2 with `R = 1` and the 30 KB lower threshold. |
| `!протокол финиш` | Finalization run. Stage 1 + Stage 2 with `R = 0`, no lower threshold, refuse if latest turn incomplete. Stage 2 adds Abstract and flips status. |

Any other input — refuse with `[error] unrecognized command: <input>`, exit non-zero. Do not interpret variants, do not translate, do not guess intent.

---

## Atomicity discipline (cross-stage)

Every file the subagent writes — `NNN_<ChapterSlug>.md`, `index.json`, `protocol.md` — goes through `temp + os.replace`. No partial Markdown or partial JSON is ever observable. Multi-file commits (one chapter spans up to four files) follow the order in [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §Multi-file commit order — every intermediate state is recoverable.

The subagent must not hold open file descriptors across model calls. File operations are short and bracketed by reads/writes.

---

## Recovery / cleanup pass

The cleanup pass at the start of every invocation (Stage 1 step 2) repairs any partial-seal state left by an earlier crashed run. Detection and repair, per [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §Multi-file commit order:

| Detected state | Repair |
|---|---|
| Orphan chapter-file (chapter-file on disk, no `index.json` entry points at it) | Delete the orphan; the next fresh run may attempt the same range. |
| Sealed entries, originals still present | No incorrectness; lazy-delete originals; proceed. |
| Sealed chapter, no matching `### NNN — <ChapterSlug>` header in `protocol.md` | Treat as queued for Stage 2; append the narrative section as if Stage 1 had just sealed it. |
| `### NNN — <ChapterSlug>` header in `protocol.md`, no sealed chapter in `index.json` | Treat as orphan header; refuse the run with `[error] protocol.md references unsealed chapter NNN — manual intervention required`. This is unrecoverable automatically and should not happen if the commit order is honoured. |

The cleanup pass must be deterministic: the same input state always produces the same repair, regardless of which command (`!протокол` or `!протокол финиш`) triggered the run.

---

## Subagent profile — required structure

The deployable profile [`instructions/subagents/protocolist.md`](../subagents/protocolist.md) must contain:

1. **Frontmatter** — YAML block at the top of the file. Required keys:
   - `name: protocolist`
   - `description:` — one-line description for catalogue / spawn lookup.
   - `model:` — default model (recommend `sonnet`).
   - `allowed-tools:` — explicit list, minimally `[Read, Write, Edit, Bash, Glob, Grep]`.
   - `output-format: stream-json` — mandatory; the user-facing `!протокол` contract requires live progress (see [`subchat.md`](../../cockpit/specs/subchat.md) §Stdout streaming contract).

   Optional keys (defaulted by subchat if absent): `max-turns`, `disallowed-tools`, `permission-mode`, `effort`.

2. **Body** — executable inscription of this spec. Recommended sections (the profile may reorganize but must cover the load-bearing content):
   - `# Protocolist` — H1 identity.
   - `## Input contract` — `--msg` argument, cwd is the SessionFolder, file paths under `transcript/` and `protocol.md`.
   - `## Commands` — exhaustive list of recognized `--msg` values, mirroring §Commands.
   - `## Conveyor` — one-paragraph overview + the canonical diagram.
   - `## Stage 1` — procedure mirroring §Stage 1 Procedure, with the §Right-edge gap and §Batch boundary rules quoted verbatim where load-bearing.
   - `## Stage 2` — procedure mirroring §Stage 2 Procedure, including the §Editorial bar.
   - `## Recovery` — cleanup pass per §Recovery.
   - `## Streaming` — discipline per §Stage 1 Streaming.
   - `## Bash discipline` — non-interactive commands only (`claude -p` headless has no TTY); the profile must explicitly forbid interactive prompts.
   - `## Anti-patterns` — short list of forbidden behaviours mirroring §Forbidden content below.

The profile must be self-contained — at runtime there is no spec-fetching mechanism. If something in this spec is load-bearing for execution, it must be inscribed in the profile body.

## Forbidden content (in the profile)

- **Auto-trigger logic.** No "process automatically every N turns". Only `!протокол` / `!протокол финиш`.
- **OBJ index mutations.** Subagent must not touch `objectives/.../index.md`. PROGRESS updates are Igor's responsibility, not protocolist's.
- **OBJ closure flow.** No path that effects `open → closed` on any Objective.
- **Templated narrative output.** No fixed phrasing for protocol chapters. The model generates the narrative each time.
- **Inline file format definitions.** Chapter-file and `protocol.md` content schemas live in [`cockpit/specs/schemas/session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) — single source of truth. The profile may quote a brief excerpt where load-bearing for execution, but never duplicates the schema in full and never diverges from it.
- **External I/O.** No network calls, no MCP usage. Subagent uses only its `allowed-tools` (filesystem + bash).
- **Bash interactive commands.** Since harness defaults are replaced via `--system-prompt`, the profile must explicitly forbid interactive Bash commands.
- **Non-atomic writes.** Direct `open(...).write(...)` to a final path is forbidden; every write must be temp + `os.replace` (or `Edit` against an existing file, which the tool implements atomically).

## Editorial bar (for the profile file itself)

- **Behavioural, not descriptive.** Every rule answers "what to do when event X happens".
- **3-whys per rule.** The "why does this matter?" chain must terminate in a concrete consumer / failure mode / system invariant.
- **Self-contained.** No runtime fetches of specs. If something is load-bearing, state it inline.
- **No defensive prose.** "Just in case the agent..." — drop it.
- **Length budget.** ≤ 500 lines (looser than `igor.md` because this is a specialized agent, not a general persona).

---

## Sources of truth

- [`cockpit/specs/schemas/session_folder.md`](../../cockpit/specs/schemas/session_folder.md) — session folder layout, `state.md` section ownership.
- [`cockpit/specs/schemas/session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) — content schemas for chapter-files and `protocol.md`, multi-file commit order, idempotence rules.
- [`cockpit/specs/stop_hook.md`](../../cockpit/specs/stop_hook.md) — `index.json` schema and field-level ownership.
- [`cockpit/specs/subchat.md`](../../cockpit/specs/subchat.md) — how this subagent is loaded and invoked; concurrency lock; streaming contract.
- [`cockpit/specs/domain-model.md`](../../cockpit/specs/domain-model.md) — OBJ entity semantics (referenced to understand what PROGRESS is, even though subagent does not touch it).
- [`cockpit/specs/architecture.md`](../../cockpit/specs/architecture.md) — system overview.

When `protocolist.md` and a spec disagree — the spec wins, `protocolist.md` is updated.

## Adequacy criteria (for review of `protocolist.md`)

`protocolist.md` is adequate to its purpose when:

1. Frontmatter is valid YAML with required keys (`name`, `description`, `model`, `allowed-tools`, `output-format: stream-json`).
2. Stage 1 and Stage 2 procedures are inscribed mirroring §Stage 1 / §Stage 2 here, with the §Right-edge gap rule and the §Batch boundary thresholds (30 KB / 50–80 KB / `R = 1`/`R = 0`) stated verbatim.
3. Both commands (`!протокол`, `!протокол финиш`) are documented with their concrete effects, including `финиш` refusing on incomplete latest turn.
4. The cleanup pass per §Recovery is inscribed with the four detected-state → repair pairs.
5. Atomicity discipline (§Atomicity) is explicit — every write is temp + `os.replace`; multi-file commit order matches [`session_protocol.md`](../../cockpit/specs/schemas/session_protocol.md) §Multi-file commit order.
6. Streaming discipline is explicit — frequent observable actions (tool calls, brief text statements) so the wrapping subchat has signals to surface; silent stretches > ~20s are a defect.
7. Subagent never modifies `objectives/` files. PROGRESS update path is explicitly stated as "Igor's responsibility, out of scope".
8. Bash discipline is explicit — non-interactive commands only.
9. No file format definitions are copy-pasted from schemas; cross-references used instead, with at most a load-bearing excerpt inline.
10. The full conveyor — `msg-files → chapter-files → protocol.md` — is described as one paragraph + the canonical diagram.

## Deploy

- **Source:** `Igor.source.git/instructions/subagents/protocolist.md`.
- **Context-local copy:** during `install.py` (see [`cockpit/specs/deploy.md`](../../cockpit/specs/deploy.md)), the source file is copied to `<ContextFolder>/.claude/cockpit/subagents/protocolist.md`. Deploy overwrites on every install — profile is source-of-truth in the repo. The path is explicitly *not* `.claude/agents/` because that triggers Claude Code's Custom Agent auto-discovery via the Task tool — a divergent execution path that would bypass subchat entirely.
- **Runtime materialization:** MCP `spawn_subchat(subagent="protocolist")` reads `<ContextFolder>/.claude/cockpit/subagents/protocolist.md`, splits frontmatter and body, generates `<SessionFolder>/subchats/protocolist/config.yaml` (from frontmatter + defaults per [`cockpit/specs/subchat.md`](../../cockpit/specs/subchat.md)) and `system_prompt.md` (the body).
