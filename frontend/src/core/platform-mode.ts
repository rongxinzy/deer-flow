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

/**
 * Embed mode: the admin console frames this app (console origin, same host)
 * and provides the surrounding shell, so the chat UI hides its own brand
 * row. Detected from ?embed=1 so a plain visit keeps the full chrome.
 */
export function isEmbedMode() {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("embed") === "1";
}

/**
 * The portal /chat entry page (login + employee picker). The default derives
 * from this app's own host (the portal runs on the same host, NodePort
 * 30190), so whichever hostname the user typed keeps every origin on one
 * hostname — cookies ignore ports, not hosts — and no address is baked into
 * the image. NEXT_PUBLIC_PORTAL_CHAT_URL overrides for split deployments.
 */
export function getPlatformChatURL() {
  const configured = env.NEXT_PUBLIC_PORTAL_CHAT_URL;
  if (configured) return configured;
  if (typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:30190/chat`;
  }
  return "";
}

/** The selected digital employee, from the portal-set cookie (client only). */
export function getPlatformEmployeeName(): string | null {
  if (typeof document === "undefined") return null;
  const match = /(?:^|;\s*)de_employee=([^;]+)/.exec(document.cookie);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

/**
 * The selected employee's display name, from the portal's readable
 * companion cookie (de_employee_display, URL-encoded). Absent for sessions
 * created before the cookie existed — callers fall back to the technical
 * name. Client only.
 */
export function getPlatformEmployeeDisplay(): string | null {
  if (typeof document === "undefined") return null;
  const match = /(?:^|;\s*)de_employee_display=([^;]+)/.exec(document.cookie);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

/**
 * The console workbench (员工工作台) this chat was entered from. The default
 * derives from this app's own host (the console runs on the same host,
 * NodePort 30196), so no address is baked into the image;
 * NEXT_PUBLIC_PLATFORM_CONSOLE_URL overrides for split deployments.
 */
export function getPlatformConsoleURL(): string {
  const configured = env.NEXT_PUBLIC_PLATFORM_CONSOLE_URL;
  if (configured) return configured;
  if (typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:30196/#/workbench`;
  }
  return "";
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
