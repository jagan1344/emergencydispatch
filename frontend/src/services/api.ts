// Thin REST client. The JWT is kept in localStorage; every request sends it as a Bearer token.
const TOKEN_KEY = "ems_token";
const USER_KEY = "ems_user";

export interface SessionUser { username: string; role: "ADMIN" | "DISPATCHER" | "VIEWER" }

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function getUser(): SessionUser | null {
  try { const v = localStorage.getItem(USER_KEY); return v ? JSON.parse(v) : null; } catch { return null; }
}
export function setSession(token: string, user: SessionUser) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}
export function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const t = getToken();
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(path, { method: opts.method || "GET", headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  const text = await res.text();
  const data = text ? (() => { try { return JSON.parse(text); } catch { return text; } })() : null;
  if (!res.ok) {
    if (res.status === 401 && path !== "/api/auth/login") { clearSession(); window.location.assign("/login"); }
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

export async function login(username: string, password: string): Promise<SessionUser> {
  const r = await api<{ access_token: string; username: string; role: SessionUser["role"] }>("/api/auth/login",
    { method: "POST", body: { username, password } });
  const user = { username: r.username, role: r.role };
  setSession(r.access_token, user);
  return user;
}
