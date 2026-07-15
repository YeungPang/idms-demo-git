import psycopg2
from idms_config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
from pattern_library import PatternLibrary

TEST_QUERY = "Within how many days must the invoice from Kanton Zug to Aphotonix GmbH be paid?"


def run_pattern_match_demo() -> None:
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    try:
        pl = PatternLibrary(conn)
        print(f"Test query: {TEST_QUERY}\n")
        print("Searching for matching patterns for invoice entity class...\n")
        matched_pattern = pl.match_pattern(TEST_QUERY, entity_class="invoice", language="en")
        if matched_pattern:
            print("[OK] Pattern MATCHED!")
            print(f"  Pattern text: {matched_pattern.pattern_text}")
            print(f"  Semantic concept: {matched_pattern.semantic_concept}")
            print(f"  Confidence: {matched_pattern.confidence}")
            print(f"  Mapped attributes: {matched_pattern.mapped_attributes}")
            print(f"  Computation rule: {matched_pattern.computation_rule}")
        else:
            print("[ERR] No pattern matched for this query")
            print("\nTrying just the payment term query part...")
            test_query2 = "within how many days must the invoice be paid"
            matched_pattern2 = pl.match_pattern(test_query2, entity_class="invoice", language="en")
            if matched_pattern2:
                print("[OK] Pattern MATCHED with simplified query!")
                print(f"  Pattern text: {matched_pattern2.pattern_text}")
                print(f"  Semantic concept: {matched_pattern2.semantic_concept}")
                print(f"  Confidence: {matched_pattern2.confidence}")
            else:
                print("[ERR] No pattern matched for simplified query either")
    finally:
        conn.close()


def test_pattern_match_module_smoke() -> None:
    assert "invoice" in TEST_QUERY.lower()


if __name__ == "__main__":
    run_pattern_match_demo()
