import { env } from "@/env";

/**
 * Platform mode (Zhiyuan digital-employee portal): this build is served
 * behind the de-portal reverse proxy instead of talking to a DeerFlow
 * gateway directly. Authentication and employee selection happen on the
 * portal's /chat page; the browser arrives holding three cookies
 * (de_portal_session, csrf_token, de_employee) that must accompany every
 * API call. Cookies are not port-scoped, so both origins share a host.
 */
export function isPlatformMode() {
  return env.NEXT_PUBLIC_PLATFORM_MODE === "true";
}

/** The portal /chat entry page (login + employee picker). */
export function getPlatformChatURL() {
  return env.NEXT_PUBLIC_PORTAL_CHAT_URL ?? "";
}

/** The selected digital employee, from the portal-set cookie (client only). */
export function getPlatformEmployeeName(): string | null {
  if (typeof document === "undefined") return null;
  const match = /(?:^|;\s*)de_employee=([^;]+)/.exec(document.cookie);
  return match?.[1] ? decodeURIComponent(match[1]) : null;
}

/**
 * Absolute same-origin prefix routing API calls through the portal proxy:
 * `${prefix}/api/...` (REST) and `${prefix}/threads/...` (LangGraph SDK)
 * both land on the portal's /api/v1/employees/{name}/chat/{rest...} route,
 * which strips the redundant api/ segment. Null when no employee is
 * selected yet — callers fall back to their default behavior.
 */
export function getPlatformChatPrefix(): string | null {
  const name = getPlatformEmployeeName();
  if (!name || typeof window === "undefined") return null;
  return `${window.location.origin}/api/v1/employees/${encodeURIComponent(name)}/chat`;
}
