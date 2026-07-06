#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

import domain_db
import object_db


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List SOLF schema attribute proposals")
    parser.add_argument("--status", default="proposed", help="Filter by proposal status")
    parser.add_argument("--class-name", default="", help="Optional class name filter")
    parser.add_argument(
        "--priority-bucket",
        default="high",
        choices=["", "low", "medium", "high"],
        help="Priority bucket filter",
    )
    parser.add_argument(
        "--suggested-only",
        action="store_true",
        help="Show only proposals auto-suggested for approval",
    )
    parser.add_argument("--limit", type=int, default=50, help="Maximum proposals to return")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON instead of a compact table-like summary",
    )
    return parser


def main() -> int:
    parser = build_cli()
    args = parser.parse_args()

    connection = object_db.get_connection()
    try:
        object_db.create_tables(connection, recreate=False)
        domain_db.create_tables(connection, recreate=False)
        proposals = domain_db.list_solf_attribute_proposals(
            connection,
            status=args.status or None,
            class_name=args.class_name or None,
            priority_bucket=args.priority_bucket or None,
            suggested_only=bool(args.suggested_only),
            limit=max(1, int(args.limit)),
        )
    finally:
        connection.close()

    if args.json:
        print(json.dumps(proposals, indent=2, ensure_ascii=False))
        return 0

    if not proposals:
        print("No schema proposals found.")
        return 0

    for item in proposals:
        print(
            "#{proposal_id} {class_name}.{attribute_name} "
            "status={status} priority={priority_bucket}:{priority_score} suggested={suggested_for_approval} reason={suggestion_reason} occurrences={occurrence_count} sample={sample_value}".format(
                proposal_id=item.get("proposal_id"),
                class_name=item.get("class_name"),
                attribute_name=item.get("attribute_name"),
                status=item.get("status"),
                priority_bucket=item.get("priority_bucket"),
                priority_score=item.get("priority_score"),
                suggested_for_approval=item.get("suggested_for_approval"),
                suggestion_reason=item.get("suggestion_reason") or "-",
                occurrence_count=item.get("occurrence_count"),
                sample_value=str(item.get("sample_value") or "")[:120],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())