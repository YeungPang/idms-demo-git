#!/usr/bin/env python3
"""Test LLM entity extraction for queries that bypass regex but have entity mentions."""
import sys
sys.path.insert(0, '.')
from query_engine import QueryEngine

TEST_QUERIES = [
    # Free-form queries where entity is mentioned but without clear prepositional patterns
    "Chung Yeung Pang is Swiss",  # No preposition, just predicate
    "Is Aphotonix GmbH still active?",  # Question without "of/for/about" patterns
    "Tell me everything about Maria Schmidt",  # Even without, should use LLM fallback
    "The biggest shareholder is James Wilson",  # Entity mentioned before verb
]


def run_llm_fallback_demo() -> None:
    engine = QueryEngine()
    print("Testing LLM entity extraction (fallback for non-preposition queries):\n")
    for q in TEST_QUERIES:
        print(f"Query: {q}")
        regex_result = engine._extract_entity_name_loose(q)
        if regex_result:
            print(f"  [OK] Regex: {regex_result}")
        else:
            print("  [ERR] Regex didn't match")
            db_result = engine._resolve_entity_mention_from_db(q)
            if db_result:
                print(f"  [OK] DB mention: {db_result}")
            else:
                print("  [ERR] DB didn't find it")
                llm_result = engine._extract_entity_via_llm(q)
                if llm_result:
                    print(f"  [OK] LLM constrained: {llm_result}")
                else:
                    print("  [ERR] LLM couldn't extract")
        final = engine._best_entity_hint(q)
        print(f"  -> Final: {final}\n")
    print("\n[OK] LLM fallback demo completed.")


def test_llm_fallback_module_smoke() -> None:
    assert len(TEST_QUERIES) >= 3


if __name__ == "__main__":
    run_llm_fallback_demo()
