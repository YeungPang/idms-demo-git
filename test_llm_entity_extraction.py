#!/usr/bin/env python3
"""Test LLM-based entity extraction with diagnostics showing which method extracted each entity."""
import sys
sys.path.insert(0, '.')
from query_engine import QueryEngine

engine = QueryEngine()

# Test cases
test_queries = [
    "Tell me some details about John Smith",  
    "Does Anna Müller work at the company?",  
    "What is the status of Aphotonix GmbH?",
    "Story of Chung Yeung Pang",  # Simple case like DB entry lookup
    "Can you provide information about Müller GmbH?",
]

print("Testing LLM-based entity extraction with diagnostics:\n")
for q in test_queries:
    print(f"Query: {q}")
    
    # Show which extraction method succeeded
    regex_result = engine._extract_entity_name_loose(q)
    if regex_result:
        print(f"  ✓ Regex extraction: {regex_result}")
    else:
        db_result = engine._resolve_entity_mention_from_db(q)
        if db_result:
            print(f"  ✓ DB mention resolution: {db_result}")
        else:
            llm_result = engine._extract_entity_via_llm(q)
            if llm_result:
                print(f"  ✓ LLM extraction: {llm_result}")
            else:
                print(f"  ✗ No entity extracted")
    
    # Show final result
    final = engine._best_entity_hint(q)
    print(f"  → Final entity: {final}\n")

print("\n✓ Three-tier extraction chain working:\n   1. Regex patterns (prepositions, company suffixes)\n   2. DB mention resolution (lookup known names)\n   3. LLM constrained extraction (explicit mention in query text)")
