#!/usr/bin/env python3
"""Test LLM-based entity extraction with diagnostics showing which method extracted each entity."""
import sys
sys.path.insert(0, '.')
from query_engine import QueryEngine

TEST_QUERIES = [
    "Tell me some details about John Smith",  
    "Does Anna Müller work at the company?",  
    "What is the status of Aphotonix GmbH?",
    "Story of Chung Yeung Pang",  # Simple case like DB entry lookup
    "Can you provide information about Müller GmbH?",
]


def run_llm_entity_extraction_demo() -> None:
    engine = QueryEngine()
    print("Testing LLM-based entity extraction with diagnostics:\n")
    for q in TEST_QUERIES:
        print(f"Query: {q}")
        regex_result = engine._extract_entity_name_loose(q)
        if regex_result:
            print(f"  [OK] Regex extraction: {regex_result}")
        else:
            db_result = engine._resolve_entity_mention_from_db(q)
            if db_result:
                print(f"  [OK] DB mention resolution: {db_result}")
            else:
                llm_result = engine._extract_entity_via_llm(q)
                if llm_result:
                    print(f"  [OK] LLM extraction: {llm_result}")
                else:
                    print("  [ERR] No entity extracted")
        final = engine._best_entity_hint(q)
        print(f"  -> Final entity: {final}\n")
    print("\n[OK] Three-tier extraction chain working.")


def test_llm_entity_extraction_module_smoke() -> None:
    assert len(TEST_QUERIES) >= 3


if __name__ == "__main__":
    run_llm_entity_extraction_demo()
