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
only routine manual publishing step is updating `forecast.json`; forecast
verification remains a separate editorial task.

## Local archive check

Run the same archive logic locally with:

```text
node scripts/archive-forecast.mjs previous-forecast.json forecast.json forecast-history.json
```
