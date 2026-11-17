// The whole-tree half of the `dependency-audit` CI job.
//
// `npm audit --omit=dev` (run separately) already blocks on anything that ships
// to the browser. This gates the rest of the tree in two tiers:
//
//   - BUILD tooling (what `npm run build` and `generate-pwa-assets` execute, plus
//     everything it pulls in): moderate and up BLOCKS. It runs on the release
//     path and writes the bundle that ships.
//   - DEV-only tooling (vitest, eslint, playwright, testing-library, ...): moderate
//     and up only WARNS. It never touches a shipped artifact, and an advisory with
//     no patched release would otherwise freeze every branch and the edge image
//     publish (#744).
//
// A build-tooling advisory with no fix is excused by an entry in
// audit-allowlist.json. Every entry names the advisory, the reason, a tracking
// issue and an expiry date; an expired entry fails the job, so an exception
// cannot outlive its review. Low and info findings are left to npm-audit-weekly.yml.

import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Packages the build scripts invoke. Their transitive dependencies are "build".
export const BUILD_ROOTS = [
  'vite',
  '@vitejs/plugin-react',
  '@tailwindcss/vite',
  'tailwindcss',
  'vite-plugin-pwa',
  '@vite-pwa/assets-generator',
  'typescript',
];

const SEVERITY_RANK = { info: 0, low: 1, moderate: 2, high: 3, critical: 4 };
const GATED_FROM = SEVERITY_RANK.moderate;

/** The GHSA id out of an advisory URL, or null. */
export function ghsaOf(url) {
  const m = /GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}/i.exec(url ?? '');
  return m ? m[0] : null;
}

/**
 * Package name -> names it depends on, merged across every place `npm ls --all`
 * lists it. Merging by name (not by position) means a deduped node that npm
 * prints without its children still resolves through its other occurrences.
 */
export function dependencyGraph(lsTree) {
  const graph = new Map();
  const visit = (deps) => {
    for (const [name, node] of Object.entries(deps ?? {})) {
      const edges = graph.get(name) ?? new Set();
      for (const child of Object.keys(node?.dependencies ?? {})) edges.add(child);
      graph.set(name, edges);
      visit(node?.dependencies);
    }
  };
  visit(lsTree?.dependencies);
  return graph;
}

/** Package name -> the path from a build root that reaches it (BFS, shortest). */
export function buildPaths(graph, roots = BUILD_ROOTS) {
  const paths = new Map();
  const queue = [];
  for (const root of roots) {
    if (!graph.has(root)) continue;
    paths.set(root, [root]);
    queue.push(root);
  }
  while (queue.length > 0) {
    const name = queue.shift();
    for (const child of graph.get(name) ?? []) {
      if (paths.has(child)) continue;
      paths.set(child, [...paths.get(name), child]);
      queue.push(child);
    }
  }
  return paths;
}

/** The advisories npm itself reports at the root of each chain (`via` objects). */
export function rootAdvisories(audit) {
  const found = [];
  for (const [pkg, vuln] of Object.entries(audit?.vulnerabilities ?? {})) {
    for (const via of vuln.via ?? []) {
      if (typeof via === 'string') continue;
      found.push({
        pkg,
        id: ghsaOf(via.url) ?? String(via.source ?? via.url ?? 'unknown'),
        severity: via.severity ?? vuln.severity,
        title: via.title ?? '',
        url: via.url ?? '',
      });
    }
  }
  return found;
}

/**
 * Classify every moderate+ advisory. Pure: `now` is a Date so tests can move time.
 * Returns { errors, warnings, notices } as plain strings.
 */
export function evaluate({ audit, lsTree, allowlist, now }) {
  const errors = [];
  const warnings = [];
  const notices = [];
  const paths = buildPaths(dependencyGraph(lsTree));
  const used = new Set();

  for (const entry of allowlist) {
    const expires = new Date(`${entry.expires}T23:59:59Z`);
    if (!entry.expires || Number.isNaN(expires.getTime())) {
      errors.push(`audit-allowlist.json: ${entry.id} has no valid "expires" date (YYYY-MM-DD).`);
    } else if (expires < now) {
      errors.push(
        `audit-allowlist.json: the exception for ${entry.id} (${entry.package}) expired on ` +
          `${entry.expires}. Re-check upstream for a patched release; if there is still none, ` +
          `extend the date with a fresh reason (${entry.issue}).`,
      );
    }
  }

  const seen = new Set();
  for (const adv of rootAdvisories(audit)) {
    if (SEVERITY_RANK[adv.severity] < GATED_FROM) continue;
    const key = `${adv.id}|${adv.pkg}`;
    if (seen.has(key)) continue;
    seen.add(key);

    const via = paths.get(adv.pkg);
    const label = `${adv.id} (${adv.severity}) in ${adv.pkg}: ${adv.title}`;
    if (!via) {
      warnings.push(`${label} -- dev-only tooling, does not block. ${adv.url}`);
      continue;
    }
    const allowed = allowlist.find((e) => e.id === adv.id && e.package === adv.pkg);
    if (allowed) {
      used.add(allowed);
      // An expired entry already failed above; do not also call it "excused".
      if (new Date(`${allowed.expires}T23:59:59Z`) < now) continue;
      notices.push(
        `${label} -- build tooling, excused until ${allowed.expires} (${allowed.issue}): ${allowed.reason}`,
      );
      continue;
    }
    errors.push(
      `${label} -- reached by build tooling via ${via.join(' > ')}. Fix it, or add an expiring ` +
        `entry to webapp/audit-allowlist.json. ${adv.url}`,
    );
  }

  for (const entry of allowlist) {
    if (!used.has(entry)) {
      notices.push(
        `audit-allowlist.json: ${entry.id} (${entry.package}) matches nothing in this audit any more -- remove the entry.`,
      );
    }
  }
  return { errors, warnings, notices };
}

function runJson(webappDir, args) {
  // `npm audit` exits 1 when it finds anything and `npm ls` exits 1 on any tree
  // problem, so the exit status says nothing about whether the JSON is usable.
  const r = spawnSync('npm', args, { cwd: webappDir, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  try {
    return JSON.parse(r.stdout);
  } catch {
    throw new Error(`could not parse the JSON from \`npm ${args.join(' ')}\`: ${r.stderr || r.stdout}`);
  }
}

function main() {
  const webappDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const allowlist = JSON.parse(readFileSync(path.join(webappDir, 'audit-allowlist.json'), 'utf8'));
  const audit = runJson(webappDir, ['audit', '--json']);
  const lsTree = runJson(webappDir, ['ls', '--all', '--json']);
  if (audit.error) throw new Error(`npm audit failed: ${JSON.stringify(audit.error)}`);

  const { errors, warnings, notices } = evaluate({ audit, lsTree, allowlist, now: new Date() });
  for (const m of notices) console.log(`::notice::${m}`);
  for (const m of warnings) console.log(`::warning::${m}`);
  for (const m of errors) console.log(`::error::${m}`);
  console.log(
    `build-tooling audit: ${errors.length} blocking, ${warnings.length} dev-only warning(s), ${notices.length} notice(s)`,
  );
  process.exit(errors.length > 0 ? 1 : 0);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
