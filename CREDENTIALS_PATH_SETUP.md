# GOOGLE_APPLICATION_CREDENTIALS Path Resolution

## Overview

The credentials path resolver supports both **relative** and **full** paths for `GOOGLE_APPLICATION_CREDENTIALS`, enabling flexible deployment across different environments (local development, Docker, cloud).

## Configuration

### .env File (Two Options)

Choose **one or both** (full path takes priority):

```bash
# Option 1: Relative path (relative to IDMS-Proto project root)
# Good for: Development, Docker containers, packaged distributions
GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH=secr/idms-prot-key.json

# Option 2: Full/absolute path
# Good for: Production servers, VMs, when credentials are in fixed locations
GOOGLE_APPLICATION_CREDENTIALS_FULL_PATH=C:/Project/WebTech/python/IDMS-Proto/secr/idms-prot-key.json

# Priority: If both are set, FULL_PATH is used
```

## Usage

### Method 1: Automatic Setup (Recommended)

In your application startup code:

```python
from credentials_path_resolver import setup_google_credentials

# Call once at startup
credentials_path = setup_google_credentials()
print(f"Credentials set to: {credentials_path}")
```

This automatically:
1. Loads credentials path from .env
2. Resolves relative paths to full paths
3. Sets `os.environ["GOOGLE_APPLICATION_CREDENTIALS"]`

### Method 2: Get Path Without Setting

If you need the path without modifying environment:

```python
from credentials_path_resolver import resolve_google_credentials_path

path = resolve_google_credentials_path()
# Use path as needed
```

### Method 3: Get Configuration Info

Useful for debugging:

```python
from credentials_path_resolver import get_credentials_path_info
import json

info = get_credentials_path_info()
print(json.dumps(info, indent=2))

# Output includes:
# - Which paths are configured
# - Which source is being used
# - Resolved path
# - Whether file exists
```

## Integration Points

### FastAPI Application

```python
# In idms_api_server/application.py

from fastapi import FastAPI
from credentials_path_resolver import setup_google_credentials

app = FastAPI(...)

@app.on_event("startup")
async def startup_event():
    """Setup credentials on API startup."""
    try:
        creds_path = setup_google_credentials()
        print(f"Google credentials configured: {creds_path}")
    except Exception as e:
        print(f"Warning: Could not setup credentials: {e}")
```

### Existing Code That Loads .env

If you already load .env elsewhere:

```python
# In your existing initialization code
from dotenv import load_dotenv
from credentials_path_resolver import setup_google_credentials

load_dotenv()
setup_google_credentials()  # Call after load_dotenv()
```

### LLM/GenAI Client Initialization

```python
from credentials_path_resolver import setup_google_credentials
from google.cloud import aiplatform

# Setup credentials first
setup_google_credentials()

# Then initialize clients that need it
aiplatform.init(project="your-project")
```

## Deployment Scenarios

### Scenario 1: Local Development

**.env file:**
```bash
GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH=secr/idms-prot-key.json
```

**Behavior:**
- Credentials file at: `./IDMS-Proto/secr/idms-prot-key.json`
- Works regardless of working directory

### Scenario 2: Docker Container

**.env file (inside container):**
```bash
GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH=secr/idms-prot-key.json
```

**Dockerfile:**
```dockerfile
FROM python:3.13

WORKDIR /app
COPY IDMS-Proto /app/IDMS-Proto
COPY .env /app/IDMS-Proto/

# Credentials file copied to image
COPY secr/idms-prot-key.json /app/IDMS-Proto/secr/

CMD ["uvicorn", "idms_api:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Scenario 3: Cloud Deployment (e.g., GKE)

**.env file:**
```bash
GOOGLE_APPLICATION_CREDENTIALS_FULL_PATH=/var/secrets/google/key.json
```

**Kubernetes manifest:**
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: idms-api
spec:
  containers:
  - name: idms-api
    image: idms-api:latest
    env:
    - name: GOOGLE_APPLICATION_CREDENTIALS_FULL_PATH
      value: /var/secrets/google/key.json
    volumeMounts:
    - name: google-cloud-key
      mountPath: /var/secrets/google
  volumes:
  - name: google-cloud-key
    secret:
      secretName: google-application-credentials
```

### Scenario 4: Packaged Distribution

**pyproject.toml (if using Poetry/setuptools):**
```toml
[tool.poetry]
packages = [
    { include = "IDMS-Proto" }
]
```

