#!/usr/bin/env python3
"""Comprehensive test of all historical user queries with detailed results logging."""

import requests
import json
import re
import sys
from datetime import datetime


def _looks_like_email(value: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value or ""))


def _looks_like_date(value: str) -> bool:
    text = value or ""
    patterns = (
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b",
        r"\b\d{1,2}[./-]\d{4}\b",
    )
    return any(re.search(p, text) for p in patterns)


def validate_answer_quality(query_text: str, answer: str, answer_source: str) -> list[str]:
    """Return semantic quality issues for a query/answer pair."""
    issues: list[str] = []
    q = (query_text or "").lower()
    a = (answer or "").strip()
    src = (answer_source or "").lower()

    if not a:
        issues.append("empty_answer")
    if src == "not_found":
        issues.append("not_found_source")

    asks_email = any(k in q for k in (" email", "e-mail", "emailadresse", "e-mail-adresse"))
    asks_contact_person = any(k in q for k in ("contact person", "ansprechpartner", "kontakt"))
    asks_statute_date = any(k in q for k in ("statute", "statutendatum", "statut"))
    asks_full_path = "full path" in q

    if asks_email and not _looks_like_email(a):
        issues.append("email_expected_but_missing")

    if asks_contact_person and asks_email and "eori no" in a.lower():
        issues.append("returned_eori_number_for_contact_email_query")

    if asks_statute_date and not _looks_like_date(a):
        issues.append("statute_date_expected_but_missing")

    if asks_full_path and not any(token in a for token in (":\\", "\\", "/")):
        issues.append("full_path_expected_but_missing")

    return issues

def run_query(query_text: str) -> dict:
    """Execute a single query via the live API."""
    payload = {"question": query_text, "limit": 10}
    try:
        response = requests.post(
            "http://localhost:8000/api/query",
            json=payload,
            timeout=20
        )
        if response.status_code == 200:
            data = response.json()
            answer_source = data.get("result", {}).get("answer_source", "unknown")
            answer = data.get("result", {}).get("answer", "")
            quality_issues = validate_answer_quality(query_text, answer, answer_source)
            return {
                "query": query_text,
                "status_code": 200,
                "success": len(quality_issues) == 0,
                "answer_source": answer_source,
                "answer": answer,
                "intent": data.get("result", {}).get("grounding", {}).get("parsed", {}).get("intent", "unknown"),
                "confidence": data.get("result", {}).get("grounding", {}).get("parsed", {}).get("confidence", 0),
                "quality_issues": quality_issues,
            }
        else:
            return {
                "query": query_text,
                "status_code": response.status_code,
                "success": False,
                "error": response.text[:300],
            }
    except Exception as e:
        return {
            "query": query_text,
            "status_code": None,
            "success": False,
            "error": str(e)[:200],
        }


