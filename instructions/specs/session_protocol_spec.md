# `session-protocol` SKILL — Spec

Specification of the product [`instructions/skills/tools/session-protocol/SKILL.md`](../skills/tools/session-protocol/SKILL.md) — the skill that drives the SessionProtocolist conveyor (`msg-files → chapter-files → protocol → progress`).

This spec is for **humans and review agents** who maintain or audit the skill. Layering per [`igor_spec.md`](igor_spec.md).

## Purpose

`SKILL.md` is a skill loaded on demand when the user invokes `!протокол`. It directs the agent to drive a 4-node conveyor that distills the session's raw transcript into a protocol and a corresponding update to OBJ index files:

```
msg-files → chapter-files → protocol → progress
```

The protocol enables three downstream consumers: (a) **absorption** by other agents or new sessions, (b) **continuity** when resuming work on the same Objective in a future session, (c) **browsability** of the journal folder.

Audience of the product: the agent itself with the skill loaded. Skills are loaded on demand, so per-line cost is lower than for `igor.md`, but discipline still applies.

## Consumer model

The skill produces three artifacts, distributed across the SessionFolder and any touched OBJ folders:

1. **Chaptered transcript** in the SessionFolder (`<SessionFolder>/transcript/`) — full record at per-turn granularity, grouped into semantic chapter-files (format below in §Artifacts produced).
2. **Protocol** in the SessionFolder (`<SessionFolder>/protocol.md`) — curated narrative of decisions and rules (per `cockpit/specs/schemas/session_protocol.md`).
3. **PROGRESS section** in each touched OBJ folder (`objectives/OBJxxx_<Slug>/index.md` §`## PROGRESS`, per `cockpit/specs/schemas/obj_folder.md` §5) — distilled accumulation of what's been done.

Two consumer audiences with different reading patterns:

- **Human.** Enters at varying depth — PROGRESS for «what came out», protocol for «what happened and why», chaptered transcript for full context when needed.
- **AI agent.** Most often reads PROGRESS to know what's been done and continue OBJ work. Reads protocol on demand when continuation needs more context. Reaches into chaptered transcript via grep only in extreme cases.

PROGRESS is therefore the **primary continuity surface**. The skill must keep PROGRESS sufficient on its own for an agent to pick up OBJ work without reading protocol or transcript.

## Artifacts produced

Three on-disk artifacts; their formats are the skill's responsibility (the schemas in `cockpit/specs/schemas/` describe only folder layout, not the contents the skill produces).

### Chapter-file

**Path.** `<SessionFolder>/transcript/NNN_<ChapterSlug>.md` — same shape as a per-turn file, with multiple turns inside. The index `NNN` is the first turn's index (3-digit zero-padded); the slug is the chapter's. Example: `015_PromotionDecisionAndAftermath.md` opens at turn 15.

**Slug rules.** `camelCase` or `snake_case`; no dashes or dots; ≤ 30 characters; unique within the session folder.

**Content layout.**

```markdown
# <Chapter Title>

<1-2 sentence abstract — ≤ 300 characters>

## NNN <Title of turn N>

[turn N content]

## (N+1) <Title>

[turn N+1 content]

...
```

- H1 — chapter's human-readable title (matches `title` from the model response in Stage 1 procedure).
- Abstract paragraph under H1 — 1-2 sentences, ≤ 300 characters.
- Concatenated per-turn content follows, each turn preserving its `## NNN <Title>` H2 header.

**`index.json` interaction.** After sealing, each turn in the chapter range keeps its own entry in `index.json`; their `file` fields all point at the chapter-file (multi-entry → shared file). No additional chapter-pointer fields — the chapter is the set of entries sharing a `file` value.

### Protocol

**Path.** `<SessionFolder>/protocol.md` — one file per session.

**Required structure.**

```markdown
# <Meta-objective in human language>

**Дата:** YYYY-MM-DD
**Статус:** активна | завершена | прервана
**Режим:** <session engagement mode>

## Abstract

<academic form: problem → method → results → impact>

## Оглавление

1. <Chapter 1 title>
2. <Chapter 2 title>
...

## Глава 1. <Title>

<chapter body>

---

## Глава N. <Title>

<chapter body>
```

