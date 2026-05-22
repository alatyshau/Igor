// ObjectiveCode: `OBJ<xxx>` where xxx is 3-char base-36 (0-9 then A-Z).
// Per cockpit/specs/schemas/obj_folder.md.

const OBJ_PREFIX = "OBJ";
const CODE_LEN = 3;
const CODE_PATTERN = /^OBJ[0-9A-Z]{3}$/;

export function isValidObjectiveCode(code: string): boolean {
  return CODE_PATTERN.test(code);
}

export function codeToInt(code: string): number {
  if (!isValidObjectiveCode(code)) {
    throw new Error(`invalid objective code: ${code}`);
  }
  return parseInt(code.slice(OBJ_PREFIX.length), 36);
}

export function intToCode(n: number): string {
  if (!Number.isInteger(n) || n < 0 || n >= 36 ** CODE_LEN) {
    throw new Error(`objective index out of range: ${n}`);
  }
  return OBJ_PREFIX + n.toString(36).toUpperCase().padStart(CODE_LEN, "0");
}

export function nextCode(existing: Iterable<string>): string {
  let max = -1;
  for (const code of existing) {
    if (!isValidObjectiveCode(code)) continue;
    const n = codeToInt(code);
    if (n > max) max = n;
  }
  return intToCode(max + 1);
}

// SubEntityCode within an OBJ: I01, S07, T12 (Letter + 2-digit decimal).
// State.md sub-entities under a Problem use the form `P3.I01` externally;
// inside the parent file they're just `I01`.
const SUB_PATTERN = /^([IST])(\d{2})$/;

export type SubEntityType = "I" | "S" | "T";

export function isValidSubEntityCode(code: string): code is string {
  return SUB_PATTERN.test(code);
}

export function parseSubEntityCode(code: string): { type: SubEntityType; index: number } {
  const m = SUB_PATTERN.exec(code);
  if (!m) throw new Error(`invalid sub-entity code: ${code}`);
  return { type: m[1] as SubEntityType, index: parseInt(m[2]!, 10) };
}

export function formatSubEntityCode(type: SubEntityType, index: number): string {
  if (!Number.isInteger(index) || index < 1 || index > 99) {
    throw new Error(`sub-entity index out of range: ${index}`);
  }
  return `${type}${index.toString().padStart(2, "0")}`;
}

export function nextSubEntityIndex(existing: Iterable<string>, type: SubEntityType): number {
  let max = 0;
  for (const code of existing) {
    if (!SUB_PATTERN.test(code)) continue;
    const parsed = parseSubEntityCode(code);
    if (parsed.type !== type) continue;
    if (parsed.index > max) max = parsed.index;
  }
  return max + 1;
}
