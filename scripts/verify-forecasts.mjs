import { existsSync, readFileSync, writeFileSync } from "node:fs";
import {
  REGION_CONFIG,
  evaluateRegionCheckpoint,
  scoreModelBenchmarks,
  summarizeNwsObservations,
} from "./lib/forecast-accuracy.mjs";

const positional = process.argv.slice(2).filter((value) => !value.startsWith("--"));
const [historyPath = "forecast-history.json", currentPath = "forecast.json", ledgerPath = "forecast-verifications.json"] = positional;
const nowArgument = process.argv.find((value) => value.startsWith("--now="))?.slice(6);
const now = nowArgument ? new Date(nowArgument) : new Date();
const graceMinutes = Number(process.argv.find((value) => value.startsWith("--grace-minutes="))?.split("=")[1] ?? 90);
const minimumTemperatureObservations = Number(
  process.argv.find((value) => value.startsWith("--minimum-observations="))?.split("=")[1] ?? 3,
);

if (!Number.isFinite(now.getTime())) throw new Error("--now must be an ISO-8601 timestamp");

function readJson(path, fallback) {
  if (!existsSync(path)) return structuredClone(fallback);
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`Unable to read valid JSON from ${path}: ${error.message}`);
  }
}

function writeJson(path, value) {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

async function fetchJson(url, attempts = 3) {
  let finalError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetch(url, {
        headers: {
          Accept: "application/geo+json, application/json",
          "User-Agent": "amateur-weather-alerts verification workflow (github.com/mjhdp91-bot/amateur-weather-alerts)",
        },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      finalError = error;
      if (attempt < attempts) await new Promise((resolve) => setTimeout(resolve, attempt * 750));
    }
  }
  throw finalError;
}

function checkpointTemplate(forecast) {
  return Object.fromEntries(
    (forecast.verification?.checkpoints ?? []).map((checkpoint) => [
      `${checkpoint.hours}h`,
      { due: checkpoint.due, status: checkpoint.status ?? "pending" },
    ]),
  );
}

function ensureLedgerRecord(ledger, forecast, source) {
  const existing = ledger.forecasts[forecast.forecastId];
  ledger.forecasts[forecast.forecastId] = {
    forecastId: forecast.forecastId,
    issuedAt: forecast.issuedAt,
    validThrough: forecast.validThrough,
    source,
    checkpoints: {
      ...checkpointTemplate(forecast),
      ...(existing?.checkpoints ?? {}),
    },
  };
  return ledger.forecasts[forecast.forecastId];
}

function importHistoryVerification(record, issue) {
  for (const [key, checkpoint] of Object.entries(issue.verification ?? {})) {
    if (checkpoint?.status === "verified" && record.checkpoints[key]?.status !== "verified") {
      record.checkpoints[key] = checkpoint;
    }
  }
}

function summarySentence(regions) {
  const stationScores = Object.values(regions).flatMap((region) => Object.values(region.stationScores));
  const highHits = stationScores.filter((score) => score.highTemperature?.withinRange).length;
  const lowHits = stationScores.filter((score) => score.lowTemperature?.withinRange).length;
  const rainEvents = Object.values(regions).filter((region) => region.precipitation.observed).length;
  return `${highHits}/${stationScores.length} station highs and ${lowHits}/${stationScores.length} station lows fell inside the issued ranges; precipitation occurred in ${rainEvents}/${Object.keys(regions).length} regions.`;
}

async function verifyCheckpoint(forecast, checkpointHours, due) {
  const end = new Date(due);
  const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
  const regions = {};
  const sources = [];
  const modelScores = [];

  for (const [regionKey, config] of Object.entries(REGION_CONFIG)) {
    const stationObservations = {};
    for (const station of config.stations) {
      const url = new URL(`https://api.weather.gov/stations/${station}/observations`);
      url.searchParams.set("start", start.toISOString());
      url.searchParams.set("end", end.toISOString());
      url.searchParams.set("limit", "500");
      const payload = await fetchJson(url);
      const summary = summarizeNwsObservations(payload.features ?? []);
      if (summary.temperatureObservationCount < minimumTemperatureObservations) {
        throw new Error(
          `${station} supplied ${summary.temperatureObservationCount} temperature observations; ${minimumTemperatureObservations} required`,
        );
      }
      stationObservations[station] = summary;
      sources.push(url.toString());
    }
    const regionResult = evaluateRegionCheckpoint({
      forecast,
      regionKey,
      checkpointHours,
      stationObservations,
    });
    regions[regionKey] = regionResult;
    modelScores.push(...scoreModelBenchmarks({ forecast, checkpointHours, regionKey, regionResult }));
  }

  return {
    forecastId: forecast.forecastId,
    checkpointHours,
    due,
    status: "verified",
    verifiedAt: now.toISOString(),
    window: { start: start.toISOString(), end: end.toISOString() },
    officialOnly: true,
    upstateTempestExcluded: true,
    overallAssessment: summarySentence(regions),
    regions,
    modelScores,
    sources,
  };
}

const history = readJson(historyPath, { schemaVersion: 1, policy: {}, issues: [] });
const current = readJson(currentPath);
const ledger = readJson(ledgerPath, {
  schemaVersion: 1,
  methodology: {
    checkpointWindows: "Each checkpoint scores the preceding 24-hour window ending at its due time.",
    observations: "Official NWS station observations only.",
    precipitation: "A measurable precipitation or rain/drizzle/shower report at either regional station counts as occurrence.",
    upstateTempestExcluded: true,
  },
  forecasts: {},
});
const originalLedgerState = JSON.stringify(ledger);

if (!Array.isArray(history.issues)) throw new Error("forecast-history.json must contain an issues array");
if (!ledger.forecasts || Array.isArray(ledger.forecasts)) ledger.forecasts = {};

const forecasts = new Map();
for (const issue of history.issues) {
  if (!issue.originalForecast) continue;
  forecasts.set(issue.forecastId, issue.originalForecast);
  const record = ensureLedgerRecord(ledger, issue.originalForecast, "forecast-history.json");
  importHistoryVerification(record, issue);
}
forecasts.set(current.forecastId, current);
ensureLedgerRecord(ledger, current, "forecast.json");

let verifiedCount = 0;
let deferredCount = 0;
for (const [forecastId, forecast] of forecasts) {
  const record = ledger.forecasts[forecastId];
  for (const [checkpointKey, checkpoint] of Object.entries(record.checkpoints)) {
    if (checkpoint.status === "verified") continue;
    const hours = Number(checkpointKey.replace("h", ""));
    const dueTime = Date.parse(checkpoint.due);
    if (!Number.isFinite(dueTime)) throw new Error(`${forecastId} ${checkpointKey} has an invalid due time`);
    if (now.getTime() < dueTime + graceMinutes * 60 * 1000) continue;
    try {
      record.checkpoints[checkpointKey] = await verifyCheckpoint(forecast, hours, checkpoint.due);
      verifiedCount += 1;
      console.log(`Verified ${forecastId} ${checkpointKey}.`);
    } catch (error) {
      record.checkpoints[checkpointKey] = {
        ...checkpoint,
        status: "deferred",
        lastAttemptAt: now.toISOString(),
        reason: error.message,
      };
      deferredCount += 1;
      console.warn(`Deferred ${forecastId} ${checkpointKey}: ${error.message}`);
    }
  }
}

for (const issue of history.issues) {
  const record = ledger.forecasts[issue.forecastId];
  if (record) issue.verification = structuredClone(record.checkpoints);
}

if (JSON.stringify(ledger) !== originalLedgerState) ledger.generatedAt = now.toISOString();
writeJson(ledgerPath, ledger);
writeJson(historyPath, history);
console.log(`Verification complete: ${verifiedCount} newly verified, ${deferredCount} deferred.`);
