// Read/write the OBJ index.md file per cockpit/specs/schemas/obj_folder.md.

import { promises as fs } from "node:fs";
import * as path from "node:path";
import { atomicWriteText } from "./fs_atomic.js";
import type { ObjectiveState } from "./state.js";
import { isValidObjectiveCode } from "./code.js";

export interface ObjSkeleton {
  code: string;
  slug: string;
  state: ObjectiveState;
  goal: string;
  outputs: string[];
  why: string;
  blockedBy: string[];
}

const EMPTY_MARK = "*пусто*";

export function serializeObjSkeleton(o: ObjSkeleton): string {
  const lines: string[] = [];
  lines.push(`# ${o.code} ${o.slug}`);
  lines.push("");
  lines.push(`**State:** ${o.state}`);
  if (o.blockedBy.length > 0) {
    lines.push(`**Blocked by:** ${o.blockedBy.join(", ")}`);
  }
  lines.push("");
  lines.push("## WHAT");
  lines.push("");
  lines.push(`**Цель:** ${o.goal || EMPTY_MARK}`);
  lines.push("");
  lines.push("**Выходы:**");
  if (o.outputs.length === 0) {
    lines.push(EMPTY_MARK);
  } else {
    for (const out of o.outputs) lines.push(`- ${out}`);
  }
  lines.push("");
  lines.push("## WHY");
  lines.push("");
  lines.push(o.why || EMPTY_MARK);
  lines.push("");
  lines.push("## Items");
  lines.push("");
  lines.push(EMPTY_MARK);
  lines.push("");
  lines.push("## User Notes");
  lines.push("");
  lines.push(EMPTY_MARK);
  lines.push("");
  return lines.join("\n");
}

export async function writeObjIndexMd(folderPath: string, skel: ObjSkeleton): Promise<string> {
  await fs.mkdir(folderPath, { recursive: true });
  const indexPath = path.join(folderPath, "index.md");
  await atomicWriteText(indexPath, serializeObjSkeleton(skel));
  return indexPath;
}

// -- in-place mutations on an existing index.md text --

const STATE_LINE_RE = /^\*\*State:\*\*\s*\S+\s*$/m;
const BLOCKED_BY_LINE_RE = /^\*\*Blocked by:\*\*.*\r?\n?/m;
const STATE_ANCHOR_RE = /^\*\*State:\*\*.*$/m;

export function setObjectiveState(content: string, state: ObjectiveState): string {
  if (!STATE_LINE_RE.test(content)) {
    throw new Error("index.md missing **State:** line");
  }
  return content.replace(STATE_LINE_RE, `**State:** ${state}`);
}

export function setBlockedBy(content: string, blockedBy: readonly string[]): string {
  for (const code of blockedBy) {
    if (!isValidObjectiveCode(code)) {
      throw new Error(`invalid Blocked-by code: ${code}`);
    }
  }
  const stripped = content.replace(BLOCKED_BY_LINE_RE, "");
  if (blockedBy.length === 0) return stripped;

  // Insert a new Blocked-by line right after the State line.
  const newLine = `**Blocked by:** ${blockedBy.join(", ")}\n`;
  return stripped.replace(STATE_ANCHOR_RE, (match) => `${match}\n${newLine.trimEnd()}`);
}

// Parse items section for sub-entity operations.
// Format per line: `- [<code> <state>] <slug> — <text>`

const ITEM_LINE_RE = /^- \[([A-Z][0-9]{2}) ([a-z|]+)\]\s+(\S+)(?:\s+—\s+(.*))?$/;

export interface ItemLine {
  code: string;
  state: string;
  slug: string;
  text: string;
}

export function parseItems(content: string): ItemLine[] {
  const itemsSection = extractSection(content, "## Items");
  if (!itemsSection) return [];
  const items: ItemLine[] = [];
  for (const line of itemsSection.split(/\r?\n/)) {
    const m = ITEM_LINE_RE.exec(line);
    if (!m) continue;
    items.push({
      code: m[1]!,
      state: m[2]!,
      slug: m[3]!,
      text: m[4] ?? "",
    });
  }
  return items;
}

function extractSection(content: string, header: string): string | null {
  const pattern = new RegExp(
    `^${escapeRe(header)}\\s*$([\\s\\S]*?)(?=^##\\s|\\Z)`,
    "m",
  );
  const m = pattern.exec(content);
  return m ? m[1]! : null;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function appendItem(content: string, item: ItemLine): string {
  const line = formatItem(item);
  const itemsHeader = /^## Items\s*$/m;
  const match = itemsHeader.exec(content);
  if (!match) throw new Error("index.md missing `## Items` section");

  // Find the section body: from after the header to the next `## ` or end.
  const headerEnd = match.index + match[0].length;
  const after = content.slice(headerEnd);
  const nextSection = /\n## /.exec(after);
  const bodyEnd = nextSection ? headerEnd + nextSection.index : content.length;
  const body = content.slice(headerEnd, bodyEnd);

  // If body contains the empty marker, replace it; else append before the next section.
  if (body.includes(EMPTY_MARK)) {
    const replaced = body.replace(EMPTY_MARK, line);
    return content.slice(0, headerEnd) + replaced + content.slice(bodyEnd);
  }
  // Insert before trailing whitespace.
  const trimmed = body.replace(/\s*$/, "");
  const newBody = `${trimmed}\n${line}\n`;
  return content.slice(0, headerEnd) + newBody + content.slice(bodyEnd);
}

export function formatItem(item: ItemLine): string {
  const head = `- [${item.code} ${item.state}] ${item.slug}`;
  return item.text ? `${head} — ${item.text}` : head;
}

// Replace the state tag in a `- [<code> <state>] ...` line.
export function setItemState(
  content: string,
  code: string,
  newState: string,
): string {
  const lineRe = new RegExp(`^(- \\[${escapeRe(code)} )[a-z|]+(\\].*)$`, "m");
  if (!lineRe.test(content)) {
    throw new Error(`item ${code} not found in ## Items`);
  }
  return content.replace(lineRe, `$1${newState}$2`);
}

// Cascade-cancel: mark all `open` items as `canceled`.
export function cascadeCancelOpenItems(content: string): { content: string; changed: string[] } {
  const items = parseItems(content);
  const changed: string[] = [];
  let next = content;
  for (const it of items) {
    if (it.state === "open") {
      next = setItemState(next, it.code, "canceled");
      changed.push(it.code);
    }
  }
  return { content: next, changed };
}
