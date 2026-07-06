param(
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [string]$WorkflowName = "Adhoc Unsupported Workflow",
    [string]$WorkflowKey = "adhoc_unsupported_workflow",
    [string]$RequestedBy = "web:user",
    [string]$GithubRepository = "my-org/idms-demo",
    [string[]]$Labels = @("idms", "workflow", "needs-dev"),
    [switch]$Live,
    [switch]$PromptForToken,
    [switch]$IncludeDomainLabel,
    [string]$StepFunction = "function_that_does_not_exist"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$endpoint = "$($ApiBaseUrl.TrimEnd('/'))/api/business-rules/workflow-registry/validate-capability/issue-create"
$dryRun = -not $Live.IsPresent

if ($Live.IsPresent) {
    if ($PromptForToken.IsPresent -or [string]::IsNullOrWhiteSpace($env:IDMS_GITHUB_TOKEN)) {
        $env:IDMS_GITHUB_TOKEN = Read-Host "Enter GitHub token"
    }
    if ([string]::IsNullOrWhiteSpace($env:IDMS_GITHUB_TOKEN)) {
        throw "GitHub token is required for live mode. Set IDMS_GITHUB_TOKEN or use -PromptForToken."
    }
}

$payload = @{
    workflow_name = $WorkflowName
    workflow_key = $WorkflowKey
    requested_by = $RequestedBy
    github_repository = $GithubRepository
    labels = $Labels
    include_domain_label = $IncludeDomainLabel.IsPresent
    dry_run = $dryRun
    proposed_steps = @(
        @{
            step_key = "step1"
            kind = "python_binding"
            config = @{ function = $StepFunction }
        }
    )
}

Write-Host "Calling endpoint: $endpoint"
Write-Host "Mode: $(if ($dryRun) { 'DRY-RUN' } else { 'LIVE' })"

try {
    $response = Invoke-RestMethod -Method Post -Uri $endpoint -ContentType "application/json" -Body ($payload | ConvertTo-Json -Depth 20)
    $json = $response | ConvertTo-Json -Depth 20
    Write-Host "Request succeeded."
    Write-Output $json
}
catch {
    $errorMessage = $_.Exception.Message
    if ($_.Exception.Response -and $_.Exception.Response.GetResponseStream()) {
        $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        $body = $reader.ReadToEnd()
        if (-not [string]::IsNullOrWhiteSpace($body)) {
            $errorMessage = "$errorMessage`nResponse Body:`n$body"
        }
    }
    throw $errorMessage
}
