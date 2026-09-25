import { readFileSync, writeFileSync } from "node:fs";
import { isDeepStrictEqual } from "node:util";

const [previousPath, currentPath, historyPath] = process.argv.slice(2);

if (!previousPath || !currentPath || !historyPath) {
  throw new Error(
    "Usage: node scripts/archive-forecast.mjs <previous-forecast.json> <current-forecast.json> <forecast-history.json>",
  );
}

function readJson(filePath) {
  try {
    return JSON.parse(readFileSync(filePath, "utf8"));
  } catch (error) {
    throw new Error(`Unable to read valid JSON from ${filePath}: ${error.message}`);
  }
}

function requireForecast(forecast, label) {
  for (const field of ["forecastId", "issuedAt", "validThrough", "regions"]) {
    if (!forecast[field]) {
      throw new Error(`${label} is missing required field ${field}`);
    }
  }

  if (!Number.isFinite(Date.parse(forecast.issuedAt))) {
    throw new Error(`${label} has an invalid issuedAt timestamp`);
  }
}

function compactDays(region) {
  if (!Array.isArray(region?.days)) return [];

  return region.days.map(({ date, highF, lowF, precipitation }) => ({
    date,
    highF,
    lowF,
    precipitation,
  }));
}

function checkpointMap(forecast) {
  const checkpoints = forecast.verification?.checkpoints;
  if (!Array.isArray(checkpoints)) return {};

  return Object.fromEntries(
    checkpoints.map((checkpoint) => [
      `${checkpoint.hours}h`,
      {
        due: checkpoint.due,
        status: checkpoint.status ?? "pending",
      },
    ]),
  );
}

function comparableForecast(forecast) {
  const copy = structuredClone(forecast);
  delete copy.refresh;
  return copy;
}

function archiveRecord(forecast) {
  return {
    forecastId: forecast.forecastId,
    issuedAt: forecast.issuedAt,
    validThrough: forecast.validThrough,
    headline: forecast.headline,
    sourceFile: "forecast.json",
    lockedOriginal: true,
    originalForecast: forecast,
    centralKentucky: {
      days: compactDays(forecast.regions.kentucky),
    },
    upstateSouthCarolina: {
      days: compactDays(forecast.regions.upstate),
    },
    verification: checkpointMap(forecast),
  };
}

const previous = readJson(previousPath);
const current = readJson(currentPath);
const history = readJson(historyPath);

requireForecast(previous, "Previous forecast");
requireForecast(current, "Current forecast");

if (!Array.isArray(history.issues)) {
  throw new Error("forecast-history.json must contain an issues array");
}

if (
  previous.forecastId !== current.forecastId &&
  Date.parse(current.issuedAt) <= Date.parse(previous.issuedAt)
) {
  throw new Error("The replacement forecast must have a later issuedAt timestamp");
}

const generatedRecord = archiveRecord(previous);
const existingIndex = history.issues.findIndex(
  (issue) => issue.forecastId === previous.forecastId,
);

if (existingIndex >= 0) {
  const existing = history.issues[existingIndex];

  if (
    existing.originalForecast &&
    !isDeepStrictEqual(
      comparableForecast(existing.originalForecast),
      comparableForecast(previous),
    )
  ) {
    throw new Error(
      `Archive ${previous.forecastId} already has a different locked original`,
    );
  }

  history.issues[existingIndex] = {
    ...generatedRecord,
    ...existing,
    lockedOriginal: true,
    originalForecast: existing.originalForecast ?? previous,
  };
} else {
  history.issues.push(generatedRecord);
}

history.issues.sort(
  (left, right) => Date.parse(right.issuedAt) - Date.parse(left.issuedAt),
);

const updated = `${JSON.stringify(history, null, 2)}\n`;
const original = readFileSync(historyPath, "utf8");

if (updated === original) {
  console.log(`Archive ${previous.forecastId} is already current.`);
  process.exit(0);
}

writeFileSync(historyPath, updated, "utf8");
console.log(`Archived and locked forecast ${previous.forecastId}.`);
