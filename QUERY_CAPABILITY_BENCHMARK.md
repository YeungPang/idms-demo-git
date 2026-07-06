# Query Capability Benchmark

This benchmark closes the gap between fixed regression tests and real-world query flexibility.

## Why this exists

Classic regressions can all pass while the engine is still brittle for unseen user wording.
This runner evaluates robustness using query perturbations and reports conservative future-case coverage.

## What it measures

- Availability: API returns a successful response across perturbations.
- Valid answer rate: answer is non-empty and not sourced from not_found.
- Intent stability: parser intent consistency under wording variations.
- Projected future coverage: conservative 95% lower bound from observed valid responses.

## Inputs

- Seed queries from query_history_extracted.json.
- Automatically generated variants per seed (punctuation noise, casing, connectors, typo).

## Run

From the IDMS-Demo directory:

```powershell
..\.venv\Scripts\python.exe run_query_capability_benchmark.py --base-url http://127.0.0.1:8000
```

Recommended stricter run:

```powershell
..\.venv\Scripts\python.exe run_query_capability_benchmark.py --max-seeds 50 --timeout 45 --target-coverage 0.99
```

## Outputs

- log/query_capability_benchmark.json
- log/query_capability_benchmark.md

## How to use in CI and release gating

- Keep existing unit and regression tests.
- Add this benchmark as a quality gate.
- Block release if projected future coverage (95% lower bound) is below target.

Suggested progressive targets:

1. 0.85 lower bound for stabilization phase.
2. 0.92 lower bound after parser/pattern migration.
3. 0.99 lower bound only when dataset and perturbation suite are broad and continuously expanded.

## Important note on 99%

No static suite can guarantee 99% of all future user queries.
You can approach it by continuously expanding query seeds, perturbation strategies, and production-feedback replay.
The lower-bound metric helps prevent overconfidence.
