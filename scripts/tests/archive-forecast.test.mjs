import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("archive preserves verification results from the independent ledger", () => {
  const directory = mkdtempSync(join(tmpdir(), "forecast-archive-test-"));
  const previous = {
    forecastId: "old",
    issuedAt: "2026-09-01T08:00:00-04:00",
    validThrough: "2026-09-04T08:00:00-04:00",
    headline: "Old",
    regions: { kentucky: { days: [] }, upstate: { days: [] } },
    verification: { checkpoints: [{ hours: 24, due: "2026-09-02T08:00:00-04:00", status: "pending" }] },
  };
  const current = {
    ...previous,
    forecastId: "new",
    issuedAt: "2026-09-02T08:00:00-04:00",
    validThrough: "2026-09-05T08:00:00-04:00",
  };
  const ledgerCheckpoint = {
    due: "2026-09-02T08:00:00-04:00",
    status: "verified",
    verifiedAt: "2026-09-02T10:00:00-04:00",
  };
  const paths = {
    previous: join(directory, "previous.json"),
    current: join(directory, "current.json"),
    history: join(directory, "history.json"),
    ledger: join(directory, "ledger.json"),
  };
  writeFileSync(paths.previous, JSON.stringify(previous));
  writeFileSync(paths.current, JSON.stringify(current));
  writeFileSync(paths.history, JSON.stringify({ schemaVersion: 1, issues: [] }));
  writeFileSync(paths.ledger, JSON.stringify({ forecasts: { old: { checkpoints: { "24h": ledgerCheckpoint } } } }));

  const archiveScript = fileURLToPath(new URL("../archive-forecast.mjs", import.meta.url));
  const result = spawnSync(process.execPath, [archiveScript, paths.previous, paths.current, paths.history, paths.ledger], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const history = JSON.parse(readFileSync(paths.history, "utf8"));
  assert.equal(history.issues[0].lockedOriginal, true);
  assert.deepEqual(history.issues[0].verification["24h"], ledgerCheckpoint);
});
