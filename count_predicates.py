#!/usr/bin/env python3
import re

with open('solf_script.txt', 'r', encoding='utf-8') as f:
    script = f.read()

# Find all predicate definitions: name(...) ⦃...⦄
predicates = {}

# Split by lines and find predicate definitions
lines = script.split('\n')
for i, line in enumerate(lines):
    # Look for pattern: word(...) ⦃
    if '⦃' in line:
        match = re.match(r'^\s*(\w+)\s*\(', line)
        if match:
            name = match.group(1)
            predicates[name] = predicates.get(name, 0) + 1

print(f"Total unique predicates: {len(predicates)}")
print("\nPredicates with multiple clauses (>1):")
multi_clause = {k: v for k, v in predicates.items() if v > 1}
if multi_clause:
    for name, count in sorted(multi_clause.items()):
        print(f"  {name}: {count} clauses")
else:
    print("  None - all predicates have exactly 1 clause definition")

print(f"\nTotal predicate definitions (including duplicates): {sum(predicates.values())}")
print(f"\nSingle-clause predicates: {len([p for p in predicates.values() if p == 1])}")
