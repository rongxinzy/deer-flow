import { afterEach, expect, test, rs } from "@rstest/core";

const original = process.env.NEXT_PUBLIC_APP_VERSION;

afterEach(() => {
  rs.resetModules();
  if (original === undefined) {
    delete process.env.NEXT_PUBLIC_APP_VERSION;
  } else {
    process.env.NEXT_PUBLIC_APP_VERSION = original;
  }
});

test("aboutMarkdown heading interpolates the app version", async () => {
  process.env.NEXT_PUBLIC_APP_VERSION = "9.9.9-test";
  const { aboutMarkdown } =
    await import("@/components/workspace/settings/about-content");
  expect(aboutMarkdown).toContain("# 知远数字员工 · 9.9.9-test");
  // Keep upstream attribution and license when using the platform brand.
  expect(aboutMarkdown).toContain(
    "[DeerFlow](https://github.com/bytedance/deer-flow)",
  );
  expect(aboutMarkdown).toContain("MIT License");
});

test("aboutMarkdown heading reflects the package version when env is unset", async () => {
  delete process.env.NEXT_PUBLIC_APP_VERSION;
  const { APP_VERSION } = await import("@/version");
  const { aboutMarkdown } =
    await import("@/components/workspace/settings/about-content");
  expect(aboutMarkdown).toContain(`# 知远数字员工 · ${APP_VERSION}`);
});
