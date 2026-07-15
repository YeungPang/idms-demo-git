param(
    [string]$OutputDir = "handoff",
    [string]$DbName = "idms_demo",
    [string]$DbHost = "localhost",
    [string]$DbPort = "5432",
    [string]$DbUser = "postgres",
    [string]$QdrantUrl = "http://localhost:6333",
    [string]$QdrantCollection = "idms_demo_documents"
)

$ErrorActionPreference = "Stop"

function Test-CommandAvailable {
    param([string]$CommandName)
    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    return $null -ne $cmd
}

function Find-PgDumpPath {
    $cmd = Get-Command "pg_dump" -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return [string]$cmd.Source
    }

    $candidates = Get-ChildItem -Path "C:/Program Files/PostgreSQL" -Filter "pg_dump.exe" -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending
    if ($candidates -and $candidates.Count -gt 0) {
        return [string]$candidates[0].FullName
    }

    return $null
}

function Get-DotEnvValue {
    param(
        [string]$FilePath,
        [string]$Key
    )

    if (-not (Test-Path $FilePath)) {
        return ""
    }

    $line = Get-Content -Path $FilePath -ErrorAction SilentlyContinue |
        Where-Object { $_ -match "^\s*$([Regex]::Escape($Key))\s*=" } |
        Select-Object -First 1

    if (-not $line) {
        return ""
    }

    $value = ($line -replace "^\s*$([Regex]::Escape($Key))\s*=\s*", "").Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

function Assert-LastCommandSucceeded {
    param([string]$Description)
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bundleDir = Join-Path $OutputDir "idms_demo_handoff_$timestamp"
New-Item -ItemType Directory -Path $bundleDir -Force | Out-Null

$pgDumpPath = Find-PgDumpPath
if (-not $pgDumpPath) {
    throw "pg_dump is not available. Install PostgreSQL client tools or add pg_dump.exe to PATH."
}

$dotenvPath = Join-Path (Get-Location) ".env"
if (-not $env:PGPASSWORD) {
    $dotenvPassword = Get-DotEnvValue -FilePath $dotenvPath -Key "IDMS_DB_PASSWORD"
    if ($dotenvPassword) {
        $env:PGPASSWORD = $dotenvPassword
    }
}

if (-not $env:PGPASSWORD) {
    throw "No PostgreSQL password available. Set PGPASSWORD or IDMS_DB_PASSWORD in .env before running export."
}

$dbDumpPath = Join-Path $bundleDir "idms_demo_data.dump"
$dbSchemaPath = Join-Path $bundleDir "idms_demo_schema.sql"

if (Test-Path $dbDumpPath) { Remove-Item -Path $dbDumpPath -Force }
if (Test-Path $dbSchemaPath) { Remove-Item -Path $dbSchemaPath -Force }

Write-Host "Exporting PostgreSQL data dump to $dbDumpPath"
& $pgDumpPath -h $DbHost -p $DbPort -U $DbUser -d $DbName -Fc -f $dbDumpPath --no-password
Assert-LastCommandSucceeded -Description "PostgreSQL data dump"

if (-not (Test-Path $dbDumpPath) -or ((Get-Item $dbDumpPath).Length -le 0)) {
    throw "PostgreSQL data dump was created as empty or missing: $dbDumpPath"
}

Write-Host "Exporting PostgreSQL schema to $dbSchemaPath"
& $pgDumpPath -h $DbHost -p $DbPort -U $DbUser -d $DbName --schema-only -f $dbSchemaPath --no-password
Assert-LastCommandSucceeded -Description "PostgreSQL schema dump"

if (-not (Test-Path $dbSchemaPath) -or ((Get-Item $dbSchemaPath).Length -le 0)) {
    throw "PostgreSQL schema dump was created as empty or missing: $dbSchemaPath"
}

$snapshotInfo = $null
try {
    Write-Host "Requesting Qdrant snapshot for collection '$QdrantCollection'"
    $snapshotInfo = Invoke-RestMethod -Method Post -Uri "$QdrantUrl/collections/$QdrantCollection/snapshots"
} catch {
    Write-Warning "Qdrant snapshot request failed: $($_.Exception.Message)"
}

$qdrantSnapshotName = $null
$qdrantSnapshotPath = $null
if ($snapshotInfo -and $snapshotInfo.result -and $snapshotInfo.result.name) {
    $qdrantSnapshotName = [string]$snapshotInfo.result.name
    $qdrantSnapshotPath = Join-Path $bundleDir $qdrantSnapshotName
    $snapshotDownloadUrl = "$QdrantUrl/collections/$QdrantCollection/snapshots/$qdrantSnapshotName"
    Write-Host "Downloading Qdrant snapshot to $qdrantSnapshotPath"
    Invoke-WebRequest -Uri $snapshotDownloadUrl -OutFile $qdrantSnapshotPath
} else {
    Write-Warning "Qdrant snapshot metadata not available; skipping snapshot download."
}

$manifest = @{
    generated_at = (Get-Date).ToString("o")
    postgres = @{
        db_name = $DbName
        db_host = $DbHost
        db_port = $DbPort
        db_user = $DbUser
        data_dump = (Split-Path -Leaf $dbDumpPath)
        schema_dump = (Split-Path -Leaf $dbSchemaPath)
    }
    qdrant = @{
        url = $QdrantUrl
        collection = $QdrantCollection
        snapshot = $qdrantSnapshotName
        snapshot_file = if ($qdrantSnapshotPath) { Split-Path -Leaf $qdrantSnapshotPath } else { $null }
    }
    restore_hints = @(
        "Create empty PostgreSQL DB, then run pg_restore -d <db> idms_demo_data.dump",
        "If using Qdrant snapshot, upload snapshot via Qdrant restore endpoint or copy into Qdrant snapshot dir"
    )
}

$manifestPath = Join-Path $bundleDir "handoff_manifest.json"
$manifest | ConvertTo-Json -Depth 8 | Set-Content -Path $manifestPath -Encoding UTF8

Write-Host "Handoff bundle created: $bundleDir"
Write-Host "You can now zip this folder and share it out-of-band with your offsite developer."
