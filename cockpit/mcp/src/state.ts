// State machines per entity type. See cockpit/specs/domain-model.md.

import type { SubEntityType } from "./code.js";

export const OBJECTIVE_STATES = ["draft", "open", "closed", "canceled", "backlog"] as const;
export type ObjectiveState = (typeof OBJECTIVE_STATES)[number];

export function isValidObjectiveState(s: string): s is ObjectiveState {
  return (OBJECTIVE_STATES as readonly string[]).includes(s);
}

// State subfolder mapping under objectives/.
export function objectiveSubdir(state: ObjectiveState): string | null {
  switch (state) {
    case "draft":
    case "open":
      return null; // active = top-level objectives/
    case "closed":
      return "closed";
    case "canceled":
      return "cancelled"; // note: schema uses British "cancelled" for folder
    case "backlog":
      return "backlog";
  }
}

// Sub-entity states per type. Per domain-model.md §1.4, Suggestion has no
// `declined` state — active reject and silent withdrawal both map to `canceled`.
const ISSUE_STATES = ["open", "closed", "canceled"] as const;
const SUGGESTION_STATES = ["open", "confirmed", "canceled"] as const;
const TASK_STATES = ["open", "closed", "canceled"] as const;

export type IssueState = (typeof ISSUE_STATES)[number];
export type SuggestionState = (typeof SUGGESTION_STATES)[number];
export type TaskState = (typeof TASK_STATES)[number];
export type SubEntityState = IssueState | SuggestionState | TaskState;

export function validStatesFor(type: SubEntityType): readonly string[] {
  switch (type) {
    case "I":
      return ISSUE_STATES;
    case "S":
      return SUGGESTION_STATES;
    case "T":
      return TASK_STATES;
  }
}

export function isValidStateForType(type: SubEntityType, state: string): boolean {
  return (validStatesFor(type) as readonly string[]).includes(state);
}

export function isTerminalSubState(state: string): boolean {
  return ["closed", "canceled", "confirmed"].includes(state);
}

// Initial states allowed for objective_create. Terminal states (closed,
// canceled) cannot be the *initial* state — they are only reached via
// transition from an active state with proper preconditions (closure ritual,
// cascade, etc.).
export const VALID_INITIAL_OBJECTIVE_STATES = ["draft", "open", "backlog"] as const;
export type InitialObjectiveState = (typeof VALID_INITIAL_OBJECTIVE_STATES)[number];

export function isValidInitialObjectiveState(s: string): s is InitialObjectiveState {
  return (VALID_INITIAL_OBJECTIVE_STATES as readonly string[]).includes(s);
}
