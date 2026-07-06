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

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bundleDir = Join-Path $OutputDir "idms_demo_handoff_$timestamp"
New-Item -ItemType Directory -Path $bundleDir -Force | Out-Null

if (-not (Test-CommandAvailable -CommandName "pg_dump")) {
    throw "pg_dump is not available in PATH. Install PostgreSQL client tools or add pg_dump to PATH."
}

$dbDumpPath = Join-Path $bundleDir "idms_demo_data.dump"
$dbSchemaPath = Join-Path $bundleDir "idms_demo_schema.sql"

Write-Host "Exporting PostgreSQL data dump to $dbDumpPath"
pg_dump -h $DbHost -p $DbPort -U $DbUser -d $DbName -Fc -f $dbDumpPath

Write-Host "Exporting PostgreSQL schema to $dbSchemaPath"
pg_dump -h $DbHost -p $DbPort -U $DbUser -d $DbName --schema-only -f $dbSchemaPath

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
