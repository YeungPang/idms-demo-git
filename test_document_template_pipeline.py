import importlib.util
import io
import unittest
from pathlib import Path

from pypdf import PdfReader


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
        self.assertIn("CH3830000001164958138", payload.replace(" ", ""))
        self.assertIn("EPD", payload)

    def test_build_template_context_computes_line_item_and_totals(self):
        context = pipeline.build_template_context(
            "quotation",
            {
                "quotation_number": "Q-2026-001",
                "line_items": [
                    {"description": "Product A", "quantity": 2, "unit_price": 100, "vat_rate": 8.1},
                    {"description": "Product B", "quantity": 1, "unit_price": 50, "vat_rate": 8.1},
                ],
            },
        )

        self.assertEqual(context["line_items"][0]["amount"], 200.0)
        self.assertEqual(context["line_items"][1]["amount"], 50.0)
        self.assertAlmostEqual(float(context["subtotal"]), 250.0, places=2)
        self.assertAlmostEqual(float(context["tax_total"]), 20.25, places=2)
        self.assertAlmostEqual(float(context["grand_total"]), 270.25, places=2)

    def test_infer_pdf_qr_bill_region_from_template_labels(self):
        template_path = Path(__file__).with_name("generated") / "template_references" / "balm_invoice_test.pdf"
        if not template_path.exists():
            self.skipTest("balm invoice template PDF not available")

        region = pipeline.infer_pdf_qr_bill_region(
            template_path.read_bytes(),
            {"field_rules": {}},
        )

        self.assertIsNotNone(region)
        assert region is not None
        self.assertEqual(int(region.get("page") or 0), 1)
        self.assertGreater(float(region.get("w") or 0), 300.0)
        self.assertGreater(float(region.get("h") or 0), 150.0)

    def test_infer_qr_data_from_pdf_text_extracts_reference_and_iban(self):
        template_path = Path(__file__).with_name("generated") / "template_references" / "balm_invoice_test.pdf"
        if not template_path.exists():
            self.skipTest("balm invoice template PDF not available")

        qr_data = pipeline.infer_qr_data_from_pdf_text(
            template_path.read_bytes(),
            {"field_rules": {}},
        )

        self.assertEqual(str(qr_data.get("reference") or ""), "00 00000 00000 00000 00026 30181")
        self.assertIn("CH81", str(qr_data.get("qr_iban") or ""))
        self.assertTrue(str(qr_data.get("creditor_name") or ""))

    def test_render_template_document_uses_template_name_and_generation_date_as_default_filename(self):
        if pipeline.canvas is None:
            self.skipTest("reportlab not installed")

        from reportlab.pdfgen import canvas as rl_canvas

        base = io.BytesIO()
        pdf = rl_canvas.Canvas(base, pagesize=(200, 200))
        pdf.drawString(20, 180, "Template")
        pdf.save()

        _content, filename, _media_type = pipeline.render_template_document(
            template_bytes=base.getvalue(),
            template_format="pdf",
            output_format="pdf",
            document_kind="invoice",
            payload={"generation_date": "2026-07-13", "document_title": "Ignored Title"},
            template_name="balm_invoice_test.pdf",
        )

        self.assertEqual(filename, "balm_invoice_test-2026-07-13.pdf")

    def test_render_template_document_uses_explicit_filename_when_provided(self):
        if pipeline.canvas is None:
            self.skipTest("reportlab not installed")

        from reportlab.pdfgen import canvas as rl_canvas

        base = io.BytesIO()
        pdf = rl_canvas.Canvas(base, pagesize=(200, 200))
        pdf.drawString(20, 180, "Template")
        pdf.save()

        _content, filename, _media_type = pipeline.render_template_document(
            template_bytes=base.getvalue(),
            template_format="pdf",
            output_format="pdf",
            document_kind="invoice",
            payload={"output_filename": "custom_name_for_export.pdf", "generation_date": "2026-07-13"},
            template_name="balm_invoice_test.pdf",
        )

        self.assertEqual(filename, "custom_name_for_export.pdf")

    def test_preserve_template_qr_bill_region_renders_payment_slip_text(self):
        if pipeline.canvas is None:
            self.skipTest("reportlab not installed")

        from reportlab.pdfgen import canvas as rl_canvas

        base = io.BytesIO()
        pdf = rl_canvas.Canvas(base, pagesize=(595, 842))
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, 820, "Invoice Header")
        pdf.save()

        rendered, _filename, _media_type = pipeline.render_template_document(
            template_bytes=base.getvalue(),
            template_format="pdf",
            output_format="pdf",
            document_kind="invoice",
            payload={"service_period": "01.04.2026 bis 30.06.2026", "generation_date": "2026-07-13"},
            qr_data={
                "qr_iban": "CH4431999123000889012",
                "creditor_name": "SEVECO AG",
                "creditor_street": "Untermuli 6",
                "creditor_postal_code": "6300",
                "creditor_city": "Zug",
                "debtor_name": "Stiftung Balm",
                "debtor_street": "Balmstrasse 49",
                "debtor_postal_code": "8645",
                "debtor_city": "Jona",
                "reference": "00 00000 00000 00000 00026 30181",
                "reference_type": "QRR",
                "amount": "252.95",
                "currency": "CHF",
            },
            layout={
                "qr_bill_region": {
                    "page": 1,
                    "x": 8,
                    "y": 36,
                    "w": 579,
                    "h": 230,
                },
                "qr_bill_redraw": True,
            },
            render_mode="preserve_template",
            template_name="balm_invoice_test.pdf",
        )

        text = "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(rendered)).pages)
        self.assertIn("Empfangsschein", text)
        self.assertIn("Zahlteil", text)

    def test_preserve_template_qr_bill_overwrites_reference_in_place_without_full_redraw(self):
        if pipeline.canvas is None:
            self.skipTest("reportlab not installed")

        from reportlab.pdfgen import canvas as rl_canvas

        base = io.BytesIO()
        pdf = rl_canvas.Canvas(base, pagesize=(595, 842))
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(320, 175, "Referenz")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(320, 162, "00 00000 00000 00000 00026 30181")
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(320, 94, "Wahrung")
        pdf.drawString(397, 94, "Betrag")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(320, 82, "CHF")
        pdf.drawString(397, 82, "252.95")
        pdf.save()

        rendered, _filename, _media_type = pipeline.render_template_document(
            template_bytes=base.getvalue(),
            template_format="pdf",
            output_format="pdf",
            document_kind="invoice",
            payload={"generation_date": "2026-07-13"},
            qr_data={
                "reference": "00 00000 00000 00000 00026 30182",
                "reference_type": "QRR",
                "amount": "199.10",
                "currency": "CHF",
            },
            layout={
                "qr_bill_region": {
                    "page": 1,
                    "x": 8,
                    "y": 36,
                    "w": 579,
                    "h": 230,
                }
            },
            render_mode="preserve_template",
            template_name="balm_invoice_test.pdf",
        )

        text = "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(rendered)).pages)
        self.assertIn("00 00000 00000 00000 00026 30182", text)

    def test_preserve_template_rewrite_does_not_erase_adjacent_line_below(self):
        if pipeline.canvas is None:
            self.skipTest("reportlab not installed")

        from reportlab.pdfgen import canvas as rl_canvas

        base = io.BytesIO()
        pdf = rl_canvas.Canvas(base, pagesize=(400, 300))
        pdf.setFont("Helvetica", 12)
        pdf.drawString(40, 220, "Wartung/Serv. Periode:")
        pdf.drawString(180, 220, "01.01.2026 bis 31.03.2026")
        pdf.drawString(40, 208, "U. Wartungs-Nr.")
        pdf.drawString(180, 208, "240001")
        pdf.save()

        layout = pipeline.infer_pdf_rewrite_layout(
            template_bytes=base.getvalue(),
            payload={"service_period": "01.04.2026 bis 30.06.2026"},
            template_payload={"field_rules": {"field_labels": {"service_period": "Wartung/Serv. Periode:"}}},
        )

        rendered, _filename, _media_type = pipeline.render_template_document(
            template_bytes=base.getvalue(),
            template_format="pdf",
            output_format="pdf",
            document_kind="invoice",
            payload={"service_period": "01.04.2026 bis 30.06.2026"},
            qr_data=None,
            layout=layout,
            render_mode="preserve_template",
            template_name="balm_invoice_test.pdf",
        )

        text = "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(rendered)).pages)
        self.assertIn("01.04.2026 bis 30.06.2026", text)
        self.assertIn("U. Wartungs-Nr.", text)
        self.assertIn("240001", text)