**Sections.**

- H1 — meta-objective of the session in full human-language phrase (not the SessionFolder slug).
- Metadata block — three labels (`Дата`, `Статус`, `Режим`); `Статус` is one of three constants.
- `## Abstract` — academic form: problem → method → results → impact. Not narrative.
- `## Оглавление` — numbered list matching the chapters below; substantive human-language titles.
- `## Глава N. <Title>` — chapters in numerical order.

The protocol does **not** duplicate the OBJ/PROB context list — that lives in `state.md`.

**Chapter format.**

- Narrative, factual tone. Reader is a future maintainer recovering what happened.
- **Key user prompts — full direct quote.** When a user prompt lays out important inputs, reasoning, or a formulation that becomes part of what the chapter records, quote it in full with only minor technical fixes. Routine exchanges are not quoted at this level. Format:

  ```
  ---

  *Андрей:*

  [full quoted text]

  ---
  ```

- Short inline quotes (≤ 1 paragraph) — normal blockquote (`> ...`).
- End of chapter — `**Извлечённое правило**` or `**Зафиксированное решение**` with explicit pointer (e.g., `OBJ003 → раздел Поведенческие правила, #5`).

**Content rules.**

- **Final formulations only.** Settled rules and decisions land in the protocol; the path to them — intermediate proposals, abandoned options, breadcrumbs of "how we got there" — does not.
- **Density principle: depth on essentials, drop the non-essential entirely.** When a part matters, full detail. When not, simply absent.
- Not the verbatim transcript (that duplicates `transcript/`).
- Not a summary of summaries — each chapter carries re-readable substance.
- Chapters as standalone units — chapter 7 is readable without 1–6.
- Cross-references by OBJ name, not enumerated sub-entity lists.

**What must NOT be in `protocol.md`.**

- System reminders, hook outputs, tool-call ephemera.
- Full tool calls — at most a one-line summary.
- Intermediate edits of a single decision — only the final formulation.
- Frozen snapshot of the OBJ/PROB context list — that lives in `state.md`.

**Create vs update.**

- *Create.* File does not exist; generate full structure.
- *Update.* File exists; append new chapters; refresh `## Abstract` only if scope shifted materially.


### PROGRESS section

**Path.** `## PROGRESS` section inside `objectives/OBJxxx_<Slug>/index.md`. Its function, style, and ordering are specified in `cockpit/specs/schemas/obj_folder.md` §5.

**Role of this skill.** The skill is a **secondary writer** of PROGRESS. The primary writer is the runtime agent during a live session (per `instructions/igor.md` §On Recognizing a Milestone). The skill writes PROGRESS only when the agent did not maintain it during the session, or when running a from-scratch backfill from transcript.

**When the skill writes.** Skill-written paragraphs follow the same content discipline as agent-written ones: clear brief narrative, appended chronologically (newest entry at the bottom). Each skill-written paragraph is tagged with the source turn range (e.g., `[turns 23–34] ...`) so a reader can trace it back to the chaptered transcript.

## Execution model

The skill is the unit of distribution and reuse. It lives at `instructions/skills/tools/session-protocol/` and consists of `SKILL.md` (the agent-facing prompt) plus any supporting scripts in the same folder (per Anthropic skill convention — scripts that the skill calls live inside the skill folder).

Invocation patterns:

- **Direct.** The agent in the active session recognizes `!протокол`, loads `SKILL.md`, executes the procedure inline using its standard tool surface (Bash, Read/Edit/Write, `claude -p` for subprocess LLM calls).
- **Subagent.** The agent delegates the work to a subagent via the `Task` tool, passing a prompt that references this skill. The subagent executes the procedure and returns a summary. Useful when the main session must preserve context budget.

Both patterns invoke the same `SKILL.md` against the same procedural contract specified below.

The skill is **stateless across invocations** — every run derives its state from the on-disk artifacts (session folder, `index.json`, OBJ folders). No in-memory state is preserved between runs.

