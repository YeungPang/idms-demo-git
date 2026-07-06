from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from xml.sax.saxutils import escape as xml_escape

try:
    import qrcode
except Exception:  # pragma: no cover - optional until dependencies are installed
    qrcode = None

try:
    from docxtpl import DocxTemplate, InlineImage
    from docx.shared import Mm
except Exception:  # pragma: no cover - optional until dependencies are installed
    DocxTemplate = None
    InlineImage = None
    Mm = None

try:
    import pypdf
except Exception:  # pragma: no cover - optional until dependencies are installed
    pypdf = None

try:
    from reportlab.lib.colors import black
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
except Exception:  # pragma: no cover - optional until dependencies are installed
    black = None
    A4 = None
    ImageReader = None
    canvas = None

try:
    import win32com.client  # type: ignore
except Exception:  # pragma: no cover - optional until dependencies are installed
    win32com = None
else:
    win32com = win32com.client


TEMPLATE_FORMATS = {"docx", "pdf"}
OUTPUT_FORMATS = {"docx", "pdf"}


DEFAULT_PDF_LAYOUTS: dict[str, dict[str, dict[str, Any]]] = {
    "invoice": {
        "document_title": {"page": 1, "x": 40, "y": 800, "font_size": 18},
        "document_number": {"page": 1, "x": 430, "y": 800, "font_size": 11},
        "issue_date": {"page": 1, "x": 430, "y": 785, "font_size": 11},
        "recipient_name": {"page": 1, "x": 40, "y": 740, "font_size": 11},
        "recipient_address": {"page": 1, "x": 40, "y": 726, "font_size": 10},
        "issuer_name": {"page": 1, "x": 40, "y": 680, "font_size": 11},
        "issuer_address": {"page": 1, "x": 40, "y": 666, "font_size": 10},
        "summary_block": {"page": 1, "x": 40, "y": 540, "font_size": 10, "width": 500},
        "qr_code_image": {"page": 1, "x": 390, "y": 120, "w": 150, "h": 150},
    },
    "quotation": {
        "document_title": {"page": 1, "x": 40, "y": 800, "font_size": 18},
        "document_number": {"page": 1, "x": 430, "y": 800, "font_size": 11},
        "issue_date": {"page": 1, "x": 430, "y": 785, "font_size": 11},
        "recipient_name": {"page": 1, "x": 40, "y": 740, "font_size": 11},
        "recipient_address": {"page": 1, "x": 40, "y": 726, "font_size": 10},
        "summary_block": {"page": 1, "x": 40, "y": 560, "font_size": 10, "width": 500},
    },
    "ledger_journal": {
        "document_title": {"page": 1, "x": 40, "y": 800, "font_size": 18},
        "document_number": {"page": 1, "x": 430, "y": 800, "font_size": 11},
        "issue_date": {"page": 1, "x": 430, "y": 785, "font_size": 11},
        "summary_block": {"page": 1, "x": 40, "y": 740, "font_size": 9, "width": 520},
    },
}


def normalize_template_format(value: str) -> str:
    format_name = str(value or "").strip().lower()
    if format_name not in TEMPLATE_FORMATS:
        raise ValueError("template_format must be docx or pdf")
    return format_name


def normalize_output_format(value: str) -> str:
    format_name = str(value or "").strip().lower()
    if format_name not in OUTPUT_FORMATS:
        raise ValueError("output_format must be docx or pdf")
    return format_name


