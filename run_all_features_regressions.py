"""Consolidated regression runner for all phases (2-4+)."""

import json
import os
from pathlib import Path
import subprocess
import sys


def run_phase2_baseline() -> dict:
    """Run Phase 2 baseline tests."""
    try:
        from run_phase2_regressions import run_phase2_regression_suite
        return run_phase2_regression_suite()
    except Exception as e:
        return {
            "result": "error",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "failures": [{"name": "phase2_baseline", "error": str(e)}],
            "tests": [{"name": "phase2_baseline", "success": False}],
        }


def run_phase3_baseline() -> dict:
    """Run Phase 3 baseline tests."""
    try:
        from test_workflow_monitoring import run_monitoring_regression_suite
        return run_monitoring_regression_suite()
    except Exception as e:
        return {
            "result": "error",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "failures": [{"name": "phase3_baseline", "error": str(e)}],
            "tests": [{"name": "phase3_baseline", "success": False}],
        }


def run_phase4_baseline() -> dict:
    """Run Phase 4 baseline tests."""
    try:
        from run_phase4_regressions import run_all as run_phase4
        return run_phase4()
    except Exception as e:
        return {
            "result": "error",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "failures": [{"name": "phase4_baseline", "error": str(e)}],
            "tests": [{"name": "phase4_baseline", "success": False}],
        }


def run_enhanced_features() -> dict:
    """Run comprehensive enhanced features tests."""
    try:
        from test_comprehensive_features import run_comprehensive_regression_suite
        return run_comprehensive_regression_suite()
    except Exception as e:
        return {
            "result": "error",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "failures": [{"name": "enhanced_features", "error": str(e)}],
            "tests": [{"name": "enhanced_features", "success": False}],
        }


def run_partial_interaction_types() -> dict:
    """Run regressions for interaction types that were previously partial."""
    try:
        from test_interaction_partial_types import run_partial_interaction_regression_suite
        return run_partial_interaction_regression_suite()
    except Exception as e:
        return {
            "result": "error",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "failures": [{"name": "partial_interaction_types", "error": str(e)}],
            "tests": [{"name": "partial_interaction_types", "success": False}],
        }


def run_query_capability_benchmark() -> dict:
    """Run perturbation-based capability benchmark as a robustness gate."""
    try:
        script_path = Path(__file__).with_name("run_query_capability_benchmark.py")
        report_path = Path(__file__).with_name("log") / "query_capability_benchmark.json"
        target = os.getenv("IDMS_BENCHMARK_TARGET", "0.85")
        max_seeds = os.getenv("IDMS_BENCHMARK_MAX_SEEDS", "30")
        timeout = os.getenv("IDMS_BENCHMARK_TIMEOUT", "30")

        cmd = [
            sys.executable,
            str(script_path),
            "--base-url",
            os.getenv("IDMS_API_BASE_URL", "http://127.0.0.1:8000"),
            "--max-seeds",
            str(max_seeds),
            "--timeout",
            str(timeout),
            "--target-coverage",
            str(target),
        ]

        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)

        projected = None
        robustness = None
        if report_path.exists():
            try:
                report_data = json.loads(report_path.read_text(encoding="utf-8"))
                kpis = report_data.get("kpis", {}) if isinstance(report_data, dict) else {}
                projected = kpis.get("projected_future_coverage")
                robustness = kpis.get("robustness_score")
            except Exception:
                projected = None
                robustness = None

        success = completed.returncode == 0
        details = {
            "target": float(target),
            "projected_future_coverage": projected,
            "robustness_score": robustness,
            "stdout_tail": "\n".join((completed.stdout or "").splitlines()[-8:]),
            "stderr_tail": "\n".join((completed.stderr or "").splitlines()[-8:]),
        }

        return {
            "result": "ok" if success else "error",
            "total": 1,
            "passed": 1 if success else 0,
            "failed": 0 if success else 1,
            "failures": []
            if success
            else [{"name": "query_capability_benchmark", "error": json.dumps(details, ensure_ascii=False)}],
            "tests": [{"name": "query_capability_benchmark", "success": success, "details": details}],
        }
    except Exception as e:
        return {
            "result": "error",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "failures": [{"name": "query_capability_benchmark", "error": str(e)}],
            "tests": [{"name": "query_capability_benchmark", "success": False}],
        }


