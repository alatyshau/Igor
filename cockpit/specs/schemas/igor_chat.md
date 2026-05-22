# Igor Chat Schema (draft)

Chat-specific phenomena: ephemeral entities, name resolution, event syntax, and change-log format.

> Storage-bound identifiers (ObjectiveCode, SubEntityCode, SessionID) are defined in their respective schemas: `obj_folder.md`, `session_folder.md`. This file covers only what lives in chat itself.

## ProblemCode

`P1..P9` — numbered only within the current chat. Problems are ephemeral and never persisted to disk, so numbering resets per chat.

**Canceled Problems free their slot.** The 9-limit counts only **active** Problems (any non-terminal state — `draft` or `open`). When a Problem is canceled or triaged away, its slot becomes reusable. The limit exists to cap cognitive load, not historical record.

Sub-entities attached to a Problem use the form `Pn.<Letter><index>` — e.g., `P3.I01`, `P3.S02`, `P3.T01`. These too are ephemeral; on triage they transfer to the triage destination (see `domain-model.md` §2.4).

At `P7`, the agent emits an easter egg: ASCII art with a short poetic warning about approaching the limit, generated fresh each time (no stored template, no repetition of prior renditions).

At `P10`, the agent refuses to create the entity and proposes starting a new chat. Tone is playful, not a strict error.

## Aliases and loose references

A code, a slug, and a loose descriptive reference ("that tracker thing", "the entity-spec goal") are synonyms for the same entity. The agent resolves the reference from any form.

This frees the user from having to remember exact codes or switch to a formal register. The resolution mechanism is part of agent behavior.

### Bilingual code aliases

Codes are **written canonically in Latin** in chat output and on disk. Cyrillic transliterations are accepted as user input aliases and resolved to the canonical form.

| Canonical (Latin) | Cyrillic aliases |
|---|---|
| `OBJxxx` | `ОБЙxxx`, `ОБxxx`, `OBxxx` |
| `Pn` (Problem) | `Пn` |
| `In` (Issue) | `Иn` |
| `Sn` (Suggestion) | `Сn` |
| `Tn` (Task) | `Тn` |

Cross-file SubEntity references accept the same letter aliases: `OBJ003.T02` ≡ `ОБЙ003.Т02` ≡ `OB003.T02`.

The agent always emits the Latin canonical form; the user may type either.

## Event markers

Verbs with a trailing `!` are one-time events emitted in chat at the moment the action takes place:

| Marker | When |
|---|---|
| `triaged!` | Problem → one or more O / I / T (transformation) |
| `renamed!` | scope of an `open` Objective expanded materially — new slug |
| `moved!` | sub-entity re-parented between Objectives |
| `merged!` | two Objectives merged into one |
| `split!` | one Objective split into two |
| `promoted!` | artifact moved from the OBJ folder to the git repo |

Event markers are not repeated in subsequent summaries.

## Change-log lines

When an entity is created or its state changes, the agent emits a one-line marker prefixed with `!`:

```
! [P3 ParallelChatTracking (open)]
! [OBJ012 ParallelChatTracking (open)] triaged! from P3 → new Objective.
! [OBJ003.I07 IndexCadenceUncertainty (open)] triaged! from P4 → Issue on OBJ003.
! [OBJ001.I04 BacklogScopeRule (closed)] Answer captured in Design Doc.
! [OBJ001 ChatEntitySystem (open)] renamed! scope expanded — new slug.
! [OBJ004 ConceptualDesign (closed)] promoted! restructure_plan.md → <repo>/specs/.
```

Change-log lines appear at the moment of change and are not repeated in subsequent messages.

## End-of-message rolls

(Format of the per-message status roll — list of open Problems, in-scope OBJs, etc. — is specified separately in the agent behavior spec.)

### Empty-roll blessing (easter egg)

When the entity roll at the end of a message is empty — no open Problems, no in-scope Objectives, nothing pending — the agent does **not** emit an empty list. Instead, a short **blessing phrase** (3–6 words) takes the slot, in the spirit of:

- *May the Force be with you.*
- *All sails full, captain.*
- *Quiet on the airwaves, commander.*
- *Clear as morning dew.*

Rules:
- Generated **on the fly**, no stored template, no repetition of earlier renditions within the same chat;
- Tone: warm, brief, slightly ceremonial — a small ritual signal of *"everything is closed, exhale"*;
- Language matches the chat (Russian or English).

This is one of two easter eggs in the chat layer; the other is the P7 ASCII-art warning (see *ProblemCode* above).
