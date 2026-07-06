# Workflow Catalog for PM, CRM, and ERP

This catalog defines high-value workflows that are implementable in IDMS now and ready to seed/operate via the workflow registry and pipeline run runtime.

## Project Management

### 1) Project Delivery Governance
- Trigger: new milestone status report or governance checkpoint.
- Inputs: project_name, milestone_name, status_report, pm_approved, optional risk_register/issue_log/doc_refs.
- Gates: evidence check and PM approval.
- KPIs: milestone health, on-time delivery %, unresolved risk count.
- Existing template key: template::project_delivery_governance.

### 2) Change Control
- Trigger: incoming change request affecting scope/schedule/cost.
- Inputs: project_name, change_request_id, change_summary, impact_scope, impact_cost, impact_schedule, governance_approved.
- Gates: impact analysis + governance approval.
- KPIs: change cycle time, approved/rejected ratio, scope creep index.
- Proposed template key: template::project_change_control.

## CRM

### 1) Lead to Opportunity
- Trigger: newly captured lead.
- Inputs: lead_name, account_name, opportunity_stage, sales_approved, optional lead_source/contact_email/doc_refs.
- Gates: qualification review + sales approval.
- KPIs: MQL->SQL conversion rate, cycle time, win-rate uplift.
- Existing template key: template::crm_lead_pipeline.

### 2) Case and SLA Management
- Trigger: new customer issue/case.
- Inputs: case_id, account_name, case_priority, case_summary, support_approved, optional sla_hours/doc_refs.
- Gates: evidence and resolution readiness + support approval.
- KPIs: first response time, SLA breach rate, MTTR, CSAT trend.
- Proposed template key: template::crm_case_management.

## ERP

### 1) Procure to Pay
- Trigger: payment readiness check for requisition/PO/invoice flow.
- Inputs: procurement_request_id, vendor_name, total_amount, currency, finance_approved, optional purchase_order_no/invoice_no/doc_refs.
- Gates: control extraction, 3-way evidence check, finance approval.
- KPIs: blocked payment rate, exception ratio, touchless processing %.
- Existing template key: template::erp_procure_to_pay.

### 2) Order to Cash
- Trigger: sales order readiness and invoicing/collection progression.
- Inputs: sales_order_no, customer_name, order_amount, currency, credit_approved, optional invoice_no/shipment_no/doc_refs.
- Gates: credit/risk check + commercial approval.
- KPIs: DSO trend, on-time invoice %, order cycle time, dispute rate.
- Proposed template key: template::erp_order_to_cash.

## Audit and Rerun Safety

IDMS now persists explicit workflow run to document links in workflow_pipeline_run_document_link.

- Start flow linkage: input_context.doc_refs are linked at run start.
- Resume flow linkage: resume doc_refs are linked with source=resume and step_key.
- Query endpoints:
  - GET /api/pipeline-runs/{run_id}/documents
  - GET /api/pipeline-runs/by-document/{document_id}
  - GET /api/pipeline-runs/by-document/{document_id}/rerun-recommendations
  - GET /api/pipeline-runs/by-document/{document_id}/rerun-payloads
  - POST /api/pipeline-runs/by-document/{document_id}/rerun-execute

This supports operational questions like:
- Which workflows have processed document X?
- Which documents were used in run Y?
- Should we rerun workflow Z because document X changed?
