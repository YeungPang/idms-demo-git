# Query Capability Benchmark

Generated: 2026-07-04T18:52:47.064926

## KPI Summary

- Seed queries: 5
- Variant queries: 20
- Availability: 75.00%
- Valid answer rate: 69.33%
- Intent stability: 100.00%
- Robustness score: 79.27%
- Projected future coverage (95% lower bound): 53.13%

## Coverage by Capability Bucket

| Bucket | Seeds | Success Rate | Valid Answer Rate | Intent Stability |
|---|---:|---:|---:|---:|
| document_retrieval | 2 | 90.00% | 90.00% | 100.00% |
| registration_numbers_dates | 2 | 66.67% | 66.67% | 100.00% |
| relationship_lookup | 1 | 33.33% | 33.33% | 100.00% |

## Coverage by Language

| Language | Seeds | Success Rate | Valid Answer Rate | Intent Stability |
|---|---:|---:|---:|---:|
| de | 1 | 100.00% | 100.00% | 100.00% |
| en | 4 | 61.67% | 61.67% | 100.00% |

## Interpretation

- This benchmark is stronger than fixed regressions because every seed query is tested with perturbations.
- The projected future coverage is conservative: it uses a 95% lower confidence bound.
- Use this as a release gate and trend over time, not a one-time score.
