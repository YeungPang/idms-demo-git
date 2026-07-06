"""Comprehensive regression tests for Phases  4+ (Enhanced features)."""

import datetime
import sys
from sql_db import create_tables, get_connection


def test_enhanced_alert_signals() -> dict[str, bool]:
    """Test all 15 enhanced alert signals are available and evaluable."""
    from workflow_reliability_manager import DEFAULT_ALERT_SIGNALS
    
    # Verify all 15 signals exist
    expected_signals = {
        "overdue.project_tasks",
        "overdue.compliance_actions",
        "overdue.workflow_cases",
        "events.denied_ratio",
        "transitions.denied_ratio",
        "project.resource_conflicts",
        "project.budget_overrun_count",
        "project.milestone_delays",
        "compliance.escalated_count",
        "compliance.incomplete_ratio",
        "sla.case_breach_ratio",
        "sla.approval_delay_count",
        "sla.approval_escalations",
        "system.error_rate",
        "system.event_backlog",
        "system.processing_delay_ms",
    }
    
    actual_signals = {s.metric_key for s in DEFAULT_ALERT_SIGNALS}
    
    assert len(DEFAULT_ALERT_SIGNALS) == 16, f"Expected 16 signals, got {len(DEFAULT_ALERT_SIGNALS)}"
    assert expected_signals == actual_signals, f"Signal mismatch: expected {expected_signals}, got {actual_signals}"
    
    return {"success": True, "signals_count": len(DEFAULT_ALERT_SIGNALS)}


def test_notifier_engine_subscription() -> dict[str, bool]:
    """Test subscription and notification delivery."""
    from notifier_engine import NotifierEngine
    
    conn_fn = get_connection
    notifier = NotifierEngine(db_connection_fn=conn_fn)
    
    result = notifier.subscribe(
        subscriber_ref="test_user_1",
        email="alert@example.com",
        webhook_url="https://webhook.example.com/alerts"
    )
    
    assert result.get("success") == True, "Subscription failed"
    assert "email" in result.get("channels", []), "Email channel not in subscription"
    assert "webhook" in result.get("channels", []), "Webhook channel not in subscription"
    
    return {"success": True, "subscriptions_created": 1}


def test_dashboard_api_health_summary() -> dict[str, bool]:
    """Test dashboard health summary endpoint."""
    from dashboard_api import DashboardAPI
    from workflow_monitor import WorkflowMonitor
    from workflow_reliability_manager import WorkflowReliabilityManager
    
    conn_fn = get_connection
    monitor = WorkflowMonitor(db_connection_fn=conn_fn)
    reliability = WorkflowReliabilityManager(db_connection_fn=conn_fn, monitor=monitor)
    dashboard = DashboardAPI(db_connection_fn=conn_fn, monitor=monitor, reliability_manager=reliability)
    
    summary = dashboard.get_health_summary()
    
    assert "status" in summary, "Status not in health summary"
    assert "metrics_summary" in summary, "Metrics summary missing"
    assert "key_metrics" in summary, "Key metrics missing"
    assert summary["status"] in {"healthy", "impaired", "critical"}, f"Invalid status: {summary['status']}"
    
    return {"success": True, "health_status": summary.get("status")}


def test_dashboard_api_incident_summary() -> dict[str, bool]:
    """Test dashboard incident summary endpoint."""
    from dashboard_api import DashboardAPI
    from workflow_monitor import WorkflowMonitor
    from workflow_reliability_manager import WorkflowReliabilityManager
    
    conn_fn = get_connection
    monitor = WorkflowMonitor(db_connection_fn=conn_fn)
    reliability = WorkflowReliabilityManager(db_connection_fn=conn_fn, monitor=monitor)
    dashboard = DashboardAPI(db_connection_fn=conn_fn, monitor=monitor, reliability_manager=reliability)
    
    summary = dashboard.get_incident_summary()
    
    assert "open" in summary, "Open incidents missing from summary"
    assert "acknowledged" in summary, "Acknowledged incidents missing"
    assert "resolved" in summary, "Resolved incidents missing"
    assert "total" in summary, "Total count missing"
    
    return {"success": True, "total_incidents": summary.get("total", 0)}


