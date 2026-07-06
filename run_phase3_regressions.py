import argparse

from run_phase2_regressions import run_all as run_phase2
from test_workflow_monitoring import run_test as run_monitoring
from test_workflow_policy_modes import run_test as run_policy_modes


def run_all() -> dict:
    results: list[dict] = []
    failures: list[str] = []

    phase2_summary = run_phase2()
    phase2_success = int(phase2_summary.get("failed") or 0) == 0
    results.append({"name": "phase2_baseline", "success": phase2_success, "result": phase2_summary})
    if not phase2_success:
        failures.append("phase2_baseline")

    for name, fn in [
        ("workflow_policy_modes", run_policy_modes),
        ("workflow_monitoring", run_monitoring),
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
    parser = argparse.ArgumentParser(description="Run Phase 3 workflow regressions")
    parser.parse_args()

    summary = run_all()
    print(summary)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())