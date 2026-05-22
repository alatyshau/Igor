# `igor.md` — Spec

Specification of the product [`instructions/igor.md`](../igor.md) — the always-loaded persona file for the cockpit assistant.

## What this is and who it's for

This spec is for **humans and review agents** who maintain or audit `igor.md`. **Not for Igor itself.** The agent operating from the `igor.md` system prompt knows nothing about this spec — just as VS Code does not read Microsoft's design docs.

Architectural layers:

| Layer | What | Read by |
|---|---|---|
| **Specs** (`cockpit/specs/`, `instructions/specs/`) | design documents | humans, review agents |
| **Products** (`igor.md`, `cockpit/mcp/`, `cockpit/hooks/`) | artifacts built per spec | deploy machinery |
| **Runtime** | products in action | the agent, the user |

`igor.md` is a product. This spec is upstream of it. The runtime is downstream.

## Purpose of the product `igor.md`

An always-loaded persona file that shapes the behavior of the agent ("Igor") inside one Context from the first to the last token of a session. Loaded into the system prompt on every turn via the Claude Code output-styles mechanism.

Audience of the product: the agent itself. Every line is paid for as `(line length) × (turns per session) × (sessions per Context)`.

## Contract — required structure

Seven section blocks in the order below, separated by three visual register banners:

```
═════ STATIC ═════
  ## Domain Model       — distilled from cockpit/specs/domain-model.md
  ## On-Disk Schema     — distilled from cockpit/specs/schemas/
  ## Engineering Bar    — work posture
  ## Absolute Prohibitions

═════ PROCEDURAL ═════  — one section per triggering event
  ## On Session Start
  ## On Reading User Input
  ## On Touching Entities
  ## On Recognizing a Milestone
  ## On Catching a Mistake or Improvement Idea
  ## On Proposing Objective Closure
  ## On Producing Output
  ## On Closing the Message

═════ REFERENCE ═════
  ## Operations
  ## Special Commands
  ## Easter Eggs
```

A short identity paragraph under `# Igor` precedes the first banner.

## Contract — what must NOT be in `igor.md`

- **Full spec content.** The Domain Model in `igor.md` is a short distillation of what the agent needs to decide correctly on every turn. The canonical model lives in `cockpit/specs/domain-model.md`. Same rule for schemas and architecture.
- **Duplicates of runtime context.** The harness already exposes the live tool list with schemas; deploy injects `cockpit_config.localization` as an `## Identity` block at the top of the file. Listing MCP tools inline or hardcoding the chat language wastes prompt tokens on duplication.
- **Defensive prose.** "It is not about X", "just in case Y" — addressed to an imagined reviewer; the agent does not need design defenses.
- **Documentation framing.** "## Overview", "The purpose of this section is…", "(timeless, behind every rule below)" — meta-wrappers around content.
- **Descriptive instead of prescriptive.** "Output reads as prose" should be rewritten as "Prefer prose over decoration."
- **Hardcoded language preference.** Chat language, anglicism rules, output-template labels arrive through deploy. Hardcoding any of them in `igor.md` creates drift on a different deploy. *Exception:* fixed schema tokens (`**Цель:**`, `**Выходы:**`, etc.) are domain constants — their form is fixed regardless of chat language.
- **Per-Context specifics.** Names of concrete Objectives, references to concrete artifacts of a concrete Context belong in that Context's OBJ files, not in the persona.

## Editorial bar

The strictest filter (Instructions Architect canon — see [`Duet-Instructions.git/skills/tools/instructions-architect.md`](../../../Duet-Instructions.git/skills/tools/instructions-architect.md)), because the file is always loaded.

Concrete requirements:

- **3-whys per rule.** The "why does this matter?" chain must terminate in a concrete consumer / failure mode / system invariant. A chain that dies in "might be nice" — drop the rule.
- **Self-contained.** No runtime fetches of specs. If something is load-bearing, state it inline (distilled, not copied wholesale).
- **Behavioral, not descriptive.** Every rule answers "what to do when event X happens".
- **Examples > explanations.** A concrete change-log line is worth more than a paragraph about format.
- **Leading WHY.** When a rule carries a WHY, it goes *before* or *inside* the rule — shaping how edge cases are interpreted, not *after* as a defense.
- **Verify before applying advice.** Any external review proposal is checked against the actual state of the runtime/system before being applied.

Length budget: ≤ ~350 lines (current size — ~300).

## Sources of truth

When `igor.md` and a spec disagree — the spec wins, `igor.md` is updated to match. Canonical sources:

- [`cockpit/specs/domain-model.md`](../../cockpit/specs/domain-model.md) — entities, state machines, cascade, event markers.
- [`cockpit/specs/schemas/`](../../cockpit/specs/schemas/) — on-disk file formats.
- [`cockpit/specs/architecture.md`](../../cockpit/specs/architecture.md) — system components, the MCP boundary, hook ordering.

## Deploy

- **Source:** [`instructions/igor.md`](../igor.md) in `Igor.source.git`.
- **Target:** `<Context>/.claude/output-styles/igor.md` (copied manually by Andrei).
- **Deploy machinery** prepends `cockpit_config.localization` from `<Context>/context.json` as an `## Identity` block at the top of the file. The agent sees that block before the body of `igor.md`.

## Adequacy criteria (for review)

`igor.md` is adequate to its purpose when:

1. Every rule passes 3-whys (load-bearing root: consumer / invariant / failure).
2. No content duplicates runtime injections (harness tool list, deployed localization).
3. No language-preference hardcoding except fixed schema tokens.
4. No defensive prose, documentation framing, or descriptive bullets.
5. Every procedural section is keyed to a concrete event.
6. The Domain Model distillation agrees with `domain-model.md` (state machines, cascade, markers, codes).
7. Persona signal is present: substitute "Assistant" for "Igor" — the posture, register, and discipline of the file are still recognizable.

## Briefing review agents

When asking Codex or a subagent to review `igor.md`, supply:

- A pointer to this spec as the product contract.
- Pointers to the canonical specs (`cockpit/specs/`) for domain-consistency checks.
- Specific questions — "verify X", "find Y", "check Z for contradiction with W" — not "review for quality".

Without these three, the review produces generic findings unanchored to the file's purpose.
