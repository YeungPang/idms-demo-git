import importlib.util
import unittest
from pathlib import Path


def _load_local_pipeline():
    module_path = Path(__file__).with_name("document_template_pipeline.py")
    spec = importlib.util.spec_from_file_location("demo_document_template_pipeline", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


pipeline = _load_local_pipeline()


class TestDocumentTemplatePipeline(unittest.TestCase):
    def test_build_template_context_normalizes_document_kind(self):
        context = pipeline.build_template_context(
            "invoice",
            {
                "invoice_number": "INV-1001",
                "invoice_date": "2026-06-27",
                "customer_name": "APHOTONIX GmbH",
                "gross_amount": 706.2,
            },
        )

        self.assertEqual(context["document_kind"], "invoice")
        self.assertEqual(context["document_number"], "INV-1001")
        self.assertEqual(context["issue_date"], "2026-06-27")
        self.assertEqual(context["recipient_name"], "APHOTONIX GmbH")
        self.assertEqual(context["grand_total"], 706.2)

    def test_build_swiss_qr_bill_payload_contains_key_fields(self):
        payload = pipeline.build_swiss_qr_bill_payload(
            {
                "qr_iban": "CH38 3000 0001 1649 5813 8",
                "amount": 706.2,
                "currency": "CHF",
                "creditor_name": "Handelsregisteramt Zug",
                "creditor_street": "Aabachstrasse 5",
                "creditor_postal_code": "6301",
                "creditor_city": "Zug",
                "creditor_country": "CH",
                "reference": "00 00020 80430 00000 11247 99230",
            }
        )

        self.assertIn("SPC", payload)
        self.assertIn("0200", payload)
        self.assertIn("Handelsregisteramt Zug", payload)
        self.assertIn("706.20", payload)
        self.assertIn("CH38 3000 0001 1649 5813 8", payload)
