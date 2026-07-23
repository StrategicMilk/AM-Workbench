import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const fixturesDir = path.dirname(fileURLToPath(import.meta.url));

export function fixturePath(name: string): string {
  if (!name || name.includes('..') || path.isAbsolute(name)) {
    throw new Error(`invalid fixture name: ${name || '<missing>'}`);
  }
  return path.join(fixturesDir, 'fixtures', name);
}

export async function readJsonFixture<T = unknown>(name: string): Promise<T> {
  const target = fixturePath(name);
  let raw: string;
  try {
    raw = await fs.readFile(target, 'utf8');
  } catch (error) {
    throw new Error(`fixture unreadable: ${target}`, { cause: error });
  }
  try {
    return JSON.parse(raw) as T;
  } catch (error) {
    throw new Error(`fixture json invalid: ${target}`, { cause: error });
  }
}
