"""
dedupe_objects.py — Merge duplicate object_instance rows.

Selects a canonical ID per group, retargets all FKs
(object_relationship.src/tar, part.object_id, attribute.src_id),
moves attributes avoiding duplicates, then deletes the shells.

Usage:
  python dedupe_objects.py          # dry-run (prints plan, changes nothing)
  python dedupe_objects.py --apply  # executes the merge inside a single transaction
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from sql_db import get_connection
except ImportError:
    import psycopg2, os
    def get_connection():
        return psycopg2.connect(
            dbname=os.getenv("IDMS_DB_NAME", "idms_proto"),
            host=os.getenv("IDMS_DB_HOST", "localhost"),
            user=os.getenv("IDMS_DB_USER", "postgres"),
            password=os.getenv("IDMS_DB_PASSWORD", ""),
            port=int(os.getenv("IDMS_DB_PORT", "5432")),
        )


# ── Merge plan ───────────────────────────────────────────────────────────────
# Each entry: {label, canonical_id, canonical_class, canonical_name, duplicates}
# Attributes from all duplicates are merged into the canonical row.
# Relationships and part rows pointing at any duplicate are retargeted to canonical.
MERGE_PLAN: list[dict[str, Any]] = []


def load_merge_plan(plan_path: str | None = None) -> list[dict[str, Any]]:
    if not str(plan_path or "").strip():
        return list(MERGE_PLAN)

    path = Path(str(plan_path)).expanduser()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Merge plan file must contain a JSON array of plan objects.")

    plans: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        duplicates = item.get("duplicates") if isinstance(item.get("duplicates"), list) else []
        plans.append(
            {
                "label": str(item.get("label") or item.get("canonical_name") or item.get("canonical_id") or "merge").strip(),
                "canonical_id": int(item.get("canonical_id") or 0),
                "canonical_class": str(item.get("canonical_class") or "entity").strip(),
                "canonical_name": str(item.get("canonical_name") or "").strip(),
                "duplicates": [int(value) for value in duplicates if str(value).strip()],
            }
        )
    return plans


def _fetch_object(cur, object_id: int) -> dict[str, Any] | None:
    cur.execute(
        "SELECT object_id, object_name, class_name, metadata FROM object_instance WHERE object_id=%s",
        (object_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"object_id": row[0], "object_name": row[1], "class_name": row[2], "metadata": row[3]}


def _fetch_attrs(cur, object_id: int) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT attr_id, attr_type, attr_json FROM attribute WHERE src_type='object' AND src_id=%s",
        (object_id,),
    )
    return [{"attr_id": r[0], "attr_type": r[1], "attr_json": r[2]} for r in cur.fetchall()]


def _merge_attrs(cur, canonical_id: int, dup_id: int, dry_run: bool) -> int:
    """Move attributes from dup_id to canonical_id, skipping exact attr_type duplicates."""
    existing = {a["attr_type"] for a in _fetch_attrs(cur, canonical_id)}
    dup_attrs = _fetch_attrs(cur, dup_id)
    moved = 0
    for a in dup_attrs:
        if a["attr_type"] not in existing:
            print(f"    + move attr [{a['attr_type']}] from obj {dup_id} → {canonical_id}")
            if not dry_run:
                cur.execute(
                    "UPDATE attribute SET src_id=%s WHERE attr_id=%s",
                    (canonical_id, a["attr_id"]),
                )
            existing.add(a["attr_type"])
            moved += 1
        else:
            print(f"    - skip attr [{a['attr_type']}] from obj {dup_id} (already on canonical)")
    return moved


def _retarget_relationships(cur, dup_id: int, canonical_id: int, dry_run: bool) -> int:
    cur.execute(
        "SELECT relationship_id, src_object_id, tar_object_id FROM object_relationship "
        "WHERE src_object_id=%s OR tar_object_id=%s",
        (dup_id, dup_id),
    )
    rows = cur.fetchall()
    count = 0
    for rel_id, src_id, tar_id in rows:
        new_src = canonical_id if src_id == dup_id else src_id
        new_tar = canonical_id if tar_id == dup_id else tar_id
        # Skip if it would create a self-loop that already exists (canonical→canonical)
        if new_src == new_tar:
            print(f"    ! skip self-loop rel {rel_id} after retarget")
            continue
        # Skip if an identical relationship already exists
        cur.execute(
            "SELECT relationship_id FROM object_relationship "
            "WHERE src_object_id=%s AND tar_object_id=%s AND relationship_name=("
            "  SELECT relationship_name FROM object_relationship WHERE relationship_id=%s"
            ") AND relationship_id != %s LIMIT 1",
            (new_src, new_tar, rel_id, rel_id),
        )
        if cur.fetchone():
            print(f"    - skip dup rel {rel_id} (identical already exists on canonical)")
            if not dry_run:
                cur.execute("DELETE FROM object_relationship WHERE relationship_id=%s", (rel_id,))
            continue
        print(f"    → retarget rel {rel_id}: src {src_id}→{new_src} tar {tar_id}→{new_tar}")
        if not dry_run:
            cur.execute(
                "UPDATE object_relationship SET src_object_id=%s, tar_object_id=%s WHERE relationship_id=%s",
                (new_src, new_tar, rel_id),
            )
        count += 1
    return count


def _retarget_parts(cur, dup_id: int, canonical_id: int, dry_run: bool) -> int:
    cur.execute("SELECT part_id, doc_id FROM part WHERE object_id=%s", (dup_id,))
    rows = cur.fetchall()
    count = 0
    for part_id, doc_id in rows:
        # Avoid duplicating an existing (doc_id, canonical_id) part row
        cur.execute(
            "SELECT part_id FROM part WHERE doc_id=%s AND object_id=%s LIMIT 1",
            (doc_id, canonical_id),
        )
        if cur.fetchone():
            print(f"    - skip part {part_id} doc={doc_id} (already linked to canonical)")
            if not dry_run:
                cur.execute("DELETE FROM part WHERE part_id=%s", (part_id,))
        else:
            print(f"    → retarget part {part_id} doc={doc_id}: obj {dup_id}→{canonical_id}")
            if not dry_run:
                cur.execute("UPDATE part SET object_id=%s WHERE part_id=%s", (canonical_id, part_id))
            count += 1
    return count


def run(apply: bool, plan_path: str | None = None) -> None:
    dry_run = not apply
    tag = "DRY-RUN" if dry_run else "APPLYING"
    merge_plan = load_merge_plan(plan_path)
    print(f"\n{'='*60}")
    print(f"  Object deduplication — {tag}")
    print(f"{'='*60}\n")

    if not merge_plan:
        print("No merge plan configured. Provide --plan <merge-plan.json> to run deduplication.")
        return

    conn = get_connection()
    try:
        with conn:
            cur = conn.cursor()
            for plan in merge_plan:
                canonical_id = plan["canonical_id"]
                label = plan["label"]
                print(f"\n── {label} (canonical={canonical_id}) ──")

                # Update canonical class_name if it changed
                cur.execute("SELECT class_name FROM object_instance WHERE object_id=%s", (canonical_id,))
                row = cur.fetchone()
                if row and row[0] != plan["canonical_class"]:
                    print(f"  Update class_name: '{row[0]}' → '{plan['canonical_class']}'")
                    if not dry_run:
                        cur.execute(
                            "UPDATE object_instance SET class_name=%s WHERE object_id=%s",
                            (plan["canonical_class"], canonical_id),
                        )

                for dup_id in plan["duplicates"]:
                    dup = _fetch_object(cur, dup_id)
                    if not dup:
                        print(f"  [skip] dup id={dup_id} not found")
                        continue
                    print(f"\n  Merging id={dup_id} ({dup['class_name']} '{dup['object_name']}') into {canonical_id}:")

                    a = _merge_attrs(cur, canonical_id, dup_id, dry_run)
                    r = _retarget_relationships(cur, dup_id, canonical_id, dry_run)
                    p = _retarget_parts(cur, dup_id, canonical_id, dry_run)

                    print(f"  → attrs moved={a}  rels retargeted={r}  parts retargeted={p}")
                    print(f"  {'[DRY] Would delete' if dry_run else 'Deleting'} object_instance id={dup_id}")
                    if not dry_run:
                        cur.execute("DELETE FROM object_instance WHERE object_id=%s", (dup_id,))

            if not dry_run:
                conn.commit()
                print("\n✓ All merges committed.")
            else:
                conn.rollback()
                print("\n(No changes made — re-run with --apply to commit.)")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run)")
    parser.add_argument("--plan", help="Path to a JSON merge plan file")
    args = parser.parse_args()
    run(apply=args.apply, plan_path=args.plan)
