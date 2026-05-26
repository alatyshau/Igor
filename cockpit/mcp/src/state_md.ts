// Read/write the session state.md for Problem-side ticket operations.
// Format per cockpit/specs/schemas/session_folder.md.

import { promises as fs } from "node:fs";
import * as path from "node:path";
import { atomicWriteText } from "./fs_atomic.js";
import { formatTicketCode, nextTicketIndex, type TicketType } from "./code.js";

const PROBLEM_PATTERN = /^P[1-9]$/;
// Lines under a Problem look like:
//   - P3.I01 Slug (open) — description
const PROBLEM_SUB_LINE_RE =
  /^\s+- (P[1-9])\.([IST])(\d{2})\s+(\S+)(?:\s+\((\S+)\))?(?:\s+—\s+(.*))?$/;

export function isValidProblemCode(code: string): boolean {
  return PROBLEM_PATTERN.test(code);
}

export interface ProblemTicket {
  problem: string; // "P3"
  type: TicketType; // I | S | T
  code: string; // "I01"
  slug: string;
  state: string;
  text: string;
}

export function readStateMdSync(content: string): { problemTickets: ProblemTicket[] } {
  const items: ProblemTicket[] = [];
  for (const line of content.split(/\r?\n/)) {
    const m = PROBLEM_SUB_LINE_RE.exec(line);
    if (!m) continue;
    items.push({
      problem: m[1]!,
      type: m[2] as TicketType,
      code: `${m[2]}${m[3]}`,
      slug: m[4]!,
      state: m[5] ?? "open",
      text: m[6] ?? "",
    });
  }
  return { problemTickets: items };
}

export function nextProblemTicketCode(
  content: string,
  problem: string,
  type: TicketType,
): string {
  const { problemTickets } = readStateMdSync(content);
  const codes = problemTickets.filter((it) => it.problem === problem).map((it) => it.code);
  const idx = nextTicketIndex(codes, type);
  return formatTicketCode(type, idx);
}

