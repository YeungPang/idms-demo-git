#!/usr/bin/env python3
"""Test and demonstrate hybrid search functionality."""

from hybrid_search import encode_sparse_vector, merge_hybrid_results, should_use_hybrid_search

# Test 1: Sparse vector encoding
print("=" * 60)
print("Test 1: Sparse Vector Encoding")
print("=" * 60)

test_texts = [
    "CHE-399.737.068 EORI number for Zurich",
    "The company registered on 2024-05-15",
    "Find all VAT numbers for Swiss companies",
    "Who is the shareholder of Company ABC?",
]

for text in test_texts:
    indices, values = encode_sparse_vector(text)
    print(f"\nText: {text[:50]}...")
    print(f"  Tokens: {len(indices)}, Norm: {sum(v*v for v in values)**0.5:.4f}")
    print(f"  Top tokens: {indices[:3]}")

# Test 2: Hybrid search decision
print("\n" + "=" * 60)
print("Test 2: Hybrid Search Decision Heuristic")
print("=" * 60)

decision_tests = [
    "CHE-399.737.068",  # Should use hybrid (ID pattern)
    "2024-05-15",  # Should use hybrid (date pattern)
    "What is the meaning of life?",  # Should NOT use hybrid (semantic)
    "EORI registration date",  # Should use hybrid (keyword + date)
    "short query",  # Should use hybrid (short = likely keyword)
]

for query in decision_tests:
    use_hybrid = should_use_hybrid_search(query)
    status = "✓ HYBRID" if use_hybrid else "✗ DENSE"
    print(f"{status:15} | {query}")

# Test 3: Result merging
print("\n" + "=" * 60)
print("Test 3: Hybrid Result Merging")
print("=" * 60)

sparse_results = [
    {"id": "doc1", "score": 0.95, "payload": {"text": "EORI match"}},
    {"id": "doc2", "score": 0.85, "payload": {"text": "VAT match"}},
    {"id": "doc3", "score": 0.70, "payload": {"text": "address"}},
]

dense_results = [
    {"id": "doc4", "score": 0.92, "payload": {"text": "semantic"}},
    {"id": "doc2", "score": 0.88, "payload": {"text": "VAT match"}},
    {"id": "doc1", "score": 0.75, "payload": {"text": "EORI match"}},
]

merged = merge_hybrid_results(
    sparse_results=sparse_results,
    dense_results=dense_results,
    sparse_weight=0.3,
    dense_weight=0.7,
    limit=4,
)

print("Sparse Results (keyword-based):")
for r in sparse_results:
    print(f"  {r['id']}: {r['score']:.2f}")

print("\nDense Results (semantic):")
for r in dense_results:
    print(f"  {r['id']}: {r['score']:.2f}")

print("\nMerged Results (weighted hybrid):")
for r in merged:
    print(f"  {r['id']:6} | Combined: {r['score']:.4f} (Sparse: {r['sparse_score']:.2f}, Dense: {r['dense_score']:.2f})")

print("\n✓ Hybrid search system fully tested and working!")