## Stage 1 procedure — msg-files → chapter-files

Input: unprocessed `NNN_msg.md` files in `<SessionFolder>/transcript/`. Output: chapter-files (per §Artifacts produced above) plus updated `index.json` entries (schema in `cockpit/specs/stop_hook.md`).

### Default parameters

- **Batch size:** 50–80 KB of unprocessed msg-files. Lower bound: 30 KB (below that — no-op).
- **Prompt norm:** ~10 KB per chapter on average (a typical batch yields 5–8 chapters).
- **Right-edge gap:** `max(5 KB, 1 whole msg-file)`.
- **Invocation forms:**
  - `!протокол` — process the next batch with default parameters above.
  - `!протокол финиш` — finalization mode: right-edge gap is removed, the 30 KB lower bound is waived, all accumulated unprocessed msg-files are processed. Used when the session has reached a state where no further turns are expected.

### Run sequence

1. **Cleanup pass** — reconcile folder against `index.json` (see below).
2. **Read** the batch — first 50–80 KB of unprocessed msg-files from the cursor (the first turn whose `index.json` entry has `file = NNN_msg.md`). If less than 5 KB of unprocessed content — no-op, exit.
3. **Model call** — produce per-turn slugs+titles and chapter ranges (see *Model response validation*). May be one model call or two sequential calls (one for per-turn metadata, second for chapters) — both produce identical on-disk results.
4. **Validate** the response. On validation failure: abort, no on-disk mutations.
5. **Seal** each qualifying chapter via the three-step atomic sequence (see *Atomicity of sealing*). Chapters that fail the right-edge gap rule are not sealed.

No inter-process lock is taken — Claude Code serializes turn processing, the Stop hook only fires at turn boundaries, and the skill runs inside a turn. Content consistency relies on atomic writes (temp + rename for `index.json` and chapter-files) and the multi-entry → shared file pattern (see `cockpit/specs/stop_hook.md` §Consistency invariants).

### Cleanup pass

Inside the lock, before any model call, the producer reconciles the folder against `index.json`:

- **Orphan chapter-files** (file on disk with chapter-shaped name, not referenced by any entry in `index.json`) — deleted.
- **Redundant msg-files** (a turn entry in `index.json` already has `file` pointing at a chapter-file, but the original `NNN_msg.md` still exists on disk) — the `NNN_msg.md` is deleted.

Each cleanup action is surfaced in the agent's chat as a one-line operational note (e.g., `cleanup: removed orphan chapter-file 015_PromotionDecision.md`). The line carries no `!` entity-marker prefix — cleanup is operational, not an entity state change. Silent cleanup is forbidden — the operator must see what was reconciled.

### Seal rule — right-edge gap

In **default invocation** (`!протокол`): a chapter from the model response is **sealed** only if the gap between its `end_turn` and the right edge of the batch's processed material satisfies both: (a) the gap is at least 5 KB of byte content, and (b) the gap contains at least one whole msg-file beyond the chapter's `end_turn`. The binding threshold is whichever of the two is more restrictive.

Chapters within the tail that fails either condition remain **unsealed**: their msg-files stay as `NNN_msg.md` in the folder and are reconsidered by the next invocation with more downstream context.

In **finalization mode** (`!протокол финиш`): the right-edge gap rule is disabled. Every chapter returned by the model is sealed, including the last one. The 30 KB lower bound on unprocessed content is also waived — the skill processes whatever is left.

Rationale: a chapter whose right edge is close to the batch boundary may still extend into the next batch. Sealing prematurely makes its boundary irreversible. On finalization, no further turns are expected, so all chapters are safe to seal.

### Atomicity of sealing

The seal operation for one chapter is a three-step sequence:

1. Write the chapter-file via temp + atomic rename.
2. Update `index.json` via temp + atomic rename. The updated entries' `file` fields now point at the chapter-file; `slug` is set per-turn.
3. Delete the now-redundant `NNN_msg.md` files inside the range.

Crash semantics:

