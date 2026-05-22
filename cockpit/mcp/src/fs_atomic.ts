// Atomic file operations: write via temp + rename so partial writes are never observable.

import { promises as fs } from "node:fs";
import * as path from "node:path";

export async function atomicWriteText(filePath: string, content: string): Promise<void> {
  const dir = path.dirname(filePath);
  const base = path.basename(filePath);
  const tmp = path.join(dir, `.${base}.${process.pid}.${Date.now()}.tmp`);
  try {
    await fs.writeFile(tmp, content, "utf8");
    await fs.rename(tmp, filePath);
  } catch (err) {
    await fs.rm(tmp, { force: true }).catch(() => {});
    throw err;
  }
}

export async function pathExists(p: string): Promise<boolean> {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}
