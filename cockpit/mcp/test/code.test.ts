import { test } from "node:test";
import assert from "node:assert/strict";
import {
  codeToInt,
  formatTicketCode,
  intToCode,
  isValidObjectiveCode,
  isValidTicketCode,
  nextCode,
  nextTicketIndex,
  parseTicketCode,
} from "../src/code.js";

// Per cockpit/specs/schemas/obj_folder.md §"ObjectiveCode":
// - Initial range: three digits (000–999). 1000 codes.
// - When exhausted, extends to A00..A09, A0A..A0Z, A10..ZZZ — first position
//   becomes A-Z (26 values), middle and right cycle 0-9 then A-Z (36 values).
// - Total reachable codes: 1000 + 26*36*36 = 34696.
// Codes like OBJ00A, OBJ0A0, OBJ5AB are syntactically base-36 alphanumeric
// but unreachable under the dual-range allocator — they are invalid.

test("ObjectiveCode: valid forms (dual-range)", () => {
  // Digit range
  assert.equal(isValidObjectiveCode("OBJ000"), true);
  assert.equal(isValidObjectiveCode("OBJ009"), true);
  assert.equal(isValidObjectiveCode("OBJ010"), true);
  assert.equal(isValidObjectiveCode("OBJ099"), true);
  assert.equal(isValidObjectiveCode("OBJ999"), true);

  // Alphanumeric range
  assert.equal(isValidObjectiveCode("OBJA00"), true);
  assert.equal(isValidObjectiveCode("OBJA09"), true);
  assert.equal(isValidObjectiveCode("OBJA0A"), true);
  assert.equal(isValidObjectiveCode("OBJA0Z"), true);
  assert.equal(isValidObjectiveCode("OBJA10"), true);
  assert.equal(isValidObjectiveCode("OBJAZZ"), true);
  assert.equal(isValidObjectiveCode("OBJZZZ"), true);
});

test("ObjectiveCode: invalid forms", () => {
  // Trailing space, wrong length, wrong prefix, lowercase
  assert.equal(isValidObjectiveCode("OBJ001 "), false);
  assert.equal(isValidObjectiveCode("OBJ00"), false);
  assert.equal(isValidObjectiveCode("OBJ0001"), false);
  assert.equal(isValidObjectiveCode("PRJ001"), false);
  assert.equal(isValidObjectiveCode("OBJ00a"), false); // lowercase

  // Unreachable under dual-range: digit in position 1 forbids letters in 2 or 3
  assert.equal(isValidObjectiveCode("OBJ00A"), false);
  assert.equal(isValidObjectiveCode("OBJ0A0"), false);
  assert.equal(isValidObjectiveCode("OBJ5AB"), false);
  assert.equal(isValidObjectiveCode("OBJ9ZZ"), false);
});

test("ObjectiveCode: int round-trip — digit range (decimal encoding)", () => {
  assert.equal(codeToInt("OBJ000"), 0);
  assert.equal(codeToInt("OBJ009"), 9);
  assert.equal(codeToInt("OBJ010"), 10);
  assert.equal(codeToInt("OBJ099"), 99);
  assert.equal(codeToInt("OBJ100"), 100);
  assert.equal(codeToInt("OBJ999"), 999);

  assert.equal(intToCode(0), "OBJ000");
  assert.equal(intToCode(9), "OBJ009");
  assert.equal(intToCode(10), "OBJ010");
  assert.equal(intToCode(99), "OBJ099");
  assert.equal(intToCode(100), "OBJ100");
  assert.equal(intToCode(999), "OBJ999");
});

