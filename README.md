# amateur-weather-alerts

Live NWS weather alerts and independent 72-hour forecasts for Central Kentucky
and Upstate South Carolina.

## Forecast publishing

Publish a new issue by replacing `forecast.json` and committing that one file.
The **Archive issued forecast** GitHub Actions workflow automatically:

1. reads the prior version of `forecast.json` from Git history;
2. stores a complete, locked copy in `forecast-history.json`;
3. preserves any verification results already recorded for that issue; and
4. commits the updated archive back to `main`.

The workflow will not duplicate an issue. If an older compact history entry exists,
it adds the full original forecast while preserving the existing entry and its
verification status. A conflicting locked copy causes the workflow to fail instead
of overwriting the archive.

GitHub Pages then serves both the current forecast and the updated archive. The
only routine manual publishing step is updating `forecast.json`. Verification is
maintained independently so an issued forecast is never silently rewritten.

## Automatic accuracy feedback

The **Verify forecast accuracy** workflow runs daily and can also be started
manually. It:

1. finds every due 24-, 48- and 72-hour checkpoint for both the active forecast
   and archived forecasts;
2. retrieves official NWS observations for KDVK, KLEX, KCEU and KLQK;
3. scores high and low temperature ranges, precipitation probability, wind,
   visibility, fog and thunder while continuing to exclude Upstate Tempest data;
4. stores the full checkpoint result in `forecast-verifications.json` and mirrors
   archived results into `forecast-history.json`; and
5. rebuilds `forecast-accuracy.json`, the cumulative machine-readable accuracy
   summary used when preparing the next forecast.

The summary reports temperature bias, mean absolute error, RMSE, range coverage
and average range width by region, station, lead time and weather regime. It also
reports precipitation Brier score and reliability bins. Model guidance is scored
only from benchmarks captured at issue time; model values are never reconstructed
after the event.

Bias corrections require at least four matching samples and an absolute mean bias
of at least 1.5°F. Qualified corrections are shrunk by 25% and limited to ±4°F.
This prevents a single unusual observation from becoming an automatic correction.

If official observations are incomplete, the checkpoint is marked `deferred`
with a reason and is retried on the next workflow run rather than being treated as
verified.

## Requirements for every new forecast

The **Validate forecast feedback** workflow rejects new forecast issues unless
they include:

- `weatherRegime` on each of the three days in both regions;
- `modelBenchmarks` containing the point values actually consulted at issuance,
  or a documented `modelBenchmarkExclusions` entry when a listed model cannot be
  scored; and
- `accuracyFeedback`, identifying the exact `forecast-accuracy.json` generation,
  all verified forecasts consulted, and the adjustment or explicit no-adjustment
  decision for each region.

Minimal feedback structure:

```json
{
  "accuracyFeedback": {
    "source": "forecast-accuracy.json",
    "summaryGeneratedAt": "2026-09-29T17:40:25.736Z",
    "consultedForecastIds": ["20260925T1320Z"],
    "appliedAdjustments": [
      {
        "region": "kentucky",
        "decision": "no-adjustment",
        "reason": "No bias has reached the four-sample threshold."
      },
      {
        "region": "upstate",
        "decision": "monitor",
        "reason": "KLQK low bias is notable but has only one verified sample."
      }
    ]
  }
}
```

This makes the feedback step auditable. A forecast cannot merely claim that prior
performance was reviewed; it must record which evidence was consulted and what
decision resulted.

## Local archive check

Run the same archive logic locally with:

```text
node scripts/archive-forecast.mjs previous-forecast.json forecast.json forecast-history.json
```

Run the accuracy tools locally with:

```text
node scripts/verify-forecasts.mjs forecast-history.json forecast.json forecast-verifications.json
node scripts/build-accuracy-summary.mjs forecast-history.json forecast.json forecast-verifications.json forecast-accuracy.json
node scripts/validate-forecast.mjs forecast.json forecast-accuracy.json --allow-legacy
node --test scripts/tests/*.test.mjs
```

`--allow-legacy` exists only for the forecast that was already live when this
system was introduced. New forecast issues must pass strict validation without it.