export function appendProblemTicket(
  content: string,
  problem: string,
  item: { type: TicketType; code: string; slug: string; state: string; text: string },
): string {
  if (!isValidProblemCode(problem)) {
    throw new Error(`invalid Problem code: ${problem}`);
  }
  const headerRe = new RegExp(`^- ${problem}\\b.*$`, "m");
  const m = headerRe.exec(content);
  if (!m) {
    throw new Error(`Problem ${problem} not found in state.md`);
  }
  const insertAfter = m.index + m[0].length;

  // Find end of Problem block (next non-indented line or EOF).
  const after = content.slice(insertAfter);
  const blockEndRel = /\n(?=- |#|$)/.exec(after);
  const insertAt = blockEndRel ? insertAfter + blockEndRel.index : content.length;

  const line = `\n  - ${problem}.${item.code} ${item.slug} (${item.state})` +
    (item.text ? ` — ${item.text}` : "");
  return content.slice(0, insertAt) + line + content.slice(insertAt);
}

export function setProblemTicketState(
  content: string,
  problem: string,
  code: string,
  newState: string,
): string {
  const fullCode = `${problem}.${code}`;
  const lineRe = new RegExp(
    `^(\\s+- ${escapeRe(fullCode)}\\s+\\S+\\s+\\()\\S+(\\))(.*)$`,
    "m",
  );
  if (!lineRe.test(content)) {
    throw new Error(`ticket ${fullCode} not found in state.md`);
  }
  return content.replace(lineRe, `$1${newState}$2$3`);
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export async function readStateMd(sessionFolder: string): Promise<string> {
  const p = path.join(sessionFolder, "state.md");
  return fs.readFile(p, "utf8");
}

export async function writeStateMd(sessionFolder: string, content: string): Promise<void> {
  const p = path.join(sessionFolder, "state.md");
  await atomicWriteText(p, content);
}

// ---------------------------------------------------------------------------
// ## Subchats — MCP-owned section, written by spawn_subchat
// ---------------------------------------------------------------------------
//
// Format (see schemas/session_folder.md §Sections):
//
//   ## Subchats
//   - protocolist (active)
//
// Section ownership: this section is mutated only by MCP `spawn_subchat`.
// Igor never edits it. Other sections (`## Input`, `## Scope`, `## Problems`)
// are preserved verbatim by this function — merge-preserving discipline.

const SUBCHATS_HEADING = "## Subchats";

/** Re-spawn policy for a line that already lists the named subagent with a
 *  state other than ``active`` (i.e., ``terminated``).
 *
 *  Choice: re-spawn reactivates. ``spawn_subchat`` regenerates config.yaml
 *  and system_prompt.md from the current profile — semantically this is a
 *  fresh start for the subagent, so the section state should reflect that.
 *  The alternative (preserve the previous state) would leave the section
 *  contradicting the on-disk reality (active config, "terminated" label).
 *  Documented in T05b/done.md §Key decisions. */
const SUBCHAT_REACTIVATE_ON_RESPAWN = true;

/** Ensure a ``- <name> (active)`` bullet exists in the ``## Subchats``
 *  section of ``content``. Idempotent: no-op if the line is already there
 *  with state ``active``. Creates the section if it is absent.
 *
 *  Other sections of ``state.md`` are returned verbatim. Returns the (new)
 *  full file content; the caller writes it atomically via ``writeStateMd``. */
export function ensureSubchatLine(content: string, subagent: string): string {
  // Known limitation (T05b adversarial Finding 3): the regex anchors at the
  // start of line with no leading-whitespace allowance, so a pre-existing
  // indented bullet like "  - protocolist (active)" would not match and a
  // duplicate column-0 bullet would be inserted. Safe in practice because
  // `## Subchats` is MCP-owned per schemas/session_folder.md §Section ownership
  // — only this function writes it, and it always emits column-0 bullets.
  // Harden to `/^\s*- .../m` if hand-edited state.md ever needs tolerance.
  const lineRe = new RegExp(
    `^- ${escapeRe(subagent)}\\s*\\(([^)]+)\\)\\s*$`,
    "m",
  );

  const section = findSection(content, SUBCHATS_HEADING);
  if (section === null) {
    return appendSubchatsSection(content, subagent);
  }

  const sectionText = content.slice(section.bodyStart, section.bodyEnd);
  const match = lineRe.exec(sectionText);
  if (match) {
    if (match[1] === "active") return content; // already active — no-op
    if (!SUBCHAT_REACTIVATE_ON_RESPAWN) return content;
    // Reactivate: rewrite this line's state to ``active`` while leaving
    // every other line and section verbatim.
    const before = content.slice(0, section.bodyStart);
    const after = content.slice(section.bodyEnd);
    const newSection = sectionText.replace(
      lineRe,
      `- ${subagent} (active)`,
    );
    return before + newSection + after;
  }

  // Section present, line absent — insert at end of section block.
  return insertLineAtSectionEnd(content, section, `- ${subagent} (active)`);
}

interface SectionRange {
  /** Index where the section's body starts (immediately after the heading
   *  line's trailing newline). */
  bodyStart: number;
  /** Index where the section's body ends — at the next ``## ``-prefixed
   *  heading, or the end of file. */
  bodyEnd: number;
}

function findSection(content: string, heading: string): SectionRange | null {
  const headingRe = new RegExp(`^${escapeRe(heading)}\\s*$`, "m");
  const m = headingRe.exec(content);
  if (!m) return null;
  // Body starts after the heading line + its newline (if any).
  let bodyStart = m.index + m[0].length;
  if (content[bodyStart] === "\n") bodyStart += 1;
  // Body ends at the next ``## `` heading or EOF.
  const rest = content.slice(bodyStart);
  const nextRe = /^## /m;
  const next = nextRe.exec(rest);
  const bodyEnd = next ? bodyStart + next.index : content.length;
  return { bodyStart, bodyEnd };
}

function insertLineAtSectionEnd(
  content: string,
  section: SectionRange,
  line: string,
): string {
  // Known limitation (T05b adversarial Finding 2): inserted line endings are
  // hardcoded LF. If `content` uses CRLF (e.g., a state.md hand-edited on
  // Windows), the result is mixed-EOL. Non-blocking today: state.md is
  // created with LF by the UserPromptSubmit hook (see schemas/session_folder.md
  // §Lifecycle); CRLF arises only via cross-platform hand-editing, which is
  // discouraged on MCP-owned sections. Mirror the dominant EOL of `content`
  // here if hand-edited Windows-style state.md ever becomes a supported
  // surface.
  const before = content.slice(0, section.bodyEnd);
  const after = content.slice(section.bodyEnd);
  // Trim any trailing blank lines on the section body so the new bullet sits
  // tight against the existing entries; preserve them after the inserted line.
  const trimmed = before.replace(/\n*$/, "");
  return `${trimmed}\n${line}\n${after.startsWith("\n") ? after : (after ? "\n" + after : "")}`;
}

function appendSubchatsSection(content: string, subagent: string): string {
  // Append at end of file, separated by a single blank line from prior
  // content. Placement note: schemas/session_folder.md shows the section
  // after ``## Problems`` in its example layout, but the section is optional
  // and the spec does not mandate placement. End-of-file keeps the insertion
  // simple and never disturbs surrounding sections — documented in
  // T05b/done.md §Key decisions.
  const trimmed = content.replace(/\n*$/, "");
  const sep = trimmed === "" ? "" : "\n\n";
  return `${trimmed}${sep}${SUBCHATS_HEADING}\n- ${subagent} (active)\n`;
}
