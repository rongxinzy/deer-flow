import { afterEach, describe, expect, it } from "@rstest/core";

import {
  getPlatformConsoleURL,
  getPlatformEmployeeDisplay,
  getPlatformEmployeeName,
} from "@/core/platform-mode";

function clearCookies() {
  for (const name of [
    "de_employee",
    "de_employee_display",
    "de_portal_session",
  ]) {
    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

afterEach(clearCookies);

describe("getPlatformEmployeeDisplay", () => {
  it("decodes the URL-escaped display name the portal mirrors", () => {
    document.cookie = `de_employee=rd-helper; path=/`;
    document.cookie = `de_employee_display=${encodeURIComponent("研发助理")}; path=/`;
    expect(getPlatformEmployeeDisplay()).toBe("研发助理");
  });

  it("is null when the companion cookie is absent", () => {
    document.cookie = `de_employee=rd-helper; path=/`;
    expect(getPlatformEmployeeName()).toBe("rd-helper");
    expect(getPlatformEmployeeDisplay()).toBeNull();
  });

  it("is null for a malformed escape instead of throwing", () => {
    document.cookie = `de_employee_display=%E0%A4%A; path=/`;
    expect(getPlatformEmployeeDisplay()).toBeNull();
  });
});

describe("getPlatformConsoleURL", () => {
  it("derives the console workbench from the current host", () => {
    // No NEXT_PUBLIC_PLATFORM_CONSOLE_URL in the test environment: the
    // default keeps protocol + hostname and assumes the console NodePort.
    const { protocol, hostname } = window.location;
    expect(getPlatformConsoleURL()).toBe(
      `${protocol}//${hostname}:30196/#/workbench`,
    );
  });
});
