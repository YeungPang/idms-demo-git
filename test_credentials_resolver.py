"""Test credentials path resolver with .env loaded."""

import os
from pathlib import Path

# Load .env first
from dotenv import load_dotenv
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Now test
from credentials_path_resolver import get_credentials_path_info, resolve_google_credentials_path
import json

print("=" * 80)
print("Testing Credentials Path Resolution")
print("=" * 80)

info = get_credentials_path_info()
print(json.dumps(info, indent=2))

print("\n" + "=" * 80)
print("Resolution Summary:")
print("=" * 80)

if info['file_exists']:
    print("[OK] SUCCESS - Credentials file found")
    print(f"  Source: {info['priority_source']}")
    print(f"  Path: {info['resolved_path']}")
else:
    print("[ERR] ERROR - No valid credentials path found")
    if info['relative_path_configured']:
        print(f"  Relative path: {info['relative_path_value']} (not found)")
    if info['full_path_configured']:
        print(f"  Full path: {info['full_path_value']} (not found)")

# Try to set it
print("\n" + "=" * 80)
print("Attempting to setup GOOGLE_APPLICATION_CREDENTIALS:")
print("=" * 80)

try:
    path = resolve_google_credentials_path()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
    print(f"[OK] Set to: {path}")
except Exception as e:
    print(f"[ERR] Error: {e}")


def test_credentials_path_info_shape() -> None:
    """Smoke-test that resolver returns the expected diagnostic structure."""
    details = get_credentials_path_info()
    assert isinstance(details, dict)
    assert "priority_source" in details
    assert "file_exists" in details
