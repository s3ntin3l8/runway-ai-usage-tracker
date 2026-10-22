import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'node_modules', 'playwright-report'] },
  {
    files: ['**/*.{ts,tsx}'],
    // The *untyped* preset, and that is a decision rather than an
    // oversight. The type-aware one
    // (`tseslint.configs.recommendedTypeChecked`) is where #754's
    // "unawaited promise" checks live, and it reports 276 findings today:
    // 57 `no-floating-promises` and 20 `no-misused-promises` in `src/`
    // alone, plus 199 more from the other type-aware rules. Turning it on
    // is a refactor of the frontend rather than a lint fix, so the counts
    // and the decision are recorded in #792.
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],

      // The React Compiler's rules. They flag patterns this codebase uses on
      // purpose and documents, and turning them on properly is a refactor of
      // a dozen components rather than a lint fix.
      //
      // - `set-state-in-effect` (11 places): resetting state when a prop
      //   changes. The React Compiler wants it derived instead of stored;
      //   each of these is a deliberate "reset per open" or "reset when the
      //   selection changes", and the comment at the call says so.
      // - `refs` (3 places) and `purity` (1): reading a ref, and calling
      //   `Date.now()`, during render.
      //
      // Track them in #792.
      'react-hooks/set-state-in-effect': 'off',
      'react-hooks/refs': 'off',
      'react-hooks/purity': 'off',
    },
  },
  {
    // The scripts are plain Node, not the browser: they render the brand
    // rasters and take the screenshots. Without this they are enumerated by
    // `eslint .` and checked by no rule at all.
    files: ['**/*.mjs'],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.node,
    },
  },
  {
    // The screenshot driver is a Node script, but two of its callbacks are
    // serialised and evaluated *in the page* by Playwright, so they see the
    // browser's globals. Declared here rather than by giving every script
    // the browser's globals.
    files: ['scripts/screenshots.mjs'],
    languageOptions: {
      globals: { document: 'readonly' },
    },
  },
  {
    // The test files use `any` for the shapes a mock returns, where the
    // point of the test is the call, not the type. The same convention the
    // Python side follows for `tests/*`. The Playwright specs are tests too.
    files: ['**/*.test.{ts,tsx}', '**/*.spec.{ts,tsx}', 'src/test/**'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
);
