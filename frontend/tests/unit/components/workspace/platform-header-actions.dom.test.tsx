import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { cleanup, render, screen } from "@testing-library/react";
import { type ReactNode } from "react";

const originalPlatformMode = process.env.NEXT_PUBLIC_PLATFORM_MODE;

afterEach(() => {
  cleanup();
  document.cookie =
    "de_employee_display=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  document.cookie =
    "de_employee=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  if (originalPlatformMode === undefined) {
    delete process.env.NEXT_PUBLIC_PLATFORM_MODE;
  } else {
    process.env.NEXT_PUBLIC_PLATFORM_MODE = originalPlatformMode;
  }
  rs.resetModules();
});

// NEXT_PUBLIC_PLATFORM_MODE is inlined at module load, so each test resets
// the module registry after setting the env and imports everything fresh —
// provider included, or the reset registry would hand the component a
// different I18nContext instance than the statically imported one.
async function renderInPlatform(platformMode: boolean, cookie?: string) {
  if (platformMode) {
    process.env.NEXT_PUBLIC_PLATFORM_MODE = "true";
  } else {
    delete process.env.NEXT_PUBLIC_PLATFORM_MODE;
  }
  rs.resetModules();
  if (cookie) document.cookie = `${cookie}; path=/`;
  const [{ PlatformHeaderActions }, { I18nProvider }, { TooltipProvider }] =
    await Promise.all([
      import("@/components/workspace/platform-header-actions"),
      import("@/core/i18n/context"),
      import("@/components/ui/tooltip"),
    ]);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <I18nProvider initialLocale="zh-CN">
      <TooltipProvider>{children}</TooltipProvider>
    </I18nProvider>
  );
  return render(<PlatformHeaderActions />, { wrapper });
}

describe("PlatformHeaderActions", () => {
  it("shows the employee display name and the back link in platform mode", async () => {
    // Golden PathEscape value from the portal: space is %20, not "+".
    const view = await renderInPlatform(
      true,
      "de_employee_display=D1%20%E9%AA%8C%E6%94%B6%E5%8A%A9%E6%89%8B",
    );
    expect(await screen.findByText("D1 验收助手")).toBeTruthy();
    // The visible copy follows the runtime locale (happy-dom reports en-US);
    // the href is the locale-independent contract.
    const link = await screen.findByRole("link", {
      name: /返回工作台$|Back to the workbench$/,
    });
    expect(link.getAttribute("href")).toBe(
      "http://localhost:30196/#/workbench",
    );
    view.unmount();
  });

  it("falls back to the technical name without the companion cookie", async () => {
    const view = await renderInPlatform(true, "de_employee=d1-helper");
    expect(await screen.findByText("d1-helper")).toBeTruthy();
    view.unmount();
  });

  it("renders nothing outside platform mode", async () => {
    const view = await renderInPlatform(false, "de_employee=d1-helper");
    expect(view.container.innerHTML).toBe("");
  });
});
