# Domain Model

The Igor.cockpit domain model: conceptual entities, their states, relationships, operations, and events. On-disk representation lives in `schemas/obj_folder.md`, `schemas/journal_folder.md`, `schemas/session_folder.md`; chat syntax (codes, aliases, event marker format) lives in `schemas/igor_chat.md`.

> Russian tokens kept verbatim in this document are fixed schema constants from the OBJ-file format: `Цель`, `Выходы`, `Обоснование закрытия`, `Merged into`. They appear here because they are part of the ubiquitous language between the user and Igor.

## 1. Entities

**State lexicon** (shared across types, used in the state tables below):
- `draft` — formulation incomplete; used only by Problem and Objective.
- `open` — active.
- `closed` — successfully completed.
- `canceled` — withdrawn (also used for "active reject" — see Suggestion).
- `backlog` — globally deferred; only Objective.
- `confirmed` — only Suggestion (positive resolution).

### 1.1 Context

A **Context** (Контекст) corresponds to a *bounded context* in DDD: it serves a definite purpose and frames a stream of work with its own glossary, artifacts, and goals. The user's life is decomposed into a hierarchy of contexts with **multiple roots** — separate top-level trees rather than one global root, reflecting that concerns like work, family, and hobbies do not share semantics. Igor is typically loaded at a **leaf-level** context, where concrete work happens; non-leaf contexts mostly serve as organizational containers.

A context is the largest unit to which a separate Objectives base and Journal are bound. Different contexts are isolated from each other.

> **Duet** (external system) is a personal knowledge-and-work management system built on a human–AI duet. It stores data in Google Drive in durable formats (Markdown, CSV, Python), indexes a hierarchy of contexts, and exposes them to LLM tools (Claude Code, etc.) via MCP. Igor relies on Duet for context orientation and for generating the multi-root VS Code workspace that bridges the private context folder with linked git repositories.

### 1.2 Objective

An **Objective** (Цель) is a qualitative, aspirational, memorable statement of WHAT is to be achieved — significant, concrete, action-oriented, and ideally inspirational. (Doerr, *Measure What Matters*; aligned with the OKR notion of *Objective*.)

Within Igor, an Objective always represents a goal the user has explicitly committed to: the agent may propose candidates but cannot create one autonomously.

An Objective conceptually carries two things:

- **WHAT** — the goal formulation (the desired outcome) together with its acceptance criteria (verifiable physical deliverables). The quantitative part of OKRs (Key Results) is replaced here by output-based acceptance criteria — binary "delivered or not", not progress metrics.
- **WHY** — the motivation: the problem this Objective addresses, the stakeholder need, the context that makes this Objective worth pursuing now.

Both WHAT and WHY are load-bearing. Without WHY, the Objective drifts when circumstances change — the user cannot tell whether a proposed action still serves the original intent. Without WHAT, there is nothing to verify on closure.

An Objective is the unit around which sub-entities (questions, proposals, actions) and working artifacts accumulate.

**States:**

| State | Transition into this state |
|---|---|
| `draft` | initial state when the user has committed to the Objective but its WHAT + WHY are not yet sharp. The agent typically refines the formulation through an **interview** (see `instructions/skills/tools/interview/`). When the formulation is clear enough to work on, the agent moves the Objective to `open`. |
| `open` | default after creation when slug, scope, WHAT and WHY are clear |
| `closed` | requires (a) all sub-entities resolved, (b) `Выходы` verifiably delivered, (c) **explicit user authorization**. Auto-close forbidden. |
| `canceled` | by user command |
| `backlog` | `open ↔ backlog` only by explicit user command. Auto-move forbidden. |

**Closure ritual.** The AI proposes closure when the preconditions are met — it never closes on its own. On transition to `closed`:
1. An actualized `Цель` formulation (phrased as a desired outcome).
2. `Обоснование закрытия` — verifiable evidence (what was delivered, where). Replaces `Выходы`.

### 1.3 Issue

An **Issue** (Вопрос) is a sub-entity of Objective: an open question that blocks clarification of the goal.

An Issue:
- is always attached to a single Objective;
- closes automatically when the AI receives a clean answer (no hedging, homonyms, or category substitution);
- is reopened (`closed → open`) only on explicit user command.

The Issue is the mechanism that keeps open questions visible until resolved — nothing falls on the floor.

**States:**

| State | Transition |
|---|---|
| `open` | default |
| `closed` | auto on clean answer; on doubt — leave `open` |
| `canceled` | by user command or cascade from the parent Objective |

### 1.4 Suggestion

A **Suggestion** (Предложение) is a sub-entity of Objective: a proposal from the AI awaiting user reaction.

A Suggestion:
- is always attached to a single parent (Objective or Problem);
- moves out of `open` only on explicit user signal — the AI never flips it on its own;
- on a soft reply ("maybe", "perhaps"), an Issue is opened about the ambiguity; the Suggestion remains `open`;
- silence is not grounds to escalate — the Suggestion stays `open` indefinitely.