def test_dashboard_api_metrics_timeline() -> dict[str, bool]:
    """Test metrics timeline retrieval."""
    from dashboard_api import DashboardAPI
    from workflow_monitor import WorkflowMonitor
    from workflow_reliability_manager import WorkflowReliabilityManager
    
    conn_fn = get_connection
    monitor = WorkflowMonitor(db_connection_fn=conn_fn)
    reliability = WorkflowReliabilityManager(db_connection_fn=conn_fn, monitor=monitor)
    dashboard = DashboardAPI(db_connection_fn=conn_fn, monitor=monitor, reliability_manager=reliability)
    
    timeline = dashboard.get_metrics_timeline(metric_key="overdue.project_tasks", hours=24)
    
    assert timeline.get("metric_key") == "overdue.project_tasks", "Metric key mismatch"
    assert "timeline" in timeline, "Timeline data missing"
    assert timeline.get("hours") == 24, "Hours parameter not preserved"
    
    return {"success": True, "data_points": timeline.get("data_points", 0)}


def test_audit_compliance_record_creation() -> dict[str, bool]:
    """Test compliance record creation and tracking."""
    from audit_compliance_manager import AuditComplianceManager
    
    conn_fn = get_connection
    audit = AuditComplianceManager(db_connection_fn=conn_fn)
    
    record = audit.create_compliance_record(
        compliance_type="gdpr",
        entity_type="project_tasks",
        requirement_ref="gdpr-011",
        entity_id=1,
        severity="info",
        findings=["test finding"]
    )
    
    assert record.get("compliance_type") == "gdpr", "Compliance type mismatch"
    assert record.get("requirement_ref") == "gdpr-011", "Requirement ref mismatch"
    assert record.get("record_id") > 0, "Record ID not returned"
    
    return {"success": True, "record_id": record.get("record_id")}


def test_audit_trail_recording() -> dict[str, bool]:
    """Test audit event recording."""
    from audit_compliance_manager import AuditComplianceManager
    
    conn_fn = get_connection
    audit = AuditComplianceManager(db_connection_fn=conn_fn)
    
    event = audit.record_audit_event(
        entity_type="project_tasks",
        action="update_status",
        actor_ref="user_123",
        entity_id=42,
        reason="Manual closure"
    )
    
    assert event.get("entity_type") == "project_tasks", "Entity type mismatch"
    assert event.get("action") == "update_status", "Action mismatch"
    assert event.get("actor_ref") == "user_123", "Actor ref mismatch"
    assert event.get("audit_id") > 0, "Audit ID not returned"
    
    return {"success": True, "audit_id": event.get("audit_id")}


def test_audit_trail_retrieval() -> dict[str, bool]:
    """Test audit trail retrieval and filtering."""
    from audit_compliance_manager import AuditComplianceManager
    
    conn_fn = get_connection
    audit = AuditComplianceManager(db_connection_fn=conn_fn)
    
    # Record some events
    audit.record_audit_event("project_tasks", "create", "sys", entity_id=1)
    audit.record_audit_event("project_tasks", "update", "user_456", entity_id=1)
    
    # Retrieve trail
    trail = audit.get_audit_trail(entity_type="project_tasks", entity_id=1, hours=24)
    
    assert trail.get("count") >= 2, "Audit trail events not returned"
    assert "events" in trail, "Events list missing"
    
    return {"success": True, "event_count": trail.get("count", 0)}


def test_compliance_remediation_flow() -> dict[str, bool]:
    """Test full compliance issue remediation workflow."""
    from audit_compliance_manager import AuditComplianceManager
    
    conn_fn = get_connection
    audit = AuditComplianceManager(db_connection_fn=conn_fn)
    
    # Create compliance issue
    record = audit.create_compliance_record(
        compliance_type="sox",
        entity_type="payments",
        requirement_ref="sox-404",
        severity="critical",
        findings=["Missing approval"]
    )
    record_id = record.get("record_id", 0)
    assert record_id > 0, "Record creation failed"
    
    # Set remediation plan
    remediation = audit.remediate_compliance_issue(
        record_id=record_id,
        remediation_plan="Add approval process",
        actor_ref="compliance_officer"
    )
    assert remediation.get("record_id") == record_id, "Remediation update failed"
    
    # Mark as resolved
    resolution = audit.mark_compliance_remediated(
        record_id=record_id,
        actor_ref="compliance_officer",
        certifier_ref="auditor_1"
    )
    assert resolution.get("status") == "resolved", "Resolution status not set"
    
    return {"success": True, "remediation_complete": True}


