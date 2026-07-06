import unittest
import hashlib
from unittest.mock import mock_open
from unittest.mock import patch

from fastapi.testclient import TestClient

from idms_api_server.application import app


class TestSchemaProposalsAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_list_schema_proposals_endpoint(self):
        fake_rows = [
            {
                "proposal_id": 7,
                "class_name": "company",
                "attribute_name": "publication_organ",
                "first_seen_doc_id": 3,
                "last_seen_doc_id": 3,
                "sample_value": "SHAB",
                "occurrence_count": 2,
                "status": "proposed",
                "metadata": {"source": "ingest_non_schema_attribute"},
                "created_at": "2026-06-16T10:00:00+00:00",
                "updated_at": "2026-06-16T10:05:00+00:00",
                "priority_score": 5,
                "priority_bucket": "medium",
                "suggested_for_approval": True,
                "suggestion_reason": "recurs_across_documents:2",
            }
        ]

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch("idms_api_server.routers.schema_proposals.domain_db.list_solf_attribute_proposals", return_value=fake_rows):
                conn_instance = conn_mock.return_value
                response = self.client.get("/api/schema-proposals", params={"status": "proposed", "priority_bucket": "medium", "suggested_only": True, "limit": 50})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("count"), 1)
        self.assertEqual((data.get("result") or [])[0]["attribute_name"], "publication_organ")
        self.assertEqual((data.get("result") or [])[0]["priority_bucket"], "medium")
        self.assertTrue((data.get("result") or [])[0]["suggested_for_approval"])
        conn_instance.close.assert_called_once()

    def test_schema_governance_summary_endpoint(self):
        proposals = [
            {
                "proposal_id": 7,
                "class_name": "company",
                "attribute_name": "publication_organ",
                "occurrence_count": 3,
                "status": "proposed",
                "priority_score": 7,
                "priority_bucket": "high",
                "suggested_for_approval": True,
                "suggestion_reason": "recurs_across_documents:3",
            },
            {
                "proposal_id": 8,
                "class_name": "company",
                "attribute_name": "tr_number",
                "occurrence_count": 1,
                "status": "approved",
                "priority_score": 2,
                "priority_bucket": "low",
                "suggested_for_approval": False,
                "suggestion_reason": "",
            },
        ]
        batches = [
            {
                "batch_id": 3,
                "batch_name": "company-batch",
                "status": "exported",
                "metadata": {},
            },
            {
                "batch_id": 4,
                "batch_name": "company-batch-2",
                "status": "reviewed",
                "metadata": {"reviewed_by": "tester"},
            },
        ]

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch("idms_api_server.routers.schema_proposals.domain_db.list_solf_attribute_proposals", return_value=proposals):
                with patch("idms_api_server.routers.schema_proposals.domain_db.list_solf_schema_promotion_batches", return_value=batches):
                    conn_instance = conn_mock.return_value
                    response = self.client.get("/api/schema-proposals/summary")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        result = data.get("result") or {}
        self.assertEqual(((result.get("proposal_summary") or {}).get("total")), 2)
        self.assertEqual(((result.get("proposal_summary") or {}).get("by_status") or {}).get("proposed"), 1)
        self.assertEqual(((result.get("proposal_summary") or {}).get("high_priority_open")), 1)
        self.assertEqual(((result.get("batch_summary") or {}).get("pending_audit_links")), 1)
        self.assertEqual(len(result.get("suggested_proposals_preview") or []), 1)
        conn_instance.close.assert_called_once()

    def test_update_schema_proposal_status_endpoint(self):
        updated = {
            "proposal_id": 7,
            "class_name": "company",
            "attribute_name": "publication_organ",
            "first_seen_doc_id": 3,
            "last_seen_doc_id": 3,
            "sample_value": "SHAB",
            "occurrence_count": 2,
            "status": "approved",
            "metadata": {"reviewed_by": "tester", "review_note": "promote for future ingests"},
            "created_at": "2026-06-16T10:00:00+00:00",
            "updated_at": "2026-06-16T10:06:00+00:00",
            "priority_score": 3,
            "priority_bucket": "low",
            "suggested_for_approval": False,
            "suggestion_reason": "",
        }

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch("idms_api_server.routers.schema_proposals.domain_db.update_solf_attribute_proposal_status", return_value=updated):
                conn_instance = conn_mock.return_value
                response = self.client.post(
                    "/api/schema-proposals/7/status",
                    json={"status": "approved", "reviewed_by": "tester", "note": "promote for future ingests"},
                )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual((data.get("result") or {}).get("status"), "approved")
        conn_instance.close.assert_called_once()

    def test_solf_patch_draft_endpoint(self):
        approved_extensions = {
            "company": ["publication_organ", "tr_number", "legal_name"],
        }
        script_text = """company ≔ {\n    objectType: class,\n    legal_name: Ø,\n    ingest: ⦃db_ingest(_entity_payload)⦄\n}\n"""

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch(
                "idms_api_server.routers.schema_proposals.domain_db.get_approved_solf_attribute_extensions",
                return_value=approved_extensions,
            ):
                with patch("idms_api_server.routers.schema_proposals.parse_solf_classes", return_value={
                    "company": type("StubClass", (), {"allowed_attributes": {"legal_name"}})()
                }):
                    with patch("pathlib.Path.read_text", mock_open(read_data=script_text)):
                        conn_instance = conn_mock.return_value
                        response = self.client.get("/api/schema-proposals/solf-patch-draft", params={"class_name": "company"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        result = data.get("result") or {}
        classes = result.get("classes") or []
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0]["class_name"], "company")
        self.assertEqual(classes[0]["missing_attributes"], ["publication_organ", "tr_number"])
        self.assertIn("publication_organ: Ø", classes[0]["patch_preview"])
        self.assertIn("tr_number: Ø", result.get("solf_patch_preview") or "")
        conn_instance.close.assert_called_once()

    def test_create_schema_promotion_batch_endpoint(self):
        approved_rows = [
            {
                "proposal_id": 7,
                "class_name": "company",
                "attribute_name": "publication_organ",
                "status": "approved",
            },
            {
                "proposal_id": 8,
                "class_name": "company",
                "attribute_name": "tr_number",
                "status": "approved",
            },
        ]
        created_batch = {
            "batch_id": 3,
            "batch_name": "company-approved-batch",
            "class_name": "company",
            "status": "draft",
            "proposal_ids": [7, 8],
            "patch_preview": "company ≔ {\n    ... existing class fields ...\n    publication_organ: Ø,\n    tr_number: Ø,\n}",
            "metadata": {"created_by": "tester"},
            "created_at": "2026-06-16T11:00:00+00:00",
            "updated_at": "2026-06-16T11:00:00+00:00",
        }

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch("idms_api_server.routers.schema_proposals.domain_db.list_solf_attribute_proposals", return_value=approved_rows):
                with patch("idms_api_server.routers.schema_proposals.parse_solf_classes", return_value={
                    "company": type("StubClass", (), {"allowed_attributes": {"legal_name"}})()
                }):
                    with patch("pathlib.Path.read_text", mock_open(read_data="company ≔ {}")):
                        with patch("idms_api_server.routers.schema_proposals.domain_db.create_solf_schema_promotion_batch", return_value=created_batch):
                            conn_instance = conn_mock.return_value
                            response = self.client.post(
                                "/api/schema-proposals/promotion-batches",
                                json={
                                    "batch_name": "company-approved-batch",
                                    "class_name": "company",
                                    "created_by": "tester",
                                    "note": "promote approved company attributes",
                                },
                            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        result = data.get("result") or {}
        self.assertEqual(result.get("batch_id"), 3)
        self.assertEqual(result.get("proposal_ids"), [7, 8])
        self.assertEqual((result.get("classes") or [])[0]["missing_attributes"], ["publication_organ", "tr_number"])
        conn_instance.close.assert_called_once()

    def test_list_schema_promotion_batches_endpoint(self):
        fake_batches = [
            {
                "batch_id": 3,
                "batch_name": "company-approved-batch",
                "class_name": "company",
                "status": "draft",
                "proposal_ids": [7, 8],
                "patch_preview": "company ≔ { ... }",
                "metadata": {"created_by": "tester"},
                "created_at": "2026-06-16T11:00:00+00:00",
                "updated_at": "2026-06-16T11:00:00+00:00",
            }
        ]

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch("idms_api_server.routers.schema_proposals.domain_db.list_solf_schema_promotion_batches", return_value=fake_batches):
                conn_instance = conn_mock.return_value
                response = self.client.get("/api/schema-proposals/promotion-batches", params={"limit": 20})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("count"), 1)
        self.assertEqual((data.get("result") or [])[0]["batch_id"], 3)
        conn_instance.close.assert_called_once()

    def test_update_schema_promotion_batch_status_endpoint(self):
        updated_batch = {
            "batch_id": 3,
            "batch_name": "company-approved-batch",
            "class_name": "company",
            "status": "reviewed",
            "proposal_ids": [7, 8],
            "patch_preview": "company ≔ { ... }",
            "metadata": {"reviewed_by": "tester", "review_note": "ready for export"},
            "created_at": "2026-06-16T11:00:00+00:00",
            "updated_at": "2026-06-16T11:10:00+00:00",
        }

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch("idms_api_server.routers.schema_proposals.domain_db.update_solf_schema_promotion_batch_status", return_value=updated_batch):
                conn_instance = conn_mock.return_value
                response = self.client.post(
                    "/api/schema-proposals/promotion-batches/3/status",
                    json={"status": "reviewed", "reviewed_by": "tester", "note": "ready for export"},
                )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual((data.get("result") or {}).get("status"), "reviewed")
        conn_instance.close.assert_called_once()

    def test_export_schema_promotion_batch_endpoint(self):
        batch = {
            "batch_id": 3,
            "batch_name": "company-approved-batch",
            "class_name": "company",
            "status": "exported",
            "proposal_ids": [7, 8],
            "patch_preview": "company ≔ {\n    ... existing class fields ...\n    publication_organ: Ø,\n    tr_number: Ø,\n}",
            "metadata": {"created_by": "tester"},
            "created_at": "2026-06-16T11:00:00+00:00",
            "updated_at": "2026-06-16T11:15:00+00:00",
        }

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch("idms_api_server.routers.schema_proposals.domain_db.get_solf_schema_promotion_batch", return_value=batch):
                conn_instance = conn_mock.return_value
                response = self.client.get("/api/schema-proposals/promotion-batches/3/export")

        self.assertEqual(response.status_code, 200)
        self.assertIn("publication_organ: Ø", response.text)
        self.assertEqual(
            response.headers.get("content-disposition"),
            'attachment; filename="company-approved-batch.solf"',
        )
        conn_instance.close.assert_called_once()

    def test_link_schema_promotion_batch_audit_endpoint(self):
        script_text = "company ≔ {\n    legal_name: Ø,\n}\n"
        expected_hash = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
        updated_batch = {
            "batch_id": 3,
            "batch_name": "company-approved-batch",
            "class_name": "company",
            "status": "applied",
            "proposal_ids": [7, 8],
            "patch_preview": "company ≔ { ... }",
            "metadata": {
                "audit_linked_by": "tester",
                "solf_script_hash": expected_hash,
                "exported_artifact_path": "generated/company-approved-batch.solf",
            },
            "created_at": "2026-06-16T11:00:00+00:00",
            "updated_at": "2026-06-16T11:20:00+00:00",
        }

        with patch("idms_api_server.routers.schema_proposals.object_db.get_connection") as conn_mock:
            with patch("pathlib.Path.read_text", mock_open(read_data=script_text)):
                with patch(
                    "idms_api_server.routers.schema_proposals.domain_db.update_solf_schema_promotion_batch_audit_link",
                    return_value=updated_batch,
                ):
                    conn_instance = conn_mock.return_value
                    response = self.client.post(
                        "/api/schema-proposals/promotion-batches/3/audit-link",
                        json={
                            "linked_by": "tester",
                            "exported_artifact_path": "generated/company-approved-batch.solf",
                            "note": "applied to canonical solf script",
                        },
                    )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual((data.get("result") or {}).get("status"), "applied")
        self.assertEqual(((data.get("result") or {}).get("metadata") or {}).get("solf_script_hash"), expected_hash)
        conn_instance.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()