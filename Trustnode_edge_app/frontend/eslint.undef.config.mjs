/* ESLint with ONE job: catch "X is not defined".
 *
 * 2026-08-27: a JSX fragment was copied between two .map() row blocks and kept
 * the OLD block's variable name (`st`). Optional chaining does not protect a
 * bare undefined identifier, so the gateway page died at render with
 * "st is not defined". `vite build` compiled it happily, and the jsdom smoke
 * only renders the dashboard, so nothing caught it.
 *
 * Deliberately no style rules - this is a correctness gate, not a linter.
 * Run: npm run lint:undef
 */
import globals from "globals";

/* The source carries inline `eslint-disable-next-line react-hooks/exhaustive-deps`
   comments. Without the plugin registered, ESLint reports "rule not found" for
   each one, which would bury the real findings. A no-op stub resolves them
   without pulling the whole React plugin in. */
const reactHooksStub = {
  rules: {
    "exhaustive-deps": { create: () => ({}) },
    "rules-of-hooks": { create: () => ({}) },
  },
};

export default [
  {
    plugins: { "react-hooks": reactHooksStub },
    files: ["src/**/*.js", "src/**/*.jsx"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, JSX: "readonly" },
    },
    linterOptions: {
      // The source carries inline disables for plugins this config does not
      // load (react-hooks); they are not errors here.
      reportUnusedDisableDirectives: false,
    },
    rules: {
      "no-undef": "error",
    },
  },
];
