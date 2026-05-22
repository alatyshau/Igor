// Read/write the session state.md for Problem-side sub-entity operations.
// Format per cockpit/specs/schemas/session_folder.md.

import { promises as fs } from "node:fs";
import * as path from "node:path";
import { atomicWriteText } from "./fs_atomic.js";
import { formatSubEntityCode, nextSubEntityIndex, type SubEntityType } from "./code.js";

const PROBLEM_PATTERN = /^P[1-9]$/;
// Lines under a Problem look like:
//   - P3.I01 Slug (open) — description
const PROBLEM_SUB_LINE_RE =
  /^\s+- (P[1-9])\.([IST])(\d{2})\s+(\S+)(?:\s+\((\S+)\))?(?:\s+—\s+(.*))?$/;

export function isValidProblemCode(code: string): boolean {
  return PROBLEM_PATTERN.test(code);
}

export interface ProblemSubItem {
  problem: string; // "P3"
  type: SubEntityType; // I | S | T
  code: string; // "I01"
  slug: string;
  state: string;
  text: string;
}

export function readStateMdSync(content: string): { problemSubItems: ProblemSubItem[] } {
  const items: ProblemSubItem[] = [];
  for (const line of content.split(/\r?\n/)) {
    const m = PROBLEM_SUB_LINE_RE.exec(line);
    if (!m) continue;
    items.push({
      problem: m[1]!,
      type: m[2] as SubEntityType,
      code: `${m[2]}${m[3]}`,
      slug: m[4]!,
      state: m[5] ?? "open",
      text: m[6] ?? "",
    });
  }
  return { problemSubItems: items };
}

export function nextProblemSubCode(
  content: string,
  problem: string,
  type: SubEntityType,
): string {
  const { problemSubItems } = readStateMdSync(content);
  const codes = problemSubItems.filter((it) => it.problem === problem).map((it) => it.code);
  const idx = nextSubEntityIndex(codes, type);
  return formatSubEntityCode(type, idx);
}

export function appendProblemSubItem(
  content: string,
  problem: string,
  item: { type: SubEntityType; code: string; slug: string; state: string; text: string },
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

export function setProblemSubItemState(
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
    throw new Error(`sub-entity ${fullCode} not found in state.md`);
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
