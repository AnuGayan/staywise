import type { MeResponse, PhoneMessage } from "./types";

/** Error raised for any non-success HTTP response the caller did not expect. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const JSON_HEADERS = { "Content-Type": "application/json", Accept: "application/json" };

function request(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(path, { credentials: "same-origin", ...init });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJson(res: Response): Promise<unknown> {
  const text = await res.text();
  if (text.length === 0) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError(`Invalid JSON from ${res.url || "server"}`, res.status);
  }
}

function errorMessage(body: unknown, fallback: string): string {
  if (isRecord(body) && typeof body.error === "string") return body.error;
  return fallback;
}

async function expectNoContent(res: Response, what: string): Promise<void> {
  if (res.ok) return;
  const body = await readJson(res).catch(() => null);
  throw new ApiError(errorMessage(body, `${what} failed (HTTP ${res.status})`), res.status);
}

export async function fetchMe(): Promise<MeResponse> {
  const res = await request("api/me", { headers: { Accept: "application/json" } });
  const body = await readJson(res).catch(() => null);
  if (res.status === 401) {
    const loginUrl =
      isRecord(body) && typeof body.loginUrl === "string" ? body.loginUrl : "auth/login";
    return { authenticated: false, loginUrl };
  }
  if (!res.ok || !isRecord(body) || body.authenticated !== true || !isRecord(body.user)) {
    throw new ApiError(
      errorMessage(body, `Could not load session (HTTP ${res.status})`),
      res.status,
    );
  }
  const { user } = body;
  const sub = typeof user.sub === "string" ? user.sub : "";
  const name = typeof user.name === "string" && user.name.length > 0 ? user.name : sub;
  const scopes = Array.isArray(body.grantedScopes)
    ? body.grantedScopes.filter((s): s is string => typeof s === "string")
    : [];
  return {
    authenticated: true,
    user: { sub, name: name.length > 0 ? name : "Guest" },
    grantedScopes: scopes,
  };
}

export async function logout(): Promise<void> {
  const res = await request("auth/logout", { method: "POST" });
  await expectNoContent(res, "Sign out");
}

export type SendChatResult =
  { ok: true; turnId: string } | { ok: false; status: number; error: string };

export async function sendChat(message: string): Promise<SendChatResult> {
  const res = await request("api/chat", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ message }),
  });
  const body = await readJson(res).catch(() => null);
  if (res.ok && isRecord(body) && typeof body.turnId === "string") {
    return { ok: true, turnId: body.turnId };
  }
  const fallback =
    res.status === 409
      ? "The concierge is still working on the previous request."
      : `Message could not be sent (HTTP ${res.status})`;
  return { ok: false, status: res.status, error: errorMessage(body, fallback) };
}

export async function resetConversation(): Promise<void> {
  const res = await request("api/reset", { method: "POST" });
  await expectNoContent(res, "Reset");
}

export async function cancelStepUp(stepupId: string): Promise<void> {
  const res = await request(`api/stepup/${encodeURIComponent(stepupId)}/cancel`, {
    method: "POST",
  });
  await expectNoContent(res, "Decline");
}

export async function fetchPhoneMessages(): Promise<PhoneMessage[]> {
  const res = await request("api/mock-phone/messages", {
    headers: { Accept: "application/json" },
  });
  const body = await readJson(res);
  if (!res.ok) {
    throw new ApiError(
      errorMessage(body, `Could not load phone messages (HTTP ${res.status})`),
      res.status,
    );
  }
  if (!Array.isArray(body)) return [];
  return body.filter(isPhoneMessage);
}

export function isPhoneMessage(value: unknown): value is PhoneMessage {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.receivedAt === "string" &&
    typeof value.text === "string" &&
    (value.otp === undefined || typeof value.otp === "string")
  );
}
