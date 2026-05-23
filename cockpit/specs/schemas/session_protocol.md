# Session Protocol Schemas

Content schemas for the artifacts produced by the `protocolist` subagent inside a SessionFolder. Layout of the SessionFolder itself is in [`session_folder.md`](session_folder.md); subagent behaviour contract is in [`../../../instructions/specs/protocolist_spec.md`](../../../instructions/specs/protocolist_spec.md). This file is the **canonical source of truth** for what is *inside* the files.

Two artifacts are defined here:

1. **Chapter-files** — `<SessionFolder>/transcript/NNN_<ChapterSlug>.md` — semantic groupings of per-turn files into chapters.
2. **`protocol.md`** — `<SessionFolder>/protocol.md` — curated narrative compiled chapter-by-chapter from the transcript.

---

## 1. Chapter-file

A chapter-file is the seal of a group of consecutive per-turn files (`NNN_msg.md`) into a single readable unit. After sealing, the original per-turn files in the range are deleted, and `transcript/index.json` updates the `file` field on every covered entry to point at the chapter-file (multi-entry → shared file pattern, see [`../stop_hook.md`](../stop_hook.md) §Consistency invariants).

### Filename

```
NNN_<ChapterSlug>.md
```

- `NNN` — first turn index in the chapter, zero-padded 3 digits.
- `<ChapterSlug>` — `camelCase` or `snake_case` slug describing the chapter; same slug rules as SessionFolder slugs (no dashes, dots, splitting characters).
- The chapter's covered turn range is `[NNN, NNN + len(turns_in_chapter) - 1]`; the upper bound is **not** encoded in the filename — it is recovered from `index.json` by reading consecutive entries sharing the same `file`.

### Content

Concatenation of the per-turn `msg-content` of every covered turn, in order, preserving each turn's `## NNN <Title>` H2 header (see [`../stop_hook.md`](../stop_hook.md) §Per-turn file). No chapter-level header is added — the chapter's identity lives in the filename slug.

### Seal markers (in `index.json`, not in the file)

A chapter-file is considered **sealed** when, for every covered turn index `i ∈ [NNN, NNN+k-1]`:

- `index.json.turns[i].file == "NNN_<ChapterSlug>.md"` (all covered entries point at the same chapter-file).
- The original `NNN_msg.md` files no longer exist on disk.

Either condition holding without the other is a **partial seal** — the cleanup pass on the next protocolist invocation must complete it (see [`../../../instructions/specs/protocolist_spec.md`](../../../instructions/specs/protocolist_spec.md) §Stage 1).

### Idempotence anchor

The pair (`covered turn range`, `chapter-file path`) uniquely identifies a sealed chapter. Re-running `!протокол` over a transcript that already has a sealed chapter for some range produces no new chapter for that range.

---

## 2. `protocol.md`

Curated narrative document at `<SessionFolder>/protocol.md`. Written by the `protocolist` subagent across multiple invocations; appended chapter-by-chapter; finalized in `!протокол финиш`.

### File scaffold (created on first invocation, never rewritten)

```markdown
# Protocol — <session slug>

**Status:** в работе

## Chapters

<!-- chapters appended below by Stage 2 -->
```

`**Status:**` values:

- `в работе` — open; further chapters may be appended.
- `завершена` — finalized by `!протокол финиш`; no further chapters are appended without explicit user reopen.

### Chapter section (appended by Stage 2)

Each chapter sealed in Stage 1 produces exactly one chapter section, appended to `## Chapters`, in chronological order:

```markdown
### NNN — <ChapterSlug>  *(turns NNN–MMM)*

<curated narrative — plain paragraphs>

> **Пользователь —** *(turn KKK)*
>
> *(verbatim direct quote of a key user prompt — full text, attribution via `---`-line if multi-paragraph)*

<narrative continues, weaving in key user prompts as block quotes when load-bearing>
```

Editorial discipline (see [`../../../instructions/specs/protocolist_spec.md`](../../../instructions/specs/protocolist_spec.md) §Stage 2 for behavioural rules):

- Only final formulations — no intermediate proposals, no cancelled paths, no "how we got here" breadcrumbs.
- Density principle — dense on substance, drop everything else entirely.
- Key user prompts are quoted in full (technical fixes only — typos, formatting). Routine exchanges are not quoted.

### Abstract (added on finalization)

`!протокол финиш` writes an **Abstract** section immediately after the H1 title and before `## Chapters`, then flips `**Status:**` to `завершена`:

```markdown
# Protocol — <session slug>

**Status:** завершена

## Abstract

<one to three paragraphs — the whole session in compressed form>

## Chapters
...
```

### Idempotence

- Re-running `!протокол` when all sealed chapters are already present in `## Chapters` → no-op (Stage 2 detects the per-chapter marker and skips).
- Per-chapter marker: a chapter section is considered **appended** when its `### NNN — <ChapterSlug>` header is present in `protocol.md`. The protocolist matches by `NNN` first, then by full header (drift in `<ChapterSlug>` between transcript and protocol is repaired by the cleanup pass).
- Re-running `!протокол финиш` when `**Status:** завершена` and no new chapters → no-op.
- Running `!протокол` (ordinary) when `**Status:** завершена` → refuses with `[error] protocol finalized — explicit reopen required`. Reopen procedure: user manually flips status back to `в работе` and removes Abstract.

### Atomicity

All writes to `protocol.md` are temp+rename atomic (`os.replace`). Partial reads of `protocol.md` mid-write never observable. Chapter-file writes follow the same rule.

### Multi-file commit order (for one chapter)

The protocolist commits a chapter through this order — any crash between steps leaves a recoverable state, repaired on the next invocation by the cleanup pass:

1. Write chapter-file at `transcript/NNN_<ChapterSlug>.md` (temp + atomic rename).
2. Update `index.json` — set `file` field for every covered turn entry to the chapter-file name (atomic rewrite of `index.json`). **This is the seal commit point.** Once (2) commits, the Stop hook treats those entries as downstream-owned and will not resurrect originals; entries pointing at a chapter-file are also skipped by Stop-hook reconciliation triggers (see [`../stop_hook.md`](../stop_hook.md) §Reconciliation triggers).
3. *(Optional)* Delete the original `NNN_msg.md` files in the covered range. Cleanup may defer or skip this — the chapter is fully sealed after (2) regardless of whether originals remain on disk. The Stop hook's `index.json`-driven invariant guarantees they will not be regenerated.
4. Append chapter section to `protocol.md` `## Chapters` (temp + atomic rename).

A crash between (1) and (2) leaves an orphan chapter-file with no `index.json` pointers — cleanup deletes the orphan and retries from (1).
A crash between (2) and (3) is benign — the chapter is sealed; stale `NNN_msg.md` files cause no incorrectness; cleanup may delete them lazily for hygiene.
A crash between (3) and (4) leaves a sealed chapter not yet appended to protocol — cleanup detects it by scanning `index.json` for sealed chapters whose header is missing in `protocol.md` and resumes from (4).

**Reversing the seal order is a contract violation.** Deleting originals before the atomic `index.json` rewrite (i.e., reordering (2) and (3)) opens a window in which the Stop hook would resurrect the deleted `NNN_msg.md` files on its next fire, producing duplicate content.
