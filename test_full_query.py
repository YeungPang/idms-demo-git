from query_engine import QueryEngine


QUERY = "Within how many days must the invoice from Kanton Zug to Aphotonix GmbH be paid?"


def run_full_query_probe() -> None:
    qe = QueryEngine()
    print(f"Testing query: {QUERY}\n")
    print("=" * 80)

    try:
        parsed = qe.parse_query(QUERY)
        print("\nParsed Query:")
        print(f"  Intent: {parsed.intent}")
        print(f"  Entity: {parsed.entity_name}")
        print(f"  Attribute: {parsed.attribute_name}")
        print(f"  Confidence: {parsed.confidence}")
        print(f"  Language: {parsed.language}")
        print(f"  Criteria: {parsed.criteria}")
    except Exception as e:
        print(f"Error parsing: {e}")
        import traceback
        traceback.print_exc()


def test_full_query_module_smoke() -> None:
    assert isinstance(QUERY, str)
    assert "invoice" in QUERY.lower()


if __name__ == "__main__":
    run_full_query_probe()


