/** Tailwind v4 and shadcn pull plugins from CSS; Knip does not parse @import in CSS by default. */
function compileCss(source: string): string {
  const specifiers = new Set<string>();
  for (const match of source.matchAll(/@(?:import|plugin)\s+['"]([^'"]+)['"]/g)) {
    const specifier = match[1];
    if (specifier) {
      specifiers.add(specifier);
    }
  }
  return [...specifiers].map((specifier) => `import '${specifier}';`).join("\n");
}

const config = {
  compilers: {
    css: compileCss,
  },
  entry: [
    "src/env.js",
    "src/mdx-components.ts",
    "src/app/**/{page,layout,loading,error,not-found,route,default}.{js,jsx,ts,tsx}",
    // Playwright configs are CLI entry points in CI, not static imports.
    "playwright.auth.config.ts",
    "playwright.real-backend.config.ts",
    "playwright.record.config.ts",
    "tests/e2e-deployed/playwright.config.ts",
    // Unit test runner and dev/build scripts.
    "scripts/**/*.{js,mjs,cjs}",
    // Nextra docs sidebar metadata (file-based routing, not imported as modules).
    "src/content/**/_meta.ts",
  ],
  project: ["src/**/*.{js,jsx,ts,tsx,css}", "scripts/**/*.{js,mjs,cjs}", "tests/**/*.{js,jsx,ts,tsx}"],
  ignore: [
    // Static demo thread assets served by URL; the HTML/JS bundle references them without imports.
    "public/demo/**",
  ],
  ignoreDependencies: [
    // ESLint flat config extends next/core-web-vitals via JS import path Knip does not resolve.
    "eslint-config-next",
  ],
  rstest: {
    entry: ["tests/unit/**/*.{test,spec}.{ts,tsx}"],
  },
  playwright: {
    entry: ["tests/e2e/**/*.{spec,test}.{ts,tsx}"],
  },
};

export default config;
