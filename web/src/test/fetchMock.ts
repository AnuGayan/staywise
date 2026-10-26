import { vi } from "vitest";

export type Route = (init: RequestInit | undefined) => Response | Promise<Response>;

/**
 * Stubs global fetch with a "METHOD /path" → handler table. Unknown routes
 * fail the request loudly so tests notice unexpected calls.
 */
export function mockFetch(routes: Record<string, Route>) {
  const spy = vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const route = routes[`${method} ${url}`];
    if (!route) return Promise.reject(new Error(`Unexpected fetch: ${method} ${url}`));
    return Promise.resolve(route(init));
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}