A Suggestion is a fixed point for the AI's intent to obtain a decision, without pushing.

**States:**

| State | Transition |
|---|---|
| `open` | default |
| `confirmed` | explicit user confirmation (positive resolution) |
| `canceled` | by user command (covers active reject and silent withdrawal) or cascade from the parent |

A previous-design distinction between *active reject* (`declined`) and *withdrawn* (`canceled`) is collapsed: both become `canceled`. The rationale of an active reject, if useful to record, lives inline in the Items entry text. `canceled` Suggestions may be pruned from `state.md` for context lean-ness (see `schemas/session_folder.md`); they remain in the parent Objective's `## Items` for history.

### 1.5 Task

A **Task** (Задача) is a sub-entity of Objective: a committed action awaiting user authorization to start.

A Task:
- is always attached to a single Objective;
- closes automatically on verifiable completion (the artifact exists, the command has executed) — the AI may close it itself;
- represents an *action*, not a *question* and not a *proposal*.

**States:**

| State | Transition |
|---|---|
| `open` | default — awaiting authorization |
| `closed` | auto on verifiable completion. The AI may close it itself. |
| `canceled` | by user command or cascade from the parent Objective |

### 1.6 Problem

A **Problem** (Проблема) is a session-scoped triage candidate at the **Objective-level**: a forward-pointing item that has not yet been classified into a formal entity.

A Problem:
- is **session-scoped** — never outlives the session; serialized within the session's `state.md` for compaction recovery and queries, but carries no cross-session persistence;
- exists so that nothing falls on the floor while triage is pending;
- **may accumulate its own sub-entities** (Issue / Suggestion / Task) during clarification — questions, proposals, and actions that arise about the Problem must have a home. On triage, those sub-entities transfer to the triage destination;
- is triaged **by the user** (not automatically) into one or more formal entities (a new Objective, or Issue / Task on an existing Objective, or a combination);
- the AI may propose a triage; the user decides;
- untriaged Problems are lost at session end — this is by design.

A Problem is a buffer between "the AI noticed something forward-pointing" and "the user decided what to do with it" — a temporary tracking container that holds whatever discussion accumulates around the candidate.

**States:**

| State | Transition into this state |
|---|---|
| `draft` | default on tentative flagging |
| `open` | `draft → open` once slug and scope are clear |
| `canceled` | by user command (cascades to its sub-entities) |

**Problem has no `closed`.** Triage (`Problem → Objective / Issue / Task / combination`) is **entity transformation**, not a state transition: the Problem is consumed (ceases to exist) and the new entities are created in its place. Its sub-entities transfer to the triage destination (see §2.4). Recorded with the `triaged!` event.

### 1.7 Session

A **Session** (Сессия) is a chat between the user and the AI agent — one bounded conversational episode. Materially, each Session corresponds to one Claude Code chat (with its own SessionID generated at start and its own SessionFolder in the journal). Sessions imported from other surfaces (e.g., a `claude.ai` web chat, ChatGPT, Gemini) also count as Sessions and live in the journal alongside Claude Code ones.

A Session:
- is bounded to **≤ 1 day** (multi-day sessions are an anti-pattern; better small and finished than large and stalled);
- has a slug (mutable during the session via an MCP tool);
- captures a transcript automatically via hooks, independently of the agent;
- comes in two types:
  - **OBJ-bound** — directed work on one or several Objectives;
  - **Exploration** — free research without a declared goal; may seed a Problem that is later triaged into a new Objective.

A Session is the atomic unit of "past work". Declared goals are the future; the journal of sessions is the past.

### 1.8 Journal

The **Journal** (Журнал) is a chronological calendar of all sessions in the context.

The Journal:
- is the source of truth for *what happened and when*;
- is not duplicated inside Objective files — the linkage between an Objective and the sessions that touched it is computed by MCP from transcripts.

The Journal is a backward-facing record of work, in contrast to Objectives (forward-facing intent).

---

## 2. Relationships

### 2.1 Sub-entity attachment

Issue, Suggestion, and Task are always attached **to a single parent** — either an **Objective** or a **Problem**. Problems may host sub-entities while they exist (chat-only, ephemeral); on triage, those sub-entities transfer to the triage destination (see §2.4).

**Affected-scope parenting:** determine the real parent by *what the sub-entity affects*, not by topical association. Example: the question "where does the tracker live as an artifact" affects mode architecture (even if the topic is "tracker"). Topical parenting forces re-parenting later; affected-scope parenting is durable.

### 2.2 Objective ↔ Objective (Blocked by)

A larger Objective may decompose into **other Objectives** (not sub-entities). For example, the master goal "digitize the book" depends on a set of Objectives "digitize section X".

The dependency is encoded as **Blocked by**: Objective A *blocked by* Objective B means A cannot reach `closed` until B is itself `closed` (or `canceled`). The dependency graph is a DAG, computed by MCP from the metadata.

