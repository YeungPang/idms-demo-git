from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import object_db


def _normalized_hash(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _pick_canonical(paths: list[Path]) -> Path:
    # Prefer canonical content-hash file name when present; else earliest lexical file.
    for p in paths:
        suffix = p.stem.split("-")[-1] if "-" in p.stem else ""
        if suffix and len(suffix) == 16 and all(ch in "0123456789abcdef" for ch in suffix):
            return sorted(paths, key=lambda x: x.name)[0] if len(paths) == 1 else sorted(paths, key=lambda x: (x.name != p.name, x.name))[0]
    return sorted(paths, key=lambda p: p.name)[0]


def _load_groups(markdown_dir: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for path in sorted(markdown_dir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        groups.setdefault(_normalized_hash(content), []).append(path)
    return {h: paths for h, paths in groups.items() if len(paths) > 1}


def _update_document_metadata_paths(conn, old_to_new: dict[str, str]) -> int:
    updated = 0
    if not old_to_new:
        return updated

    with conn.cursor() as cur:
        cur.execute("SELECT doc_id, metadata FROM document")
        rows = cur.fetchall()

    for row in rows:
        doc_id = int(row[0])
        metadata = row[1] if isinstance(row[1], dict) else {}
        user_metadata = metadata.get("user_metadata") if isinstance(metadata.get("user_metadata"), dict) else {}
        source_ref = user_metadata.get("source_reference") if isinstance(user_metadata.get("source_reference"), dict) else {}

        current = str(source_ref.get("markdown_cache_path") or "")
        if current and current in old_to_new:
            source_ref = dict(source_ref)
            source_ref["markdown_cache_path"] = old_to_new[current]
            user_metadata = dict(user_metadata)
            user_metadata["source_reference"] = source_ref
            metadata = dict(metadata)
            metadata["user_metadata"] = user_metadata

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE document SET metadata = %s::jsonb WHERE doc_id = %s",
                    (json.dumps(metadata), doc_id),
                )
            updated += 1

    return updated


def run_cleanup(apply_changes: bool) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent
    markdown_dir = repo_root / "generated" / "markdown"

    summary: dict[str, Any] = {
        "duplicate_groups": 0,
        "files_removed": 0,
        "metadata_paths_updated": 0,
        "plans": [],
    }

    groups = _load_groups(markdown_dir)
    summary["duplicate_groups"] = len(groups)

    old_to_new_rel: dict[str, str] = {}

    for content_hash, paths in sorted(groups.items()):
        canonical = _pick_canonical(paths)
        duplicates = [p for p in paths if p != canonical]
        plan = {
            "hash": content_hash,
            "keep": str(canonical.relative_to(repo_root)).replace("\\", "/"),
            "remove": [str(p.relative_to(repo_root)).replace("\\", "/") for p in duplicates],
        }
        summary["plans"].append(plan)

        for dup in duplicates:
            old_rel = str(dup.relative_to(repo_root)).replace("\\", "/")
            new_rel = str(canonical.relative_to(repo_root)).replace("\\", "/")
            old_to_new_rel[old_rel] = new_rel

        if apply_changes:
            for dup in duplicates:
                dup.unlink(missing_ok=True)
                summary["files_removed"] += 1

    conn = object_db.get_connection()
    try:
        updated = _update_document_metadata_paths(conn, old_to_new_rel)
        summary["metadata_paths_updated"] = updated
        if apply_changes:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup duplicate markdown cache files by normalized content.")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup (default is dry-run)")
    args = parser.parse_args()

    result = run_cleanup(apply_changes=args.apply)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
