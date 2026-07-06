from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, parse, request


def _now_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    data_bytes: bytes | None = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url=url, method=method.upper(), data=data_bytes, headers=headers)

    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status": int(resp.status),
                "url": url,
                "method": method.upper(),
                "response_text": body,
                "response_json": _try_json(body),
            }
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        return {
            "ok": False,
            "status": int(getattr(exc, "code", 0) or 0),
            "url": url,
            "method": method.upper(),
            "response_text": body,
            "response_json": _try_json(body),
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "url": url,
            "method": method.upper(),
            "response_text": "",
            "response_json": None,
            "error": str(exc),
        }


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _extract_llm_output(generate_response: dict[str, Any]) -> dict[str, Any] | None:
    response_json = generate_response.get("response_json")
    if not isinstance(response_json, dict):
        return None

    result = response_json.get("result")
    if not isinstance(result, dict):
        return None

    llm_output = result.get("llm_output")
    if isinstance(llm_output, dict):
        return llm_output

    return None


def run_regression(base_url: str, out_dir: Path, intent: str, persist: bool, temperature: float) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "base_url": base_url,
        "intent": intent,
        "persist": bool(persist),
        "temperature": float(temperature),
        "steps": [],
    }

    # Step 1: fetch generation pack
    pack_url = base_url.rstrip("/") + "/api/business-rules/solf-generation-pack?include_markdown=false"
    step1 = _http_json("GET", pack_url)
    step1_file = out_dir / "01_get_solf_generation_pack.json"
    _write_json(step1_file, {"request": {"method": "GET", "url": pack_url}, "response": step1})
    summary["steps"].append(
        {
            "name": "get_pack",
            "ok": bool(step1.get("ok")),
            "status": int(step1.get("status") or 0),
            "artifact": str(step1_file),
        }
    )

    # Step 2: generate and ingest from plain intent
    generate_url = base_url.rstrip("/") + "/api/business-rules/solf-generation-pack/generate-and-ingest"
    generate_payload = {
        "intent": intent,
        "persist": bool(persist),
        "created_by": "regression:script",
        "is_active": False,
        "default_clause_type": "resolve_policy",
        "temperature": float(temperature),
    }
    step2 = _http_json("POST", generate_url, payload=generate_payload)
    step2_file = out_dir / "02_post_generate_and_ingest.json"
    _write_json(
        step2_file,
        {
            "request": {"method": "POST", "url": generate_url, "json": generate_payload},
            "response": step2,
        },
    )
    summary["steps"].append(
        {
            "name": "generate_and_ingest",
            "ok": bool(step2.get("ok")),
            "status": int(step2.get("status") or 0),
            "artifact": str(step2_file),
        }
    )

    # Step 3: re-ingest using llm_output from step 2 for deterministic replay
    llm_output = _extract_llm_output(step2)
    if isinstance(llm_output, dict) and llm_output:
        ingest_url = base_url.rstrip("/") + "/api/business-rules/solf-generation-pack/ingest"
        ingest_payload = {
            "llm_output": llm_output,
            "persist": bool(persist),
            "created_by": "regression:script",
            "is_active": False,
            "default_clause_type": "resolve_policy",
        }
        step3 = _http_json("POST", ingest_url, payload=ingest_payload)
        step3_file = out_dir / "03_post_ingest_from_llm_output.json"
        _write_json(
            step3_file,
            {
                "request": {"method": "POST", "url": ingest_url, "json": ingest_payload},
                "response": step3,
            },
        )
        summary["steps"].append(
            {
                "name": "ingest_from_llm_output",
                "ok": bool(step3.get("ok")),
                "status": int(step3.get("status") or 0),
                "artifact": str(step3_file),
            }
        )
    else:
        summary["steps"].append(
            {
                "name": "ingest_from_llm_output",
                "ok": False,
                "status": 0,
                "artifact": "",
                "note": "Skipped: no llm_output found in step 2 response",
            }
        )

    summary_file = out_dir / "summary.json"
    _write_json(summary_file, summary)
    summary["summary_file"] = str(summary_file)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run SOLF generation API regression checks and save request/response artifacts."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="API base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--intent",
        default="Reconcile bank payments to invoices with split allocation and pause on ambiguity.",
        help="Plain-language intent for generate-and-ingest endpoint.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Persist generated SOLF artifacts (default: false).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="LLM temperature for generation call (default: 0.0).",
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="Optional output directory. Defaults to log/solf_generation_api_regression_<timestamp>.",
    )

    args = parser.parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = Path(__file__).resolve().parent / f"solf_generation_api_regression_{_now_stamp()}"

    summary = run_regression(
        base_url=str(args.base_url),
        out_dir=out_dir,
        intent=str(args.intent),
        persist=bool(args.persist),
        temperature=float(args.temperature),
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    has_failure = any(not bool(step.get("ok")) for step in summary.get("steps", []))
    return 1 if has_failure else 0


if __name__ == "__main__":
    sys.exit(main())
