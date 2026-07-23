import { describe, expect, it } from 'vitest';

import { collectAll } from './collectErrors.js';

describe('collectAll', () => {
  it('returns keyed values when all tasks pass', async () => {
    await expect(
      collectAll([
        { key: 'a', promise: Promise.resolve(1) },
        { key: 'b', promise: Promise.resolve(2) },
        { key: 'c', promise: Promise.resolve(3) },
      ]),
    ).resolves.toEqual({
      values: { a: 1, b: 2, c: 3 },
      errors: [],
    });
  });

  it('surfaces partial failures with successful values', async () => {
    await expect(
      collectAll([
        { key: 'a', promise: Promise.resolve(1) },
        { key: 'b', promise: Promise.reject(new Error('bad b')) },
        { key: 'c', promise: Promise.resolve(3) },
      ]),
    ).resolves.toEqual({
      values: { a: 1, c: 3 },
      errors: [{ key: 'b', error: 'bad b' }],
    });
  });

  it('surfaces all failures without values', async () => {
    await expect(
      collectAll([
        { key: 'a', promise: Promise.reject(new Error('bad a')) },
        { key: 'b', promise: Promise.reject(new Error('bad b')) },
        { key: 'c', promise: Promise.reject('bad c') },
      ]),
    ).resolves.toEqual({
      values: {},
      errors: [
        { key: 'a', error: 'bad a' },
        { key: 'b', error: 'bad b' },
        { key: 'c', error: 'bad c' },
      ],
    });
  });

  it('throws AggregateError when requested and any task fails', async () => {
    await expect(
      collectAll(
        [
          { key: 'a', promise: Promise.resolve(1) },
          { key: 'b', promise: Promise.reject(new Error('bad b')) },
        ],
        { throwOnAny: true },
      ),
    ).rejects.toThrow(AggregateError);
  });

  it('does not silently use fallback values for failed tasks', async () => {
    await expect(
      collectAll([{ key: 'a', promise: Promise.reject(new Error('bad a')), fallback: 1 }]),
    ).resolves.toEqual({
      values: {},
      errors: [{ key: 'a', error: 'bad a' }],
    });
  });
});