def test_compliance_report_generation() -> dict[str, bool]:
    """Test compliance report generation."""
    from audit_compliance_manager import AuditComplianceManager
    
    conn_fn = get_connection
    audit = AuditComplianceManager(db_connection_fn=conn_fn)
    
    # Create some compliance records
    audit.create_compliance_record("iso27001", "employee_profile", "iso-access-control", severity="warning")
    audit.create_compliance_record("iso27001", "employee_profile", "iso-encryption", severity="info")
    
    # Generate report
    report = audit.generate_compliance_report("iso27001")
    
    assert report.get("compliance_type") == "iso27001", "Compliance type mismatch"
    assert "requirements" in report, "Requirements missing from report"
    assert report.get("total_records") >= 0, "Total records count missing"
    
    return {"success": True, "report_generated": True}


def test_decision_trail_recording() -> dict[str, bool]:
    """Test important decision trail tracking."""
    from audit_compliance_manager import AuditComplianceManager
    
    conn_fn = get_connection
    audit = AuditComplianceManager(db_connection_fn=conn_fn)
    
    decision = audit.record_decision(
        decision_type="approval",
        decided_by_ref="manager_1",
        for_entity_type="projects",
        for_entity_id=5,
        decision_outcome="approved",
        decision_reason="Budget approved for Q2",
        approvals_required=2
    )
    
    assert decision.get("decision_type") == "approval", "Decision type mismatch"
    assert decision.get("status") == "pending", "Decision status should be pending"
    assert decision.get("decision_id") > 0, "Decision ID not returned"
    
    # Retrieve decision trail
    trail = audit.get_decision_trail("projects", 5)
    assert trail.get("count") >= 1, "Decision not in trail"
    
    return {"success": True, "decision_recorded": True}


def run_comprehensive_regression_suite() -> dict[str, bool]:
    """Run all enhanced feature regression tests."""
    tests = [
        ("enhanced_alert_signals", test_enhanced_alert_signals),
        ("notifier_engine_subscription", test_notifier_engine_subscription),
        ("dashboard_health_summary", test_dashboard_api_health_summary),
        ("dashboard_incident_summary", test_dashboard_api_incident_summary),
        ("dashboard_metrics_timeline", test_dashboard_api_metrics_timeline),
        ("audit_compliance_record", test_audit_compliance_record_creation),
        ("audit_trail_recording", test_audit_trail_recording),
        ("audit_trail_retrieval", test_audit_trail_retrieval),
        ("compliance_remediation", test_compliance_remediation_flow),
        ("compliance_report", test_compliance_report_generation),
        ("decision_trail", test_decision_trail_recording),
    ]
    
    results = {"passed": 0, "failed": 0, "failures": [], "tests": []}
    
    for test_name, test_fn in tests:
        try:
            result = test_fn()
            results["passed"] += 1
            results["tests"].append({"name": test_name, "success": True})
            print(f"✓ {test_name}")
        except Exception as e:
            results["failed"] += 1
            failure = {"name": test_name, "error": str(e)}
            results["failures"].append(failure)
            results["tests"].append({"name": test_name, "success": False, "error": str(e)})
            print(f"✗ {test_name}: {e}")
    
    results["total"] = results["passed"] + results["failed"]
    return {"result": "ok" if results["failed"] == 0 else "error", **results}


if __name__ == "__main__":
    try:
        conn = get_connection()
        create_tables(conn, recreate=False)
        conn.close()
        
        print("\n" + "=" * 70)
        print("COMPREHENSIVE REGRESSION TEST SUITE (Phases 4+)")
        print("=" * 70 + "\n")
        
        outcome = run_comprehensive_regression_suite()
        
        print("\n" + "=" * 70)
        print(f"Results: {outcome['passed']} passed, {outcome['failed']} failed / {outcome['total']} tests")
        print("=" * 70 + "\n")
        
        if outcome["failed"] > 0:
            print("FAILURES:")
            for failure in outcome["failures"]:
                print(f"  • {failure['name']}: {failure.get('error', 'Unknown error')}")
            sys.exit(1)
        else:
            print("ALL TESTS PASSED ✓\n")
            sys.exit(0)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
