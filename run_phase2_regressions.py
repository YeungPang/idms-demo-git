import argparse

from test_workflow_compliance_escalation import run_test as run_compliance_escalation_test
from test_workflow_terminal_transition_denial import run_test as run_terminal_denial_test


def run_all() -> dict:
    results: list[dict] = []
    failures: list[str] = []

    for name, fn in [
        ("compliance_escalation", run_compliance_escalation_test),
        ("terminal_transition_denial", run_terminal_denial_test),
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


def run_phase2_regression_suite() -> dict:
    """Compatibility wrapper used by consolidated regression runner."""
    return run_all()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase 2 workflow regressions")
    parser.parse_args()

    summary = run_all()
    print(summary)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())