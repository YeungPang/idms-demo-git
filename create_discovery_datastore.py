#!/usr/bin/env python3
"""
Discovery datastore setup is disabled in IDMS-Demo.
"""
from __future__ import annotations

import sys


def main() -> int:
    print("Discovery Engine integration is disabled in IDMS-Demo.")
    print("Use Qdrant + OpenRouter ingestion paths instead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
