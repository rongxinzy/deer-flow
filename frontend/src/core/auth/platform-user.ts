import type { User } from "./types";

/**
 * Platform mode authenticates at the portal (AEP identity), not at the
 * gateway, so the SSR guard cannot consult a gateway session and
 * short-circuits to this user instead.
 *
 * id "default" keeps the user-preferences boundary dormant — its sync
 * route is session-only on the gateway and would retry forever; the
 * portal proxy authenticates as an internal caller where that route is
 * unavailable. system_role "user" avoids firing admin-only REST calls
 * the runtime would reject for internal callers.
 */
export const PLATFORM_USER: User = {
  id: "default",
  email: "platform@portal.internal",
  system_role: "user",
  needs_setup: false,
  oauth_provider: null,
};
