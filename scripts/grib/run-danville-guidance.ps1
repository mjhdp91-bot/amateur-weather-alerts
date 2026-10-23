#requires -Version 7.0

[CmdletBinding()]
param(
    [ValidateSet('hrrr', 'rap', 'nam', 'nam_conusnest', 'gfs', 'gefs', 'ecmwf')]
    [string[]] $Models = @('hrrr', 'rap', 'nam', 'nam_conusnest', 'gfs', 'gefs', 'ecmwf'),

    [ValidateSet(72)]
    [int] $Hours = 72,

    [string] $IssuedAt,

    [switch] $DryRun,

    [switch] $CacheOnly,

    [switch] $KeepRaw
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($DryRun -and $CacheOnly) {
    throw '-DryRun and -CacheOnly are mutually exclusive.'
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)] [string] $Executable,
        [Parameter(Mandatory)] [string[]] $Arguments,
        [Parameter(Mandatory)] [string] $Stage
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Stage failed with exit code $LASTEXITCODE."
    }
}

function Invoke-CheckedJson {
    param(
        [Parameter(Mandatory)] [string] $Executable,
        [Parameter(Mandatory)] [string[]] $Arguments,
        [Parameter(Mandatory)] [string] $Stage
    )

    $outputLines = & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Stage failed with exit code $LASTEXITCODE."
    }
    $rawOutput = $outputLines -join [Environment]::NewLine
    try {
        $value = $rawOutput | ConvertFrom-Json -Depth 100
    }
    catch {
        throw "$Stage did not return valid JSON."
    }
    return [pscustomobject]@{
        RawOutput = $rawOutput
        Value = $value
    }
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = Join-Path $repositoryRoot '.venv-grib\Scripts\python.exe'
$configPath = Join-Path $repositoryRoot 'config\grib\danville.json'
$fetchScript = Join-Path $repositoryRoot 'scripts\grib\fetch-guidance.py'
$extractScript = Join-Path $repositoryRoot 'scripts\grib\extract-guidance.py'
$summaryScript = Join-Path $repositoryRoot 'scripts\grib\build-summary.py'
$validatorScript = Join-Path $repositoryRoot 'scripts\grib\validate-guidance.mjs'
$extractedPath = Join-Path $repositoryRoot 'grib-data\extracted\danville-hourly.json'
$summaryPath = Join-Path $repositoryRoot 'grib-data\summary\danville-guidance.json'

foreach ($requiredPath in @($configPath, $fetchScript, $extractScript, $summaryScript, $validatorScript)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required pipeline file is missing: $requiredPath"
    }
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'The local GRIB Python environment is missing. Follow docs/grib-guidance.md to create .venv-grib and install requirements-grib-lock.txt.'
}

if ($IssuedAt) {
    $issuedAtValue = $IssuedAt.Trim()
    if ($issuedAtValue -notmatch '(?:[zZ]|[+-]\d{2}:\d{2})$') {
        throw '-IssuedAt must be an ISO timestamp with a trailing Z or explicit +/-HH:MM UTC offset.'
    }
    try {
        $issuedTime = [DateTimeOffset]::Parse(
            $issuedAtValue,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AllowWhiteSpaces
        )
    }
    catch {
        throw '-IssuedAt must be an ISO timestamp with a trailing Z or explicit +/-HH:MM UTC offset.'
    }
}
else {
    $issuedTime = [DateTimeOffset]::UtcNow
}
$issuedIso = $issuedTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$modelList = $Models -join ','

$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $repositoryRoot
    $env:PYTHONDONTWRITEBYTECODE = '1'

    if (-not $DryRun) {
        Invoke-Checked -Executable $pythonPath -Arguments @('-c', 'import eccodes') -Stage 'ecCodes dependency check'
    }

    $fetchArguments = @(
        $fetchScript,
        '--config', $configPath,
        '--models', $modelList,
        '--hours', [string]$Hours,
        '--issued-at', $issuedIso,
        '--output-root', (Join-Path $repositoryRoot 'grib-data')
    )
    if ($DryRun) { $fetchArguments += '--dry-run' }
    if ($CacheOnly) { $fetchArguments += '--cache-only' }
    if ($KeepRaw) { $fetchArguments += '--keep-raw' }
    $fetchStage = if ($CacheOnly) { 'GRIB cache verification' } else { 'GRIB discovery and fetch' }
    $fetchResult = Invoke-CheckedJson -Executable $pythonPath -Arguments $fetchArguments -Stage $fetchStage
    Write-Output $fetchResult.RawOutput

    if ($DryRun) {
        Write-Output 'Dry run complete. No model data were downloaded, extracted, summarized or published.'
        return
    }

    $manifestRelativePath = [string]$fetchResult.Value.manifestPath
    if ([string]::IsNullOrWhiteSpace($manifestRelativePath) -or [IO.Path]::IsPathRooted($manifestRelativePath)) {
        throw "$fetchStage returned an invalid repository-relative manifestPath."
    }
    $manifestPath = [IO.Path]::GetFullPath((Join-Path $repositoryRoot $manifestRelativePath))
    $repositoryPrefix = $repositoryRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $manifestPath.StartsWith($repositoryPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$fetchStage returned a manifestPath outside the repository."
    }
    if ([IO.Path]::GetFileName($manifestPath) -ieq 'latest.json') {
        throw "$fetchStage returned the mutable latest.json alias instead of an immutable versioned manifest."
    }
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "$fetchStage returned a manifestPath that does not exist: $manifestRelativePath"
    }

    Invoke-Checked -Executable $pythonPath -Arguments @(
        $extractScript,
        '--config', $configPath,
        '--manifest', $manifestPath,
        '--output', $extractedPath,
        '--strict'
    ) -Stage 'GRIB extraction'

    Invoke-Checked -Executable $pythonPath -Arguments @(
        $summaryScript,
        '--input', $extractedPath,
        '--output', $summaryPath,
        '--start', $issuedIso,
        '--time-zone', 'America/New_York',
        '--hours', [string]$Hours
    ) -Stage 'GRIB summary generation'

    $nodePath = (Get-Command node -ErrorAction Stop).Source
    Invoke-Checked -Executable $nodePath -Arguments @($validatorScript, $summaryPath) -Stage 'GRIB summary validation'

    Write-Output "Validated local guidance: $summaryPath"
    Write-Output 'Review is required. forecast.json and the website were not modified or published.'
}
finally {
    Set-Location -LiteralPath $previousLocation
}
