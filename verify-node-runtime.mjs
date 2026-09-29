#!/usr/bin/env node
/**
 * Build-time / pre-flight runtime gate for the HuggingClaw image.
 *
 * WHY THIS EXISTS
 * ---------------
 * Stage 1 (OpenClaw) and stage 2 (Node) of the image are versioned
 * independently, and the effective OpenClaw version can also be changed at
 * container start via the OPENCLAW_VERSION env var (see start.sh). A perfectly
 * valid Docker build can therefore produce an image whose OpenClaw cannot run:
 * v2026.9.3 raised the Node floor to ">=24.16.0 <25 || >=26.1.0" to stop
 * node:sqlite truncating TEXT at embedded NUL (nodejs/node#61954), and an
 * image still on Node 22 only discovered that at runtime — where start.sh
 * reported "Gateway failed - DEV_MODE active, retrying in 10s..." and retried
 * forever, hiding the cause completely.
 *
 * So this script fails the BUILD (or a pre-flight check) instead. It mirrors
 * the gates OpenClaw itself applies:
 *
 *   1. Node must satisfy the installed OpenClaw's OWN `engines.node` range.
 *      Read from the package itself rather than hardcoded here, so this gate
 *      can never go stale the way a copied version table would.
 *   2. The node:sqlite TEXT decoder must not truncate at embedded NUL. This is
 *      a capability probe, not a version guess — vendor backports pass it too.
 *   3. The actually-loaded SQLite library must be WAL-reset-safe, because a
 *      Node build linked against shared system SQLite can load a different
 *      version than Node's own metadata advertises.
 *
 * Usage:
 *   node verify-node-runtime.mjs              # auto-detect installed OpenClaw
 *   node verify-node-runtime.mjs <pkg.json>   # check against a specific package
 * Exits 0 when the runtime can run OpenClaw, 1 with a diagnostic otherwise.
 */

import { createRequire } from "node:module";
import { existsSync, readFileSync } from "node:fs";

const require = createRequire(import.meta.url);

// Fallback only, used when no OpenClaw manifest can be found. Matches the
// policy documented for openclaw v2026.9.3+ in docs.openclaw.ai.
const FALLBACK_ENGINE_RANGE = ">=24.16.0 <25 || >=26.1.0";

const failures = [];
const notes = [];

/** Compare dotted numeric versions. Returns -1, 0, 1. Non-numeric parts ignored. */
function cmp(a, b) {
  const pa = String(a).split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d < 0 ? -1 : 1;
  }
  return 0;
}

/**
 * Minimal semver range evaluator covering the operators OpenClaw's engines
 * field actually uses: >=, >, <=, <, = and bare versions, combined with
 * whitespace (AND) and || (OR). Deliberately dependency-free so it can run in
 * a bare `node:24-slim` stage before any npm install.
 */
function satisfies(version, range) {
  return String(range)
    .split("||")
    .some((group) =>
      group
        .trim()
        .split(/\s+/)
        .filter(Boolean)
        .every((part) => {
          const m = part.match(/^(>=|<=|>|<|=)?\s*v?(.+)$/);
          if (!m) return false;
          const op = m[1] || "=";
          const c = cmp(version, m[2]);
          if (op === ">=") return c >= 0;
          if (op === ">") return c > 0;
          if (op === "<=") return c <= 0;
          if (op === "<") return c < 0;
          return c === 0;
        }),
    );
}

/** Find the OpenClaw manifest to validate against. Returns null if none found. */
function findOpenclawManifest() {
  const explicit = process.argv[2];
  const candidates = explicit
    ? [explicit]
    : [
        // baked into the image (stage 1 copy)
        "/home/node/.openclaw/openclaw-app/package.json",
        // installed by start.sh's runtime upgrade under NPM_CONFIG_PREFIX
        process.env.NPM_CONFIG_PREFIX
          ? `${process.env.NPM_CONFIG_PREFIX}/lib/node_modules/openclaw/package.json`
          : null,
        `${process.env.HOME || "/home/node"}/.local/lib/node_modules/openclaw/package.json`,
        "/usr/local/lib/node_modules/openclaw/package.json",
      ].filter(Boolean);

  for (const p of candidates) {
    try {
      if (existsSync(p)) {
        const pkg = JSON.parse(readFileSync(p, "utf8"));
        if (pkg?.engines?.node) return { path: p, version: pkg.version, range: pkg.engines.node };
      }
    } catch {
      /* unreadable or malformed manifest — try the next candidate */
    }
  }
  return null;
}

