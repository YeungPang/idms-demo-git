from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request


@dataclass
class QueryRunResult:
    index: int
    query: str
    ok: bool
    status_code: int
    duration_ms: int
    answer_source: str
    answer: str
    error: str


def _post_json(url: str, payload: dict, timeout_sec: int) -> tuple[int, dict, str]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(resp.getcode() or 0)
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {}
            return status, parsed, raw
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        status = int(getattr(exc, "code", 0) or 0)
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {}
        return status, parsed, raw


def _load_queries(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Query file not found: {path}")
    queries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        q = line.strip()
        if q:
            queries.append(q)
    return queries


def _run_queries(base_url: str, max_steps: int, timeout_sec: int, queries: list[str]) -> list[QueryRunResult]:
    out: list[QueryRunResult] = []
    endpoint = base_url.rstrip("/") + "/api/query"

    for idx, query in enumerate(queries, start=1):
        started = datetime.now(timezone.utc)
        status, parsed, raw = _post_json(
            url=endpoint,
            payload={"question": query, "max_steps": int(max_steps)},
            timeout_sec=int(timeout_sec),
        )
        ended = datetime.now(timezone.utc)
        duration_ms = int((ended - started).total_seconds() * 1000)

        result = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
        answer = str(result.get("answer") or "").strip()
        answer_source = str(result.get("answer_source") or "").strip()

        success_flag = bool(parsed.get("success") is True)
        ok = bool(success_flag and status >= 200 and status < 300 and answer)

        err_text = ""
        if not ok:
            detail = parsed.get("detail") if isinstance(parsed, dict) else None
            err_text = str(detail or raw or "query_failed").strip()

        out.append(
            QueryRunResult(
                index=idx,
                query=query,
                ok=ok,
                status_code=status,
                duration_ms=duration_ms,
                answer_source=answer_source,
                answer=answer,
                error=err_text,
            )
        )

    return out


def _write_reports(
    results: list[QueryRunResult],
    out_dir: Path,
    query_file: Path,
    base_url: str,
    max_steps: int,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"query_regression_run_{stamp}.json"
    md_path = out_dir / f"query_regression_run_{stamp}.md"

    passed = len([r for r in results if r.ok])
    failed = len(results) - passed

    payload = {
        "generated_at_utc": stamp,
        "base_url": base_url,
        "max_steps": int(max_steps),
        "query_file": str(query_file),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "results": [
            {
                "index": r.index,
                "query": r.query,
                "ok": r.ok,
                "status_code": r.status_code,
                "duration_ms": r.duration_ms,
                "answer_source": r.answer_source,
                "answer": r.answer,
                "error": r.error,
            }
            for r in results
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    lines: list[str] = []
    lines.append("# Query Regression Run Report")
    lines.append("")
    lines.append(f"- Generated (UTC): {stamp}")
    lines.append(f"- API base URL: {base_url}")
    lines.append(f"- Query file: {query_file}")
    lines.append(f"- max_steps: {max_steps}")
    lines.append(f"- Total: {len(results)}")
    lines.append(f"- Passed: {passed}")
    lines.append(f"- Failed: {failed}")
    lines.append("")
    lines.append("| # | Status | HTTP | ms | Source | Query |")
    lines.append("|---:|---|---:|---:|---|---|")
    for r in results:
        state = "PASS" if r.ok else "FAIL"
        source = r.answer_source.replace("|", "\\|") if r.answer_source else ""
        query = r.query.replace("|", "\\|")
        lines.append(f"| {r.index} | {state} | {r.status_code} | {r.duration_ms} | {source} | {query} |")

    lines.append("")
    lines.append("## Failed Cases")
    lines.append("")
    failed_items = [r for r in results if not r.ok]
    if not failed_items:
        lines.append("No failed queries.")
    else:
        for r in failed_items:
            lines.append(f"### {r.index}. {r.query}")
            lines.append(f"- HTTP: {r.status_code}")
            lines.append(f"- Source: {r.answer_source or '(none)'}")
            lines.append(f"- Error: {r.error or '(none)'}")
            lines.append(f"- Answer: {r.answer or '(empty)'}")
            lines.append("")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run golden query regression suite against /api/query")
    parser.add_argument(
        "--query-file",
        default="log/query_regression_golden_40_2026-07-05.txt",
        help="Path to one-query-per-line input file",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL for API server",
    )
    parser.add_argument("--max-steps", type=int, default=4, help="max_steps sent to /api/query")
    parser.add_argument("--timeout-sec", type=int, default=45, help="Per-query HTTP timeout")
    parser.add_argument(
        "--out-dir",
        default="log/regression_runs",
        help="Output directory for run artifacts",
    )
    args = parser.parse_args()

    query_file = Path(args.query_file)
    if not query_file.is_absolute():
        query_file = Path.cwd() / query_file
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = Path.cwd() / out_dir

    try:
        queries = _load_queries(query_file)
    except Exception as exc:
        print(f"Failed to load query file: {exc}", file=sys.stderr)
        return 1

    if not queries:
        print("No queries found in query file.", file=sys.stderr)
        return 1

    results = _run_queries(
        base_url=str(args.base_url),
        max_steps=int(args.max_steps),
        timeout_sec=int(args.timeout_sec),
        queries=queries,
    )
    json_path, md_path = _write_reports(
        results=results,
        out_dir=out_dir,
        query_file=query_file,
        base_url=str(args.base_url),
        max_steps=int(args.max_steps),
    )

    passed = len([r for r in results if r.ok])
    failed = len(results) - passed
    print(f"Run complete. total={len(results)} passed={passed} failed={failed}")
    print(f"JSON: {json_path}")
    print(f"MD:   {md_path}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
