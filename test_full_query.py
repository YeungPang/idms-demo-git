from query_engine import QueryEngine

# Initialize query engine
qe = QueryEngine()

# User's original query
query = "Within how many days must the invoice from Kanton Zug to Aphotonix GmbH be paid?"

print(f"Testing query: {query}\n")
print("=" * 80)

# Parse the query to see how it's being interpreted
try:
    parsed = qe.parse_query(query)
    
    print(f"\nParsed Query:")
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


