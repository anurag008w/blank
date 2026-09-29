#!/usr/bin/env node
/**
 * Build-time runtime gate for the HuggingClaw image.
 *
 * WHY THIS EXISTS
 * ---------------
 * The image pulls `ghcr.io/openclaw/openclaw:<version>` (stage 1) and runs it on
 * a separate Node base image (stage 2). Those two are versioned INDEPENDENTLY,
 * so a perfectly valid Docker build can produce an image whose OpenClaw refuses
 * to start at runtime. When that happens `start.sh` only reports
 * "Gateway failed - DEV_MODE active, retrying in 10s..." and retries forever,
 * which hides the real cause completely.
 *
 * So this script runs during `docker build` and fails the BUILD instead. It
 * mirrors the three gates OpenClaw itself applies at startup:
 *
 *   1. Node version must be inside OpenClaw's support table.
 *      v2026.9.3+ declares engines: ">=24.16.0 <25 || >=26.1.0"
 *      Node 22, 23 and 25 are all excluded.
 *   2. The node:sqlite TEXT decoder must not truncate at embedded NUL
 *      (nodejs/node#61954). This is a capability probe, not a version guess —
 *      vendor backports pass it too, exactly like OpenClaw's own probe.
 *   3. The actually-loaded SQLite library must be WAL-reset-safe, because a
 *      Node build linked against shared system SQLite can load a version
 *      different from what Node's own metadata advertises.
 *
 * Usage:  node verify-node-runtime.mjs
 * Exits 0 when the runtime can run OpenClaw, 1 with a diagnostic otherwise.
 */

import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

const OPENCLAW_MIN = "2026.9.3"; // first release with the >=24.16.0 <25 || >=26.1.0 floor

// OpenClaw v2026.9.3+ support table (docs.openclaw.ai/install/node-compatibility).
const SUPPORTED_RANGES = [
  { label: "Node 24 LTS", min: "24.16.0", max: "25.0.0" },
  { label: "Node 26", min: "26.1.0", max: null },
];

// WAL-reset-safe SQLite lines. Anything older risks the WAL-reset corruption bug.
const SQLITE_SAFE_RANGES = [
  { label: ">= 3.51.3", min: "3.51.3" },
  { label: "3.50.7+ within 3.50.x", min: "3.50.7", max: "3.51.0" },
  { label: "3.44.6+ within 3.44.x", min: "3.44.6", max: "3.45.0" },
];

const failures = [];
const notes = [];

/** Compare dotted numeric versions. Returns -1, 0, 1. Non-numeric parts are ignored. */
function cmp(a, b) {
  const pa = String(a).split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d < 0 ? -1 : 1;
  }
  return 0;
}

function inRange(version, { min, max }) {
  // `max` is absent on open-ended ranges, so treat null AND undefined as "no ceiling".
  return cmp(version, min) >= 0 && (!max || cmp(version, max) < 0);
}

// ── Gate 1: Node version inside OpenClaw's support table ─────────────────────
const nodeVersion = process.versions.node;
const nodeOk = SUPPORTED_RANGES.some((r) => inRange(nodeVersion, r));
if (!nodeOk) {
  failures.push(
    `Node ${nodeVersion} is outside OpenClaw's support table ` +
      `(required by openclaw >= ${OPENCLAW_MIN}).\n` +
      `           Supported: ${SUPPORTED_RANGES.map((r) => `${r.label} (${r.min}${r.max ? ` .. <${r.max}` : "+"})`).join(", ")}\n` +
      `           Node 22, 23 and 25 are all excluded by OpenClaw.\n` +
      `           Fix: change the stage-2 FROM line in the Dockerfile.`,
  );
} else {
  const hit = SUPPORTED_RANGES.find((r) => inRange(nodeVersion, r));
  notes.push(`Node ${nodeVersion} is supported (${hit.label}).`);
}

// ── Gate 2 + 3: node:sqlite capability and the loaded SQLite library ─────────
let sqliteVersion = null;
try {
  const { DatabaseSync } = require("node:sqlite");
  const db = new DatabaseSync(":memory:");

  sqliteVersion = db.prepare("SELECT sqlite_version() AS v").get().v;

  // Embedded AND trailing NUL, the two cases OpenClaw's decoder is known to drop.
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

if (sqliteVersion) {
  const sqliteOk = SQLITE_SAFE_RANGES.some((r) => inRange(sqliteVersion, r));
  if (!sqliteOk) {
    failures.push(
      `Loaded SQLite ${sqliteVersion} is not WAL-reset-safe ` +
        `(needs ${SQLITE_SAFE_RANGES.map((r) => r.label).join(", ")}). ` +
        `This image must not link against an older shared system SQLite.`,
    );
  } else {
    const hit = SQLITE_SAFE_RANGES.find((r) => inRange(sqliteVersion, r));
    notes.push(`Loaded SQLite ${sqliteVersion} is WAL-reset-safe (${hit.label}).`);
  }
}

// ── report ──────────────────────────────────────────────────────────────────
console.log("── verify-node-runtime ──────────────────────────────");
for (const n of notes) console.log(`  ok    ${n}`);
if (failures.length === 0) {
  console.log("  PASS  runtime can run OpenClaw\n");
  process.exit(0);
}
for (const f of failures) console.error(`  FAIL  ${f}`);
console.error(
  "\n  Build aborted. Shipping this image would only produce an endless\n" +
    '  "Gateway failed - DEV_MODE active, retrying in 10s..." loop at runtime.\n',
);
process.exit(1);
