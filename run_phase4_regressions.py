import argparse

from run_phase3_regressions import run_all as run_phase3
from test_temporal_attribute_conflict_arbitration import run_test as run_temporal_attribute_conflict_arbitration
from test_workflow_alert_reconciliation import run_test as run_alert_reconciliation


def run_all() -> dict:
    results: list[dict] = []
    failures: list[str] = []

    phase3_summary = run_phase3()
    phase3_success = int(phase3_summary.get("failed") or 0) == 0
    results.append({"name": "phase3_baseline", "success": phase3_success, "result": phase3_summary})
    if not phase3_success:
        failures.append("phase3_baseline")

    for name, fn in [
        ("temporal_attribute_conflict_arbitration", run_temporal_attribute_conflict_arbitration),
        ("workflow_alert_reconciliation", run_alert_reconciliation),
    ]:
        try:
            result = fn()
            results.append({"name": name, "success": True, "result": result})
        except AssertionError as exc:
            results.append({"name": name, "success": False, "error": str(exc)})
            failures.append(name)
        except Exception as exc:
            results.append({"name": name, "success": False, "error": f"unexpected_error: {exc}"})
            failures.append(name)

    return {
        "result": "ok" if not failures else "failed",
        "total": len(results),
        "passed": len(results) - len(failures),
        "failed": len(failures),
        "failures": failures,
        "tests": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase 4 workflow regressions")
    parser.parse_args()

    summary = run_all()
    print(summary)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
