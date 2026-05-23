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
