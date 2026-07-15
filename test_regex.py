import re

text = "Within how many days must the invoice from Kanton Zug to Aphotonix GmbH be paid?"

payment_term_patterns = [
    r"\b(?:within|how\s+many|in\s+what).*(?:days?|time).*(?:must|should|will).*(?:be\s+)?paid\b",
    r"\b(?:payment.*deadline|payment.*terms|due\s+date|zahlungsfrist|zahlungslaufzeit)\b",
]

print(f"Testing text: {text}\n")
for i, pattern in enumerate(payment_term_patterns):
    match = re.search(pattern, text, flags=re.IGNORECASE)
    print(f"Pattern {i}: {pattern}")
    print(f"  Match: {match is not None}")
    if match:
        print(f"  Matched text: '{match.group()}'")
    print()


def test_regex_module_smoke() -> None:
    assert any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in payment_term_patterns)