- Crash between step 1 and step 2 — chapter-file exists but is not referenced. **Orphan.**
- Crash between step 2 and step 3 — msg-files coexist with chapter-file already referencing them. **Redundant.**

Both states are recoverable by the cleanup pass.

### Model response validation

A valid Stage 1 model response has two arrays — `turns` (per-turn metadata) and `chapters` (groupings):

```json
{
  "turns": [
    { "index": 0, "slug": "greetingAndScope",       "title": "Greeting and scope" },
    { "index": 1, "slug": "firstObjectiveProposal", "title": "First objective proposal" },
    ...
  ],
  "chapters": [
    { "start_turn": 0, "end_turn": 4,  "slug": "openingMoves",       "title": "Opening moves",       "abstract": "Initial framing and scope agreement." },
    { "start_turn": 5, "end_turn": 11, "slug": "objectiveCoalesces", "title": "Objective coalesces", "abstract": "OBJ001 takes shape; sub-entities identified." },
    ...
  ]
}
```

All turn indices are **absolute** (from session start), not batch-relative.

Constraints:

- `turns` covers every turn index in the batch from the cursor onward; each entry has a `slug` and `title`.
- `chapters` ranges are non-overlapping integers within the batch range.
- Chapter ranges form a **contiguous prefix** of the batch — covering all turns from the cursor up to the last `end_turn`. Turns beyond the last `end_turn` are part of an in-progress chapter and remain unprocessed in this batch.
- Each chapter `slug` matches the slug format defined in §Artifacts produced → Chapter-file → Slug rules, and is unique within the session folder (checked against existing chapter-files before sealing).
- Each chapter `title` is a short human-readable phrase used as the H1 inside the chapter-file.
- Each chapter `abstract` is ≤ 300 characters.
- Each per-turn `slug` matches the slug format and is unique within the batch.

On any validation failure: the entire batch is aborted — no chapters sealed, no msg-files renamed, no msg-files deleted. The error is surfaced; the next run can retry.

### Edge cases

- **No unprocessed msg-files.** No-op.
- **Less than 5 KB of unprocessed content.** No-op (wait for more material).
- **Model returns an empty `chapters` array.** No-op for this batch (model considers all unprocessed material in-progress).
- **First run on a session.** Same rules apply; cleanup pass on a clean folder is trivially a no-op.

## Stage 2 procedure — chapter-files → protocol

Input: sealed chapter-files in `<SessionFolder>/transcript/` (produced by Stage 1) that have not yet been narrated in `protocol.md`. Output: appended H2 chapters in `<SessionFolder>/protocol.md` (per `protocol.md` format in §Artifacts produced → Protocol), with the `## Оглавление` updated accordingly.

### Default parameters

- **Granularity:** one model call per chapter-file (one transcript chapter ↔ one protocol chapter, always).
- **Coverage:** every chapter-file past the cursor is reflected in the protocol — no skips. Thin chapters get a brief paragraph; substantive ones get full narrative. Density principle (see §Artifacts produced → Protocol → Content rules) governs length.
- **Cursor:** `protocol_cursor` — an integer turn-index stored at the top level of `transcript/index.json`. Marks the highest `end_turn` already narrated. Absent value means 0 (nothing narrated yet).

### Run sequence

1. **Read** `protocol_cursor` from `transcript/index.json` (default 0 if absent).
2. **Collect** sealed chapter-files in turn order whose `start_turn > protocol_cursor`. If none — no-op, exit.
3. **Scaffold `protocol.md`** if absent: write the skeleton (H1 placeholder, metadata block with `Дата` / `Статус: активна` / `Режим`, empty `## Abstract`, empty `## Оглавление`). The H1 meta-objective is derived from the first chapter's content in the first model call below.
4. **For each unprocessed chapter-file in turn order:**
   a. Read its content (slug, abstract, concatenated turn body).
   b. **Model call** — produce one narrative chapter for `protocol.md` following the format and editorial discipline of §Artifacts produced → Protocol. On the very first call (when scaffolding), also produce a proposed H1 meta-objective.
   c. **Validate** the response (see *Model response validation* below). On failure: abort the chapter-file's processing, do not advance cursor for it, surface the error.
   d. **Append** the produced `## Глава N. <Title>` block to `protocol.md` (where `N` is the next sequential chapter number). On scaffold-call, set the H1 from the model's proposed meta-objective at the same time.
   e. **Append** the chapter title to `## Оглавление`.
   f. **Advance** `protocol_cursor` to the chapter-file's last turn index. Write `index.json` atomically.