def _safe_filename(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "generated-document"
    cleaned: list[str] = []
    for character in text:
        if character.isalnum() or character in {"-", "_", "."}:
            cleaned.append(character)
        else:
            cleaned.append("-")
    return "".join(cleaned).strip("-_.") or "generated-document"


def _stringify(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_stringify(item) for item in value if _stringify(item))
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _flatten_lines(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            lines.extend(_flatten_lines(item))
        return lines
    text = _stringify(value)
    return [line for line in text.splitlines() if line.strip()]


def build_template_context(document_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    context = dict(payload or {})
    kind = str(document_kind or context.get("document_kind") or "document").strip().lower() or "document"
    context["document_kind"] = kind
    context.setdefault("document_title", context.get("title") or context.get("name") or context.get("subject") or kind.title())
    context.setdefault("document_number", context.get("invoice_number") or context.get("quotation_number") or context.get("journal_number") or context.get("number") or "")
    context.setdefault("issue_date", context.get("invoice_date") or context.get("quotation_date") or context.get("document_date") or context.get("date") or "")
    context.setdefault("currency", context.get("currency") or "CHF")
    context.setdefault("line_items", context.get("line_items") or [])
    context.setdefault("notes", context.get("notes") or context.get("remarks") or "")
    context.setdefault("summary_block", context.get("summary_block") or context.get("body_text") or context.get("notes") or "")
    context.setdefault("recipient_name", context.get("recipient_name") or context.get("customer_name") or "")
    context.setdefault("recipient_address", context.get("recipient_address") or context.get("customer_address") or "")
    context.setdefault("issuer_name", context.get("issuer_name") or context.get("supplier_name") or context.get("seller_name") or "")
    context.setdefault("issuer_address", context.get("issuer_address") or context.get("supplier_address") or context.get("seller_address") or "")
    context.setdefault("subtotal", context.get("subtotal") or context.get("net_amount") or "")
    context.setdefault("tax_total", context.get("tax_total") or context.get("vat_amount") or context.get("tax_amount") or "")
    context.setdefault("grand_total", context.get("grand_total") or context.get("total_amount") or context.get("gross_amount") or "")
    return context


def build_swiss_qr_bill_payload(qr_data: dict[str, Any]) -> str:
    payment = dict(qr_data or {})
    creditor = payment.get("creditor") if isinstance(payment.get("creditor"), dict) else {}
    debtor = payment.get("debtor") if isinstance(payment.get("debtor"), dict) else {}
    lines = [
        "SPC",
        "0200",
        "1",
        str(payment.get("qr_iban") or payment.get("iban") or "").strip(),
        str(creditor.get("name") or payment.get("creditor_name") or "").strip(),
        str(creditor.get("street") or payment.get("creditor_street") or "").strip(),
        str(creditor.get("house_no") or payment.get("creditor_house_no") or "").strip(),
        str(creditor.get("postal_code") or payment.get("creditor_postal_code") or "").strip(),
        str(creditor.get("city") or payment.get("creditor_city") or "").strip(),
        str(creditor.get("country") or payment.get("creditor_country") or "CH").strip() or "CH",
        "",
        f"{float(payment.get('amount')):.2f}" if payment.get("amount") not in (None, "") else "",
        str(payment.get("currency") or "CHF").strip() or "CHF",
        str(debtor.get("name") or payment.get("debtor_name") or "").strip(),
        str(debtor.get("street") or payment.get("debtor_street") or "").strip(),
        str(debtor.get("house_no") or payment.get("debtor_house_no") or "").strip(),
        str(debtor.get("postal_code") or payment.get("debtor_postal_code") or "").strip(),
        str(debtor.get("city") or payment.get("debtor_city") or "").strip(),
        str(debtor.get("country") or payment.get("debtor_country") or "").strip(),
        str(payment.get("reference_type") or ("QRR" if str(payment.get("reference") or payment.get("qr_reference") or "").strip() else "NON")).strip() or "NON",
        str(payment.get("reference") or payment.get("qr_reference") or "").strip(),
        str(payment.get("additional_information") or payment.get("additional_info") or "").strip(),
    ]
    while len(lines) < 34:
        lines.append("")
    return "\n".join(lines[:34])


def _render_qr_png_bytes(payload_text: str) -> bytes:
    if qrcode is None:
        raise RuntimeError("qrcode is not installed; run install_document_generation.ps1 first")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(payload_text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_docx_template_bytes(template_bytes: bytes, context: dict[str, Any], qr_data: dict[str, Any] | None = None) -> bytes:
    if DocxTemplate is None:
        raise RuntimeError("docxtpl is not installed; run install_document_generation.ps1 first")

    with tempfile.TemporaryDirectory() as temp_dir:
        template_path = Path(temp_dir) / "template.docx"
        template_path.write_bytes(template_bytes)
        template = DocxTemplate(str(template_path))
        render_context = dict(context)
        if qr_data:
            payload_text = build_swiss_qr_bill_payload(qr_data)
            render_context["qr_bill_payload"] = payload_text
            render_context["qr_bill_text"] = render_context.get("qr_bill_text") or payload_text
            qr_png = _render_qr_png_bytes(payload_text)
            qr_path = Path(temp_dir) / "qr.png"
            qr_path.write_bytes(qr_png)
            if InlineImage is not None and Mm is not None:
                render_context["qr_code_image"] = InlineImage(template, str(qr_path), width=Mm(45))
                render_context["qr_code"] = render_context["qr_code_image"]
                render_context["qr_bill_image"] = render_context["qr_code_image"]
        template.render(render_context, autoescape=True)
        output = io.BytesIO()
        template.save(output)
        return output.getvalue()


def _convert_docx_to_pdf_bytes(docx_bytes: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        docx_path = temp_dir_path / "rendered.docx"
        pdf_path = temp_dir_path / "rendered.pdf"
        docx_path.write_bytes(docx_bytes)

        if win32com is not None:
            word = None
            document = None
            try:
                word = win32com.Dispatch("Word.Application")
                word.Visible = False
                document = word.Documents.Open(str(docx_path), ReadOnly=1)
                document.ExportAsFixedFormat(str(pdf_path), 17)
            finally:
                if document is not None:
                    document.Close(False)
                if word is not None:
                    word.Quit()
            if pdf_path.exists():
                return pdf_path.read_bytes()

        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(temp_dir_path), str(docx_path)],
                check=True,
                capture_output=True,
            )
            if pdf_path.exists():
                return pdf_path.read_bytes()

        raise RuntimeError(
            "Unable to convert DOCX to PDF. Install Microsoft Word, LibreOffice, or provide a PDF template layout."
        )


def _draw_overlay_page(page_width: float, page_height: float, context: dict[str, Any], layout: dict[str, Any], qr_png_bytes: bytes | None = None) -> bytes:
    if canvas is None:
        raise RuntimeError("reportlab is not installed; run install_document_generation.ps1 first")
    buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(buffer, pagesize=(page_width, page_height))
    for field_name, spec in layout.items():
        if not isinstance(spec, dict):
            continue
        page = int(spec.get("page") or 1)
        if page != 1:
            continue
        x = float(spec.get("x") or 0)
        y = float(spec.get("y") or 0)
        size = int(spec.get("font_size") or 10)
        width = float(spec.get("width") or 0)
        value = context.get(field_name)
        if value in (None, "", [], {}):
            continue
        text_lines = _flatten_lines(value)
        pdf_canvas.setFillColor(black)
        pdf_canvas.setFont("Helvetica", size)
        if not text_lines:
            pdf_canvas.drawString(x, y, _stringify(value))
            continue
        for line_index, line in enumerate(text_lines):
            if width:
                pdf_canvas.drawString(x, y - (line_index * (size + 2)), line[: max(1, int(width / max(size * 0.5, 1)))])
            else:
                pdf_canvas.drawString(x, y - (line_index * (size + 2)), line)

    qr_spec = layout.get("qr_code_image") if isinstance(layout.get("qr_code_image"), dict) else None
    if qr_spec and qr_png_bytes:
        x = float(qr_spec.get("x") or 0)
        y = float(qr_spec.get("y") or 0)
        w = float(qr_spec.get("w") or 150)
        h = float(qr_spec.get("h") or 150)
        pdf_canvas.drawImage(ImageReader(io.BytesIO(qr_png_bytes)), x, y, width=w, height=h, mask="auto")

    pdf_canvas.showPage()
    pdf_canvas.save()
    return buffer.getvalue()


def _render_pdf_summary_fallback(context: dict[str, Any], document_kind: str, qr_png_bytes: bytes | None = None) -> bytes:
    if canvas is None:
        raise RuntimeError("reportlab is not installed; run install_document_generation.ps1 first")
    from reportlab.lib.pagesizes import A4

    buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50
    pdf_canvas.setFont("Helvetica-Bold", 18)
    pdf_canvas.drawString(40, y, _stringify(context.get("document_title") or document_kind.title()))
    y -= 28
    pdf_canvas.setFont("Helvetica", 10)
    for key in ["document_number", "issue_date", "recipient_name", "recipient_address", "issuer_name", "issuer_address", "currency", "subtotal", "tax_total", "grand_total"]:
        value = _stringify(context.get(key))
        if value:
            for line in value.splitlines():
                pdf_canvas.drawString(40, y, f"{key}: {line}")
                y -= 14
    summary_lines = _flatten_lines(context.get("summary_block"))
    if summary_lines:
        pdf_canvas.setFont("Helvetica-Bold", 11)
        pdf_canvas.drawString(40, y - 4, "Summary")
        y -= 18
        pdf_canvas.setFont("Helvetica", 10)
        for line in summary_lines:
            pdf_canvas.drawString(40, y, line)
            y -= 12
    if qr_png_bytes:
        pdf_canvas.drawImage(ImageReader(io.BytesIO(qr_png_bytes)), width - 210, 60, width=160, height=160, mask="auto")
    pdf_canvas.showPage()
    pdf_canvas.save()
    return buffer.getvalue()


def _overlay_pdf_template_bytes(template_bytes: bytes, context: dict[str, Any], layout: dict[str, Any], qr_png_bytes: bytes | None = None) -> bytes:
    if pypdf is None:
        raise RuntimeError("pypdf is not installed; run install_document_generation.ps1 first")
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(template_bytes))
    writer = PdfWriter()
    page_count = len(reader.pages)
    layout_by_page: dict[int, dict[str, Any]] = {}
    for field_name, spec in layout.items():
        if not isinstance(spec, dict):
            continue
        page = int(spec.get("page") or 1)
        layout_by_page.setdefault(page, {})[field_name] = spec

    for page_index, page in enumerate(reader.pages, start=1):
        media_box = page.mediabox
        page_width = float(media_box.width)
        page_height = float(media_box.height)
        overlay_layout = layout_by_page.get(page_index, {})
        if overlay_layout or (page_index == 1 and qr_png_bytes):
            overlay_pdf = _draw_overlay_page(page_width, page_height, context, overlay_layout, qr_png_bytes if page_index == 1 else None)
            overlay_reader = PdfReader(io.BytesIO(overlay_pdf))
            page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def render_template_document(
    *,
    template_bytes: bytes,
    template_format: str,
    output_format: str,
    document_kind: str,
    payload: dict[str, Any],
    qr_data: dict[str, Any] | None = None,
    layout: dict[str, Any] | None = None,
    template_name: str = "template",
) -> tuple[bytes, str, str]:
    template_format_name = normalize_template_format(template_format)
    output_format_name = normalize_output_format(output_format)
    context = build_template_context(document_kind, payload)
    qr_png_bytes = _render_qr_png_bytes(build_swiss_qr_bill_payload(qr_data)) if qr_data else None
    filename_stem = _safe_filename(str(context.get("document_title") or context.get("document_number") or template_name))
    filename = f"{filename_stem}.{output_format_name}"

    if template_format_name == "docx":
        rendered_docx = _render_docx_template_bytes(template_bytes, context, qr_data=qr_data)
        if output_format_name == "docx":
            return rendered_docx, filename, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        try:
            pdf_bytes = _convert_docx_to_pdf_bytes(rendered_docx)
            return pdf_bytes, filename, "application/pdf"
        except Exception:
            return _render_pdf_summary_fallback(context, document_kind, qr_png_bytes), filename, "application/pdf"

    effective_layout = dict(DEFAULT_PDF_LAYOUTS.get(str(document_kind or "").strip().lower(), {}))
    if layout:
        effective_layout.update(layout)
    if output_format_name != "pdf":
        raise ValueError("PDF templates can only be rendered to PDF")
    pdf_bytes = _overlay_pdf_template_bytes(template_bytes, context, effective_layout, qr_png_bytes)
    return pdf_bytes, filename, "application/pdf"
