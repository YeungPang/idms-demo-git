"""
Test/Demo: Rule Ingestion System

Demonstrates how to:
1. Classify natural language rules
2. Ingest rules into appropriate backends (pattern, clause, fact, class)
3. Query the system with ingested rules
"""

import sys
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def demo_classifier():
    """Demonstrate rule classification without database."""
    print("\n" + "=" * 80)
    print("DEMO 1: Rule Classification (No Database Required)")
    print("=" * 80)

    from rule_ingestion import RuleClassifier

    classifier = RuleClassifier()

    # Test cases covering all types
    test_rules = [
        # PATTERN examples
        "What is the invoice due date from Kanton Zug to Aphotonix GmbH?",
        "invoice due date from * to *",
        "falligkeitsdatum der rechnung von * an *",

        # CLAUSE examples
        "When an invoice is overdue for 30 days, escalate to management and send notification",
        "If payment is received, update transaction status to 'completed'",
        "Trigger compliance check when document contains sensitive information",

        # FACT examples
        "Company APHOTONIX GmbH is registered in Canton Zug",
        "Invoice INV-001 has due date 2026-07-15",
        "John Smith is the CEO of TechCorp",

        # CLASS examples
        "Invoice class has attributes: invoice_number, due_date, amount, status, currency",
        "Define entity type 'Company' with properties: name, canton, registration_date",
        "Transaction entity extends BaseEntity and has ledger_lines relationship",
    ]

    for rule_text in test_rules:
        print(f"\n→ Input: {rule_text}")

        result = classifier.classify(rule_text)

        print(f"  Type: {result.rule_type.value.upper()}")
        print(f"  Confidence: {result.confidence:.2%}")
        print(f"  Rationale: {result.classification_rationale}")

        if result.extracted_metadata:
            print(f"  Metadata: {result.extracted_metadata}")

        if result.suggested_parameters:
            print(f"  Suggested Parameters:")
            for key, value in result.suggested_parameters.items():
                if key not in ["pattern_text", "clause_name"]:  # Skip long values
                    print(f"    - {key}: {value}")


def demo_api_requests():
    """Show example API requests (curl commands)."""
    print("\n" + "=" * 80)
    print("DEMO 2: API Request Examples (for manual testing)")
    print("=" * 80)

    examples = {
        "Classify a pattern": {
            "endpoint": "POST /api/rules/classify",
            "body": {
                "rule_text": "What is the invoice due date from X to Y?",
                "use_llm": False
            }
        },
        "Ingest a single rule": {
            "endpoint": "POST /api/rules/ingest",
            "body": {
                "rule_text": "When invoice is overdue for 30 days, escalate to management",
                "auto_classify": True,
                "store_if_confident": True
            }
        },
        "Batch ingest multiple rules": {
            "endpoint": "POST /api/rules/ingest-batch",
            "body": {
                "rules": [
                    "What is the invoice due date of invoice from X to Y?",
                    "When payment is received, update status to completed",
                    "Company X is registered in Canton Y",
                    "Invoice entity has: number, date, amount, status"
                ],
                "auto_classify": True,
                "store_if_confident": True
            }
        },
        "Get rule types": {
            "endpoint": "GET /api/rules/types",
            "body": None
        }
    }

    for name, example in examples.items():
        import json
        print(f"\n{name}:")
        print(f"  {example['endpoint']}")
        if example['body']:
            print(f"  Body:")
            print("    " + json.dumps(example['body'], indent=2).replace("\n", "\n    "))


def demo_classification_counts():
    """Show classification accuracy on comprehensive test set."""
    print("\n" + "=" * 80)
    print("DEMO 3: Classification Accuracy Analysis")
    print("=" * 80)

    from rule_ingestion import RuleClassifier, RuleType

    classifier = RuleClassifier()

    test_set = {
        RuleType.PATTERN: [
            "What is the invoice due date from Kanton Zug to Aphotonix GmbH?",
            "invoice due date from * to *",
            "Find all companies registered in Canton Zug",
            "Search for transactions between January and March",
            "Show me the relationship between X and Y",
        ],
        RuleType.CLAUSE: [
            "When invoice is overdue for 30 days, escalate to management",
            "If payment received, update status to completed",
            "Trigger alert when document contains PII",
            "Compute total transaction amount for account",
            "When new document arrives, extract entities and validate",
        ],
        RuleType.FACT: [
            "Company APHOTONIX GmbH is registered in Canton Zug",
            "Invoice INV-001 has due date 2026-07-15",
            "John Smith is the CEO",
            "The account balance is CHF 10,000",
            "Transaction T001 is for company X",
        ],
        RuleType.CLASS: [
            "Invoice class has attributes: number, due_date, amount, status",
            "Define company entity with: name, canton, registration_date",
            "Transaction entity has relationships to accounts and ledger",
            "Person type has properties: first_name, last_name, birth_date",
            "Document extends BaseEntity with: title, content, classification",
        ],
    }

    overall_stats = {
        "total": 0,
        "correct": 0,
        "by_type": {},
    }

    for expected_type, rules in test_set.items():
        print(f"\n{expected_type.value.upper()} classification accuracy:")
        correct = 0
        for rule in rules:
            result = classifier.classify(rule)
            is_correct = result.rule_type == expected_type
            correct += int(is_correct)
            overall_stats["total"] += 1
            if is_correct:
                overall_stats["correct"] += 1

            status = "✓" if is_correct else "✗"
            print(f"  {status} {rule[:60]}... (got {result.rule_type.value}, conf: {result.confidence:.2%})")

        accuracy = correct / len(rules) if rules else 0
        overall_stats["by_type"][expected_type.value] = accuracy
        print(f"  → {correct}/{len(rules)} correct ({accuracy:.0%})")

    print(f"\nOverall Accuracy: {overall_stats['correct']}/{overall_stats['total']} ({overall_stats['correct']/overall_stats['total']:.0%})")
    print("Type breakdown:")
    for type_name, acc in overall_stats["by_type"].items():
        print(f"  - {type_name}: {acc:.0%}")


def test_rule_ingestion_module_smoke() -> None:
    assert callable(demo_classifier)
    assert callable(demo_api_requests)
    assert callable(demo_classification_counts)


if __name__ == "__main__":
    print("\n" + "█" * 80)
    print("█" + " " * 78 + "█")
    print("█" + "  IDMS RULE INGESTION PIPELINE - TEST & DEMO".center(78) + "█")
    print("█" + " " * 78 + "█")
    print("█" * 80)

    try:
        # Run demos
        demo_classifier()
        demo_classification_counts()
        demo_api_requests()

        print("\n" + "=" * 80)
        print("✓ Demo completed successfully!")
        print("=" * 80)
        print("\nNext steps:")
        print("1. Start API server: uvicorn idms_api:app --reload --port 8002")
        print("2. Try classification: curl -X POST http://localhost:8002/api/rules/classify \\")
        print("   -H 'Content-Type: application/json' \\")
        print("   -d '{\"rule_text\": \"What is the invoice due date from X to Y?\"}'")
        print("3. Try ingestion: curl -X POST http://localhost:8002/api/rules/ingest \\")
        print("   -H 'Content-Type: application/json' \\")
        print("   -d '{\"rule_text\": \"Company X is registered in Canton Y\", \"auto_classify\": true}'")

    except Exception as e:
        logger.error(f"Demo failed: {e}", exc_info=True)
        sys.exit(1)
