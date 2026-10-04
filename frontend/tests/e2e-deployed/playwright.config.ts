import { defineConfig, devices } from "@playwright/test";

/**
 * Deployed-stack smoke E2E (platform mode).
 *
 * Unlike the upstream replay-gateway suites, this config starts NOTHING: it
 * drives the real deployed stack — the de-portal /chat entry page (login +
 * employee picker) and the platform-mode chat UI served behind it. Point it
 * at the stack with env vars and run:
 *
 *   npx playwright test -c tests/e2e-deployed/playwright.config.ts
 *
 * Requires a reachable employee in Ready phase and a model provider that
 * actually answers (not a flapping upstream).
 */
export default defineConfig({
  testDir: ".",
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: process.env.E2E_DEPLOYED_APP_URL ?? "http://127.0.0.1:30195",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
