// The whole-tree half of the `dependency-audit` CI job: see
// audit-build-deps-core.mjs for what it gates and why. Kept as a bare entrypoint
// that calls `main()` unconditionally, so it can never exit 0 having audited nothing.

import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { evaluate, reportLines } from './audit-build-deps-core.mjs';

function runJson(webappDir, args) {
  // `npm audit` exits 1 when it finds anything and `npm ls` exits 1 on any tree
  // problem, so the exit status says nothing about whether the JSON is usable.
  const r = spawnSync('npm', args, {
    cwd: webappDir,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });
  try {
    return JSON.parse(r.stdout);
  } catch {
    throw new Error(
      `could not parse the JSON from \`npm ${args.join(' ')}\`: ${r.stderr || r.stdout}`,
    );
  }
}

function main() {
  const webappDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const allowlist = JSON.parse(readFileSync(path.join(webappDir, 'audit-allowlist.json'), 'utf8'));
  const audit = runJson(webappDir, ['audit', '--json']);
  const lsTree = runJson(webappDir, ['ls', '--all', '--json']);
  if (audit.error) throw new Error(`npm audit failed: ${JSON.stringify(audit.error)}`);

  const result = evaluate({ audit, lsTree, allowlist, now: new Date() });
  for (const line of reportLines(result)) console.log(line);
  process.exit(result.errors.length > 0 ? 1 : 0);
}

main();
