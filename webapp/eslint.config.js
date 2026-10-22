import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'node_modules', 'playwright-report'] },
  {
    files: ['**/*.{ts,tsx}'],
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
    // The test files use `any` for the shapes a mock returns, where the
    // point of the test is the call, not the type. The same convention the
    // Python side follows for `tests/*`.
    files: ['**/*.test.{ts,tsx}', 'src/test/**'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
);
