#!/usr/bin/env python3
"""Generic test for entity lookup behavior across name variants."""
import sys
import re
from collections import defaultdict

sys.path.insert(0, '.')
from query_engine import QueryEngine

def _active_person_names(query_engine: QueryEngine) -> list[str]:
    with query_engine._get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT object_name
                FROM object_instance
                WHERE LOWER(class_name) = 'person'
                  AND COALESCE(status, 'active') = 'active'
                  AND valid_from <= CURRENT_DATE
                  AND (valid_until IS NULL OR valid_until > CURRENT_DATE)
                ORDER BY object_name
                """
            )
            rows = cur.fetchall()
    return [str(row[0]).strip() for row in rows if str(row[0] or '').strip()]


def _extract_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z0-9äöüÄÖÜß]+", str(name or "")) if t]


def _build_generic_queries(query_engine: QueryEngine) -> list[str]:
    names = _active_person_names(query_engine)
    if not names:
        return []

    by_surname: dict[str, list[str]] = defaultdict(list)
    for name in names:
        tokens = _extract_tokens(name)
        if len(tokens) < 2:
            continue
        surname = tokens[-1]
        by_surname[surname].append(name)

    ambiguous_surname = None
    ambiguous_names: list[str] = []
    for surname, grouped_names in by_surname.items():
        unique_group = sorted(set(grouped_names))
        if len(unique_group) >= 2:
            ambiguous_surname = surname
            ambiguous_names = unique_group
            break

    seed_name = ambiguous_names[0] if ambiguous_names else names[0]
    seed_tokens = _extract_tokens(seed_name)

    queries: list[str] = []
    if ambiguous_surname:
        queries.append(f"Mr. {ambiguous_surname}")
        queries.append(f"Dr. {ambiguous_surname}")

    queries.append(seed_name)

    if len(seed_tokens) >= 2:
        initials = ".".join(token[0].upper() for token in seed_tokens[:-1]) + f". {seed_tokens[-1]}"
        queries.append(initials)
        queries.append(f"{seed_tokens[0]} {seed_tokens[-1]}")

    seen = set()
    deduped: list[str] = []
    for q in queries:
        if q not in seen:
            deduped.append(q)
            seen.add(q)
    return deduped




def run_name_variants_demo() -> int:
    engine = QueryEngine()
    test_queries = _build_generic_queries(engine)
    if not test_queries:
        print("No active person names found; skipped name-variant checks.")
        return 0

    print("Testing generic entity extraction and resolution for name variants:\n")
    for q in test_queries:
        print(f"Query: '{q}'")
        entity_hint = engine._best_entity_hint(q)
        print(f"  [1] Entity extraction: '{entity_hint}'")
        if entity_hint:
            try:
                with engine._get_connection() as conn:
                    details = engine._resolve_entity_name_for_lookup_details(conn, q)
                    resolved = details.get("resolved_name") if isinstance(details, dict) else None
                    ambiguous = bool(details.get("ambiguous")) if isinstance(details, dict) else False
                    candidates = list(details.get("candidates") or []) if isinstance(details, dict) else []
                    print(f"  [2] SQL resolution: '{resolved}'")
                    if ambiguous:
                        print(f"  [2a] Ambiguous candidates: {candidates}")
                    with conn.cursor() as cur:
                        tokens = re.findall(r"[a-zA-Z0-9äöüÄÖÜß]{3,}", entity_hint)
                        if tokens:
                            last_token = tokens[-1]
                            cur.execute("""
                                                        SELECT object_name, class_name
                            FROM object_instance
                            WHERE LOWER(object_name) LIKE LOWER(%s)
                              AND COALESCE(status, 'active') = 'active'
                              AND valid_from <= CURRENT_DATE
                              AND (valid_until IS NULL OR valid_until > CURRENT_DATE)
                            ORDER BY object_name
                        """, (f"%{last_token}%",))
                            matches = cur.fetchall()
                            if matches:
                                print(f"  [3] Partial matches (%{last_token}%): {len(matches)} results")
                                for name, otype in matches[:3]:
                                    print(f"       - {name} ({otype})")
                                if len(matches) > 3:
                                    print(f"       ... and {len(matches) - 3} more")
            except Exception as e:
                print(f"  Error: {e}")
        print()
    return 0


def test_name_variants_module_smoke() -> None:
    assert callable(_extract_tokens)
    assert _extract_tokens("Chung Yeung Pang")[-1] == "Pang"


if __name__ == "__main__":
    raise SystemExit(run_name_variants_demo())
