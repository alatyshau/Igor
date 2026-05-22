import { test } from "node:test";
import assert from "node:assert/strict";
import {
  codeToInt,
  formatSubEntityCode,
  intToCode,
  isValidObjectiveCode,
  isValidSubEntityCode,
  nextCode,
  nextSubEntityIndex,
  parseSubEntityCode,
} from "../src/code.js";

test("ObjectiveCode: valid forms", () => {
  assert.equal(isValidObjectiveCode("OBJ000"), true);
  assert.equal(isValidObjectiveCode("OBJ009"), true);
  assert.equal(isValidObjectiveCode("OBJ00A"), true);
  assert.equal(isValidObjectiveCode("OBJZZZ"), true);
});

test("ObjectiveCode: invalid forms", () => {
  assert.equal(isValidObjectiveCode("OBJ001 "), false);
  assert.equal(isValidObjectiveCode("OBJ00"), false);
  assert.equal(isValidObjectiveCode("OBJ0001"), false);
  assert.equal(isValidObjectiveCode("PRJ001"), false);
  assert.equal(isValidObjectiveCode("OBJ00a"), false); // lowercase
});

test("ObjectiveCode: int round-trip", () => {
  assert.equal(codeToInt("OBJ000"), 0);
  assert.equal(codeToInt("OBJ009"), 9);
  assert.equal(codeToInt("OBJ00A"), 10);
  assert.equal(codeToInt("OBJ00Z"), 35);
  assert.equal(codeToInt("OBJ010"), 36);
  assert.equal(codeToInt("OBJZZZ"), 36 ** 3 - 1);

  assert.equal(intToCode(0), "OBJ000");
  assert.equal(intToCode(35), "OBJ00Z");
  assert.equal(intToCode(36), "OBJ010");
  assert.equal(intToCode(36 ** 3 - 1), "OBJZZZ");
});

test("ObjectiveCode: nextCode picks max+1", () => {
  assert.equal(nextCode([]), "OBJ000");
  assert.equal(nextCode(["OBJ000"]), "OBJ001");
  assert.equal(nextCode(["OBJ002", "OBJ000", "OBJ001"]), "OBJ003");
  assert.equal(nextCode(["OBJ009"]), "OBJ00A");
  assert.equal(nextCode(["OBJ00Z"]), "OBJ010");
  // non-OBJ entries are ignored
  assert.equal(nextCode(["junk", "OBJ005"]), "OBJ006");
});

test("ObjectiveCode: intToCode bounds", () => {
  assert.throws(() => intToCode(-1));
  assert.throws(() => intToCode(36 ** 3));
});

test("SubEntityCode: parse and format", () => {
  assert.equal(isValidSubEntityCode("I01"), true);
  assert.equal(isValidSubEntityCode("S99"), true);
  assert.equal(isValidSubEntityCode("T42"), true);
  assert.equal(isValidSubEntityCode("X01"), false);
  assert.equal(isValidSubEntityCode("I1"), false);

  const parsed = parseSubEntityCode("S07");
  assert.equal(parsed.type, "S");
  assert.equal(parsed.index, 7);

  assert.equal(formatSubEntityCode("I", 1), "I01");
  assert.equal(formatSubEntityCode("T", 99), "T99");
  assert.throws(() => formatSubEntityCode("I", 0));
  assert.throws(() => formatSubEntityCode("I", 100));
});

test("SubEntityCode: nextSubEntityIndex is type-scoped", () => {
  const codes = ["I01", "I02", "S01", "T05"];
  assert.equal(nextSubEntityIndex(codes, "I"), 3);
  assert.equal(nextSubEntityIndex(codes, "S"), 2);
  assert.equal(nextSubEntityIndex(codes, "T"), 6);
  assert.equal(nextSubEntityIndex([], "I"), 1);
});
