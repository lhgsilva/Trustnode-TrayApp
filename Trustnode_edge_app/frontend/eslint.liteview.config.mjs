/* ESLint for the hand-written phone/LAN app, with ONE job: "X is not defined".
 *
 * `backend/static/lite_view/index.html` is served as-is. No build reads it, no
 * bundler parses it, and `eslint.undef.config.mjs` only matches `src/**`, so
 * nothing has ever checked it. The one fault that cannot be seen by reading -
 * an identifier declared inside one function and used from another - therefore
 * ships: `liteAxisId` did on 2026-10-06, and `buildManualY` was doing it on
 * 2026-10-17, which made the tag monitor throw before it drew anything.
 *
 * scripts/test_lite_view_undef.py extracts the module script to
 * `.lite_view_probe.liteview.mjs` and runs this against it. The glob below is
 * what makes the rule apply at all - a config whose `files` matches nothing
 * lints nothing and reports success, which is how the first version of that
 * test passed over a deliberately undefined identifier.
 */
import globals from "globals";

export default [
  {
    files: ["**/*.liteview.mjs"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.serviceworker,
        process: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
    },
  },
];
