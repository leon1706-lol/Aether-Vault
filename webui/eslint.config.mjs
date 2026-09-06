// v1.3.4: Next.js 16 removed the `next lint` CLI subcommand entirely (it used to wrap
// ESLint and translate `.eslintrc.json`); ESLint 9 (required by eslint-config-next@16,
// which pins `eslint: >=9.0.0`) also switched to flat config as the only supported
// format. This replaces the old `.eslintrc.json` — `package.json`'s `lint` script now
// runs `eslint` directly instead of `next lint`.
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";

const eslintConfig = [
  ...nextCoreWebVitals,
  {
    ignores: [".next/**", "node_modules/**", "playwright-report/**", "test-results/**"],
  },
  {
    rules: {
      // v1.3.4: new in eslint-plugin-react-hooks 7.x (bundled by eslint-config-next 16,
      // absent from the 14.x line this codebase used before) -- flags EVERY setState
      // call that's the first synchronous statement in a useEffect body, including the
      // extremely common (and, per React's own docs, accepted) `setLoading(true)` guard
      // at the top of a data-fetching effect -- every one of the 10 real hits this rule
      // found here is exactly that pattern (useDashboard/useIncrementalReveal/RunsPanel/
      // StoragePanel/WeightDiffPanel/ChangeSetsPanel), not the "derive state during
      // render" anti-pattern the rule exists to catch. Turned off with this note rather
      // than rewritten across ten call sites for a stricter default this repo never
      // opted into.
      "react-hooks/set-state-in-effect": "off",
    },
  },
];

export default eslintConfig;
