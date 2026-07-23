let calls = [];
let activeRoutes = new Map();
let activeFetch = null;
let originalFetch = globalThis.fetch;

function getVitestApi() {
  return globalThis.vi ?? null;
}

function makeSpy(implementation) {
  const vi = getVitestApi();
  if (vi?.fn) {
    return vi.fn(implementation);
  }

  const spy = (...args) => {
    spy.mock.calls.push(args);
    return implementation(...args);
  };
  spy.mock = { calls: [] };
  return spy;
}

function normalizeMethod(method = 'GET') {
  return String(method || 'GET').toUpperCase();
}

function normalizePath(input) {
  const rawUrl = typeof input === 'string' ? input : input?.url;
  if (!rawUrl) {
    return '/';
  }

  try {
    const parsed = new URL(rawUrl, 'http://test.local');
    return parsed.pathname;
  } catch {
    return String(rawUrl).split('?')[0] || '/';
  }
}

function routeKey(method, url) {
  return `${normalizeMethod(method)} ${normalizePath(url)}`;
}

function normalizeRouteEntries(routes = {}) {
  return new Map(
    Object.entries(routes).map(([key, value]) => {
      const trimmed = key.trim();
      const hasMethodPrefix = /^[A-Za-z]+[\s:]+/.test(trimmed);
      if (hasMethodPrefix) {
        const [method, ...urlParts] = trimmed.split(/[\s:]+/);
        return [routeKey(method, urlParts.join(':')), value];
      }
      return [routeKey('GET', trimmed), value];
    }),
  );
}

function stringifyBody(body) {
  if (typeof body === 'string') {
    return body;
  }
  if (body === undefined || body === null) {
    return '';
  }
  return JSON.stringify(body);
}

function responseFromRoute(route) {
  const status = Number(route?.status ?? 200);
  const body = route?.body ?? null;
  const headers = new Headers(route?.headers ?? { 'content-type': 'application/json' });

  return {
    ok: status >= 200 && status < 400,
    status,
    statusText: route?.statusText ?? '',
    headers,
    json: makeSpy(async () => body),
    text: makeSpy(async () => stringifyBody(body)),
    clone() {
      return responseFromRoute(route);
    },
  };
}

async function resolveRoute(route, input, options) {
  if (route instanceof Error) {
    throw route;
  }
  if (typeof route === 'function') {
    return responseFromRoute(await route(input, options));
  }
  return responseFromRoute(route);
}

export function jsonResponse(body, status = 200, headers = {}) {
  return {
    status,
    body,
    headers: {
      'content-type': 'application/json',
      ...headers,
    },
  };
}

export function errorResponse(message = 'request failed', status = 500, extra = {}) {
  return jsonResponse({ error: message, ...extra }, status);
}

export function createFetchMock(routes = {}) {
  activeRoutes = normalizeRouteEntries(routes);
  calls = [];
  activeFetch = makeSpy(async (input, options = {}) => {
    const method = normalizeMethod(options?.method ?? input?.method ?? 'GET');
    const key = routeKey(method, input);
    calls.push({ url: typeof input === 'string' ? input : input?.url, method, options });

    if (!activeRoutes.has(key)) {
      return responseFromRoute(errorResponse(`No mocked fetch route for ${key}`, 404));
    }

    return resolveRoute(activeRoutes.get(key), input, options);
  });

  const vi = getVitestApi();
  if (vi?.stubGlobal) {
    vi.stubGlobal('fetch', activeFetch);
  } else {
    originalFetch = globalThis.fetch;
    globalThis.fetch = activeFetch;
  }
  return activeFetch;
}

export function getCalls() {
  return calls.map((call) => ({ ...call, options: { ...(call.options ?? {}) } }));
}

export function resetFetchMock() {
  calls = [];
  activeRoutes = new Map();
  activeFetch = null;
  const vi = getVitestApi();
  if (vi?.unstubAllGlobals) {
    vi.unstubAllGlobals();
  } else {
    globalThis.fetch = originalFetch;
  }
  if (vi?.restoreAllMocks) {
    vi.restoreAllMocks();
  }
}

globalThis.beforeEach?.(() => {
  calls = [];
});

globalThis.afterEach?.(() => {
  resetFetchMock();
});