test("ObjectiveCode: int round-trip — alphanumeric range (A00 = 1000)", () => {
  // Position cycles: pos1 = A-Z, pos2/pos3 = 0-9 then A-Z.
  assert.equal(codeToInt("OBJA00"), 1000);
  assert.equal(codeToInt("OBJA01"), 1001);
  assert.equal(codeToInt("OBJA09"), 1009);
  assert.equal(codeToInt("OBJA0A"), 1010);
  assert.equal(codeToInt("OBJA0Z"), 1035);
  assert.equal(codeToInt("OBJA10"), 1036);
  assert.equal(codeToInt("OBJA1Z"), 1071);
  assert.equal(codeToInt("OBJAZZ"), 1000 + 35 * 36 + 35); // 2295
  assert.equal(codeToInt("OBJB00"), 1000 + 36 * 36); // 2296
  assert.equal(codeToInt("OBJZZZ"), 1000 + 25 * 36 * 36 + 35 * 36 + 35); // 34695

  assert.equal(intToCode(1000), "OBJA00");
  assert.equal(intToCode(1009), "OBJA09");
  assert.equal(intToCode(1010), "OBJA0A");
  assert.equal(intToCode(1035), "OBJA0Z");
  assert.equal(intToCode(1036), "OBJA10");
  assert.equal(intToCode(2295), "OBJAZZ");
  assert.equal(intToCode(2296), "OBJB00");
  assert.equal(intToCode(34695), "OBJZZZ");
});

test("ObjectiveCode: nextCode follows dual-range", () => {
  assert.equal(nextCode([]), "OBJ000");
  assert.equal(nextCode(["OBJ000"]), "OBJ001");
  assert.equal(nextCode(["OBJ009"]), "OBJ010"); // not OBJ00A — digit range continues
  assert.equal(nextCode(["OBJ010"]), "OBJ011");
  assert.equal(nextCode(["OBJ099"]), "OBJ100");
  assert.equal(nextCode(["OBJ999"]), "OBJA00"); // digit range exhausted → alpha
  assert.equal(nextCode(["OBJA00"]), "OBJA01");
  assert.equal(nextCode(["OBJA09"]), "OBJA0A");
  assert.equal(nextCode(["OBJA0Z"]), "OBJA10");
  assert.equal(nextCode(["OBJA1Z"]), "OBJA20");
  assert.equal(nextCode(["OBJAZZ"]), "OBJB00");

  // Unordered input
  assert.equal(nextCode(["OBJ002", "OBJ000", "OBJ001"]), "OBJ003");
  // Non-OBJ and unreachable entries are ignored
  assert.equal(nextCode(["junk", "OBJ005", "OBJ00A"]), "OBJ006");
});

test("ObjectiveCode: intToCode bounds", () => {
  assert.throws(() => intToCode(-1));
  assert.throws(() => intToCode(34696)); // 1 past ZZZ
});

test("ObjectiveCode: codeToInt rejects unreachable forms", () => {
  // Even if regex-shaped, these are unreachable under dual-range.
  assert.throws(() => codeToInt("OBJ00A"));
  assert.throws(() => codeToInt("OBJ0A0"));
  assert.throws(() => codeToInt("OBJ5AB"));
});

test("TicketCode: parse and format", () => {
  assert.equal(isValidTicketCode("I01"), true);
  assert.equal(isValidTicketCode("S99"), true);
  assert.equal(isValidTicketCode("T42"), true);
  assert.equal(isValidTicketCode("X01"), false);
  assert.equal(isValidTicketCode("I1"), false);

  const parsed = parseTicketCode("S07");
  assert.equal(parsed.type, "S");
  assert.equal(parsed.index, 7);

  assert.equal(formatTicketCode("I", 1), "I01");
  assert.equal(formatTicketCode("T", 99), "T99");
  assert.throws(() => formatTicketCode("I", 0));
  assert.throws(() => formatTicketCode("I", 100));
});

test("TicketCode: nextTicketIndex is type-scoped", () => {
  const codes = ["I01", "I02", "S01", "T05"];
  assert.equal(nextTicketIndex(codes, "I"), 3);
  assert.equal(nextTicketIndex(codes, "S"), 2);
  assert.equal(nextTicketIndex(codes, "T"), 6);
  assert.equal(nextTicketIndex([], "I"), 1);
});
