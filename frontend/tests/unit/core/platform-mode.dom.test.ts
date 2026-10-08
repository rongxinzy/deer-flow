import { afterEach, describe, expect, it } from "@rstest/core";

import {
  getPlatformConsoleURL,
  getPlatformEmployeeDisplay,
  getPlatformEmployeeName,
} from "@/core/platform-mode";

type HappyDOMWindow = typeof window & {
  happyDOM: { setURL: (url: string) => void };
};

function clearCookies() {
  for (const name of [
    "de_employee",
    "de_employee_display",
    "de_portal_session",
  ]) {
    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

afterEach(() => {
  clearCookies();
  (window as HappyDOMWindow).happyDOM.setURL("http://localhost:3000/");
});

describe("getPlatformEmployeeDisplay", () => {
  it("decodes the path-escaped display name the portal mirrors", () => {
    document.cookie = `de_employee=rd-helper; path=/`;
    // Golden wire values: the portal path-escapes (net/url PathEscape), so
    // assert the literal cookie contents instead of recomputing with an
    // encoder that could pick a different, incompatible escape.
    document.cookie = `de_employee_display=%E7%A0%94%E5%8F%91%E5%8A%A9%E7%90%86; path=/`;
    expect(getPlatformEmployeeDisplay()).toBe("研发助理");
  });

  it("decodes spaces as %20 — never a literal plus", () => {
    // A form-style "+" would survive decodeURIComponent verbatim and render
    // "Sales+Assistant" in the header.
    document.cookie = `de_employee_display=Sales%20Assistant; path=/`;
    expect(getPlatformEmployeeDisplay()).toBe("Sales Assistant");
  });

  it("keeps a literal plus as a plus", () => {
    document.cookie = `de_employee_display=C++%20%E5%8A%A9%E6%89%8B; path=/`;
    expect(getPlatformEmployeeDisplay()).toBe("C++ 助手");
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
    // A non-default host proves the value comes from window.location — a
    // hardcoded localhost:30196 would pass on happy-dom's default URL.
    (window as HappyDOMWindow).happyDOM.setURL(
      "http://de.example:1234/workspace",
    );
    expect(getPlatformConsoleURL()).toBe("http://de.example:30196/#/workbench");
  });
});
