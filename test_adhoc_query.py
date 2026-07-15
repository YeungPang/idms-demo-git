#!/usr/bin/env python3
"""Re-test ad-hoc historical queries that were asked but not in regression suite."""

import requests
import json
import sys

def run_query(query_text: str) -> dict:
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
            "response": response.json() if response.status_code == 200 else str(response.text),
            "success": response.status_code == 200
        }
    except Exception as e:
        return {
            "query": query_text,
            "status_code": None,
            "error": str(e),
            "success": False
        }


if __name__ == "__main__":
    # Ad-hoc historical queries that were asked but NOT in regression suite
    adhoc_queries = [
        "When and where was Chung Yeung Pang born?",
        "Where was Chung Yeung Pang born?",
        "What is Chung Yeung Pang's date of birth?",
        "Show me the full content of the note of personal profile of Chung Yeung Pang",
    ]
    
    print("=" * 80)
    print("RE-TESTING AD-HOC HISTORICAL QUERIES")
    print("=" * 80)
    
    results = []
    for i, query in enumerate(adhoc_queries, 1):
        print(f"\n[{i}/{len(adhoc_queries)}] Testing: {query}")
        result = run_query(query)
        results.append(result)
        
        if result["success"]:
            response_data = result["response"]
            print(f"   Status: ✅ 200 OK")
            print(f"   Answer Source: {response_data.get('answer_source', 'N/A')}")
            
            answer = response_data.get("answer", "").strip()
            if answer:
                if len(answer) > 200:
                    print(f"   Answer Preview: {answer[:200]}...")
                else:
                    print(f"   Answer: {answer}")
            else:
                print(f"   Answer: (empty)")
        else:
            print(f"   Status: ❌ FAILED")
            if "error" in result:
                print(f"   Error: {result['error']}")
            else:
                print(f"   Status Code: {result['status_code']}")
                print(f"   Response: {result['response']}")
    
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    passed = sum(1 for r in results if r["success"])
    failed = len(results) - passed
    print(f"Passed: {passed}/{len(results)}")
    print(f"Failed: {failed}/{len(results)}")
    
    if failed > 0:
        print("\nFailed queries:")
        for r in results:
            if not r["success"]:
                print(f"  - {r['query']}")


def test_adhoc_query_module_smoke() -> None:
    """Ensure helper is importable under pytest collection."""
    result = run_query("health check")
    assert isinstance(result, dict)
    assert "success" in result
