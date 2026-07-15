import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfReader

from fastapi.testclient import TestClient

from idms_api_server.application import app
from idms_api_server.routers import documents as documents_router
import document_template_pipeline


class TestDocumentGenerationAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.sample_payload = {
            "document": {
                "doc_id": 12,
                "doc_key": "Aphotonix_registration",
                "doc_path": "gs://bucket/aphotonix_registration.pdf",
                "doc_cat": "invoice",
                "doc_type": "invoice",
                "doc_date": "2026-06-27",
                "doc_desc": "Supplier invoice",
                "doc_theme": "finance",
                "keyword_text": "Aphotonix GmbH invoice",
                "identifiers_kv": {"invoice_no": "INV-2026-0001"},
                "metadata": {"user_description": "Example document"},
                "status": "active",
                "valid_from": "2026-06-27",
                "valid_until": None,
                "entry_date": "2026-06-27T10:00:00+00:00",
            },
            "parts": [
                {
                    "part_key": "supplier",
                    "part_class_name": "company",
                    "object_name": "APHOTONIX GmbH",
                    "canonical_full_name": "APHOTONIX GmbH",
                    "relationship_name": None,
                    "relationship_cat": None,
                    "metadata": {"confidence": 0.99},
                }
            ],
        }

    def test_export_document_csv(self):
        with patch("idms_api_server.routers.documents.document_generation.load_document_export_payload", return_value=self.sample_payload):
            response = self.client.get("/api/documents/12/export", params={"format": "csv"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        csv_text = response.content.decode("utf-8-sig")
        self.assertIn("Aphotonix_registration", csv_text)
        self.assertIn("supplier", csv_text)

    def test_export_document_docx(self):
        with patch("idms_api_server.routers.documents.document_generation.load_document_export_payload", return_value=self.sample_payload):
            response = self.client.get("/api/documents/12/export", params={"format": "docx"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertTrue(response.content.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("Document Export", document_xml)
        self.assertIn("APHOTONIX GmbH", document_xml)

    def test_export_document_pdf(self):
        with patch("idms_api_server.routers.documents.document_generation.load_document_export_payload", return_value=self.sample_payload):
            response = self.client.get("/api/documents/12/export", params={"format": "pdf"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertIn(b"Document Export", response.content)

    def test_download_generated_file_endpoint(self):
        generated_dir = (documents_router.PROJECT_ROOT / "generated").resolve()
        generated_dir.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=generated_dir, suffix=".txt", delete=False) as handle:
                handle.write(b"generated artifact")
                temp_path = Path(handle.name)

            rel_path = temp_path.relative_to(documents_router.PROJECT_ROOT)
            response = self.client.get(
                "/api/documents/generated-file",
                params={"path": str(rel_path), "disposition": "attachment"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"generated artifact")
            self.assertIn("attachment", response.headers.get("content-disposition", ""))
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def test_generate_invoice_from_reference_next_from_reference(self):
        reference_entry = {
            "reference_name": "acme-invoice",
            "base_payload": {
                "invoice_number": "INV-2026-0001",
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                    "reference": "00 00000 00000 00000 00026 30181",
                },
            },
        }
        generated_result = {
            "reference_name": "acme-invoice",
            "saved_path": "generated/docs/acme.pdf",
            "filename": "acme.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1234,
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents._generate_from_reference_internal", return_value=generated_result
        ) as mock_generate:
            response = self.client.post(
                "/api/documents/template-references/generate-invoice",
                json={
                    "reference_name": "acme-invoice",
                    "reference_mode": "next_from_reference",
                    "persist_reference_state": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        ref = ((body.get("result") or {}).get("generated_reference") or {}).get("storage_string")
        self.assertTrue(isinstance(ref, str) and len(ref) == 27 and ref.isdigit())

        called_payload = mock_generate.call_args.kwargs.get("payload") or {}
        self.assertEqual(str(((called_payload.get("qr_data") or {}).get("reference_type") or "")), "QRR")
        self.assertEqual(str((called_payload.get("reference") or "")), str(ref))

    def test_generate_invoice_from_reference_next_from_sequence(self):
        reference_entry = {
            "reference_name": "acme-invoice",
            "base_payload": {
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                },
            },
        }
        generated_result = {
            "reference_name": "acme-invoice",
            "saved_path": "generated/docs/acme.pdf",
            "filename": "acme.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1234,
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents._generate_from_reference_internal", return_value=generated_result
        ) as mock_generate:
            response = self.client.post(
                "/api/documents/template-references/generate-invoice",
                json={
                    "reference_name": "acme-invoice",
                    "reference_mode": "next_from_sequence",
                    "current_sequence": 26301,
                    "persist_reference_state": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        ref = ((body.get("result") or {}).get("generated_reference") or {}).get("storage_string")
        self.assertTrue(isinstance(ref, str) and len(ref) == 27 and ref.isdigit())
        self.assertEqual(((body.get("result") or {}).get("generated_reference") or {}).get("next_sequence_to_store"), 26302)

        called_payload = mock_generate.call_args.kwargs.get("payload") or {}
        self.assertEqual(str((called_payload.get("reference") or "")), str(ref))

    def test_generate_invoice_from_reference_uses_atomic_reservation_when_enabled(self):
        reference_entry = {
            "reference_name": "acme-invoice",
            "base_payload": {
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                },
            },
        }
        generated_result = {
            "reference_name": "acme-invoice",
            "saved_path": "generated/docs/acme.pdf",
            "filename": "acme.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1234,
        }
        reserved = {
            "storage_string": "000000000000000000000263019",
            "visual_string": "00 00000 00000 00000 00026 3019",
            "mode": "next_from_sequence",
            "persisted": True,
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents._reserve_generated_reference_atomic", return_value=reserved
        ) as mock_reserve, patch(
            "idms_api_server.routers.documents._generate_from_reference_internal", return_value=generated_result
        ):
            response = self.client.post(
                "/api/documents/template-references/generate-invoice",
                json={
                    "reference_name": "acme-invoice",
                    "reference_mode": "next_from_sequence",
                    "current_sequence": 26301,
                    "persist_reference_state": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_reserve.called)

    def test_generate_invoice_from_reference_runs_linked_workflow_each_time(self):
        reference_entry = {
            "reference_name": "balm_invoice",
            "doc_name": "Balm Invoice",
            "document_kind": "invoice",
            "linked_workflow_key": "template::balm_invoice_generation",
            "linked_workflow_id": 12,
            "linked_workflow_version_id": 34,
            "base_payload": {
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                    "reference": "00 00000 00000 00000 00026 30181",
                },
            },
        }
        generated_result = {
            "reference_name": "balm_invoice",
            "saved_path": "generated/docs/balm.pdf",
            "filename": "balm.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1234,
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_registry_entry_by_key",
            return_value={
                "workflow_id": 12,
                "workflow_key": "template::balm_invoice_generation",
                "workflow_name": "Balm Invoice Generation",
                "is_active": True,
            },
        ), patch(
            "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_active_version_by_key",
            return_value={
                "workflow_id": 12,
                "workflow_key": "template::balm_invoice_generation",
                "workflow_version_id": 34,
                "has_active_version": True,
            },
        ), patch(
            "idms_api_server.routers.documents.WorkflowPipelineExecutor.start_pipeline_run",
            return_value={
                "run_id": 901,
                "run_status": "completed",
                "output_context": {
                    "payload": {
                        "service_period": "01.04.2026 bis 30.06.2026",
                        "invoice_date": "2026-07-11",
                    },
                    "qr_data": {
                        "qr_iban": "CH4431999123000889012",
                        "currency": "CHF",
                        "reference": "00 00000 00000 00000 00026 30181",
                    },
                    "reference_policy": {
                        "mode": "next_from_reference_fixed_segments",
                        "fixed_segments": [{"start": 21, "value": "26"}],
                        "derived_reference_fields": [
                            {
                                "field": "RECHNUNG",
                                "source": "payload_digits",
                                "start": 1,
                                "end": 26,
                                "trim_leading_zeros": True,
                            }
                        ],
                    },
                },
            },
        ) as mock_start, patch(
            "idms_api_server.routers.documents._generate_from_reference_internal",
            return_value=generated_result,
        ) as mock_generate:
            response = self.client.post(
                "/api/documents/template-references/generate-invoice",
                json={
                    "reference_name": "balm_invoice",
                    "persist_reference_state": False,
                    "payload": {"service_period": "01.04.2026 bis 30.06.2026"},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_start.called)
        called_payload = mock_generate.call_args.kwargs.get("payload") or {}
        self.assertEqual(called_payload.get("service_period"), "01.04.2026 bis 30.06.2026")
        self.assertEqual(called_payload.get("invoice_date"), "2026-07-11")
        self.assertEqual(str(called_payload.get("RECHNUNG") or ""), str((called_payload.get("reference") or ""))[:26].lstrip("0") or "0")
        self.assertEqual(str(((called_payload.get("reference") or "")[20:22])), "26")

        body = response.json()
        workflow_run = ((body.get("result") or {}).get("workflow_run") or {})
        self.assertEqual(int(workflow_run.get("run_id") or 0), 901)

    def test_generate_invoice_from_reference_surfaces_workflow_pause(self):
        reference_entry = {
            "reference_name": "balm_invoice",
            "doc_name": "Balm Invoice",
            "document_kind": "invoice",
            "linked_workflow_key": "template::balm_invoice_generation",
            "linked_workflow_id": 12,
            "linked_workflow_version_id": 34,
            "base_payload": {
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                    "reference": "00 00000 00000 00000 00026 30181",
                },
            },
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_registry_entry_by_key",
            return_value={
                "workflow_id": 12,
                "workflow_key": "template::balm_invoice_generation",
                "workflow_name": "Balm Invoice Generation",
                "is_active": True,
            },
        ), patch(
            "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_active_version_by_key",
            return_value={
                "workflow_id": 12,
                "workflow_key": "template::balm_invoice_generation",
                "workflow_version_id": 34,
                "has_active_version": True,
            },
        ), patch(
            "idms_api_server.routers.documents.WorkflowPipelineExecutor.start_pipeline_run",
            return_value={
                "run_id": 902,
                "run_status": "paused",
                "paused_at_step_key": "require_service_period",
            },
        ):
            response = self.client.post(
                "/api/documents/template-references/generate-invoice",
                json={
                    "reference_name": "balm_invoice",
                    "persist_reference_state": False,
                    "payload": {},
                },
            )

        self.assertEqual(response.status_code, 409)
        detail = (response.json() or {}).get("detail") or {}
        self.assertTrue(detail.get("workflow_paused"))
        self.assertEqual(detail.get("paused_at_step_key"), "require_service_period")

    def test_generate_invoice_from_reference_applies_generic_derived_reference_fields(self):
        reference_entry = {
            "reference_name": "generic_report",
            "doc_name": "Generic Report",
            "document_kind": "invoice",
            "base_payload": {
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                    "reference": "00 00000 00000 00000 00026 30181",
                },
            },
        }
        generated_result = {
            "reference_name": "generic_report",
            "saved_path": "generated/docs/report.pdf",
            "filename": "report.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1234,
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents.WorkflowPipelineExecutor.start_pipeline_run",
            return_value={
                "run_id": 903,
                "run_status": "completed",
                "output_context": {
                    "payload": {},
                    "qr_data": {
                        "qr_iban": "CH4431999123000889012",
                        "currency": "CHF",
                        "reference": "00 00000 00000 00000 00026 30181",
                    },
                    "reference_policy": {
                        "mode": "next_from_reference_fixed_segments",
                        "fixed_segments": [{"start": 21, "value": "26"}],
                        "derived_reference_fields": [
                            {"field": "customer_reference_payload", "source": "payload_digits", "start": 1, "end": 26, "trim_leading_zeros": False},
                            {"field": "short_reference", "source": "payload_digits", "start": 21, "end": 22, "trim_leading_zeros": False},
                        ],
                    },
                },
            },
        ), patch(
            "idms_api_server.routers.documents._validate_template_linked_workflow_for_generation",
            return_value={
                "linked_workflow_key": "template::generic_reference",
                "linked_workflow_version_id": 55,
            },
        ), patch(
            "idms_api_server.routers.documents._generate_from_reference_internal",
            return_value=generated_result,
        ) as mock_generate:
            response = self.client.post(
                "/api/documents/template-references/generate-invoice",
                json={
                    "reference_name": "generic_report",
                    "persist_reference_state": False,
                    "payload": {},
                },
            )

        self.assertEqual(response.status_code, 200)
        called_payload = mock_generate.call_args.kwargs.get("payload") or {}
        self.assertEqual(str(called_payload.get("short_reference") or ""), "26")
        self.assertEqual(len(str(called_payload.get("customer_reference_payload") or "")), 26)

    def test_validate_reference_endpoint_valid(self):
        response = self.client.post(
            "/api/documents/template-references/validate-reference",
            json={"reference": "00 00000 00000 00000 00026 30181"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        self.assertTrue(body.get("valid"))
        self.assertEqual(len(str(body.get("storage_string") or "")), 27)

    def test_validate_reference_endpoint_invalid(self):
        response = self.client.post(
            "/api/documents/template-references/validate-reference",
            json={"reference": "123"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        self.assertFalse(body.get("valid"))

    def test_template_reference_field_schema_endpoint(self):
        reference_entry = {
            "reference_name": "quote-template",
            "document_kind": "quotation",
            "template_format": "docx",
            "base_payload": {
                "quotation_number": "Q-1001",
                "customer_name": "ACME AG",
                "line_items": [
                    {
                        "description": "Widget A",
                        "quantity": 2,
                        "unit_price": 10,
                    }
                ],
            },
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry):
            response = self.client.get("/api/documents/template-references/quote-template/field-schema")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        result = body.get("result") or {}
        self.assertEqual(result.get("reference_name"), "quote-template")
        self.assertIn("quotation_number", result.get("discovered_fields") or [])
        self.assertIn("line_items", result.get("discovered_fields") or [])
        self.assertIn("description", ((result.get("line_items") or {}).get("columns") or []))

    def test_template_reference_field_schema_includes_linked_workflow_derived_fields(self):
        reference_entry = {
            "reference_name": "balm_invoice_test",
            "document_kind": "invoice",
            "template_format": "pdf",
            "linked_workflow_key": "workflow::balm_invoice_wf",
            "base_payload": {
                "qr_data": {"reference_type": "QRR"},
            },
        }
        mock_connection = SimpleNamespace(close=lambda: None)

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_active_version_by_key",
            return_value={"workflow_version_id": 31, "has_active_version": True},
        ), patch("idms_api_server.routers.documents.object_db.get_connection", return_value=mock_connection), patch(
            "idms_api_server.routers.documents.object_db.list_solf_workflow_steps",
            return_value=[
                {
                    "config": {
                        "required_context_fields": ["service_period"],
                        "context_to_payload_fields": {"service_period": "service_period"},
                        "date_payload_fields": ["invoice_date", "issue_date", "document_date", "date"],
                    }
                }
            ],
        ):
            response = self.client.get("/api/documents/template-references/balm_invoice_test/field-schema")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        result = body.get("result") or {}
        discovered = result.get("discovered_fields") or []
        suggested = result.get("suggested_override_fields") or []
        self.assertIn("service_period", discovered)
        self.assertIn("invoice_date", discovered)
        self.assertIn("date", discovered)
        self.assertIn("service_period", suggested)
        linked_schema = result.get("linked_workflow_schema") or {}
        self.assertTrue(linked_schema.get("has_linked_workflow"))
        self.assertEqual(linked_schema.get("workflow_key"), "workflow::balm_invoice_wf")
        self.assertEqual(int(linked_schema.get("workflow_version_id") or 0), 31)

    def test_template_reference_pdf_analysis_endpoint_returns_inference_artifacts(self):
        reference_entry = {
            "reference_name": "balm_invoice_test",
            "document_kind": "invoice",
            "template_format": "pdf",
            "stored_path": __file__,
            "render_mode": "preserve_template",
            "base_payload": {
                "field_rules": {
                    "field_labels": {
                        "service_period": "Wartung/Serv. Periode:",
                    }
                }
            },
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "pathlib.Path.exists", return_value=True,
        ), patch("pathlib.Path.read_bytes", return_value=b"pdf-bytes"), patch(
            "idms_api_server.routers.documents.document_template_pipeline.extract_pdf_text_spans",
            return_value=[{"text": "Wartung/Serv. Periode:"}],
        ), patch(
            "idms_api_server.routers.documents.document_template_pipeline.infer_pdf_rewrite_layout",
            return_value={"service_period": {"page": 1, "x": 100}},
        ), patch(
            "idms_api_server.routers.documents.document_template_pipeline.infer_pdf_qr_bill_region",
            return_value={"page": 1, "x": 10, "y": 20, "w": 300, "h": 100},
        ), patch(
            "idms_api_server.routers.documents.document_template_pipeline.infer_qr_data_from_pdf_text",
            return_value={"reference": "00 00000 00000 00000 00026 30181"},
        ):
            response = self.client.post(
                "/api/documents/template-references/balm_invoice_test/analyze-pdf",
                json={"payload": {"service_period": "01.04.2026 bis 30.06.2026"}},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        result = body.get("result") or {}
        self.assertEqual(str(result.get("render_mode") or ""), "preserve_template")
        self.assertIn("service_period", result.get("inferred_rewrite_layout") or {})
        self.assertIn("llm_overwrite_suggestions", result)
        self.assertIn("workflow_overwrite_targets", result)
        self.assertEqual(((result.get("inferred_qr_defaults") or {}).get("reference") or ""), "00 00000 00000 00000 00026 30181")

    def test_template_reference_pdf_analysis_uses_workflow_overwrite_targets_for_llm_suggestions(self):
        reference_entry = {
            "reference_name": "balm_invoice_test",
            "document_kind": "invoice",
            "template_format": "pdf",
            "stored_path": __file__,
            "linked_workflow_key": "workflow::balm_invoice_wf",
            "base_payload": {
                "field_rules": {
                    "field_labels": {},
                }
            },
        }
        mock_connection = SimpleNamespace(close=lambda: None)

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "pathlib.Path.exists", return_value=True,
        ), patch("pathlib.Path.read_bytes", return_value=b"pdf-bytes"), patch(
            "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_active_version_by_key",
            return_value={"workflow_version_id": 77, "has_active_version": True},
        ), patch("idms_api_server.routers.documents.object_db.get_connection", return_value=mock_connection), patch(
            "idms_api_server.routers.documents.object_db.list_solf_workflow_steps",
            return_value=[
                {
                    "step_key": "prepare_generation",
                    "config": {
                        "required_context_fields": ["service_period"],
                        "context_to_payload_fields": {"service_period": "service_period"},
                        "date_payload_fields": ["issue_date"],
                    },
                }
            ],
        ), patch(
            "idms_api_server.routers.documents.document_template_pipeline.extract_pdf_text_spans",
            return_value=[{"text": "Wartung/Serv. Periode:"}, {"text": "Datum:"}],
        ), patch(
            "idms_api_server.routers.documents.document_template_pipeline.infer_pdf_rewrite_layout",
            return_value={},
        ), patch(
            "idms_api_server.routers.documents.document_template_pipeline.infer_pdf_qr_bill_region",
            return_value=None,
        ), patch(
            "idms_api_server.routers.documents.document_template_pipeline.infer_qr_data_from_pdf_text",
            return_value={},
        ), patch(
            "idms_api_server.routers.documents._suggest_overwrite_labels_with_llm",
            return_value={
                "used_llm": True,
                "suggestions": [
                    {
                        "field": "service_period",
                        "chosen_label": "Wartung/Serv. Periode:",
                        "label_candidates": ["Wartung/Serv. Periode:"],
                        "confidence": 0.93,
                        "reason": "llm_match",
                    }
                ],
                "reason": "ok",
            },
        ) as mock_suggest:
            response = self.client.post(
                "/api/documents/template-references/balm_invoice_test/analyze-pdf",
                json={"payload": {"service_period": "01.04.2026 bis 30.06.2026"}},
            )

        self.assertEqual(response.status_code, 200)
        result = (response.json() or {}).get("result") or {}
        overwrite_fields = (((result.get("workflow_overwrite_targets") or {}).get("fields")) or [])
        self.assertIn("service_period", overwrite_fields)
        self.assertIn("issue_date", overwrite_fields)
        self.assertTrue(((result.get("llm_overwrite_suggestions") or {}).get("used_llm")))
        self.assertTrue(mock_suggest.called)

    def test_template_reference_field_schema_includes_derived_reference_fields(self):
        reference_entry = {
            "reference_name": "balm_invoice",
            "document_kind": "invoice",
            "template_format": "pdf",
            "stored_path": __file__,
            "linked_workflow_key": "workflow::balm_invoice_wf",
            "base_payload": {},
        }
        mock_connection = SimpleNamespace(close=lambda: None)

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_active_version_by_key",
            return_value={"workflow_version_id": 77, "has_active_version": True},
        ), patch("idms_api_server.routers.documents.object_db.get_connection", return_value=mock_connection), patch(
            "idms_api_server.routers.documents.object_db.list_solf_workflow_steps",
            return_value=[
                {
                    "step_key": "prepare_balm_invoice_generation",
                    "config": {
                        "reference_policy": {
                            "derived_reference_fields": [
                                {"field": "RECHNUNG", "source": "payload_digits", "start": 1, "end": 26, "trim_leading_zeros": True}
                            ]
                        },
                        "date_payload_fields": ["issue_date"],
                    },
                }
            ],
        ):
            response = self.client.get("/api/documents/template-references/balm_invoice/field-schema")

        self.assertEqual(response.status_code, 200)
        result = (response.json() or {}).get("result") or {}
        linked_schema = result.get("linked_workflow_schema") or {}
        self.assertIn("RECHNUNG", linked_schema.get("derived_fields") or [])
        self.assertIn("RECHNUNG", linked_schema.get("derived_overwrite_fields") or [])

    def test_apply_template_field_label_suggestions_persists_to_registry(self):
        registry = [
            {
                "reference_name": "balm_invoice_test",
                "template_format": "pdf",
                "base_payload": {
                    "field_rules": {
                        "field_labels": {
                            "service_period": "Wartung/Serv. Periode:",
                        }
                    }
                },
            }
        ]

        with patch(
            "idms_api_server.routers.documents._load_template_reference_registry",
            return_value=registry,
        ), patch(
            "idms_api_server.routers.documents._save_template_reference_registry",
        ) as mock_save, patch(
            "idms_api_server.routers.documents._load_linked_workflow_schema_fields",
            return_value={
                "has_linked_workflow": True,
                "workflow_key": "workflow::balm_invoice_wf",
                "workflow_version_id": 31,
                "fields": ["service_period", "issue_date"],
                "overwrite_targets": {
                    "fields": ["service_period", "issue_date"],
                    "details": [],
                },
            },
        ):
            response = self.client.post(
                "/api/documents/template-references/balm_invoice_test/apply-field-label-suggestions",
                json={
                    "suggestions": [
                        {
                            "field": "service_period",
                            "chosen_label": "Wartung/Serv. Periode:",
                            "confidence": 0.96,
                        },
                        {
                            "field": "issue_date",
                            "chosen_label": "Datum:",
                            "confidence": 0.91,
                        },
                        {
                            "field": "ignored_non_workflow",
                            "chosen_label": "Should not persist",
                            "confidence": 0.99,
                        },
                    ],
                    "min_confidence": 0.5,
                    "only_workflow_targets": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json() or {}
        result = payload.get("result") or {}
        self.assertIn("issue_date", result.get("applied_fields") or [])
        self.assertIn("ignored_non_workflow", result.get("skipped_fields") or [])

        self.assertTrue(mock_save.called)
        saved_registry = mock_save.call_args.args[0]
        saved_entry = saved_registry[0]
        labels = (((saved_entry.get("base_payload") or {}).get("field_rules") or {}).get("field_labels") or {})
        self.assertEqual(str(labels.get("issue_date") or ""), "Datum:")

    def test_generate_invoice_from_reference_override_fields_allows_only_selected_paths(self):
        reference_entry = {
            "reference_name": "acme-invoice",
            "base_payload": {
                "invoice_date": "2026-06-01",
                "customer_name": "Original Customer",
                "recipient": {
                    "address": {
                        "city": "Zurich",
                    }
                },
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                },
            },
        }
        generated_result = {
            "reference_name": "acme-invoice",
            "saved_path": "generated/docs/acme.pdf",
            "filename": "acme.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1234,
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents._generate_from_reference_internal", return_value=generated_result
        ) as mock_generate:
            response = self.client.post(
                "/api/documents/template-references/generate-invoice",
                json={
                    "reference_name": "acme-invoice",
                    "reference_mode": "next_from_sequence",
                    "current_sequence": 10,
                    "persist_reference_state": False,
                    "override_fields": ["invoice_date", "recipient.address.city"],
                    "payload": {
                        "invoice_date": "2026-07-09",
                        "customer_name": "Should Not Apply",
                        "recipient": {
                            "address": {
                                "city": "Basel",
                            }
                        },
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        called_payload = mock_generate.call_args.kwargs.get("payload") or {}
        self.assertEqual(called_payload.get("invoice_date"), "2026-07-09")
        self.assertEqual(called_payload.get("customer_name"), "Original Customer")
        self.assertEqual((((called_payload.get("recipient") or {}).get("address") or {}).get("city")), "Basel")

    def test_generate_invoice_from_reference_falls_back_to_template_sequence_seed_when_seed_missing(self):
        reference_entry = {
            "reference_name": "balm_invoice_test",
            "source_filename": "Wartung Rechnung 263018.pdf",
            "base_payload": {
                "source_reference": "Wartung Rechnung 263018.pdf",
                "qr_data": {
                    "reference_type": "QRR",
                },
            },
        }
        generated_result = {
            "reference_name": "balm_invoice_test",
            "saved_path": "generated/docs/balm_invoice_test.pdf",
            "filename": "balm_invoice_test.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1024,
        }

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents._generate_from_reference_internal", return_value=generated_result
        ) as mock_generate:
            response = self.client.post(
                "/api/documents/template-references/generate-invoice",
                json={
                    "reference_name": "balm_invoice_test",
                    "persist_reference_state": False,
                    "payload": {
                        "service_period": "01.04.2026 bis 30.06.2026",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        called_payload = mock_generate.call_args.kwargs.get("payload") or {}
        called_qr = dict(called_payload.get("qr_data") if isinstance(called_payload.get("qr_data"), dict) else {})
        generated_reference = str(called_qr.get("reference") or "")
        self.assertEqual(len(generated_reference), 27)

    def test_generate_from_reference_uses_preserve_template_render_mode(self):
        reference_entry = {
            "reference_name": "balm_invoice_test",
            "document_kind": "invoice",
            "template_format": "pdf",
            "stored_path": __file__,
            "render_mode": "preserve_template",
            "base_payload": {},
        }
        target_path = Path(tempfile.gettempdir()) / "balm-preserve-template.pdf"

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents._validate_template_linked_workflow_for_generation",
            return_value=None,
        ), patch("pathlib.Path.exists", return_value=True), patch("pathlib.Path.read_bytes", return_value=b"pdf-bytes"), patch(
            "idms_api_server.routers.documents.document_template_pipeline.render_template_document",
            return_value=(b"rendered", "balm.pdf", "application/pdf"),
        ) as mock_render, patch(
            "idms_api_server.routers.documents._resolve_output_file_path",
            return_value=target_path,
        ):
            result = documents_router._generate_from_reference_internal(
                reference_name="balm_invoice_test",
                specification="",
                output_format="pdf",
                document_kind="invoice",
                payload={},
                override_fields=[],
                output_path="",
                output_dir="",
            )

        self.assertEqual(str(result.get("render_mode") or ""), "preserve_template")
        self.assertEqual(mock_render.call_args.kwargs.get("render_mode"), "preserve_template")

    def test_generate_from_reference_spreadsheet_defaults_filename_to_template_and_generation_date(self):
        reference_entry = {
            "reference_name": "sales_template",
            "document_kind": "invoice",
            "template_format": "xlsx",
            "stored_path": __file__,
            "base_payload": {},
        }
        target_path = Path(tempfile.gettempdir()) / "sales-template-2026-07-13.csv"

        with patch("idms_api_server.routers.documents._find_template_reference", return_value=reference_entry), patch(
            "idms_api_server.routers.documents._validate_template_linked_workflow_for_generation",
            return_value=None,
        ), patch("pathlib.Path.exists", return_value=True), patch(
            "idms_api_server.routers.documents._resolve_output_file_path",
            return_value=target_path,
        ):
            result = documents_router._generate_from_reference_internal(
                reference_name="sales_template",
                specification="",
                output_format="csv",
                document_kind="invoice",
                payload={"generation_date": "2026-07-13", "amount": 100},
                override_fields=[],
                output_path="",
                output_dir="",
            )

        self.assertEqual(str(result.get("filename") or ""), "sales_template-2026-07-13.csv")

    def test_pdf_overlay_erase_box_replaces_visible_text(self):
        if document_template_pipeline.canvas is None:
            self.skipTest("reportlab not installed")

        from reportlab.pdfgen import canvas as rl_canvas

        base = io.BytesIO()
        pdf = rl_canvas.Canvas(base, pagesize=(300, 300))
        pdf.setFont("Helvetica", 12)
        pdf.drawString(40, 200, "OLDVALUE")
        pdf.save()

        rendered, _filename, _media_type = document_template_pipeline.render_template_document(
            template_bytes=base.getvalue(),
            template_format="pdf",
            output_format="pdf",
            document_kind="invoice",
            payload={"service_period": "NEWVALUE"},
            qr_data=None,
            layout={
                "service_period": {
                    "page": 1,
                    "x": 40,
                    "y": 200,
                    "font_size": 12,
                    "erase_box": {"x": 38, "y": 196, "w": 80, "h": 16},
                }
            },
            render_mode="preserve_template",
            template_name="test.pdf",
        )

        text = "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(rendered)).pages)
        self.assertIn("NEWVALUE", text)
        if getattr(document_template_pipeline, "fitz", None) is not None:
            self.assertNotIn("OLDVALUE", text)

    def test_infer_pdf_rewrite_layout_from_field_labels(self):
        if document_template_pipeline.canvas is None:
            self.skipTest("reportlab not installed")

        from reportlab.pdfgen import canvas as rl_canvas

        base = io.BytesIO()
        pdf = rl_canvas.Canvas(base, pagesize=(400, 300))
        pdf.setFont("Helvetica", 12)
        pdf.drawString(40, 220, "Wartung/Serv. Periode:")
        pdf.drawString(180, 220, "01.01.2026 bis 31.03.2026")
        pdf.drawString(240, 200, "Datum:")
        pdf.drawString(300, 200, "20.01.2026")
        pdf.save()

        layout = document_template_pipeline.infer_pdf_rewrite_layout(
            template_bytes=base.getvalue(),
            payload={
                "service_period": "01.04.2026 bis 30.06.2026",
                "issue_date": "2026-07-13",
            },
            template_payload={
                "field_rules": {
                    "field_labels": {
                        "service_period": "Wartung/Serv. Periode:",
                        "issue_date": "Datum:",
                    }
                }
            },
        )

        self.assertIn("service_period", layout)
        self.assertIn("issue_date", layout)
        self.assertGreater(float(layout["service_period"].get("x") or 0), 100.0)
        self.assertGreater(float(layout["issue_date"].get("x") or 0), 260.0)

    def test_generate_invoice_from_instruction_runs_solf_and_invoice_generation(self):
        reference_entry = {
            "reference_name": "balm-invoice",
            "base_payload": {
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                    "reference": "00 00000 00000 00000 00026 30181",
                },
            },
        }
        generated_result = {
            "reference_name": "balm-invoice",
            "saved_path": "generated/docs/balm.pdf",
            "filename": "balm.pdf",
            "media_type": "application/pdf",
            "size_bytes": 3333,
            "qr_generation": {
                "backend": "qrbill",
                "mode": "direct",
                "ref_type": "QRR",
            },
        }
        solf_result = {
            "processed": {
                "persisted": True,
                "rule_name": "balm_invoice_runtime_workflow",
            }
        }

        with patch(
            "idms_api_server.routers.documents.business_rules_engine.generate_and_process_solf_from_intent",
            return_value=solf_result,
        ) as mock_solf, patch(
            "idms_api_server.routers.documents._find_template_reference",
            return_value=reference_entry,
        ), patch(
            "idms_api_server.routers.documents._generate_from_reference_internal",
            return_value=generated_result,
        ):
            response = self.client.post(
                "/api/documents/template-references/generate-invoice-from-instruction",
                json={
                    "instruction_text": "Generate a new balm_invoice for maintenance/service period 01.04.2026 bis 30.06.2026.",
                    "persist_generated_solf": True,
                    "invoice_request": {
                        "reference_name": "balm-invoice",
                        "reference_mode": "next_from_reference",
                        "persist_reference_state": False,
                        "payload": {
                            "service_period": "01.04.2026 bis 30.06.2026",
                        },
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        result = body.get("result") or {}
        self.assertEqual(str(result.get("instruction_text") or ""), "Generate a new balm_invoice for maintenance/service period 01.04.2026 bis 30.06.2026.")
        self.assertIn("solf_generation", result)
        self.assertEqual(((result.get("qr_generation") or {}).get("backend") or ""), "qrbill")
        self.assertIn("invoice_generation", result)
        self.assertEqual((((result.get("invoice_generation") or {}).get("qr_generation") or {}).get("backend") or ""), "qrbill")
        self.assertTrue(mock_solf.called)

    def test_generate_invoice_from_instruction_requires_instruction_text(self):
        response = self.client.post(
            "/api/documents/template-references/generate-invoice-from-instruction",
            json={
                "instruction_text": "",
                "invoice_request": {
                    "reference_name": "balm-invoice",
                    "reference_mode": "next_from_sequence",
                    "current_sequence": 12,
                    "persist_reference_state": False,
                },
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_generate_invoice_from_instruction_reuses_existing_rule_by_name(self):
        reference_entry = {
            "reference_name": "balm_invoice",
            "base_payload": {
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                    "reference": "00 00000 00000 00000 00026 30181",
                },
            },
        }
        generated_result = {
            "reference_name": "balm_invoice",
            "saved_path": "generated/docs/balm.pdf",
            "filename": "balm.pdf",
            "media_type": "application/pdf",
            "size_bytes": 4444,
        }
        existing_rules = [
            {"rule_id": 51, "rule_name": "balm_invoice", "is_active": True},
        ]

        with patch(
            "idms_api_server.routers.documents.business_rules_engine.list_business_rules",
            return_value=existing_rules,
        ), patch(
            "idms_api_server.routers.documents.business_rules_engine.generate_and_process_solf_from_intent",
        ) as mock_generate_solf, patch(
            "idms_api_server.routers.documents._find_template_reference",
            return_value=reference_entry,
        ), patch(
            "idms_api_server.routers.documents._generate_from_reference_internal",
            return_value=generated_result,
        ):
            response = self.client.post(
                "/api/documents/template-references/generate-invoice-from-instruction",
                json={
                    "instruction_text": "Generate a new balm_invoice.",
                    "reuse_existing_solf_rule": True,
                    "existing_rule_name": "balm_invoice",
                    "invoice_request": {
                        "reference_name": "balm_invoice",
                        "reference_mode": "next_from_reference",
                        "persist_reference_state": False,
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        result = body.get("result") or {}
        solf_generation = result.get("solf_generation") or {}
        self.assertTrue(bool(solf_generation.get("reused")))
        self.assertEqual(int(solf_generation.get("rule_id") or 0), 51)
        self.assertFalse(mock_generate_solf.called)

    def test_generate_invoice_from_instruction_generates_when_no_existing_rule(self):
        reference_entry = {
            "reference_name": "balm_invoice",
            "base_payload": {
                "qr_data": {
                    "qr_iban": "CH4431999123000889012",
                    "currency": "CHF",
                    "reference": "00 00000 00000 00000 00026 30181",
                },
            },
        }
        generated_result = {
            "reference_name": "balm_invoice",
            "saved_path": "generated/docs/balm.pdf",
            "filename": "balm.pdf",
            "media_type": "application/pdf",
            "size_bytes": 5555,
        }
        solf_result = {"processed": {"persisted": True, "rule_name": "balm_invoice_runtime"}}

        with patch(
            "idms_api_server.routers.documents.business_rules_engine.list_business_rules",
            return_value=[],
        ), patch(
            "idms_api_server.routers.documents.business_rules_engine.generate_and_process_solf_from_intent",
            return_value=solf_result,
        ) as mock_generate_solf, patch(
            "idms_api_server.routers.documents._find_template_reference",
            return_value=reference_entry,
        ), patch(
            "idms_api_server.routers.documents._generate_from_reference_internal",
            return_value=generated_result,
        ):
            response = self.client.post(
                "/api/documents/template-references/generate-invoice-from-instruction",
                json={
                    "instruction_text": "Generate a new balm_invoice.",
                    "reuse_existing_solf_rule": True,
                    "existing_rule_name": "balm_invoice_missing",
                    "invoice_request": {
                        "reference_name": "balm_invoice",
                        "reference_mode": "next_from_reference",
                        "persist_reference_state": False,
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_generate_solf.called)

    def test_ingest_template_reference_with_linked_workflow_key_persists_linkage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(documents_router, "TEMPLATE_REFERENCE_DIR", Path(temp_dir)), patch(
                "idms_api_server.routers.documents._load_template_reference_registry",
                return_value=[],
            ), patch(
                "idms_api_server.routers.documents._save_template_reference_registry"
            ) as mock_save, patch(
                "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_registry_entry_by_key",
                return_value={
                    "workflow_id": 12,
                    "workflow_key": "template::balm_invoice_generation",
                    "workflow_name": "Balm Invoice Generation",
                    "is_active": True,
                },
            ), patch(
                "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_active_version_by_key",
                return_value={
                    "workflow_id": 12,
                    "workflow_key": "template::balm_invoice_generation",
                    "workflow_version_id": 34,
                    "has_active_version": True,
                },
            ):
                response = self.client.post(
                    "/api/documents/template-references/ingest",
                    data={
                        "reference_name": "balm_invoice",
                        "doc_name": "Balm Invoice",
                        "document_kind": "invoice",
                        "description": "Balm invoice template",
                        "payload_json": '{"seller_name":"BALM AG"}',
                        "workflow_key": "template::balm_invoice_generation",
                    },
                    files={
                        "reference_file": (
                            "balm_invoice.docx",
                            b"fake-template-content",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("success"))
        result = body.get("result") or {}
        self.assertEqual(int(result.get("linked_workflow_id") or 0), 12)
        self.assertEqual(str(result.get("linked_workflow_key") or ""), "template::balm_invoice_generation")
        self.assertEqual(int(result.get("linked_workflow_version_id") or 0), 34)
        self.assertEqual(str(result.get("render_mode") or ""), "overlay")
        self.assertTrue(mock_save.called)

    def test_ingest_template_reference_persists_render_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(documents_router, "TEMPLATE_REFERENCE_DIR", Path(temp_dir)), patch(
                "idms_api_server.routers.documents._load_template_reference_registry",
                return_value=[],
            ), patch(
                "idms_api_server.routers.documents._save_template_reference_registry"
            ) as mock_save:
                response = self.client.post(
                    "/api/documents/template-references/ingest",
                    data={
                        "reference_name": "balm_invoice_pdf",
                        "doc_name": "Balm Invoice PDF",
                        "document_kind": "invoice",
                        "payload_json": '{"template_rule_options":{"render_mode":"preserve_template"}}',
                    },
                    files={
                        "reference_file": (
                            "balm_invoice.pdf",
                            b"fake-template-content",
                            "application/pdf",
                        )
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        result = body.get("result") or {}
        self.assertEqual(str(result.get("render_mode") or ""), "preserve_template")
        self.assertTrue(mock_save.called)

    def test_ingest_template_reference_auto_selects_preserve_template_for_linked_workflow_overwrites(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(documents_router, "TEMPLATE_REFERENCE_DIR", Path(temp_dir)), patch(
                "idms_api_server.routers.documents._load_template_reference_registry",
                return_value=[],
            ), patch(
                "idms_api_server.routers.documents._save_template_reference_registry"
            ) as mock_save, patch(
                "idms_api_server.routers.documents._resolve_template_linked_workflow",
                return_value={
                    "linked_workflow_id": 14,
                    "linked_workflow_key": "workflow::balm_invoice_wf",
                    "linked_workflow_name": "balm_invoice_wf",
                    "linked_workflow_version_id": 14,
                },
            ), patch(
                "idms_api_server.routers.documents._load_linked_workflow_schema_fields",
                return_value={
                    "has_linked_workflow": True,
                    "workflow_key": "workflow::balm_invoice_wf",
                    "workflow_version_id": 14,
                    "fields": ["service_period", "issue_date"],
                    "overwrite_targets": {"fields": ["service_period", "issue_date"], "details": []},
                },
            ), patch(
                "idms_api_server.routers.documents._auto_apply_workflow_overwrite_field_labels",
                return_value={
                    "applied": ["service_period", "issue_date"],
                    "field_labels": {"service_period": ["Dienstperiode"], "issue_date": ["Rechnungsdatum"]},
                    "workflow_overwrite_targets": {"fields": ["service_period", "issue_date"], "details": []},
                    "analysis": {"used_llm": False, "suggestions": []},
                    "skipped": [],
                },
            ):
                response = self.client.post(
                    "/api/documents/template-references/ingest",
                    data={
                        "reference_name": "balm_invoice_auto",
                        "doc_name": "Balm Invoice Auto",
                        "document_kind": "invoice",
                        "payload_json": '{"template_rule_options":{"reuse_existing_solf_rule":true}}',
                        "workflow_key": "workflow::balm_invoice_wf",
                    },
                    files={
                        "reference_file": (
                            "balm_invoice.pdf",
                            b"fake-template-content",
                            "application/pdf",
                        )
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        result = body.get("result") or {}
        self.assertEqual(str(result.get("render_mode") or ""), "preserve_template")
        field_labels = (((result.get("base_payload") or {}).get("field_rules") or {}).get("field_labels") or {})
        self.assertEqual(field_labels.get("service_period"), ["Dienstperiode"])
        self.assertEqual(field_labels.get("issue_date"), ["Rechnungsdatum"])
        self.assertTrue(mock_save.called)

    def test_generate_from_reference_auto_applies_workflow_labels_for_legacy_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "linked-template.pdf"
            template_path.write_bytes(b"fake-pdf")

            with patch(
                "idms_api_server.routers.documents._find_template_reference",
                return_value={
                    "reference_name": "balm_invoice",
                    "doc_name": "Balm Invoice",
                    "document_kind": "invoice",
                    "stored_path": str(template_path),
                    "template_format": "pdf",
                    "base_payload": {"invoice_number": "INV-1"},
                    "linked_workflow_key": "workflow::balm_invoice_wf",
                    "linked_workflow_id": 14,
                },
            ), patch(
                "idms_api_server.routers.documents._load_linked_workflow_schema_fields",
                return_value={
                    "has_linked_workflow": True,
                    "workflow_key": "workflow::balm_invoice_wf",
                    "workflow_version_id": 14,
                    "fields": ["service_period", "issue_date"],
                    "overwrite_targets": {"fields": ["service_period", "issue_date"], "details": []},
                },
            ), patch(
                "idms_api_server.routers.documents._auto_apply_workflow_overwrite_field_labels",
                return_value={
                    "applied": ["service_period"],
                    "field_labels": {"service_period": ["Dienstperiode"]},
                    "workflow_overwrite_targets": {"fields": ["service_period", "issue_date"], "details": []},
                    "analysis": {"used_llm": False, "suggestions": []},
                    "skipped": [],
                },
            ) as mock_auto_apply, patch(
                "idms_api_server.routers.documents.document_template_pipeline.infer_pdf_rewrite_layout",
                return_value={
                    "service_period": {
                        "page": 1,
                        "x": 10,
                        "y": 10,
                        "width": 100,
                        "font_size": 10,
                        "sample_text": "",
                    }
                },
            ), patch(
                "idms_api_server.routers.documents.document_template_pipeline.infer_pdf_qr_bill_region",
                return_value={"page": 1, "x": 0, "y": 0, "w": 1, "h": 1},
            ), patch(
                "idms_api_server.routers.documents.document_template_pipeline.infer_qr_data_from_pdf_text",
                return_value={"qr_iban": "CH4431999123000889012", "currency": "CHF"},
            ), patch(
                "idms_api_server.routers.documents.document_template_pipeline.render_template_document",
                return_value=(b"%PDF-1.4 fake", "linked-template.pdf", "application/pdf"),
            ), patch(
                "idms_api_server.routers.documents._resolve_output_file_path",
                return_value=template_path,
            ):
                response = self.client.post(
                    "/api/documents/template-references/generate",
                    json={
                        "reference_name": "balm_invoice",
                        "output_format": "pdf",
                        "document_kind": "invoice",
                        "payload": {},
                        "override_fields": [],
                        "output_path": str(template_path),
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_auto_apply.called)

    def test_ingest_template_reference_rejects_unknown_linked_workflow_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(documents_router, "TEMPLATE_REFERENCE_DIR", Path(temp_dir)), patch(
                "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_registry_entry_by_key",
                return_value=None,
            ):
                response = self.client.post(
                    "/api/documents/template-references/ingest",
                    data={
                        "reference_name": "balm_invoice",
                        "doc_name": "Balm Invoice",
                        "document_kind": "invoice",
                        "payload_json": "{}",
                        "workflow_key": "template::missing",
                    },
                    files={
                        "reference_file": (
                            "balm_invoice.docx",
                            b"fake-template-content",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )
                    },
                )

        self.assertEqual(response.status_code, 400)
        detail = str((response.json() or {}).get("detail") or "")
        self.assertIn("workflow_key", detail)

    def test_generate_from_reference_rejects_inactive_linked_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "linked-template.docx"
            template_path.write_bytes(b"fake-docx")

            with patch(
                "idms_api_server.routers.documents._find_template_reference",
                return_value={
                    "reference_name": "balm_invoice",
                    "doc_name": "Balm Invoice",
                    "document_kind": "invoice",
                    "stored_path": str(template_path),
                    "template_format": "docx",
                    "base_payload": {"invoice_number": "INV-1"},
                    "linked_workflow_key": "template::balm_invoice_generation",
                    "linked_workflow_id": 12,
                },
            ), patch(
                "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_registry_entry_by_key",
                return_value={
                    "workflow_id": 12,
                    "workflow_key": "template::balm_invoice_generation",
                    "workflow_name": "Balm Invoice Generation",
                    "is_active": False,
                },
            ):
                response = self.client.post(
                    "/api/documents/template-references/generate",
                    json={
                        "reference_name": "balm_invoice",
                        "output_format": "pdf",
                        "document_kind": "invoice",
                        "payload": {},
                    },
                )

        self.assertEqual(response.status_code, 409)
        detail = str((response.json() or {}).get("detail") or "")
        self.assertIn("inactive", detail.lower())

    def test_generate_from_reference_includes_linked_workflow_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "linked-template.docx"
            template_path.write_bytes(b"fake-docx")

            with patch(
                "idms_api_server.routers.documents._find_template_reference",
                return_value={
                    "reference_name": "balm_invoice",
                    "doc_name": "Balm Invoice",
                    "document_kind": "invoice",
                    "stored_path": str(template_path),
                    "template_format": "docx",
                    "base_payload": {"invoice_number": "INV-2"},
                    "linked_workflow_key": "template::balm_invoice_generation",
                    "linked_workflow_id": 12,
                },
            ), patch(
                "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_registry_entry_by_key",
                return_value={
                    "workflow_id": 12,
                    "workflow_key": "template::balm_invoice_generation",
                    "workflow_name": "Balm Invoice Generation",
                    "is_active": True,
                },
            ), patch(
                "idms_api_server.routers.documents.business_rules_engine.get_solf_workflow_active_version_by_key",
                return_value={
                    "workflow_id": 12,
                    "workflow_key": "template::balm_invoice_generation",
                    "workflow_version_id": 34,
                    "has_active_version": True,
                },
            ), patch(
                "idms_api_server.routers.documents.document_template_pipeline.render_template_document",
                return_value=(b"%PDF-1.4 fake", "balm.pdf", "application/pdf"),
            ):
                response = self.client.post(
                    "/api/documents/template-references/generate",
                    json={
                        "reference_name": "balm_invoice",
                        "output_format": "pdf",
                        "document_kind": "invoice",
                        "payload": {},
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        linked = (((body.get("result") or {}).get("linked_workflow")) or {})
        self.assertEqual(int(linked.get("linked_workflow_id") or 0), 12)
        self.assertEqual(str(linked.get("linked_workflow_key") or ""), "template::balm_invoice_generation")
        self.assertEqual(int(linked.get("linked_workflow_version_id") or 0), 34)
        self.assertIn("qr_generation", (body.get("result") or {}))