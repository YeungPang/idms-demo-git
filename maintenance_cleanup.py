from __future__ import annotations

import argparse
import json
from typing import Any

from cleanup_markdown_cache import run_cleanup
from dedupe_documents import run_dedupe


def run_maintenance(apply_changes: bool) -> dict[str, Any]:
    # Order matters: merge duplicate documents first so references are canonical,
    # then prune duplicate markdown cache files.
    dedupe_result = run_dedupe(apply_changes=apply_changes)
    markdown_result = run_cleanup(apply_changes=apply_changes)

    return {
        "mode": "apply" if apply_changes else "dry-run",
        "document_dedupe": dedupe_result,
        "markdown_cache_cleanup": markdown_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run document dedupe and markdown cache cleanup in a single maintenance command."
    )
    parser.add_argument("--apply", action="store_true", help="Apply maintenance changes (default: dry-run)")
    args = parser.parse_args()

    result = run_maintenance(apply_changes=args.apply)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