**.env (packaged with code):**
```bash
# Use relative path for portability
GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH=secr/idms-prot-key.json
```

Installation:
```bash
pip install idms-proto-0.1.0.whl
python -m idms_proto.api  # Works from anywhere
```

## Error Handling

### Error: File not found (relative path)

**Symptom:**
```
FileNotFoundError: GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH resolves to 
non-existent file: secr/idms-prot-key.json
```

**Solution:**
- Verify file exists at: `PROJECT_ROOT/secr/idms-prot-key.json`
- Check `project_root` parameter is correct
- Confirm working directory or set it explicitly

### Error: File not found (full path)

**Symptom:**
```
FileNotFoundError: GOOGLE_APPLICATION_CREDENTIALS_FULL_PATH points to 
non-existent file: C:/path/to/key.json
```

**Solution:**
- Verify absolute path is correct
- Check file permissions (readable)
- Use correct path separators for OS

### Error: No credentials configured

**Symptom:**
```
ValueError: No GOOGLE_APPLICATION_CREDENTIALS configured
```

**Solution:**
- Add one of these to .env:
  - `GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH=secr/idms-prot-key.json`
  - `GOOGLE_APPLICATION_CREDENTIALS_FULL_PATH=/path/to/key.json`

## Testing

### Standalone Test

```bash
python test_credentials_resolver.py
```

Output shows:
- Which paths are configured
- Which source is used
- Whether file was found
- The resolved path

### Integration Test

```python
import os
from dotenv import load_dotenv
from credentials_path_resolver import resolve_google_credentials_path

# Load .env
load_dotenv()

# Test resolution
try:
    path = resolve_google_credentials_path()
    print(f"✓ Credentials resolved to: {path}")
    assert os.path.exists(path), f"File not found: {path}"
    print("✓ File exists")
except Exception as e:
    print(f"✗ Error: {e}")
    raise
```

## Migration from Old Setup

If you previously had:

```bash
# Old .env
GOOGLE_APPLICATION_CREDENTIALS=C:/Project/WebTech/python/IDMS-Proto/secr/idms-prot-key.json
```

To migrate:

1. **Remove old entry** from .env
2. **Add new entries** (choose one or both):
   ```bash
   GOOGLE_APPLICATION_CREDENTIALS_FULL_PATH=C:/Project/WebTech/python/IDMS-Proto/secr/idms-prot-key.json
   GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH=secr/idms-prot-key.json
   ```
3. **Add setup call** to your application startup:
   ```python
   from credentials_path_resolver import setup_google_credentials
   setup_google_credentials()
   ```

## Reference

### Function Signatures

```python
def resolve_google_credentials_path(
    project_root: Optional[str] = None
) -> str:
    """Resolve path, return full path string."""
    pass

def setup_google_credentials(
    project_root: Optional[str] = None
) -> str:
    """Resolve and set os.environ, return path string."""
    pass

def get_credentials_path_info() -> dict:
    """Get configuration info for debugging."""
    pass
```

### Return Value: get_credentials_path_info()

```python
{
    "full_path_configured": bool,          # Is FULL_PATH env var set?
    "full_path_value": str,                # Value of FULL_PATH
    "relative_path_configured": bool,      # Is RELATIVE_PATH env var set?
    "relative_path_value": str,            # Value of RELATIVE_PATH
    "priority_source": str,                # "full_path", "relative_path", or "error"
    "resolved_path": Optional[str],        # Final resolved path or None
    "file_exists": bool,                   # Does file exist at resolved path?
    "current_env": str,                    # Current GOOGLE_APPLICATION_CREDENTIALS env
}
```

## Best Practices

1. **Development:** Use `GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH`
   - Portable across machines
   - Works with any working directory

2. **Production:** Use `GOOGLE_APPLICATION_CREDENTIALS_FULL_PATH`
   - Explicit, no ambiguity
   - Easier to troubleshoot

3. **Docker:** Use `GOOGLE_APPLICATION_CREDENTIALS_RELATIVE_PATH`
   - Mount credentials to consistent location in container
   - Scale easily across instances

4. **CI/CD:** Use `GOOGLE_APPLICATION_CREDENTIALS_FULL_PATH`
   - Inject via environment at runtime
   - Secret management via CI/CD platform

5. **Debugging:** Call `get_credentials_path_info()` in logs
   - Helps diagnose path issues
   - Verify which source is active
