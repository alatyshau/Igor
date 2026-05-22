import { test } from "node:test";
import assert from "node:assert/strict";
import { isValidSlug } from "../src/slug.js";
import {
  isValidObjectiveState,
  isValidStateForType,
  objectiveSubdir,
} from "../src/state.js";

test("slug: CamelCase and snake_case accepted", () => {
  assert.equal(isValidSlug("ChatEntitySystem"), true);
  assert.equal(isValidSlug("camelCase"), true);
  assert.equal(isValidSlug("snake_case_slug"), true);
  assert.equal(isValidSlug("Mixed_Style"), true);
});

test("slug: dashes and dots and leading digits rejected", () => {
  assert.equal(isValidSlug("with-dash"), false);
  assert.equal(isValidSlug("with.dot"), false);
  assert.equal(isValidSlug("with space"), false);
  assert.equal(isValidSlug("1starts_with_digit"), false);
  assert.equal(isValidSlug(""), false);
});

test("ObjectiveState: enum membership", () => {
  for (const s of ["draft", "open", "closed", "canceled", "backlog"]) {
    assert.equal(isValidObjectiveState(s), true, `expected ${s} to be valid`);
  }
  assert.equal(isValidObjectiveState("confirmed"), false);
  assert.equal(isValidObjectiveState("done"), false);
});

test("ObjectiveState: subdir mapping", () => {
  assert.equal(objectiveSubdir("draft"), null);
  assert.equal(objectiveSubdir("open"), null);
  assert.equal(objectiveSubdir("closed"), "closed");
  assert.equal(objectiveSubdir("canceled"), "cancelled");
  assert.equal(objectiveSubdir("backlog"), "backlog");
});

test("Sub-entity states are type-scoped", () => {
  // Issue
  assert.equal(isValidStateForType("I", "open"), true);
  assert.equal(isValidStateForType("I", "closed"), true);
  assert.equal(isValidStateForType("I", "canceled"), true);
  assert.equal(isValidStateForType("I", "confirmed"), false);

  // Suggestion
  assert.equal(isValidStateForType("S", "confirmed"), true);
  assert.equal(isValidStateForType("S", "declined"), true);
  assert.equal(isValidStateForType("S", "closed"), false);

  // Task
  assert.equal(isValidStateForType("T", "closed"), true);
  assert.equal(isValidStateForType("T", "confirmed"), false);
});
