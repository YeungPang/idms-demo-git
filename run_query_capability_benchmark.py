#!/usr/bin/env python3
"""Capability and robustness benchmark for query coverage.

This runner complements deterministic regressions with stress evaluation:
- Query perturbation (paraphrase-like rewrites, typos, punctuation, casing)
- Route stability checks (intent/source drift)
- Reliability checks (timeouts/not_found)
- Coverage checks across capability buckets and languages

The goal is to estimate real-world flexibility, not just pass/fail on fixed prompts.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_HISTORY_FILE = ROOT / "query_history_extracted.json"
DEFAULT_REPORT_JSON = ROOT / "log" / "query_capability_benchmark.json"
DEFAULT_REPORT_MD = ROOT / "log" / "query_capability_benchmark.md"

DEFAULT_BENCHMARK_QUERIES = [
    "Show me all the themes of documents containing Chung Yeung Pang.",
    "Show me the address of Chung Yeung Pang",
    "Show me the document concerning Chung Yeung Pang",
    "Show me the full personal information of Chung Yeung Pang",
    "Show me the note concerning Chung Yeung Pang",
    "What companies does B. Pang hold shares?",
    "What companies does Chung Yeung Pang hold shares?",
    "Who are the shareholders of Aphotonix GmbH and how many shares each one holds?",
    "What day was Aphotonix GmbH registered?",
    "What is the Statute date in the Handelsregisterauszug of Aphotonix GmbH?",
    "What is the total capital of Aphotonix GmbH?",
    "When was Aphotonix GmbH registered?",
    "What are the name and email address of the contact person for the EORI registration from Aphotonix GmbH?",
    "Which document mentioned Benjamin Pang? Please return the file name with the full path.",
    "Was ist der EORI-Nr. von Aphotonix GmbH?",
    "Was ist der email für Kontakt für EORI-Ansprechpartner von Aphotonix GmbH?",
    "Wer ist der EORI-Ansprechpartner von Aphotonix GmbH?",
    "Wie lautet das Statutendatum im Handelsregisterauszug der Aphotonix GmbH?",
    "Wie lautet die Adresse der Aphotonix GmbH?",
]


@dataclass(frozen=True)
class QuerySeed:
    text: str
    bucket: str
    language: str


def _is_german(text: str) -> bool:
    lowered = text.lower()
    markers = (
        " wie ",
        " was ",
        " der ",
        " die ",
        " das ",
        " und ",
        " für ",
        " von ",
        " lautet",
        "zeigen sie",
    )
    return any(marker in f" {lowered} " for marker in markers)


def _bucketize(text: str) -> str:
    lowered = text.lower()

    if any(k in lowered for k in ("document", "note", "dokument", "datei", "file name", "full path")):
        return "document_retrieval"
    if any(k in lowered for k in ("shareholder", "shares", "gesellschafter", "owner", "hold shares")):
        return "relationship_lookup"
    if any(k in lowered for k in ("full profile", "full information", "personal information", "voll", "profil")):
        return "composite_profile"
    if any(k in lowered for k in ("eori", "vat", "mwst", "registration", "statute", "statut")):
        return "registration_numbers_dates"
    return "attribute_lookup"


def load_history_queries(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[str] = []
    for item in data:
        text = str(item).strip()
        if len(text) >= 12:
            out.append(text)
    return out


def is_candidate_information_query(text: str) -> bool:
    lowered = text.lower().strip()
    if len(lowered) < 12:
        return False

    excluded_markers = (
        "please implement",
        "can you",
        "last task",
        "regression",
        "did it",
        "why does",
        "why should",
        "error:",
        "i came across",
        "i run into",
        "could you",
        "do we have",
        "is the",
        "would",
        "yes please",
    )
    if any(marker in lowered for marker in excluded_markers):
        return False

    # Favor user-facing data retrieval intents.
    intent_markers = (
        "show me",
        "what is",
        "what are",
        "which",
        "who",
        "when",
        "where",
        "list",
        "tell me",
        "wie ",
        "was ",
        "zeigen sie",
        "welche",
        "wer ",
        "wann",
        "wo ",
    )
    has_intent = any(lowered.startswith(marker) for marker in intent_markers)
    has_domain_signal = any(
        k in lowered
        for k in (
            "aphotonix",
            "pang",
            "eori",
            "share",
            "gesellschafter",
            "document",
            "note",
            "adresse",
            "address",
            "email",
            "statute",
            "statut",
            "registration",
        )
    )

    conversational_markers = (
        "i ",
        "it ",
        "there ",
        "this ",
        "that ",
        "we ",
    )
    if lowered.startswith(conversational_markers) and not lowered.startswith(("wie ", "was ", "wer ", "wann", "wo ")):
        return False

    # Either explicit question punctuation or imperative retrieval verbs.
    has_query_shape = lowered.endswith("?") or lowered.startswith(("show me", "list", "tell me", "zeigen sie"))

    return has_intent and has_domain_signal and has_query_shape


def build_seed_set(history_file: Path, max_seeds: int) -> list[QuerySeed]:
    raw = load_history_queries(history_file)

    filtered = [q for q in raw if is_candidate_information_query(q)]

    merged = list(DEFAULT_BENCHMARK_QUERIES)
    merged.extend(filtered)

    deduped: list[str] = []
    seen: set[str] = set()
    for q in merged:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(q)

    seeds = [
        QuerySeed(
            text=q,
            bucket=_bucketize(q),
            language="de" if _is_german(q) else "en",
        )
        for q in deduped
    ]

    if len(seeds) > max_seeds:
        random.shuffle(seeds)
        seeds = seeds[:max_seeds]

    return seeds


def perturbations(text: str, language: str) -> list[str]:
    base = text.strip()
    variants: list[str] = []

    variants.append(base)

    # Punctuation/casing noise.
    variants.append(base.rstrip("?.!") + "?")
    variants.append(base.rstrip("?.!") + " please")
    variants.append(base.lower())

    # Connector substitutions.
    connector_swaps_en = {
        " concerning ": " related to ",
        " related to ": " about ",
        " containing ": " mentioning ",
        " with ": " including ",
    }
    connector_swaps_de = {
        " für ": " betreffend ",
        " von ": " zu ",
        " im ": " in dem ",
        " und ": " sowie ",
    }
    swaps = connector_swaps_de if language == "de" else connector_swaps_en
    tmp = f" {base} "
    for src, dst in swaps.items():
        if src in tmp.lower():
            pattern = re.compile(re.escape(src), flags=re.IGNORECASE)
            variants.append(pattern.sub(dst, tmp, count=1).strip())

    # Single typo simulation on a long token.
    tokens = re.split(r"(\s+)", base)
    long_token_idx = None
    for idx, token in enumerate(tokens):
        if token.isalpha() and len(token) >= 7:
            long_token_idx = idx
            break
    if long_token_idx is not None:
        token = tokens[long_token_idx]
        typo = token[:2] + token[3:] if len(token) > 3 else token
        tokens2 = tokens[:]
        tokens2[long_token_idx] = typo
        variants.append("".join(tokens2))

    # Remove duplicates preserving order.
    unique: list[str] = []
    seen: set[str] = set()
    for v in variants:
        k = v.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        unique.append(v.strip())
    return unique


def call_query_api(base_url: str, question: str, timeout: int, max_steps: int) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/query",
            json={"question": question, "max_steps": max_steps},
            timeout=timeout,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        if response.status_code != 200:
            return {
                "ok": False,
                "http_status": response.status_code,
                "elapsed_ms": elapsed_ms,
                "error": response.text[:500],
            }

        payload = response.json() if response.content else {}
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        grounding = result.get("grounding", {}) if isinstance(result, dict) else {}
        parsed = grounding.get("parsed", {}) if isinstance(grounding, dict) else {}

        answer = str(result.get("answer", "") or "")
        answer_source = str(result.get("answer_source", "unknown") or "unknown")

        return {
            "ok": True,
            "http_status": 200,
            "elapsed_ms": elapsed_ms,
            "answer": answer,
            "answer_len": len(answer.strip()),
            "answer_source": answer_source,
            "intent": parsed.get("intent", "unknown"),
            "confidence": float(parsed.get("confidence", 0) or 0),
            "entity_name": parsed.get("entity_name"),
            "attribute_name": parsed.get("attribute_name"),
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "ok": False,
            "http_status": None,
            "elapsed_ms": elapsed_ms,
            "error": str(exc)[:500],
        }


def evaluate_seed(base_url: str, seed: QuerySeed, timeout: int, max_steps: int) -> dict[str, Any]:
    variants = perturbations(seed.text, seed.language)
    rows: list[dict[str, Any]] = []

    for v in variants:
        row = call_query_api(base_url=base_url, question=v, timeout=timeout, max_steps=max_steps)
        row["query"] = v
        rows.append(row)

    successes = [r for r in rows if r.get("ok")]
    fail_count = len(rows) - len(successes)

    intents = [str(r.get("intent", "unknown")) for r in successes]
    intent_counts = Counter(intents)
    dominant_intent, dominant_intent_count = (intent_counts.most_common(1)[0] if intent_counts else ("unknown", 0))

    sources = [str(r.get("answer_source", "unknown")) for r in successes]
    source_counts = Counter(sources)

    valid_answers = [r for r in successes if r.get("answer_len", 0) > 0 and str(r.get("answer_source", "")).lower() != "not_found"]

    elapsed = [int(r.get("elapsed_ms", 0) or 0) for r in rows]

    return {
        "seed": seed.text,
        "bucket": seed.bucket,
        "language": seed.language,
        "variant_count": len(rows),
        "success_count": len(successes),
        "failure_count": fail_count,
        "valid_answer_count": len(valid_answers),
        "success_rate": len(successes) / len(rows) if rows else 0,
        "valid_answer_rate": len(valid_answers) / len(rows) if rows else 0,
        "intent_stability": (dominant_intent_count / len(successes)) if successes else 0,
        "dominant_intent": dominant_intent,
        "source_distribution": dict(source_counts),
        "elapsed_ms_p50": int(statistics.median(elapsed)) if elapsed else 0,
        "elapsed_ms_p95": int(sorted(elapsed)[max(0, int(0.95 * len(elapsed)) - 1)]) if elapsed else 0,
        "rows": rows,
    }


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1 + (z * z) / total
    centre = p + (z * z) / (2 * total)
    margin = z * ((p * (1 - p) + (z * z) / (4 * total)) / total) ** 0.5
    return max(0.0, (centre - margin) / denom)


def aggregate_report(seed_reports: list[dict[str, Any]]) -> dict[str, Any]:
    total_variants = sum(int(r["variant_count"]) for r in seed_reports)
    total_success = sum(int(r["success_count"]) for r in seed_reports)
    total_valid = sum(int(r["valid_answer_count"]) for r in seed_reports)

    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_lang: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for report in seed_reports:
        by_bucket[str(report["bucket"])].append(report)
        by_lang[str(report["language"])].append(report)

    def _avg(items: list[dict[str, Any]], field: str) -> float:
        if not items:
            return 0.0
        return sum(float(i[field]) for i in items) / len(items)

    bucket_summary = {
        bucket: {
            "seed_count": len(items),
            "avg_success_rate": round(_avg(items, "success_rate"), 4),
            "avg_valid_answer_rate": round(_avg(items, "valid_answer_rate"), 4),
            "avg_intent_stability": round(_avg(items, "intent_stability"), 4),
        }
        for bucket, items in sorted(by_bucket.items())
    }

    language_summary = {
        lang: {
            "seed_count": len(items),
            "avg_success_rate": round(_avg(items, "success_rate"), 4),
            "avg_valid_answer_rate": round(_avg(items, "valid_answer_rate"), 4),
            "avg_intent_stability": round(_avg(items, "intent_stability"), 4),
        }
        for lang, items in sorted(by_lang.items())
    }

    avg_intent_stability = _avg(seed_reports, "intent_stability")
    avg_valid_rate = _avg(seed_reports, "valid_answer_rate")
    availability = (total_success / total_variants) if total_variants else 0.0

    # Conservative "future confidence" estimate.
    lb_availability = wilson_lower_bound(total_success, total_variants)
    lb_valid = wilson_lower_bound(total_valid, total_variants)

    # Composite robustness score gives a single gateable KPI.
    robustness_score = (
        0.40 * availability +
        0.35 * avg_valid_rate +
        0.25 * avg_intent_stability
    )

    # Conservative projected coverage for future cases is the lower bound over valid responses.
    projected_future_coverage = lb_valid

    return {
        "generated_at": datetime.now().isoformat(),
        "totals": {
            "seed_count": len(seed_reports),
            "variant_count": total_variants,
            "success_count": total_success,
            "valid_answer_count": total_valid,
        },
        "kpis": {
            "availability": round(availability, 4),
            "avg_valid_answer_rate": round(avg_valid_rate, 4),
            "avg_intent_stability": round(avg_intent_stability, 4),
            "robustness_score": round(robustness_score, 4),
            "projected_future_coverage": round(projected_future_coverage, 4),
            "projected_coverage_ci95_lower": round(projected_future_coverage, 4),
            "availability_ci95_lower": round(lb_availability, 4),
        },
        "by_bucket": bucket_summary,
        "by_language": language_summary,
        "seed_reports": seed_reports,
    }


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    kpis = report.get("kpis", {})
    totals = report.get("totals", {})

    lines = [
        "# Query Capability Benchmark",
        "",
        f"Generated: {report.get('generated_at', 'unknown')}",
        "",
        "## KPI Summary",
        "",
        f"- Seed queries: {totals.get('seed_count', 0)}",
        f"- Variant queries: {totals.get('variant_count', 0)}",
        f"- Availability: {kpis.get('availability', 0):.2%}",
        f"- Valid answer rate: {kpis.get('avg_valid_answer_rate', 0):.2%}",
        f"- Intent stability: {kpis.get('avg_intent_stability', 0):.2%}",
        f"- Robustness score: {kpis.get('robustness_score', 0):.2%}",
        f"- Projected future coverage (95% lower bound): {kpis.get('projected_future_coverage', 0):.2%}",
        "",
        "## Coverage by Capability Bucket",
        "",
        "| Bucket | Seeds | Success Rate | Valid Answer Rate | Intent Stability |",
        "|---|---:|---:|---:|---:|",
    ]

    for bucket, row in report.get("by_bucket", {}).items():
        lines.append(
            "| "
            f"{bucket} | {row.get('seed_count', 0)} | {row.get('avg_success_rate', 0):.2%} | "
            f"{row.get('avg_valid_answer_rate', 0):.2%} | {row.get('avg_intent_stability', 0):.2%} |"
        )

    lines += [
        "",
        "## Coverage by Language",
        "",
        "| Language | Seeds | Success Rate | Valid Answer Rate | Intent Stability |",
        "|---|---:|---:|---:|---:|",
    ]

    for lang, row in report.get("by_language", {}).items():
        lines.append(
            "| "
            f"{lang} | {row.get('seed_count', 0)} | {row.get('avg_success_rate', 0):.2%} | "
            f"{row.get('avg_valid_answer_rate', 0):.2%} | {row.get('avg_intent_stability', 0):.2%} |"
        )

    lines += [
        "",
        "## Interpretation",
        "",
        "- This benchmark is stronger than fixed regressions because every seed query is tested with perturbations.",
        "- The projected future coverage is conservative: it uses a 95% lower confidence bound.",
        "- Use this as a release gate and trend over time, not a one-time score.",
    ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    random.seed(args.seed)

    seeds = build_seed_set(Path(args.history_file), max_seeds=args.max_seeds)
    if not seeds:
        print("No seed queries found. Please provide a valid query history file.")
        return 2

    print("=" * 100)
    print("QUERY CAPABILITY BENCHMARK")
    print("=" * 100)
    print(f"Base URL: {args.base_url}")
    print(f"Seed queries: {len(seeds)}")
    print(f"Timeout per request: {args.timeout}s")
    print()

    reports: list[dict[str, Any]] = []

    for idx, seed in enumerate(seeds, start=1):
        print(f"[{idx:02d}/{len(seeds)}] {seed.bucket:28s} | {seed.language} | {seed.text[:80]}")
        report = evaluate_seed(
            base_url=args.base_url,
            seed=seed,
            timeout=args.timeout,
            max_steps=args.max_steps,
        )
        reports.append(report)

        print(
            " "
            f" -> success={report['success_rate']:.0%}, "
            f"valid={report['valid_answer_rate']:.0%}, "
            f"stability={report['intent_stability']:.0%}, "
            f"p95={report['elapsed_ms_p95']}ms"
        )

    aggregated = aggregate_report(reports)

    json_path = Path(args.report_json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(aggregated, indent=2, ensure_ascii=False), encoding="utf-8")

    md_path = Path(args.report_md)
    write_markdown_report(aggregated, md_path)

    kpis = aggregated.get("kpis", {})
    projected = float(kpis.get("projected_future_coverage", 0))

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Robustness score:                    {float(kpis.get('robustness_score', 0)):.2%}")
    print(f"Projected future coverage (95% LB):  {projected:.2%}")
    print(f"Availability (95% LB):               {float(kpis.get('availability_ci95_lower', 0)):.2%}")
    print(f"Reports written to: {json_path} and {md_path}")

    # Exit gate can be used in CI.
    if projected >= args.target_coverage:
        print(f"PASS: projected coverage is above target ({args.target_coverage:.0%}).")
        return 0

    print(f"FAIL: projected coverage is below target ({args.target_coverage:.0%}).")
    return 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run query capability benchmark with perturbation testing.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL of running API service.")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY_FILE), help="Path to query seed history JSON.")
    parser.add_argument("--max-seeds", type=int, default=30, help="Max number of base queries to evaluate.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout per query in seconds.")
    parser.add_argument("--max-steps", type=int, default=4, help="max_steps passed to /api/query.")
    parser.add_argument("--target-coverage", type=float, default=0.99, help="Coverage gate for CI (0-1).")
    parser.add_argument("--report-json", default=str(DEFAULT_REPORT_JSON), help="Output JSON report path.")
    parser.add_argument("--report-md", default=str(DEFAULT_REPORT_MD), help="Output Markdown report path.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
