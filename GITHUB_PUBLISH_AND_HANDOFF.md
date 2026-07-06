# Publish IDMS-Demo to GitHub Safely (With Data Handoff)

This guide prepares IDMS-Demo for an offsite developer while keeping API keys out of GitHub.

## 1) Security First

1. Keep real secrets only in local `.env`.
2. Commit `.env.example` only.
3. Ensure `.env` is ignored by git (`.gitignore` already includes this).
4. If `.env` was ever committed in any prior repo/history, rotate those keys before sharing.

## 2) Initialize and Publish Git Repository

From the IDMS-Demo directory:

```powershell
cd C:/Project/WebTech/python/IDMS-Demo
git init
git add .
git status
git commit -m "Initial publish: IDMS-Demo with safe env template and handoff tooling"
git branch -M main
git remote add origin https://github.com/<your-org-or-user>/<your-repo>.git
git push -u origin main
```

If you accidentally staged `.env`, remove it before commit:

```powershell
git restore --staged .env
```

If `.env` was already committed, remove and recommit:

```powershell
git rm --cached .env
git commit -m "Remove .env from repository tracking"
```

## 3) Export Your Working Data for Offsite Developer

Run the export helper script:

```powershell
cd C:/Project/WebTech/python/IDMS-Demo
./scripts/export_handoff_bundle.ps1 -DbName idms_demo -DbUser postgres -DbHost localhost -DbPort 5432 -QdrantUrl http://localhost:6333 -QdrantCollection idms_demo_documents
```

This creates a timestamped folder under `handoff/` containing:

1. PostgreSQL data dump (`.dump`)
2. PostgreSQL schema export (`.sql`)
3. Qdrant collection snapshot (if available)
4. `handoff_manifest.json`

Zip this folder and share it through a private channel (not public GitHub).

## 4) Offsite Developer Setup Steps

### 4.1 Clone and Python Environment

```powershell
git clone https://github.com/<your-org-or-user>/<your-repo>.git
cd <your-repo>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 4.2 Configure Environment Variables

```powershell
copy .env.example .env
```

Then fill `.env` values (OpenRouter key, DB password, etc.).

### 4.3 Start Qdrant Runtime

```powershell
docker compose -f docker-compose.qdrant.yml up -d
```

Verify Qdrant:

```powershell
Invoke-RestMethod -Method Get -Uri http://localhost:6333/collections
```

### 4.4 Restore PostgreSQL Data

Create target DB, then restore dump:

```powershell
createdb -h localhost -p 5432 -U postgres idms_demo
pg_restore -h localhost -p 5432 -U postgres -d idms_demo --clean --if-exists --no-owner --no-privileges <path-to>/idms_demo_data.dump
```

### 4.5 Restore Qdrant Data (Snapshot)

If snapshot file exists in handoff bundle, import it using Qdrant snapshot restore procedure for your version.

## 5) Run the API

```powershell
uvicorn idms_api:app --host 0.0.0.0 --port 8000 --reload
```

Test app URL:

```text
http://localhost:8000/
```

## 6) Recommended Collaboration Model

1. Keep code/config in GitHub.
2. Keep production secrets in `.env` only (or secret manager).
3. Share DB/Qdrant snapshots as private artifacts per handoff.
4. For ongoing sync, create periodic sanitized handoff bundles rather than committing dumps to git.
