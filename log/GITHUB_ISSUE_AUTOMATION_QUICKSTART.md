# GitHub Issue Automation Quickstart

This guide shows how to use the workflow capability endpoints to generate and create GitHub issues.

## 1) Start the API

From `C:\Project\WebTech\python\IDMS-Demo`:

```powershell
c:/Project/WebTech/python/.venv/Scripts/python.exe -m uvicorn idms_api_server.application:app --host 127.0.0.1 --port 8000
```

## 2) Optional repository defaults

```powershell
$env:IDMS_GITHUB_REPOSITORY = "my-org/idms-demo"
# or set separately:
# $env:IDMS_GITHUB_OWNER = "my-org"
# $env:IDMS_GITHUB_REPO = "idms-demo"
```

## 3) Dry-run first (safe)

```powershell
$payload = @{
  workflow_name = "Adhoc Unsupported Workflow"
  workflow_key = "adhoc_unsupported_workflow"
  requested_by = "web:user"
  github_repository = "my-org/idms-demo"
  labels = @("idms", "workflow", "needs-dev")
  include_domain_label = $true
  dry_run = $true
  proposed_steps = @(
    @{
      step_key = "step1"
      kind = "python_binding"
      config = @{ function = "function_that_does_not_exist" }
    }
  )
}

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/business-rules/workflow-registry/validate-capability/issue-create" -ContentType "application/json" -Body ($payload | ConvertTo-Json -Depth 20)
```

Expected: `success=true`, `dry_run=true`, and a populated `result.create_issue_payload`.

## 4) Live create (requires token)

Set token in terminal session:

```powershell
$env:IDMS_GITHUB_TOKEN = Read-Host "Enter GitHub token"
```

Then run with `dry_run = $false`:

```powershell
$payload.dry_run = $false
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/business-rules/workflow-registry/validate-capability/issue-create" -ContentType "application/json" -Body ($payload | ConvertTo-Json -Depth 20)
```

Expected on success: returned issue metadata in `result.issue` including `number` and `html_url`.

## 5) Related endpoints

- Markdown template only: `POST /api/business-rules/workflow-registry/validate-capability/issue-markdown`
- JSON payload only: `POST /api/business-rules/workflow-registry/validate-capability/issue-json`
- Create issue (dry-run or live): `POST /api/business-rules/workflow-registry/validate-capability/issue-create`

## 6) One-command PowerShell script

You can use the helper script at [scripts/invoke_workflow_issue_create.ps1](scripts/invoke_workflow_issue_create.ps1).

Dry-run:

```powershell
.\scripts\invoke_workflow_issue_create.ps1
```

Live create (prompts for token if needed):

```powershell
.\scripts\invoke_workflow_issue_create.ps1 -Live -PromptForToken
```

Target a specific repo/workflow:

```powershell
.\scripts\invoke_workflow_issue_create.ps1 -GithubRepository "my-org/idms-demo" -WorkflowName "Bank Reconciliation Workflow" -WorkflowKey "bank_recon_v2" -IncludeDomainLabel
```

## Notes

- Keep dry-run enabled until labels/repo/title/body look correct.
- For GitHub Enterprise, set `github_api_base_url` in request body or `IDMS_GITHUB_API_BASE_URL` in env.
- If token is missing and `dry_run=false`, API returns HTTP 400 by design.
