# IDMS-Proto

[![Phase 2 Regressions](https://github.com/<OWNER>/<REPO>/actions/workflows/phase2-regressions.yml/badge.svg)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase2-regressions.yml)
[![Phase 2 Regressions (main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase2-regressions.yml/badge.svg?branch=main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase2-regressions.yml?query=branch%3Amain)
[![Phase 3 Regressions](https://github.com/<OWNER>/<REPO>/actions/workflows/phase3-regressions.yml/badge.svg)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase3-regressions.yml)
[![Phase 3 Regressions (main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase3-regressions.yml/badge.svg?branch=main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase3-regressions.yml?query=branch%3Amain)
[![Phase 4 Regressions](https://github.com/<OWNER>/<REPO>/actions/workflows/phase4-regressions.yml/badge.svg)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase4-regressions.yml)
[![Phase 4 Regressions (main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase4-regressions.yml/badge.svg?branch=main)](https://github.com/<OWNER>/<REPO>/actions/workflows/phase4-regressions.yml?query=branch%3Amain)

Replace `<OWNER>/<REPO>` with your GitHub repository path to activate the badge.

## GitHub Publish and Offsite Handoff

For secure repository publishing (without committing `.env` secrets) and full PostgreSQL/Qdrant handoff instructions, see [GITHUB_PUBLISH_AND_HANDOFF.md](GITHUB_PUBLISH_AND_HANDOFF.md).

## Workflow Regressions

Run the full Phase 2 regression suite locally:

```bash
python run_phase2_regressions.py
```

Run the full Phase 3 regression suite locally:

```bash
python run_phase3_regressions.py
```

Run the full Phase 4 regression suite locally:

```bash
python run_phase4_regressions.py
```

## Logging and Ingestion Artifacts

Runtime logs are written to `log/log-YYYY-MM-DD.log`.

Ingestion writes reusable markdown artifacts under `generated/markdown/` and reuses them on later runs when possible.

Useful ingest flags:

```bash
python ingest.py <source> --no-reuse-markdown
python ingest.py <source> --no-persist-markdown
```

## Workflow Monitoring CLI

The monitor command provides operational workflow KPIs and health checks.

Get current metrics over a sliding window:

```bash
python interaction.py monitor stats --window-hours 24
```

Expected output shape:

```json
{
	"result": "ok",
	"window_hours": 24,
	"metrics": {
		"event_count": 0,
		"state_transition_count": 0,
		"overdue_workflow_cases": 0,
		"overdue_compliance_actions": 0,
		"escalated_compliance_actions": 0
	}
}
```

Check health status for the same window:

```bash
python interaction.py monitor health --window-hours 24
```

Expected output shape:

```json
{
	"result": "ok",
	"window_hours": 24,
	"health": {
		"status": "healthy|warning|critical",
		"signals": {
			"overdue_workflow_cases": 0,
			"overdue_compliance_actions": 0,
			"escalated_compliance_actions": 0
		}
	}
}
```

Persist a metric snapshot:

```bash
python interaction.py monitor snapshot --window-hours 24 --label daily
```

Read recent snapshot history:

```bash
python interaction.py monitor history --limit 10
```

## Reliability Alerts and Incidents (Phase 4)

Evaluate alert policy thresholds against current metrics:

```bash
python interaction.py monitor alerts --window-hours 24
```

Reconcile incidents from current alert state (open/update/auto-resolve):

```bash
python interaction.py monitor reconcile --window-hours 24 --actor-ref system
```

List incidents by status:

```bash
python interaction.py monitor incidents --status open --limit 20
```

Acknowledge or resolve an incident:

```bash
python interaction.py monitor incident --incident-id 12 --incident-action acknowledge --actor-ref analyst
python interaction.py monitor incident --incident-id 12 --incident-action resolve --actor-ref analyst
```

## What-If Simulation CLI (Interaction Type 6)

Create a scenario from a base project:

```bash
python interaction.py monitor create-scenario \
	--base-project-id 101 \
	--scenario-name "Markus unavailable next week" \
	--scenario-type what_if \
	--modifications-json '{"resource_count":{"original":4,"modified":3,"reason":"one dev unavailable"}}'
```

Run schedule simulation:

```bash
python interaction.py monitor simulate --scenario-id 12 --include-resource-check
```

Calculate budget impact:

```bash
python interaction.py monitor budget-impact --scenario-id 12
```

Compare two scenarios:

```bash
python interaction.py monitor compare-scenarios --scenario1-id 12 --scenario2-id 13
```

List project scenarios and get details:

```bash
python interaction.py monitor scenarios --project-id 101 --status draft
python interaction.py monitor scenario-details --scenario-id 12
```

## Action CLI Extensions (Completed Partial Types)

Ingest a document through the SOLF-guarded action pipeline:

```bash
python interaction.py action ingest_document --payload-json "{'source':'docs/spec.txt','description':'spec ingest','tags':['ops']}"
```

Ingest raw information text through the same action pipeline:

```bash
python interaction.py action ingest_information --payload-json "{'information_text':'Budget reduced by 5%','description':'meeting note'}"
```

Generate a structured document from context:

```bash
python interaction.py action generate_document --payload-json "{'document_type':'status_report','subject':'Weekly Ops','context':{'owner':'ops'}}"
```

Update by natural key (without numeric entity_id):

```bash
python interaction.py action update_entity --payload-json "{'entity_type':'company','natural_key_field':'name','natural_key_value':'Acme','field':'vat_no','new_value':'CHE-123'}"
```

## Unified API and Web Test App

The repository now includes a FastAPI service for IDMS ingestion, chat, interactions/actions, and grounded queries.

Start the API server:

```bash
uvicorn idms_api:app --host 0.0.0.0 --port 8000 --reload
```

Open the test web app:

```text
http://localhost:8000/
```

Main API endpoints:

1. `POST /api/ingest/upload`
2. `POST /api/ingest/path`
3. `POST /api/chat`
4. `POST /api/query`
5. `POST /api/action`
6. `GET /api/health`

Upload ingestion accepts a local file plus note/instructions, tags, metadata JSON, and optional client file path.

Path ingestion accepts a source path or `gs://` URI, optional file name, note, tags, and metadata.

API module layout (for long-term maintenance):

1. `idms_api.py`: thin compatibility entrypoint exporting `app`
2. `idms_api_server/application.py`: FastAPI app assembly
3. `idms_api_server/routers/system.py`: UI + health endpoints
4. `idms_api_server/routers/ingestion.py`: file/path ingestion endpoints
5. `idms_api_server/routers/interaction.py`: chat/query/action endpoints
6. `idms_api_server/schemas.py`: request models
7. `idms_api_server/deps.py`: shared dependencies and helpers