if __name__ == "__main__":
    # Historical queries that were logged but NOT in regression suite
    PASS_MARK = "PASS"
    FAIL_MARK = "FAIL"
    untested_queries = [
        # English queries about Chung Yeung Pang
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
    
    print("=" * 120)
    print("RE-TESTING HISTORICAL QUERIES (NOT in regression suite)")
    print("=" * 120)
    print()
    
    results = []
    for i, query in enumerate(untested_queries, 1):
        print(f"[{i:2d}/{len(untested_queries)}] {query[:90]}")
        result = run_query(query)
        results.append(result)
        
        if result["success"]:
            print(f"  {PASS_MARK} | Source: {result['answer_source']:25s} | Intent: {result['intent']:20s} | Conf: {result['confidence']:.2f}")
            if result['answer']:
                preview = result['answer'][:100] if len(result['answer']) > 100 else result['answer']
                print(f"         Answer: {preview}")
            else:
                print(f"         Answer: (empty)")
        else:
            if result.get("status_code") == 200:
                print(f"  {FAIL_MARK} | Status: semantic_mismatch | Issues: {', '.join(result.get('quality_issues', []))}")
            else:
                print(f"  {FAIL_MARK} | Status: {result['status_code']} | Error: {result.get('error', 'Unknown')[:60]}")
        print()
    
    print("=" * 120)
    print("SUMMARY")
    print("=" * 120)
    passed = sum(1 for r in results if r["success"])
    failed = len(results) - passed
    
    print(f"Total Queries Tested: {len(results)}")
    print(f"Passed: {passed}/{len(results)} ({100*passed//len(results)}%)")
    print(f"Failed: {failed}/{len(results)}")
    print()
    
    # Group by answer source
    sources = {}
    for r in results:
        if r["success"]:
            source = r["answer_source"]
            if source not in sources:
                sources[source] = 0
            sources[source] += 1
    
    if sources:
        print("Answer Sources Distribution:")
        for source, count in sorted(sources.items(), key=lambda x: -x[1]):
            print(f"  {source:30s}: {count:3d} queries")
    print()
    
    # Group by intent
    intents = {}
    for r in results:
        if r["success"]:
            intent = r["intent"]
            if intent not in intents:
                intents[intent] = 0
            intents[intent] += 1
    
    if intents:
        print("Query Intents Distribution:")
        for intent, count in sorted(intents.items(), key=lambda x: -x[1]):
            print(f"  {intent:30s}: {count:3d} queries")
    print()
    
    # Generate detailed log file
    log_filename = "log/query_retest_results.log"
    with open(log_filename, "w", encoding="utf-8") as f:
        f.write("=" * 120 + "\n")
        f.write(f"HISTORICAL QUERY RE-TEST REPORT\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n")
        f.write("=" * 120 + "\n\n")
        
        f.write(f"SUMMARY\n")
        f.write("-" * 120 + "\n")
        f.write(f"Total Queries: {len(results)}\n")
        f.write(f"Passed: {passed} ({100*passed//len(results)}%)\n")
        f.write(f"Failed: {failed}\n\n")
        
        if sources:
            f.write("ANSWER SOURCES:\n")
            for source, count in sorted(sources.items(), key=lambda x: -x[1]):
                f.write(f"  {source:30s}: {count:3d} queries\n")
            f.write("\n")
        
        if intents:
            f.write("QUERY INTENTS:\n")
            for intent, count in sorted(intents.items(), key=lambda x: -x[1]):
                f.write(f"  {intent:30s}: {count:3d} queries\n")
            f.write("\n")
        
        f.write("=" * 120 + "\n")
        f.write("DETAILED RESULTS\n")
        f.write("=" * 120 + "\n\n")
        
        for i, result in enumerate(results, 1):
            f.write(f"[{i:2d}] {result['query']}\n")
            f.write("-" * 120 + "\n")
            
            if result["success"]:
                f.write(f"Status:        {PASS_MARK}\n")
                f.write(f"Answer Source: {result['answer_source']}\n")
                f.write(f"Intent:        {result['intent']}\n")
                f.write(f"Confidence:    {result['confidence']:.2f}\n")
                if result['answer']:
                    f.write(f"Answer:\n{result['answer']}\n")
                else:
                    f.write(f"Answer:        (empty)\n")
            else:
                f.write(f"Status:        {FAIL_MARK}\n")
                if result.get("status_code") == 200:
                    f.write(f"HTTP Status:   200\n")
                    f.write(f"Error:         semantic_mismatch ({', '.join(result.get('quality_issues', []))})\n")
                    if result.get("answer_source") is not None:
                        f.write(f"Answer Source: {result.get('answer_source')}\n")
                    if result.get("answer"):
                        f.write(f"Answer:\n{result.get('answer')}\n")
                else:
                    f.write(f"HTTP Status:   {result['status_code']}\n")
                    f.write(f"Error:         {result.get('error', 'Unknown')}\n")
            
            f.write("\n")
    
    print(f"\nDetailed results saved to: {log_filename}")
    
    # Exit with appropriate code
    sys.exit(0 if failed == 0 else 1)


def test_historical_queries_comprehensive_module_smoke() -> None:
    result = run_query("health check")
    assert isinstance(result, dict)
    assert "success" in result
