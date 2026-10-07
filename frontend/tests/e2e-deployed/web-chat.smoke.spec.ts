import { expect, test } from "@playwright/test";

/**
 * Full-stack smoke: portal login → employee pick → platform chat UI → one
 * real round trip (SSE through the chat UI's route handler and the portal
 * proxy into the runtime and back).
 *
 * Env knobs (defaults target the local k3s dev stack):
 *   E2E_DEPLOYED_PORTAL   portal origin hosting the /chat entry page
 *   E2E_DEPLOYED_EMPLOYEE employee name to pick
 *   E2E_PORTAL_USERNAME / E2E_PORTAL_PASSWORD  AEP credentials
 */
const PORTAL = process.env.E2E_DEPLOYED_PORTAL ?? "http://127.0.0.1:30190";
const EMPLOYEE = process.env.E2E_DEPLOYED_EMPLOYEE ?? "sales-helper";
const USERNAME = process.env.E2E_PORTAL_USERNAME ?? "admin";
const PASSWORD =
  process.env.E2E_PORTAL_PASSWORD ?? "change-this-admin-password";

test("portal login → employee → chat round trip", async ({ page }) => {
  test.setTimeout(150_000);

  // 1. Portal entry page: AEP login.
  await page.goto(`${PORTAL}/chat`);
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();

  // 2. Employee picker appears; pick the configured employee. This lands us
  // on the chat UI origin with the portal session cookies shared across
  // ports (same host).
  const employeeButton = page.locator("button.employee", {
    hasText: EMPLOYEE,
  });
  await expect(employeeButton).toBeVisible({ timeout: 30_000 });
  await employeeButton.click();

  // 3. The chat UI loads a workspace with a composer.
  const composer = page.locator("textarea, [contenteditable='true']").first();
  await expect(composer).toBeVisible({ timeout: 60_000 });

  // 4. One deterministic round trip. The run goes through the platform
  // prefix (/api/v1/employees/<name>/chat/...) — assert the stream route
  // answered 200 while the reply renders.
  // The composer's Enter handling has changed across UI generations (it
  // inserts a newline in some modes); the Submit button is the stable
  // contract, so drive that directly.
  const stream = page.waitForResponse(
    (response) =>
      response.url().includes("/runs/stream") && response.status() === 200,
    { timeout: 120_000 },
  );
  await composer.fill("请只回复两个字：收到");
  await page.getByRole("button", { name: "Submit" }).click();
  await stream;

  await expect(page.getByText("收到").first()).toBeVisible({
    timeout: 120_000,
  });
});