def run_all_regressions() -> dict:
    """Run all regression suites in sequence."""
    print("\n" + "=" * 80)
    print("FULL REGRESSION SUITE: Phases 2 → 3 → 4 → Enhanced → Partial-Type → Capability")
    print("=" * 80 + "\n")

    all_results = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "failures": [],
        "suites": [],
    }

    # Phase 2
    print("[1/6] Running Phase 2 baseline tests...")
    phase2_result = run_phase2_baseline()
    all_results["suites"].append({"name": "phase2", "result": phase2_result})
    all_results["total"] += phase2_result.get("total", 0)
    all_results["passed"] += phase2_result.get("passed", 0)
    all_results["failed"] += phase2_result.get("failed", 0)
    all_results["failures"].extend(phase2_result.get("failures", []))
    print(
        f"  Result: {phase2_result.get('passed', 0)}/{phase2_result.get('total', 0)} passed\n"
    )

    # Phase 3
    print("[2/6] Running Phase 3 baseline tests...")
    phase3_result = run_phase3_baseline()
    all_results["suites"].append({"name": "phase3", "result": phase3_result})
    all_results["total"] += phase3_result.get("total", 0)
    all_results["passed"] += phase3_result.get("passed", 0)
    all_results["failed"] += phase3_result.get("failed", 0)
    all_results["failures"].extend(phase3_result.get("failures", []))
    print(
        f"  Result: {phase3_result.get('passed', 0)}/{phase3_result.get('total', 0)} passed\n"
    )

    # Phase 4
    print("[3/6] Running Phase 4 baseline tests...")
    phase4_result = run_phase4_baseline()
    all_results["suites"].append({"name": "phase4", "result": phase4_result})
    all_results["total"] += phase4_result.get("total", 0)
    all_results["passed"] += phase4_result.get("passed", 0)
    all_results["failed"] += phase4_result.get("failed", 0)
    all_results["failures"].extend(phase4_result.get("failures", []))
    print(
        f"  Result: {phase4_result.get('passed', 0)}/{phase4_result.get('total', 0)} passed\n"
    )

    # Enhanced Features
    print("[4/6] Running enhanced features tests...")
    enhanced_result = run_enhanced_features()
    all_results["suites"].append({"name": "enhanced_features", "result": enhanced_result})
    all_results["total"] += enhanced_result.get("total", 0)
    all_results["passed"] += enhanced_result.get("passed", 0)
    all_results["failed"] += enhanced_result.get("failed", 0)
    all_results["failures"].extend(enhanced_result.get("failures", []))
    print(
        f"  Result: {enhanced_result.get('passed', 0)}/{enhanced_result.get('total', 0)} passed\n"
    )

    # Partial Interaction Type Coverage
    print("[5/6] Running partial interaction type regressions...")
    partial_result = run_partial_interaction_types()
    all_results["suites"].append({"name": "partial_interaction_types", "result": partial_result})
    all_results["total"] += partial_result.get("total", 0)
    all_results["passed"] += partial_result.get("passed", 0)
    all_results["failed"] += partial_result.get("failed", 0)
    all_results["failures"].extend(partial_result.get("failures", []))
    print(
        f"  Result: {partial_result.get('passed', 0)}/{partial_result.get('total', 0)} passed\n"
    )

    # Capability Benchmark
    print("[6/6] Running capability benchmark (perturbation robustness)...")
    capability_result = run_query_capability_benchmark()
    all_results["suites"].append({"name": "query_capability_benchmark", "result": capability_result})
    all_results["total"] += capability_result.get("total", 0)
    all_results["passed"] += capability_result.get("passed", 0)
    all_results["failed"] += capability_result.get("failed", 0)
    all_results["failures"].extend(capability_result.get("failures", []))
    print(
        f"  Result: {capability_result.get('passed', 0)}/{capability_result.get('total', 0)} passed\n"
    )

    # Summary
    print("=" * 80)
    print(f"TOTAL: {all_results['passed']}/{all_results['total']} tests passed")
    print("=" * 80)

    if all_results["failures"]:
        print("\nFAILURES:")
        for failure in all_results["failures"]:
            print(f"  • {failure.get('name', 'unknown')}: {failure.get('error', 'Unknown error')}")

    all_results["result"] = "ok" if all_results["failed"] == 0 else "error"
    return all_results


if __name__ == "__main__":
    try:
        outcome = run_all_regressions()
        
        if outcome["result"] == "ok":
            print("\n✓ ALL REGRESSION SUITES PASSED\n")
            sys.exit(0)
        else:
            print(f"\n✗ REGRESSION FAILURES DETECTED ({outcome['failed']} failed)\n")
            sys.exit(1)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
