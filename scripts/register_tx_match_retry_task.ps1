param(
    [string]$TaskName = "IDMS-TxMatch-Retry-Queue",
    [int]$IntervalMinutes = 15,
    [string]$PythonExe = "c:/Project/WebTech/python/.venv/Scripts/python.exe"
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunnerScript = Join-Path $ScriptDir "run_tx_match_retry_worker.ps1"

if (-not (Test-Path $RunnerScript)) {
    throw "Runner script not found: $RunnerScript"
}

$pwsh = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path $pwsh)) {
    throw "PowerShell executable not found: $pwsh"
}

$runnerArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerScript`" -PythonExe `"$PythonExe`" -Limit 100"
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $runnerArgs

$triggerAt = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $triggerAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Retry IDMS tx_match_index_pending queue" -Force | Out-Null
Write-Host "Scheduled task registered: $TaskName (every $IntervalMinutes minutes)"
