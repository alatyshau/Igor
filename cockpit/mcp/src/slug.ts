// Slug rules per cockpit/specs/schemas/obj_folder.md and session_folder.md:
// camelCase or snake_case; no dashes, dots, or other splitting characters.

const SLUG_PATTERN = /^[A-Za-z][A-Za-z0-9_]*$/;

export function isValidSlug(slug: string): boolean {
  return SLUG_PATTERN.test(slug);
}

export function assertValidSlug(slug: string): void {
  if (!isValidSlug(slug)) {
    throw new Error(
      `invalid slug "${slug}": must start with a letter and contain only [A-Za-z0-9_]`,
    );
  }
}