Sub-entities (Issue / Suggestion / Task) are for **small** questions, proposals, and actions inside a single Objective. OBJ-OBJ dependencies are for **decomposition** of work between Objectives.

### 2.3 Session ↔ Objective

Sessions touch Objectives. The mapping is N:N:
- one session may touch several Objectives;
- one Objective is worked on across many sessions.

Each session-level touch has an **engagement mode** — a property of the session's relationship with the Objective, independent of the Objective's own global state:

| Engagement mode | What the session does with the Objective |
|---|---|
| `draft` | softest: just draft the Objective idea (often born from triage of a Problem); may end up in the global backlog if the user requests |
| `what\|why` | refine WHAT and WHY — typically interview-mode; no execution |
| `how` | design the approach: structure, sub-entities, plan — but not execute |
| `work` | actual execution: produce deliverables (`Выходы`) |

The engagement mode is recorded in the session's `state.md`. The same Objective may be in `work` mode in one session and `what|why` mode in another — the mode is per-touch, not a property of the Objective itself.

**The source of truth for "which session touched which Objective" is on the session side.** A session's `state.md` lists in-scope Objectives with their engagement modes; the Objective itself does not duplicate the session list. MCP computes the linkage on demand by scanning the journal.

This avoids write amplification (one session = one record), eliminates sync risk, and keeps the Objective free of metadata bloat.

### 2.4 Problem triage destinations

A Problem can transform into:
- **a new Objective** — the Problem is the goal itself;
- **an Issue on an existing Objective** — the Problem is a question that blocks that Objective;
- **a Task on an existing Objective** — the Problem is a committed action under that Objective;
- **a combination** — parts of the Problem go to different destinations.

Triage is performed **by the user**. The AI may propose a triage ("I'd triage this as a new Objective"), but the user decides. After triage, the Problem ceases to exist; the `triaged!` event is recorded.

**Sub-entities transfer on triage.** Issues, Suggestions, and Tasks attached to the Problem move to the triage destination:
- Triaged into a new Objective → all sub-entities re-parent to that new Objective.
- Triaged into an existing Objective (as Issue/Task or combination) → sub-entities re-parent to that destination Objective as direct sub-entities at the OBJ level.
- If sub-entities lose their context after re-parenting (a question that no longer applies in the new framing) — the user may cancel them explicitly.

---

## 3. Operations

### 3.1 On Objective

**Merge** — one Objective is absorbed by another. Sub-entities transfer to the destination; the merged Objective enters `canceled` and references the destination by code.

**Split** — the inverse: one Objective is divided into two. New Objectives receive new codes.

**Delete** — full removal. Exceptional case (cleanup of experimental artifacts). The code is freed for reuse. Only after explicit user authorization.

**Move to / from backlog** — `open ↔ backlog`. Only by explicit user command. Auto-move forbidden.

**Closure** — `open → closed`. See 1.2 (closure ritual).

### 3.2 On Session

**Rename** — the session's slug is changed mid-session. Backed by an MCP tool that updates both the on-disk path and the session state file in one transaction.

**Archive** — every session remains in the journal forever; there is no separate "archive" operation.

### 3.3 On Sub-entities

**Create** — two paths:
- directly on an existing Objective during work (the AI recognizes a new question / proposal / action);
- from triage of a Problem (Problem → Issue or Task on an existing Objective).

Both paths yield identical lifecycles.

**Close** — Issue / Task close automatically when their conditions hold; Suggestion only via `confirmed` (or `canceled`) from the user.

**Cancel** — by user command or cascade from a canceled parent Objective.

**Re-parent** between Objectives — the sub-entity moves from the source to the destination; the source loses the entry entirely (no shadow record), the destination gains it. Recorded with the `moved!` event.

### 3.4 Promotion

A working artifact moves from an Objective's working area into a permanent location (typically the context's git repository). This is the maturity transition: *unripe → ripe*.

Trigger — the closure ritual of the Objective (or an explicit user command). Recorded with the `promoted!` event.

### 3.5 Cascade

**When an Objective or Problem is canceled**, all of its `open` sub-entities (Issue, Suggestion, Task) automatically transition to `canceled`. Cascade applies identically regardless of whether the parent is an Objective or a Problem.

**The reverse does not apply:** canceling a sub-entity does not cancel its parent.

---

## 4. Event Markers

Verbs with a trailing `!` are one-time events emitted in chat at the moment the action takes place. They are not repeated in subsequent summaries.

| Marker | When |
|---|---|
| `triaged!` | Problem → one or more Objective / Issue / Task (transformation) |
| `renamed!` | scope of an `open` Objective expanded materially — new slug |
| `moved!` | sub-entity re-parented between Objectives |
| `merged!` | two Objectives merged into one |
| `split!` | one Objective split into two |
| `promoted!` | artifact promoted from an Objective to the git repository |

The exact in-chat format of change-log lines (the `! [CODE Slug (state)]` syntax) is specified in `schemas/igor_chat.md`.
