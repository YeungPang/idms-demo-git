param(
    [string[]]$Statuses = @("pending", "failed"),
    [int]$Limit = 50,
    [string]$PythonExe = "c:/Project/WebTech/python/.venv/Scripts/python.exe"
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$WorkerScript = Join-Path $RepoRoot "tx_match_retry_worker.py"

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}
if (-not (Test-Path $WorkerScript)) {
    throw "Worker script not found: $WorkerScript"
}

$statusArgs = @()
foreach ($s in $Statuses) {
    if ($s -and $s.Trim()) {
        $statusArgs += "--status"
        $statusArgs += $s.Trim()
    }
}

$cmdArgs = @($WorkerScript) + $statusArgs + @("--limit", "$Limit")
& $PythonExe @cmdArgs
exit $LASTEXITCODE
