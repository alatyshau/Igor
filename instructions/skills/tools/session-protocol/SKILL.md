---
name: session-protocol
description: Generates and updates the curated session protocol — a narrative document at `journal/YYYY/MM/DD/HHMM_<Slug>/protocol.md` capturing decisions, principles, and key dialogue extracts from the session. Trigger on the `!протокол` command, or propose it at the end of a significant work block. The protocol is distinct from raw transcript (in the session's `transcript/` subfolder) — it is reader-facing, substantive, and structured by chapter.
---

# Session Protocol

The protocol is the curated narrative of a session — what was discussed, what was decided, and what was extracted as durable rules or commitments. It lives in the session folder alongside the raw transcript and the entity-state snapshot, but it is none of these: it is the reader-facing document a person opens after the session to recover what happened without scrolling chat or wading through raw transcript.

## When to invoke

- **`!протокол` command.** The user explicitly requests creation or update of the protocol.
- **Agent proposal at a checkpoint.** At the end of a significant work block — major decision adopted, multiple sub-entities resolved, scope shifted — the agent may propose: *«предлагаю обновить протокол — накопилось много решений»*. The user decides.

The skill does not run silently. No background protocol generation.

## Output location

```
journal/YYYY/MM/DD/HHMM_<Slug>/protocol.md
```

Path follows the session folder convention. The skill assumes the session folder exists; if not, it asks the user to confirm session identity before creating the protocol.

## Structure

The protocol file follows this structure:

1. **H1** — meta-objective in human language (full phrase, not slug). Example: `# Рождение системы работы Игоря в чатах`.
2. **Metadata block** — `Дата`, `Статус` (`активна` / `завершена` / `прервана`), `Режим` (mode of session — e.g., AdhocDeveloper).
3. **`## Abstract`** — academic form: problem (why the session was needed) → method → results (artifacts produced) → impact (where they land).
4. **`## Оглавление`** — numbered list of chapter titles. Substantive, human-language. Not technical jargon, not literary flourishes.
5. **`## Глава N. <Title>`** — chapters in order.

The protocol does **not** duplicate the OBJ/PROB context list — that lives in `state.md` (session-state) and is recoverable from `objectives/` files. Including it in the protocol would create a frozen snapshot that immediately diverges from authoritative sources.

## Chapter format

- **Narrative, factual tone.** Not literary. The reader is a future maintainer recovering what happened.
- **Substantial original text from the user or the agent** — quote in full (with typo corrections), separated by a horizontal rule and attribution:

  ```
  ---

  *Андрей:*

  [full text]

  ---
  ```

- **Short inline quotes** (≤ 1 paragraph) — normal blockquote (`> ...`).
- **End of chapter** — `Извлечённое правило` or `Зафиксированное решение`, with an explicit pointer to where it landed (e.g., `OBJ003 → раздел Y`).

## Content rules

- **Not the whole verbatim chat.** That duplicates the transcript and adds no value.
- **Not a summary of summaries.** Each chapter must carry substance someone re-reading later actually benefits from.
- **Chapters as standalone units.** A reader who opens only chapter 7 should understand chapter 7's substance without reading 1–6.
- **Cross-references to OBJ files** — by name, not as enumerated lists of sub-entities.

## What NOT to include

- System reminders, hook outputs, tool-call ephemera — noise.
- Full tool calls (link or brief one-line summary at most).
- Intermediate edits of a single decision — only the final formulation.

## Create vs update

The skill operates in two modes:

- **Create.** Protocol file does not exist. Generate the full structure based on session content up to this point.
- **Update.** Protocol file exists. Append new chapters that reflect work since the last update. Refresh `## Abstract` only if scope shifted materially.


## Source material

- Recent chat history (visible context) — primary source for narrative.
- `transcript/` folder in the session — for verifying quotes, recovering text dropped by compaction, and pulling exact wording for substantial quotes.
- OBJ files (`objectives/`) — for cross-checking final state of entities mentioned.
- `state.md` in session folder — for the current OBJ/PROB context list.

Use the transcript only as a verification source, not as the protocol's structure. The transcript is the raw record; the protocol is the curated reading.

## Anti-patterns

- **Verbatim transcript dump.** Protocol is curated, not raw. Raw lives in `transcript/`.
- **Pure summary form throughout.** Chapters must carry substance, not restate the Abstract.
- **Literary chapter titles.** "Утро 18 мая. «Чем займёмся?»" is the wrong shape. "Слова «как должно быть» — это уже Цель" is the right shape.
- **Including system noise.** Hooks, reminders, system-injected text — exclude.
- **Auto-generation without explicit trigger.** The skill runs only on `!протокол` or after explicit agent proposal followed by user authorization.
- **Rewriting earlier chapters during an update.** Append, do not redo. Earlier chapters reflect the state at the time they were written; that is part of the record.