All file writes (`protocol.md`, `index.json`) go through temp + atomic rename.

### Finalization step (only in `!протокол финиш`)

After all chapter-files have been processed and the cursor is at the end of the session, the skill performs two final updates to `protocol.md`:

1. **`## Abstract` write/refresh.** A separate model call summarizes the full protocol (all `## Глава N` blocks) into the academic form: problem → method → results → impact. Replaces any prior Abstract content.
2. **`**Статус:**` transition.** The metadata `**Статус:**` value is changed from `активна` to `завершена`.

Both changes are written via temp + atomic rename. Outside finalization mode (default `!протокол`), `## Abstract` and `**Статус:**` are not touched by the skill.

### Per-chapter narrative discipline

The model must produce, for each chapter-file, a single `## Глава N. <Title>` block conforming to §Artifacts produced → Protocol → Chapter format. Recap of the rules the model must honor:

- **Every chapter gets a paragraph.** No skips. If a transcript chapter contains no settled rule or decision, the protocol chapter is a brief honest note — *«обсуждение X прошло без зафиксированного решения; ключевые вопросы вынесены в Y»* — and that is its substance.
- **Final formulations only.** Settled rules and decisions; not intermediate proposals or breadcrumbs.
- **Density:** depth on essentials, brevity on the rest.
- **Direct quote for load-bearing user prompts** (`---` attribution); short inline blockquotes for shorter passages; agent text paraphrased by default.
- **End-of-chapter marker** — `**Извлечённое правило**` or `**Зафиксированное решение**` — with explicit pointer when applicable.

### Model response validation

A valid Stage 2 model response is:

```json
{
  "meta_objective": "<H1 phrase — only on scaffolding call; null otherwise>",
  "chapter": {
    "title": "<Chapter title for the H2 heading>",
    "body": "<Full chapter body in markdown, ready to inject under `## Глава N. <Title>`>"
  }
}
```

Constraints:

- `title` is a human-readable phrase, ≤ 80 characters.
- `body` is non-empty markdown conforming to the chapter format rules above.
- On scaffolding call (first chapter-file ever processed for this session): `meta_objective` is a non-empty human-language phrase used as the H1. On all subsequent calls: `meta_objective` is `null` (or absent).

On validation failure: skip this chapter-file (cursor not advanced), surface the error, continue with subsequent chapter-files in the current run.

### Edge cases

- **No chapter-files past cursor.** No-op.
- **`protocol.md` exists but no chapter-files exist yet (Stage 1 hasn't run or produced nothing).** No-op.
- **`protocol.md` exists but `protocol_cursor` is missing in `index.json`.** Reconstruct cursor by reading the last `## Глава N` block in `protocol.md`, matching it to the chapter-file by title or position, and setting cursor to that chapter-file's last turn. If reconstruction is ambiguous, surface the error and require manual intervention.
- **Model returns malformed JSON.** Treat as validation failure for this chapter-file.
- **User has manually edited `protocol.md` between runs.** The skill respects user edits — it only appends new chapters; it does not validate the existing content.

## SKILL.md contract

The shape and content discipline that `SKILL.md` must satisfy.

### Required structure

`SKILL.md` must contain:

