from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib import error, request


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url=url, method=method.upper(), data=data, headers=headers)
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status": int(resp.status),
                "response_text": body,
                "response_json": _try_json(body),
            }
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        return {
            "ok": False,
            "status": int(getattr(exc, "code", 0) or 0),
            "response_text": body,
            "response_json": _try_json(body),
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "response_text": "",
            "response_json": None,
            "error": str(exc),
        }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def run(base_url: str, out_dir: Path, retry_limit: int, admin_token: str = "") -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "base_url": base_url,
        "steps": [],
    }

    query = {
        "statuses": "pending,failed",
        "limit": "50",
    }
    if admin_token:
        query["admin_token"] = admin_token

    # Step 1: list pending queue
    url_list = base_url.rstrip("/") + "/api/maintenance/tx-match/pending?" + urlencode(query)
    res1 = _http_json("GET", url_list)
    path1 = out_dir / "01_list_pending.json"
    _write(path1, {"request": {"method": "GET", "url": url_list}, "response": res1})
    summary["steps"].append({"name": "list_pending", "ok": bool(res1.get("ok")), "status": int(res1.get("status") or 0), "artifact": str(path1)})

    # Step 2: retry batch
    url_retry = base_url.rstrip("/") + "/api/maintenance/tx-match/pending/retry"
    payload_retry = {
        "statuses": ["pending", "failed"],
        "limit": int(retry_limit),
    }
    if admin_token:
        payload_retry["admin_token"] = admin_token
    res2 = _http_json("POST", url_retry, payload_retry)
    path2 = out_dir / "02_retry_batch.json"
    _write(path2, {"request": {"method": "POST", "url": url_retry, "json": payload_retry}, "response": res2})
    summary["steps"].append({"name": "retry_batch", "ok": bool(res2.get("ok")), "status": int(res2.get("status") or 0), "artifact": str(path2)})

    # Step 3: list again
    res3 = _http_json("GET", url_list)
    path3 = out_dir / "03_list_pending_after_retry.json"
    _write(path3, {"request": {"method": "GET", "url": url_list}, "response": res3})
    summary["steps"].append({"name": "list_pending_after_retry", "ok": bool(res3.get("ok")), "status": int(res3.get("status") or 0), "artifact": str(path3)})

    summary_path = out_dir / "summary.json"
    _write(summary_path, summary)
    summary["summary_file"] = str(summary_path)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run tx_match pending queue API regression checks.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--retry-limit", type=int, default=20)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--admin-token", default="")
    args = parser.parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(__file__).resolve().parent / f"tx_match_pending_queue_regression_{stamp}"

    token = str(args.admin_token or "").strip() or str(os.environ.get("IDMS_MAINTENANCE_TOKEN", "")).strip()
    summary = run(
        base_url=str(args.base_url),
        out_dir=out_dir,
        retry_limit=max(1, int(args.retry_limit)),
        admin_token=token,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    failed = any(not bool(step.get("ok")) for step in summary.get("steps", []))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