// ── Gate 1: Node satisfies the installed OpenClaw's engines.node ────────────
const nodeVersion = process.versions.node;
const manifest = findOpenclawManifest();
const engineRange = manifest?.range ?? FALLBACK_ENGINE_RANGE;

if (!manifest) {
  notes.push(
    `No OpenClaw manifest found; fell back to the documented range "${FALLBACK_ENGINE_RANGE}".`,
  );
}

if (!satisfies(nodeVersion, engineRange)) {
  failures.push(
    `Node ${nodeVersion} does not satisfy OpenClaw's engines.node ("${engineRange}")` +
      (manifest ? ` [openclaw ${manifest.version}]` : "") +
      `.\n` +
      `           Fix: change the stage-2 FROM line in the Dockerfile to a Node release\n` +
      `           inside that range, then rebuild.`,
  );
} else {
  const who = manifest ? `openclaw ${manifest.version}` : "the documented OpenClaw policy";
  notes.push(`Node ${nodeVersion} satisfies ${who} (engines.node: "${engineRange}").`);
}

// ── Gate 2 + 3: node:sqlite capability and the loaded SQLite library ───────
let sqliteVersion = null;
try {
  const { DatabaseSync } = require("node:sqlite");
  const db = new DatabaseSync(":memory:");

  sqliteVersion = db.prepare("SELECT sqlite_version() AS v").get().v;

  // Embedded AND trailing NUL: the two cases OpenClaw's TEXT decoder drops.
  const cases = [
    { name: "embedded NUL", value: "a\0b", expect: 3 },
    { name: "trailing NUL", value: "ab\0", expect: 3 },
  ];
  db.exec("CREATE TABLE probe (v TEXT)");
  const insert = db.prepare("INSERT INTO probe (v) VALUES (?)");
  const select = db.prepare("SELECT v FROM probe");

  for (const c of cases) {
    db.exec("DELETE FROM probe");
    insert.run(c.value);
    const got = select.get().v;
    if (typeof got !== "string" || got.length !== c.expect) {
      failures.push(
        `node:sqlite truncates TEXT at ${c.name} on Node ${nodeVersion} ` +
          `(stored ${JSON.stringify(c.value)}, read ${JSON.stringify(got)}). ` +
          `This is nodejs/node#61954; the first fixed releases are 24.16.0 and 26.1.0.`,
      );
    } else {
      notes.push(`node:sqlite preserves ${c.name} (probe passed).`);
    }
  }
  db.close();
} catch (err) {
  failures.push(`node:sqlite probe could not run: ${err.message}`);
}

// WAL-reset-safe SQLite lines. Anything older risks the WAL-reset corruption bug.
const SQLITE_SAFE_RANGES = [
  { label: ">= 3.51.3", min: "3.51.3" },
  { label: "3.50.7+ within 3.50.x", min: "3.50.7", max: "3.51.0" },
  { label: "3.44.6+ within 3.44.x", min: "3.44.6", max: "3.45.0" },
];
const inSqliteRange = (v, r) => cmp(v, r.min) >= 0 && (!r.max || cmp(v, r.max) < 0);

if (sqliteVersion) {
  const hit = SQLITE_SAFE_RANGES.find((r) => inSqliteRange(sqliteVersion, r));
  if (!hit) {
    failures.push(
      `Loaded SQLite ${sqliteVersion} is not WAL-reset-safe ` +
        `(needs ${SQLITE_SAFE_RANGES.map((r) => r.label).join(", ")}). ` +
        `This image must not link against an older shared system SQLite.`,
    );
  } else {
    notes.push(`Loaded SQLite ${sqliteVersion} is WAL-reset-safe (${hit.label}).`);
  }
}

// ── report ────────────────────────────────────────────────────────────────
console.log("── verify-node-runtime ──────────────────────────────");
for (const n of notes) console.log(`  ok    ${n}`);
if (failures.length === 0) {
  console.log("  PASS  runtime can run OpenClaw\n");
  process.exit(0);
}
for (const f of failures) console.error(`  FAIL  ${f}`);
console.error(
  "\n  Check aborted. Continuing would only produce an endless\n" +
    '  "Gateway failed - DEV_MODE active, retrying in 10s..." loop at runtime.\n',
);
process.exit(1);