1. **Frontmatter** — `name`, `description`. The `description` must mention `!протокол` and one-line the conveyor (used by Claude Code's skill loader for trigger-matching).
2. **`## When to invoke`** — declares trigger discipline (only `!протокол`, no auto-trigger paths), accepted argument forms (scope of one invocation), and incremental-processing semantics (one invocation handles a batch of msg-files; the skill resumes from prior runs without reprocessing already-handled content).
3. **`## Conveyor`** — one-paragraph overview of the 4-node pipeline plus the canonical diagram.
4. **`## Stage 1: msg-files → chapter-files`** — directs the agent to implement the procedure per spec §Stage 1 procedure above. References the run sequence, validation rules, and atomicity invariants; does not re-state them.
5. **`## Stage 2: chapter-files → protocol`** — directs the agent to implement the procedure per spec §Stage 2 procedure above. References the run sequence, narrative discipline, and validation rules; does not re-state them.
6. **`## Stage 3: protocol → progress`** — input, output, scaffold-vs-update discipline. Writes only to the single `## PROGRESS` section of each affected OBJ index file (per `schemas/obj_folder.md` §5). If the section is absent, the skill scaffolds the standard OBJ structure; otherwise it updates. The skill does not mutate any other section of any OBJ file.
7. **`## Source material`** — what the agent reads at each stage (transcript, `state.md`, OBJ files, related protocols).
8. **`## Anti-patterns`** — common failure modes the agent must avoid.

### Forbidden content

- **Runtime context the harness already supplies.** Tool lists, Claude Code mechanics, MCP signatures.
- **Full file format definitions.** Artifact formats live in §Artifacts produced above; the SKILL.md references them by anchor, not by inline duplication.
- **Auto-trigger logic.** No "after N turns", no "on session end", no hooks. Only `!протокол`.
- **OBJ closure flow.** No path that effects `open → closed`. Auto-close is forbidden by the closure ritual (`domain-model.md` §1.2); the skill may *propose*, never *execute*.
- **Templated output patterns.** No fixed phrasing for protocol chapters, no canonical heading lists beyond the schema. The agent generates the narrative each time.
- **Embedded examples of generated protocols.** The format is specified in §Artifacts produced → Protocol above; the skill does not inline sample outputs.

## Sources of truth

- [`cockpit/specs/schemas/session_folder.md`](../../cockpit/specs/schemas/session_folder.md) — session folder layout; agent-owned `state.md` format.
- [`cockpit/specs/schemas/obj_folder.md`](../../cockpit/specs/schemas/obj_folder.md) — OBJ folder layout; placement of the `## PROGRESS` section.
- [`cockpit/specs/stop_hook.md`](../../cockpit/specs/stop_hook.md) — `transcript/` per-turn file format, `index.json` schema, SessionStateFile, concurrency model.
- [`cockpit/specs/domain-model.md`](../../cockpit/specs/domain-model.md) — OBJ entity state machine and invariants (Stage 3).
- [`cockpit/specs/architecture.md`](../../cockpit/specs/architecture.md) — system overview, maturity axis, component layout.

When `SKILL.md` and a spec disagree — the spec wins, `SKILL.md` is updated.

## Editorial bar

- **Behavioral, not descriptive.** Every rule answers "what to do when event X happens".
- **3-whys per rule.** The "why does this matter?" chain must terminate in a concrete consumer / failure mode / system invariant.
- **Self-contained.** No runtime fetches of schemas. If something is load-bearing, state it inline (distilled, not copied wholesale).
- **No defensive prose.** "Just in case the agent…" — drop it.
- **Length budget.** ≤ 250 lines (looser than `igor.md` because the skill is loaded on demand, not on every turn).

## Adequacy criteria (for review)

`SKILL.md` is adequate to its purpose when:

1. Every rule passes 3-whys (load-bearing root: consumer / invariant / failure).
2. All three Stages are specified with explicit input, output, and at least one invariant each.
3. Stage 3 writes only to `## PROGRESS` per `schemas/obj_folder.md` §5; scaffold-vs-update discipline is declared.
4. Trigger discipline is explicit — no auto-trigger paths can be inferred from the prose.
5. No file format definitions are inlined; all reference `cockpit/specs/schemas/`.
6. No content duplicates `igor.md` or other always-loaded sources.
7. Anti-patterns section includes at minimum: templated narrative, silent Stage 3 commits, side files outside session folder.
8. The conveyor diagram is present and matches the canonical form: `msg-files → chapter-files → protocol → progress`.
