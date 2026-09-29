import { readFileSync } from "node:fs";
import { validateForecastFeedback } from "./lib/forecast-accuracy.mjs";

const [forecastPath = "forecast.json", accuracyPath = "forecast-accuracy.json"] = process.argv.slice(2).filter((value) => !value.startsWith("--"));
const allowLegacy = process.argv.includes("--allow-legacy");

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`Unable to read valid JSON from ${path}: ${error.message}`);
  }
}

const forecast = readJson(forecastPath);
const accuracy = readJson(accuracyPath);
const errors = validateForecastFeedback(forecast, accuracy, { allowLegacy });

if (errors.length) {
  console.error(`Forecast validation failed with ${errors.length} error${errors.length === 1 ? "" : "s"}:`);
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

console.log(`Forecast ${forecast.forecastId} passed ${allowLegacy ? "legacy-compatible" : "strict"} validation.`);
