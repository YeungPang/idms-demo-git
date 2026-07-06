import io
import unittest
import zipfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from idms_api_server.application import app


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