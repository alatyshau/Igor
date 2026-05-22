// ObjectiveCode: `OBJ<xxx>` under a dual-range scheme defined in
// cockpit/specs/schemas/obj_folder.md §"ObjectiveCode":
//   - Digit range: 000..999 — three decimal digits, pure decimal encoding
//     (1000 codes, indices 0..999).
//   - Alphanumeric range: A00..ZZZ — position 1 is A-Z (26 values), positions
//     2 and 3 cycle 0-9 then A-Z (36 values each), giving 26*36*36 = 33696
//     codes, indices 1000..34695.
//
// Codes like "00A", "0AB", "5BC" are syntactically base-36 alphanumeric but
// unreachable under this allocator and are treated as invalid.

const OBJ_PREFIX = "OBJ";
const CODE_LEN = 3;
const SHAPE_PATTERN = /^OBJ[0-9A-Z]{3}$/;

const DIGIT_RANGE_SIZE = 1000;
const ALPHA_FIRST_POS = 26; // A-Z in position 1
const ALPHA_REST_POS = 36; // 0-9 then A-Z in positions 2/3
const ALPHA_RANGE_SIZE = ALPHA_FIRST_POS * ALPHA_REST_POS * ALPHA_REST_POS;
export const MAX_OBJECTIVE_INDEX = DIGIT_RANGE_SIZE + ALPHA_RANGE_SIZE - 1; // 34695

function isDigitChar(c: string): boolean {
  return c >= "0" && c <= "9";
}

// Position 2/3 char (0-9 then A-Z) → numeric value 0..35.
function alphaPosValue(c: string): number {
  if (isDigitChar(c)) return c.charCodeAt(0) - "0".charCodeAt(0);
  return c.charCodeAt(0) - "A".charCodeAt(0) + 10;
}

// Numeric value 0..35 → position 2/3 char (0-9 then A-Z).
function alphaPosChar(v: number): string {
  if (v < 10) return String(v);
  return String.fromCharCode("A".charCodeAt(0) + v - 10);
}

export function isValidObjectiveCode(code: string): boolean {
  if (!SHAPE_PATTERN.test(code)) return false;
  const body = code.slice(OBJ_PREFIX.length);
  const p1 = body[0]!;
  if (isDigitChar(p1)) {
    // Digit range — positions 2 and 3 must also be digits.
    return isDigitChar(body[1]!) && isDigitChar(body[2]!);
  }
  // Alphanumeric range — p1 is A-Z; positions 2/3 may be any base-36 char,
  // which is guaranteed by the shape pattern.
  return true;
}

export function codeToInt(code: string): number {
  if (!isValidObjectiveCode(code)) {
    throw new Error(`invalid or unreachable objective code: ${code}`);
  }
  const body = code.slice(OBJ_PREFIX.length);
  const p1 = body[0]!;
  if (isDigitChar(p1)) {
    return parseInt(body, 10);
  }
  const p1Index = p1.charCodeAt(0) - "A".charCodeAt(0);
  const p2Value = alphaPosValue(body[1]!);
  const p3Value = alphaPosValue(body[2]!);
  return (
    DIGIT_RANGE_SIZE +
    p1Index * ALPHA_REST_POS * ALPHA_REST_POS +
    p2Value * ALPHA_REST_POS +
    p3Value
  );
}

export function intToCode(n: number): string {
  if (!Number.isInteger(n) || n < 0 || n > MAX_OBJECTIVE_INDEX) {
    throw new Error(`objective index out of range: ${n}`);
  }
  if (n < DIGIT_RANGE_SIZE) {
    return OBJ_PREFIX + n.toString().padStart(CODE_LEN, "0");
  }
  const offset = n - DIGIT_RANGE_SIZE;
  const p1Index = Math.floor(offset / (ALPHA_REST_POS * ALPHA_REST_POS));
  const p2Value = Math.floor(offset / ALPHA_REST_POS) % ALPHA_REST_POS;
  const p3Value = offset % ALPHA_REST_POS;
  const p1Char = String.fromCharCode("A".charCodeAt(0) + p1Index);
  return OBJ_PREFIX + p1Char + alphaPosChar(p2Value) + alphaPosChar(p3Value);
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
