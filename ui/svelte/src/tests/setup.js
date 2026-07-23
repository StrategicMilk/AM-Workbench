import '@testing-library/jest-dom/vitest';

import { cleanupRenderedComponents } from './helpers/render.js';

globalThis.afterEach?.(() => {
  return cleanupRenderedComponents();
});
