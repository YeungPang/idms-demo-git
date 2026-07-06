#!/usr/bin/env python3
"""Comprehensive test of all historical user queries that were logged but NOT in regression suite."""

import requests
import json
import sys

def test_query(query_text: str) -> dict:
    """Execute a single query via the live API."""
    payload = {"question": query_text, "limit": 10}
    try:
        response = requests.post(
            "http://localhost:8000/api/query",
            json=payload,
            timeout=15
        )
        return {
            "query": query_text,
            "status_code": response.status_code,
            "response": response.json() if response.status_code == 200 else str(response.text)[:300],
            "success": response.status_code == 200
        }
    except Exception as e:
        return {
            "query": query_text,
            "status_code": None,
            "error": str(e)[:200],
            "success": False
        }


if __name__ == "__main__":
    # Historical queries that were logged but NOT in regression suite
    untested_queries = [
        # English queries about Chung Yeung Pang (NOT YET RE-TESTED)
        "Show me all the themes of documents containing Chung Yeung Pang.",
        "Show me the address of Chung Yeung Pang",
        "Show me the document concerning Chung Yeung Pang",
        "Show me the full information of Chung Yeung Pang",
        "Show me the full personal information of Chung Yeung Pang",
        "Show me the full profile of Chung Yeung Pang",
        "Show me the node for the profile of Chung Yeung Pang",
        "Show me the note concerning Chung Yeung Pang",
        "Show me the note titles and file names of all documents that have description of Chung Yeung Pang",
        
        # Shareholder queries
        "What companies does B. Pang hold shares?",
        "What companies does Chung Yeung Pang hold shares?",
        "What companies does Mr. Pang hold shares?",
        "Who are the shareholders of Aphotonix GmbH and how many shares each one holds?",
        
        # Aphotonix GmbH queries
        "What day was Aphotonix GmbH registered?",
        "What is the Statute date in the Handelsregisterauszug of Aphotonix GmbH?",
        "What is the total capital of Aphotonix GmbH?",
        "When was Aphotonix GmbH found?",
        "When was Aphotonix GmbH registered?",
        "What are the name and email address of the contact person for the EORI registration from Aphotonix GmbH?",
        
        # Document reference queries
        "Which document mentioned Benjamin Pang? Please return the file name with the full path.",
        
        # German language queries
        "Was ist der EORI-Nr. von Aphotonix GmbH?",
        "Was ist der email für Kontakt für EORI-Ansprechpartner von Aphotonix GmbH?",
        "Werr ist der EORI-Ansprechpartner von Aphotonix GmbH?",
        "Wie lautet das Statutendatum im Handelsregisterauszug der Aphotonix GmbH?",
        "Wie lautet die Adresse der Aphotonix GmbH?",
    ]
    
    print("=" * 100)
    print("RE-TESTING HISTORICAL QUERIES (NOT in regression suite)")
    print("=" * 100)
    
    results = []
    for i, query in enumerate(untested_queries, 1):
        print(f"\n[{i:2d}/{len(untested_queries)}] Testing: {query[:80]}")
        result = test_query(query)
        results.append(result)
        
        if result["success"]:
            response_data = result["response"]
            answer_source = response_data.get("answer_source", "N/A")
            print(f"    Status: ✅ 200 OK | Source: {answer_source}")
            
            answer = response_data.get("answer", "").strip()
            if answer:
                preview = answer[:120] if len(answer) > 120 else answer
                print(f"    Answer Preview: {preview}")
            else:
                print(f"    Answer: (empty)")
        else:
            print(f"    Status: ❌ FAILED")
            if "error" in result:
                print(f"    Error: {result['error'][:100]}")
            else:
                print(f"    Status Code: {result['status_code']}")
    
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    passed = sum(1 for r in results if r["success"])
    failed = len(results) - passed
    print(f"Passed: {passed}/{len(results)} ({100*passed//len(results)}%)")
    print(f"Failed: {failed}/{len(results)}")
    
    if failed > 0:
        print("\nFailed queries:")
        for i, r in enumerate(results, 1):
            if not r["success"]:
                query_preview = r['query'][:70]
                print(f"  [{i:2d}] {query_preview}")
