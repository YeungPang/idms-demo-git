#!/usr/bin/env python3
"""Discovery Engine backfill is disabled in IDMS-Demo."""

from __future__ import annotations

import sys


def main() -> int:
    print("Discovery Engine integration is disabled in IDMS-Demo.")
    print("Use Qdrant + OpenRouter ingestion paths instead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
            )
            summary["indexed"] += 1
            operation = str(result.get("operation") or "").strip().lower()
            if operation == "create":
                summary["created"] += 1
            elif operation == "update":
                summary["updated"] += 1
            print(f"[OK] doc_id={doc_id} discovery_id={discovery_doc_id} operation={operation or 'unknown'}")
        except Exception as exc:
            summary["errors"] += 1
            print(f"[ERR] doc_id={doc_id} discovery_id={discovery_doc_id} error={exc}")

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill Discovery index from document table")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of active documents to process")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be indexed without writing")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    limit = args.limit if args.limit and args.limit > 0 else None
    summary = run_backfill(limit=limit, dry_run=bool(args.dry_run))
    print("\nSUMMARY")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
