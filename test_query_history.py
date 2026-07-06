from __future__ import annotations

import json
from pathlib import Path

QUERY_HISTORY_JSON = Path(__file__).with_name("query_history_extracted.json")


def load_all_queries() -> list[str]:
    if not QUERY_HISTORY_JSON.exists():
        return []
    data = json.loads(QUERY_HISTORY_JSON.read_text(encoding="utf-8"))
    return [str(item).strip() for item in data if str(item).strip()]


def load_substantive_queries(min_length: int = 12) -> list[str]:
    queries = load_all_queries()
    return [item for item in queries if len(item) >= min_length]


ALL_QUERIES = load_all_queries()
SUBSTANTIVE_QUERIES = load_substantive_queries()


if __name__ == "__main__":
    print(f"All queries: {len(ALL_QUERIES)}")
    for idx, query in enumerate(ALL_QUERIES, start=1):
        safe_query = query.encode("cp1252", errors="replace").decode("cp1252")
        print(f"{idx}. {safe_query}")
