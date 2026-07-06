from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import object_db
import solf_function

try:
    import domain_db
except Exception:  # pragma: no cover - optional domain module
    domain_db = None


LOGGER = logging.getLogger("idms.domain_function")
ENFORCE_REQUIRED_DOMAIN_ACTIONS = os.getenv("IDMS_ENFORCE_REQUIRED_DOMAIN_ACTIONS", "true").strip().lower() in {"1", "true", "yes", "on"}
REQUIRED_DOMAIN_ACTION_POLICY = os.getenv(
    "IDMS_REQUIRED_DOMAIN_ACTION_POLICY",
    "invoice:fail,bill:fail,receipt:warn,employment_contract:fail,employment_pattern:fail",
)


def _allowed_attributes(class_defs: dict[str, Any], class_name: str) -> list[str]:
    class_def = class_defs.get(class_name)
    if class_def is None:
        return []
    return sorted(getattr(class_def, "allowed_attributes", set()) or [])


def _build_accounting_prompt_guidance(class_defs: dict[str, Any], routed: dict[str, Any]) -> str:
    document_type = str(routed.get("document_type") or "").strip().lower()
    if document_type not in {"invoice", "bill", "receipt"}:
        return ""
    if "accounting_transaction" not in class_defs:
        return ""

    allowed_attrs = _allowed_attributes(class_defs, "accounting_transaction")
    return (
        "Accounting extraction rules for this financial document:\n"
        "- Include one accounting_transaction entity when enough evidence exists for posting.\n"
        "- When the routed document type is invoice, bill, or receipt, prefer outputting accounting_transaction rather than leaving booking implicit.\n"
        "- accounting_transaction.attributes must include ledger_lines array with at least one debit and one credit line.\n"
        "- Each ledger_lines item should include: account_number, direction (debit|credit), amount_source_currency, source_currency, exchange_rate_to_chf, amount_chf, swiss_vat_code.\n"
        "- Enforce double-entry consistency: total debit amount_chf must equal total credit amount_chf.\n"
        "- If posting evidence is incomplete, still extract the financial entity and make booking incompleteness explicit in semantic content.\n"
        "- transaction_id should be deterministic from document number when possible.\n"
        f"- Allowed accounting_transaction attributes: {json.dumps(allowed_attrs, ensure_ascii=False)}\n"
    )


def _build_hr_prompt_guidance(class_defs: dict[str, Any], routed: dict[str, Any]) -> str:
    document_type = str(routed.get("document_type") or "").strip().lower()
    entity_hints = [str(item).strip().lower() for item in (routed.get("entity_type_hints") or []) if str(item).strip()]

    hr_doc_types = {"employment_contract", "payroll_record", "social_contribution_report", "workflow_case"}
    hr_hints = {"employee_profile", "employment_contract", "department", "role"}
    if document_type not in hr_doc_types and not any(hint in hr_hints for hint in entity_hints):
        return ""

    employee_attrs = _allowed_attributes(class_defs, "employee_profile")
    employment_attrs = _allowed_attributes(class_defs, "employment_contract")
    return (
        "HR extraction rules for employee and employment documents:\n"
        "- Extract employee_profile when personnel identity and employment status are present.\n"
        "- Extract employment_contract when contractual terms exist (employer, role, dates, work percentage, salary).\n"
        "- If a person is shown holding a role or position in an employer, prefer emitting employment_contract rather than only generic person-role-company links when employment semantics are clear.\n"
        "- For employee_profile include at least one identifier: employee_no or ahv_number when available.\n"
        "- For employment_contract include person_ref, employer_ref, and start_date when available.\n"
        "- Keep policy/legal narrative fields in the source document language.\n"
        f"- Allowed employee_profile attributes: {json.dumps(employee_attrs, ensure_ascii=False)}\n"
        f"- Allowed employment_contract attributes: {json.dumps(employment_attrs, ensure_ascii=False)}\n"
    )


def _build_product_reference_prompt_guidance(class_defs: dict[str, Any], routed: dict[str, Any]) -> str:
    document_type = str(routed.get("document_type") or "").strip().lower()
    if document_type != "product_reference":
        return ""

    return (
        "Product reference extraction rules for reference images and product catalogs:\n"
        "- Extract product information including: product_name (required), product_number (required), version, description, technical_specs, dimensions, weight.\n"
        "- product_number should be the manufacturer or internal product identifier, not a serial number.\n"
        "- Include high-confidence metadata and specifications visible in the reference material.\n"
        "- Mark ambiguous or unclear information with a low confidence value.\n"
        "- For images, focus on identifying the product type and extracting any visible identifiers or specifications.\n"
        "- Do not attempt to extract usage logs, historical data, or individual serial numbers from reference materials.\n"
    )


def build_extraction_guidance(class_defs: dict[str, Any], routed: dict[str, Any]) -> str:
    parts = [
        _build_accounting_prompt_guidance(class_defs, routed),
        _build_hr_prompt_guidance(class_defs, routed),
        _build_product_reference_prompt_guidance(class_defs, routed),
    ]
    return "\n".join(part for part in parts if part).strip()


def generate_product_reference_markdown(extracted: dict[str, Any]) -> str:
    """Generate synthetic markdown for product reference documents.
    
    Creates description markdown from extracted product attributes rather than
    attempting OCR/markdown conversion of the reference image.
    """
    doc_payload = extracted.get("document") if isinstance(extracted.get("document"), dict) else {}
    entities = extracted.get("entities") if isinstance(extracted.get("entities"), list) else []
    
    # Find product entity
    product_entity = None
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_class = str(entity.get("class_name") or entity.get("entity_type") or "").strip().lower()
        if entity_class in {"product", "product_reference"}:
            product_entity = entity
            break
    
    if not product_entity:
        # Fallback: no product entity found
        return ""
    
    attrs = product_entity.get("attributes") if isinstance(product_entity.get("attributes"), dict) else {}
    product_name = str(attrs.get("product_name") or "").strip()
    product_number = str(attrs.get("product_number") or "").strip()
    version = str(attrs.get("version") or "").strip()
    description = str(attrs.get("description") or "").strip()
    technical_specs = attrs.get("technical_specs")
    dimensions = str(attrs.get("dimensions") or "").strip()
    weight = str(attrs.get("weight") or "").strip()
    
    parts = []
    if product_name:
        parts.append(f"# {product_name}")
    if product_number:
        parts.append(f"**Product Number:** {product_number}")
    if version:
        parts.append(f"**Version:** {version}")
    if description:
        parts.append(f"\n{description}")
    
    if dimensions or weight:
        parts.append("\n## Specifications")
        if dimensions:
            parts.append(f"- **Dimensions:** {dimensions}")
        if weight:
            parts.append(f"- **Weight:** {weight}")
    
    if isinstance(technical_specs, dict):
        parts.append("\n## Technical Details")
        for key, value in technical_specs.items():
            parts.append(f"- **{key}:** {value}")
    elif isinstance(technical_specs, list):
        parts.append("\n## Technical Details")
        for spec in technical_specs:
            parts.append(f"- {spec}")
    elif technical_specs and str(technical_specs).strip():
        parts.append(f"\n## Technical Details\n{technical_specs}")
    
    return "\n".join(parts).strip()


def ensure_domain_tables(connection: Any) -> None:
    if domain_db is None or not hasattr(domain_db, "create_tables"):
        return
    domain_db.create_tables(connection, recreate=False)


def _next_entity_id(entities: list[dict[str, Any]]) -> str:
    existing = {
        str(entity.get("entity_id") or "").strip()
        for entity in entities
        if isinstance(entity, dict)
    }
    index = 1
    while f"e{index}" in existing:
        index += 1
    return f"e{index}"


def _normalized_class(raw_class_name: Any) -> str:
    return str(raw_class_name or "").strip().lower()


def _entity_attributes(entity: dict[str, Any]) -> dict[str, Any]:
    return entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}


def _has_rule_based_booking_context(document_payload: dict[str, Any]) -> bool:
    metadata = document_payload.get("metadata") if isinstance(document_payload.get("metadata"), dict) else {}
    direct_rule_names = metadata.get("processing_rule_names") if isinstance(metadata.get("processing_rule_names"), list) else []
    if any(str(item or "").strip() for item in direct_rule_names):
        return True
    user_metadata = metadata.get("user_metadata") if isinstance(metadata.get("user_metadata"), dict) else {}
    nested_rule_names = user_metadata.get("processing_rule_names") if isinstance(user_metadata.get("processing_rule_names"), list) else []
    if any(str(item or "").strip() for item in nested_rule_names):
        return True

    applications = metadata.get("post_extraction_rule_applications") if isinstance(metadata.get("post_extraction_rule_applications"), list) else []
    booking_keys = {
        "legal_entity_ref",
        "company_ref",
        "employer_ref",
        "current_employer_ref",
        "booking_debit_account_name",
        "booking_debit_account_number",
        "booking_particulars",
    }
    for item in applications:
        if not isinstance(item, dict):
            continue
        changed_keys = item.get("changed_keys") if isinstance(item.get("changed_keys"), list) else []
        for raw in changed_keys:
            key = str(raw or "").strip().lower()
            if key.startswith("document."):
                key = key[9:]
            if key.startswith("attributes."):
                key = key[11:]
            if key in booking_keys:
                return True
    return False


def _normalize_reference_context_values(
    values: dict[str, Any],
    entity_name_by_id: dict[str, str],
    *,
    reference_keys: tuple[str, ...],
) -> dict[str, Any]:
    normalized = dict(values or {})
    for key in reference_keys:
        raw_value = str(normalized.get(key) or "").strip()
        if not raw_value or not _looks_like_entity_reference_token(raw_value):
            continue
        resolved_name = str(entity_name_by_id.get(raw_value) or "").strip()
        if resolved_name:
            normalized[key] = resolved_name
        else:
            normalized.pop(key, None)
    return normalized


def _collect_reference_hints_from_entities(entities: list[Any]) -> dict[str, str]:
    reference_keys = ("legal_entity_ref", "company_ref", "employer_ref", "current_employer_ref")
    hints: dict[str, str] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        for key in reference_keys:
            raw = str(attrs.get(key) or "").strip()
            if not raw or _looks_like_entity_reference_token(raw):
                continue
            hints.setdefault(key, raw)
    company_like = str(
        hints.get("legal_entity_ref")
        or hints.get("company_ref")
        or hints.get("employer_ref")
        or hints.get("current_employer_ref")
        or ""
    ).strip()
    if company_like:
        hints.setdefault("legal_entity_ref", company_like)
        hints.setdefault("company_ref", company_like)
        hints.setdefault("employer_ref", company_like)
        hints.setdefault("current_employer_ref", company_like)
    return hints


def _to_decimal_amount(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value

    text = str(value).strip()
    if not text:
        return Decimal("0")

    text = text.replace("'", "").replace(" ", "")
    text = re.sub(r"[^0-9,\.\-]", "", text)
    if text.count(",") > 0 and text.count(".") == 0:
        text = text.replace(",", ".")
    elif text.count(",") > 0 and text.count(".") > 0:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "")
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _normalize_party_value(value: Any, entity_name_by_id: dict[str, str] | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if entity_name_by_id and _looks_like_entity_reference_token(text):
        text = str(entity_name_by_id.get(text) or text).strip()
    return re.sub(r"\s+", " ", text).strip().lower()


def _collect_party_values(
    source_attrs: dict[str, Any],
    keys: tuple[str, ...],
    entity_name_by_id: dict[str, str] | None = None,
) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = _normalize_party_value(source_attrs.get(key), entity_name_by_id=entity_name_by_id)
        if value:
            values.add(value)
    return values


def _infer_invoice_party_role(
    source_attrs: dict[str, Any],
    *,
    company_hint: str = "",
    entity_name_by_id: dict[str, str] | None = None,
) -> str:
    source_party_keys = (
        "issuer_name",
        "issuer",
        "supplier_name",
        "supplier_ref",
        "seller_name",
        "seller_ref",
        "billed_by",
        "from_company",
        "sender",
        "sender_name",
    )
    counterparty_keys = (
        "bill_to",
        "bill_to_name",
        "invoice_to",
        "invoice_to_name",
        "recipient",
        "recipient_name",
        "customer",
        "customer_name",
        "customer_ref",
        "payer",
        "payer_name",
        "payer_company",
        "payable_by",
        "debtor",
        "billed_to",
        "buyer",
        "buyer_name",
    )

    source_values = _collect_party_values(source_attrs, source_party_keys, entity_name_by_id=entity_name_by_id)
    counterparty_values = _collect_party_values(source_attrs, counterparty_keys, entity_name_by_id=entity_name_by_id)
    hint = _normalize_party_value(company_hint, entity_name_by_id=entity_name_by_id)

    if hint:
        if hint in source_values and hint not in counterparty_values:
            return "sales_invoice"
        if hint in counterparty_values and hint not in source_values:
            return "vendor_invoice"

    if source_values and not counterparty_values:
        return "sales_invoice"
    if counterparty_values and not source_values:
        return "vendor_invoice"

    if hint and hint in source_values:
        return "sales_invoice"
    if hint and hint in counterparty_values:
        return "vendor_invoice"

    return ""


def _ledger_lines_need_normalization(ledger_lines: Any) -> bool:
    if not isinstance(ledger_lines, list) or not ledger_lines:
        return True

    allowed_accounts = {1060, 1100, 2000, 2200, 3000, 3010, 3200, 4200, 6400, 6500}
    for line in ledger_lines:
        if not isinstance(line, dict):
            return True
        try:
            account_number = int(str(line.get("account_number") or "").strip())
        except ValueError:
            return True
        if account_number not in allowed_accounts:
            return True
    return False


def _resolve_booking_debit_account_number(source_attrs: dict[str, Any], document_type: str) -> int:
    for key in ("booking_debit_account_number", "debit_account_number", "expense_account_number", "account_number"):
        raw_value = source_attrs.get(key)
        if raw_value in (None, ""):
            continue
        try:
            account_number = int(str(raw_value).strip())
        except ValueError:
            account_number = 0
        if account_number > 0:
            return account_number

    account_name = str(
        source_attrs.get("booking_debit_account_name")
        or source_attrs.get("expense_account_name")
        or source_attrs.get("account_name")
        or ""
    ).strip().lower()
    if account_name:
        account_aliases = {
            "travel expense": 6400,
            "travel expenses": 6400,
            "travelling expense": 6400,
            "travelling expenses": 6400,
            "transport expense": 6400,
            "transport expenses": 6400,
            "general and administrative expense": 6500,
            "office and admin expense": 6500,
            "purchases": 4200,
        }
        for alias, account_number in account_aliases.items():
            if alias in account_name:
                return account_number

    if document_type in {"invoice", "bill", "receipt"}:
        return 4200
    if document_type == "ticket":
        return 6400
    return 6500


def _resolve_booking_credit_account_number(
    source_attrs: dict[str, Any],
    document_type: str,
    invoice_party_role: str | None = None,
) -> int:
    role = str(invoice_party_role or "").strip().lower()
    for key in ("booking_credit_account_number", "revenue_account_number", "sales_account_number", "credit_account_number", "account_number"):
        raw_value = source_attrs.get(key)
        if raw_value in (None, ""):
            continue
        try:
            account_number = int(str(raw_value).strip())
        except ValueError:
            account_number = 0
        if account_number > 0:
            return account_number

    account_name = str(
        source_attrs.get("booking_credit_account_name")
        or source_attrs.get("revenue_account_name")
        or source_attrs.get("sales_account_name")
        or source_attrs.get("account_name")
        or ""
    ).strip().lower()
    if account_name:
        account_aliases = {
            "service revenue": 3010,
            "services revenue": 3010,
            "domestic sales": 3000,
            "sales revenue": 3000,
            "export revenue": 3200,
            "export sales": 3200,
        }
        for alias, account_number in account_aliases.items():
            if alias in account_name:
                return account_number

    if role == "sales_invoice":
        return 3000
    if document_type in {"invoice", "bill", "receipt"}:
        return 2000
    if document_type == "ticket":
        return 2000
    return 2000


def _resolve_booking_receivable_account_number(source_attrs: dict[str, Any], document_type: str) -> int:
    for key in ("booking_debit_account_number", "receivable_account_number", "accounts_receivable_account_number", "debit_account_number", "account_number"):
        raw_value = source_attrs.get(key)
        if raw_value in (None, ""):
            continue
        try:
            account_number = int(str(raw_value).strip())
        except ValueError:
            account_number = 0
        if account_number > 0:
            return account_number

    account_name = str(
        source_attrs.get("booking_debit_account_name")
        or source_attrs.get("receivable_account_name")
        or source_attrs.get("account_name")
        or ""
    ).strip().lower()
    if account_name:
        account_aliases = {
            "accounts receivable": 1100,
            "trade receivables": 1100,
            "debtors": 1100,
            "customers": 1100,
        }
        for alias, account_number in account_aliases.items():
            if alias in account_name:
                return account_number

    if document_type in {"invoice", "bill", "receipt"}:
        return 1100
    return 1100
def _resolve_booking_particulars(source_attrs: dict[str, Any], fallback: str) -> str:
    return str(
        source_attrs.get("booking_particulars")
        or source_attrs.get("expense_particulars")
        or source_attrs.get("particulars")
        or fallback
    ).strip() or fallback


def _has_booking_rule_override(source_attrs: dict[str, Any]) -> bool:
    for key in (
        "booking_debit_account_number",
        "booking_debit_account_name",
        "booking_particulars",
        "legal_entity_ref",
        "company_ref",
        "employer_ref",
        "current_employer_ref",
    ):
        value = source_attrs.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def _is_payable_ticket_entity(entity: dict[str, Any] | None) -> bool:
    if not isinstance(entity, dict):
        return False
    class_name = _normalized_class(entity.get("class_name") or entity.get("entity_type"))
    attrs = _entity_attributes(entity)
    entity_name = str(entity.get("entity_name") or "").strip().lower()
    ticket_like = class_name == "ticket" or "ticket" in entity_name or any(
        key in attrs
        for key in (
            "ticket_id",
            "ticket_type",
            "fare_type",
            "class_of_service",
            "journey_type",
            "origin",
            "destination",
            "valid_from",
            "valid_until",
        )
    )
    if not ticket_like:
        return False
    for key in (
        "ticket_amount",
        "fare_amount",
        "gross_amount",
        "total_amount",
        "amount",
        "price",
        "fare",
    ):
        if _to_decimal_amount(attrs.get(key)) > 0:
            return True
    return False


def _is_ticket_like_entity(entity: dict[str, Any] | None) -> bool:
    if not isinstance(entity, dict):
        return False
    class_name = _normalized_class(entity.get("class_name") or entity.get("entity_type"))
    attrs = _entity_attributes(entity)
    entity_name = str(entity.get("entity_name") or "").strip().lower()
    return class_name == "ticket" or "ticket" in entity_name or any(
        key in attrs
        for key in (
            "ticket_id",
            "ticket_type",
            "fare_type",
            "class_of_service",
            "journey_type",
            "origin",
            "destination",
            "valid_from",
            "valid_until",
        )
    )


def _entity_has_positive_amount(entity: dict[str, Any] | None) -> bool:
    if not isinstance(entity, dict):
        return False
    attrs = _entity_attributes(entity)
    for key in (
        "total_amount",
        "gross_amount",
        "total_gross_amount",
        "invoice_total",
        "amount_total",
        "amount",
        "amount_chf",
        "price_chf",
        "ticket_amount",
        "fare_amount",
        "price",
        "fare",
    ):
        if _to_decimal_amount(attrs.get(key)) > 0:
            return True
    return False


def _extract_chf_amount_from_structured_tables(document_payload: dict[str, Any]) -> Decimal:
    metadata = document_payload.get("metadata") if isinstance(document_payload.get("metadata"), dict) else {}

    structured_sources: list[dict[str, Any]] = []
    structured = metadata.get("structured_tables")
    if isinstance(structured, dict):
        structured_sources.append(structured)

    user_metadata = metadata.get("user_metadata") if isinstance(metadata.get("user_metadata"), dict) else {}
    nested_structured = user_metadata.get("structured_tables")
    if isinstance(nested_structured, dict):
        structured_sources.append(nested_structured)

    chf_regex = re.compile(r"CHF\s*([0-9'.,]+)", re.IGNORECASE)
    best = Decimal("0")

    for source in structured_sources:
        tables = source.get("tables") if isinstance(source.get("tables"), list) else []
        for table in tables:
            rows = table.get("rows") if isinstance(table.get("rows"), list) else []
            for row in rows:
                cells = row if isinstance(row, list) else [row]
                for cell in cells:
                    text = str(cell or "").strip()
                    if not text:
                        continue
                    for match in chf_regex.findall(text):
                        amount = _to_decimal_amount(match)
                        if amount > best:
                            best = amount

    return best


def _is_booking_relevant_financial_entity(entity: dict[str, Any] | None) -> bool:
    if not isinstance(entity, dict):
        return False
    class_name = _normalized_class(entity.get("class_name") or entity.get("entity_type"))
    if class_name in {"invoice", "bill", "receipt"}:
        return True
    return _is_payable_ticket_entity(entity)


def _derive_summary_ledger_lines_from_amounts(
    source_attrs: dict[str, Any],
    document_type: str,
    invoice_party_role: str | None = None,
) -> list[dict[str, Any]]:
    role = str(invoice_party_role or source_attrs.get("invoice_party_role") or source_attrs.get("invoice_flow") or "").strip().lower()
    total = Decimal("0")
    for key in (
        "total_amount",
        "gross_amount",
        "total_gross_amount",
        "invoice_total",
        "amount_total",
        "amount",
        "amount_chf",
        "price_chf",
    ):
        total = _to_decimal_amount(source_attrs.get(key))
        if total > 0:
            break

    if total <= 0:
        return []

    vat_amount = Decimal("0")
    for key in ("vat_amount", "tax_amount", "total_tax_amount", "mwst_amount", "ust_amount"):
        vat_amount = _to_decimal_amount(source_attrs.get(key))
        if vat_amount > 0:
            break

    if vat_amount < 0:
        vat_amount = Decimal("0")
    if vat_amount > total:
        vat_amount = total

    base_amount = total - vat_amount
    currency = str(source_attrs.get("currency") or "CHF").strip().upper() or "CHF"


    if role == "sales_invoice":
        debit_account = _resolve_booking_receivable_account_number(source_attrs, document_type)
        credit_account = _resolve_booking_credit_account_number(source_attrs, document_type, invoice_party_role=role)
        debit_line_description = _resolve_booking_particulars(source_attrs, "Derived accounts receivable from invoice total")
        revenue_description = _resolve_booking_particulars(source_attrs, "Derived sales revenue from invoice total")

        lines: list[dict[str, Any]] = [
            {
                "account_number": debit_account,
                "direction": "debit",
                "source_currency": currency,
                "amount_source_currency": str(total),
                "exchange_rate_to_chf": "1",
                "amount_chf": str(total),
                "swiss_vat_code": "NONE",
                "line_description": debit_line_description,
            },
            {
                "account_number": credit_account,
                "direction": "credit",
                "source_currency": currency,
                "amount_source_currency": str(base_amount),
                "exchange_rate_to_chf": "1",
                "amount_chf": str(base_amount),
                "swiss_vat_code": "NONE",
                "line_description": revenue_description,
            },
        ]
        if vat_amount > 0:
            lines.append(
                {
                    "account_number": 2200,
                    "direction": "credit",
                    "source_currency": currency,
                    "amount_source_currency": str(vat_amount),
                    "exchange_rate_to_chf": "1",
                    "amount_chf": str(vat_amount),
                    "swiss_vat_code": "IPB81",
                    "line_description": "Derived output VAT from invoice total",
                }
            )
        return lines

    debit_account = _resolve_booking_debit_account_number(source_attrs, document_type)
    credit_account = _resolve_booking_credit_account_number(source_attrs, document_type, invoice_party_role=role)
    debit_line_description = _resolve_booking_particulars(source_attrs, "Derived base expense from invoice total")

    lines = [
        {
            "account_number": debit_account,
            "direction": "debit",
            "source_currency": currency,
            "amount_source_currency": str(base_amount),
            "exchange_rate_to_chf": "1",
            "amount_chf": str(base_amount),
            "swiss_vat_code": "NONE",
            "line_description": debit_line_description,
        }
    ]

    if vat_amount > 0:
        lines.append(
            {
                "account_number": 1060,
                "direction": "debit",
                "source_currency": currency,
                "amount_source_currency": str(vat_amount),
                "exchange_rate_to_chf": "1",
                "amount_chf": str(vat_amount),
                "swiss_vat_code": "IPB81",
                "line_description": "Derived input VAT from invoice total",
            }
        )

    lines.append(
        {
            "account_number": credit_account,
            "direction": "credit",
            "source_currency": currency,
            "amount_source_currency": str(total),
            "exchange_rate_to_chf": "1",
            "amount_chf": str(total),
            "swiss_vat_code": "NONE",
            "line_description": "Derived accounts payable from invoice total",
        }
    )

    return lines


def _append_policy_note(document_payload: dict[str, Any], note: dict[str, Any]) -> None:
    metadata = document_payload.get("metadata") if isinstance(document_payload.get("metadata"), dict) else {}
    notes = metadata.get("action_policy_notes") if isinstance(metadata.get("action_policy_notes"), list) else []
    notes.append(note)
    metadata["action_policy_notes"] = notes
    document_payload["metadata"] = metadata


def _append_enforcement_note(document_payload: dict[str, Any], note: dict[str, Any]) -> None:
    metadata = document_payload.get("metadata") if isinstance(document_payload.get("metadata"), dict) else {}
    notes = metadata.get("enforcement_notes") if isinstance(metadata.get("enforcement_notes"), list) else []
    notes.append(note)
    metadata["enforcement_notes"] = notes
    document_payload["metadata"] = metadata


def _derive_accounting_transaction_entity(routed: dict[str, Any], payload: dict[str, Any], class_defs: dict[str, Any]) -> None:
    document_type = str(routed.get("document_type") or "").strip().lower()
    if "accounting_transaction" not in class_defs:
        return

    entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
    document_payload = payload.get("document") if isinstance(payload.get("document"), dict) else {}
    force_booking_derivation = _has_rule_based_booking_context(document_payload)
    reference_hints = _collect_reference_hints_from_entities(entities)
    entity_name_by_id = {
        str(entity.get("entity_id") or "").strip(): str(entity.get("entity_name") or entity.get("name") or "").strip()
        for entity in entities
        if isinstance(entity, dict)
        and str(entity.get("entity_id") or "").strip()
        and str(entity.get("entity_name") or entity.get("name") or "").strip()
    }

    source_entity: dict[str, Any] | None = None
    source_candidates: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if _is_booking_relevant_financial_entity(entity) or (force_booking_derivation and _is_ticket_like_entity(entity)):
            source_candidates.append(entity)

    if source_candidates:
        source_entity = next((entity for entity in source_candidates if _entity_has_positive_amount(entity)), source_candidates[0])

    if (
        document_type not in {"invoice", "bill", "receipt"}
        and not _is_booking_relevant_financial_entity(source_entity)
        and not (force_booking_derivation and _is_ticket_like_entity(source_entity))
    ):
        return

    source_entity_type = _normalized_class((source_entity or {}).get("class_name") or (source_entity or {}).get("entity_type"))
    effective_document_type = source_entity_type or document_type
    if _is_payable_ticket_entity(source_entity) or (force_booking_derivation and _is_ticket_like_entity(source_entity)):
        effective_document_type = "ticket"

    # If LLM already extracted accounting_transaction, keep it and normalize
    # derived refs/ledger lines when booking directives were applied.
    existing_accounting_entities = [
        entity
        for entity in entities
        if isinstance(entity, dict)
        and _normalized_class(entity.get("class_name") or entity.get("entity_type")) == "accounting_transaction"
    ]
    if existing_accounting_entities:
        if source_entity is not None:
            source_attrs = _entity_attributes(source_entity)
            # Booking directives can be attached to accounting_transaction entities by runtime rules.
            # Merge those booking hints into source attrs before deriving summary ledger lines.
            merged_booking_attrs = dict(source_attrs)
            has_booking_override = _has_booking_rule_override(source_attrs)
            for accounting_entity in existing_accounting_entities:
                accounting_attrs = _entity_attributes(accounting_entity)
                if not _has_booking_rule_override(accounting_attrs):
                    continue
                has_booking_override = True
                for key in (
                    "booking_debit_account_number",
                    "booking_debit_account_name",
                    "booking_particulars",
                    "expense_account_number",
                    "expense_account_name",
                    "debit_account_number",
                    "account_number",
                    "particulars",
                ):
                    value = accounting_attrs.get(key)
                    if value not in (None, "", [], {}):
                        merged_booking_attrs[key] = value
            merged_booking_attrs = _normalize_reference_context_values(
                merged_booking_attrs,
                entity_name_by_id,
                reference_keys=("legal_entity_ref", "company_ref", "employer_ref", "current_employer_ref"),
            )
            for key, value in reference_hints.items():
                merged_booking_attrs.setdefault(key, value)
            should_normalize_existing = force_booking_derivation or any(
                _ledger_lines_need_normalization(
                    (entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}).get("ledger_lines")
                )
                for entity in existing_accounting_entities
            )
            should_propagate_reference_context = any(
                str(merged_booking_attrs.get(key) or "").strip()
                for key in ("legal_entity_ref", "company_ref", "employer_ref", "current_employer_ref")
            )
            if should_normalize_existing or has_booking_override or should_propagate_reference_context:
                if _to_decimal_amount(merged_booking_attrs.get("amount")) <= 0:
                    fallback_amount = _extract_chf_amount_from_structured_tables(document_payload)
                    if fallback_amount > 0:
                        merged_booking_attrs["amount"] = str(fallback_amount)
                        merged_booking_attrs["currency"] = merged_booking_attrs.get("currency") or "CHF"
                inferred_invoice_role = ""
                if effective_document_type in {"invoice", "bill"}:
                    inferred_invoice_role = _infer_invoice_party_role(
                        merged_booking_attrs,
                        company_hint=str(
                            merged_booking_attrs.get("legal_entity_ref")
                            or merged_booking_attrs.get("company_ref")
                            or merged_booking_attrs.get("employer_ref")
                            or merged_booking_attrs.get("current_employer_ref")
                            or ""
                        ),
                        entity_name_by_id=entity_name_by_id,
                    )
                if inferred_invoice_role:
                    merged_booking_attrs["invoice_party_role"] = inferred_invoice_role
                    merged_booking_attrs.setdefault("invoice_flow", inferred_invoice_role)
                normalized_lines = _derive_summary_ledger_lines_from_amounts(
                    merged_booking_attrs,
                    effective_document_type,
                    invoice_party_role=inferred_invoice_role,
                )
                for accounting_entity in existing_accounting_entities:
                    attrs = accounting_entity.get("attributes") if isinstance(accounting_entity.get("attributes"), dict) else {}
                    attrs = dict(attrs)
                    if normalized_lines:
                        attrs["ledger_lines"] = normalized_lines
                        if attrs.get("debit_total") in (None, "", 0):
                            attrs["debit_total"] = merged_booking_attrs.get("gross_amount")
                        if attrs.get("credit_total") in (None, "", 0):
                            attrs["credit_total"] = merged_booking_attrs.get("gross_amount")
                        if not attrs.get("transaction_date"):
                            attrs["transaction_date"] = merged_booking_attrs.get("invoice_date") or document_payload.get("doc_date")
                        if merged_booking_attrs.get("booking_particulars") and not attrs.get("description"):
                            attrs["description"] = merged_booking_attrs.get("booking_particulars")
                    for key in ("legal_entity_ref", "company_ref", "employer_ref", "current_employer_ref"):
                        value = merged_booking_attrs.get(key)
                        if value not in (None, "", [], {}):
                            attrs[key] = value
                    accounting_entity["attributes"] = attrs
                if normalized_lines:
                    _append_policy_note(
                        document_payload,
                        {
                            "policy": "accounting_transaction_required",
                            "status": "normalized_existing",
                            "reason": "unsupported_or_rule_selected",
                        },
                    )
                else:
                    _append_policy_note(
                        document_payload,
                        {
                            "policy": "accounting_transaction_required",
                            "status": "propagated_existing_context",
                            "reason": "rule_reference_context",
                        },
                    )
                payload["entities"] = entities
                payload["document"] = document_payload
        return

    if source_entity is None:
        _append_policy_note(document_payload, {"policy": "accounting_transaction_required", "status": "not_derived", "reason": "missing_financial_source_entity"})
        payload["document"] = document_payload
        return

    source_attrs = _entity_attributes(source_entity)
    ledger_lines = source_attrs.get("ledger_lines") if isinstance(source_attrs.get("ledger_lines"), list) else None
    if ledger_lines is None and isinstance(source_attrs.get("lines"), list):
        ledger_lines = source_attrs.get("lines")
    if not ledger_lines and (force_booking_derivation or _has_booking_rule_override(source_attrs) or _is_booking_relevant_financial_entity(source_entity)):
        if _to_decimal_amount(source_attrs.get("amount")) <= 0:
            fallback_amount = _extract_chf_amount_from_structured_tables(document_payload)
            if fallback_amount > 0:
                source_attrs = dict(source_attrs)
                source_attrs["amount"] = str(fallback_amount)
                source_attrs["currency"] = source_attrs.get("currency") or "CHF"
        inferred_invoice_role = ""
        if effective_document_type in {"invoice", "bill"}:
            inferred_invoice_role = _infer_invoice_party_role(
                source_attrs,
                company_hint=str(
                    source_attrs.get("legal_entity_ref")
                    or source_attrs.get("company_ref")
                    or source_attrs.get("employer_ref")
                    or source_attrs.get("current_employer_ref")
                    or ""
                ),
                entity_name_by_id=entity_name_by_id,
            )
        if inferred_invoice_role:
            source_attrs = dict(source_attrs)
            source_attrs["invoice_party_role"] = inferred_invoice_role
            source_attrs.setdefault("invoice_flow", inferred_invoice_role)
        ledger_lines = _derive_summary_ledger_lines_from_amounts(
            source_attrs,
            effective_document_type,
            invoice_party_role=inferred_invoice_role,
        )
        if ledger_lines:
            _append_policy_note(document_payload, {
                "policy": "accounting_transaction_required",
                "status": "derived_from_totals",
                "reason": "missing_ledger_lines_but_booking_rule_selected",
                "source_entity": source_entity.get("entity_name"),
            })
    if not ledger_lines:
        _append_policy_note(document_payload, {
            "policy": "accounting_transaction_required",
            "status": "not_derived",
            "reason": "missing_ledger_lines",
            "source_entity": source_entity.get("entity_name"),
        })
        payload["document"] = document_payload
        return

    source_name = str(source_entity.get("entity_name") or source_entity.get("name") or document_payload.get("doc_key") or document_type).strip()
    transaction_id = source_attrs.get("transaction_id") or source_attrs.get("invoice_no") or source_attrs.get("bill_no") or source_attrs.get("receipt_no") or document_payload.get("doc_key") or source_name
    transaction_date = source_attrs.get("transaction_date") or source_attrs.get("invoice_date") or source_attrs.get("payment_date") or source_attrs.get("value_date") or document_payload.get("document_effective_date") or document_payload.get("doc_date")
    entities_by_name = {
        str(entity.get("entity_name") or entity.get("name") or "").strip().lower(): entity
        for entity in entities
        if isinstance(entity, dict) and str(entity.get("entity_name") or entity.get("name") or "").strip()
    }
    relationships = payload.get("relationships") if isinstance(payload.get("relationships"), list) else []

    booking_context: dict[str, Any] = {}
    for key in (
        "legal_entity_ref",
        "company_ref",
        "employer_ref",
        "current_employer_ref",
        "person_ref",
        "employee_ref",
        "payer_ref",
        "owner_ref",
        "submitted_by_ref",
        "entered_by_ref",
    ):
        value = source_attrs.get(key)
        if value is not None:
            booking_context[key] = value

    entities_by_id = {
        str(entity.get("entity_id") or "").strip(): entity
        for entity in entities
        if isinstance(entity, dict) and str(entity.get("entity_id") or "").strip()
    }
    for key in (
        "legal_entity_ref",
        "company_ref",
        "employer_ref",
        "current_employer_ref",
        "person_ref",
        "employee_ref",
        "payer_ref",
        "owner_ref",
        "submitted_by_ref",
        "entered_by_ref",
    ):
        value = str(booking_context.get(key) or "").strip()
        if not _looks_like_entity_reference_token(value):
            continue
        candidate = entities_by_id.get(value)
        candidate_name = str((candidate or {}).get("entity_name") or (candidate or {}).get("name") or "").strip()
        if candidate_name:
            booking_context[key] = candidate_name

    booking_context = _normalize_reference_context_values(
        booking_context,
        entity_name_by_id,
        reference_keys=(
            "legal_entity_ref",
            "company_ref",
            "employer_ref",
            "current_employer_ref",
            "person_ref",
            "employee_ref",
            "payer_ref",
            "owner_ref",
            "submitted_by_ref",
            "entered_by_ref",
        ),
    )
    for key, value in reference_hints.items():
        booking_context.setdefault(key, value)

    inferred_invoice_role = ""
    if effective_document_type in {"invoice", "bill"}:
        inferred_invoice_role = _infer_invoice_party_role(
            source_attrs,
            company_hint=str(
                booking_context.get("legal_entity_ref")
                or booking_context.get("company_ref")
                or booking_context.get("employer_ref")
                or booking_context.get("current_employer_ref")
                or ""
            ),
            entity_name_by_id=entity_name_by_id,
        )
    if inferred_invoice_role:
        booking_context["invoice_party_role"] = inferred_invoice_role
        booking_context.setdefault("invoice_flow", inferred_invoice_role)

    person_name = ""
    for key in ("person_ref", "employee_ref", "payer_ref", "owner_ref", "submitted_by_ref", "entered_by_ref"):
        value = str(booking_context.get(key) or source_attrs.get(key) or "").strip()
        if value:
            person_name = value
            break

    if person_name:
        person_entity = entities_by_name.get(person_name.lower())
        person_attrs = _entity_attributes(person_entity) if isinstance(person_entity, dict) else {}
        employer_ref = str(
            booking_context.get("legal_entity_ref")
            or booking_context.get("company_ref")
            or booking_context.get("employer_ref")
            or booking_context.get("current_employer_ref")
            or person_attrs.get("current_employer_ref")
            or ""
        ).strip()
        if not employer_ref and isinstance(person_entity, dict):
            person_id = str(person_entity.get("entity_id") or "").strip()
            employer_types = {"employed_by", "works_for", "member_of", "position_in", "belongs_to_company"}
            company_like = {"company", "organization", "legal_entity"}
            for rel in relationships:
                if not isinstance(rel, dict):
                    continue
                if str(rel.get("source_entity_id") or "").strip() != person_id:
                    continue
                rel_type = str(rel.get("relationship_type") or "related_to").strip().lower()
                if rel_type not in employer_types:
                    continue
                target = entities_by_name.get(str(rel.get("target_name") or "").strip().lower())
                if target is None:
                    target_id = str(rel.get("target_entity_id") or "").strip()
                    for candidate in entities:
                        if isinstance(candidate, dict) and str(candidate.get("entity_id") or "").strip() == target_id:
                            target = candidate
                            break
                target_class = _normalized_class(target.get("class_name") or target.get("entity_type")) if isinstance(target, dict) else ""
                if target_class in company_like and isinstance(target, dict):
                    employer_ref = str(target.get("entity_name") or target.get("name") or "").strip()
                    break
        if employer_ref:
            booking_context.setdefault("person_ref", person_name)
            booking_context.setdefault("employee_ref", person_name)
            booking_context.setdefault("company_ref", employer_ref)
            booking_context.setdefault("employer_ref", employer_ref)
            booking_context.setdefault("current_employer_ref", employer_ref)
            booking_context.setdefault("legal_entity_ref", employer_ref)

    entities.append(
        {
            "entity_id": _next_entity_id(entities),
            "entity_name": f"posting:{source_name}",
            "class_name": "accounting_transaction",
            "operation": "ingest",
            "effective_from": transaction_date,
            "attributes": {
                "transaction_id": transaction_id,
                "source_type": effective_document_type,
                "transaction_date": transaction_date,
                "description": _resolve_booking_particulars(source_attrs, str(source_attrs.get("description") or document_payload.get("doc_theme") or source_name)),
                "source_document_ref": document_payload.get("doc_key") or source_name,
                "receipt_archive_url": document_payload.get("doc_path"),
                "currency": source_attrs.get("currency"),
                "ledger_lines": ledger_lines,
                "invoice_party_role": booking_context.get("invoice_party_role"),
                "invoice_flow": booking_context.get("invoice_flow"),
                "debit_total": source_attrs.get("gross_amount"),
                "credit_total": source_attrs.get("gross_amount"),
                **booking_context,
            },
            "confidence": source_entity.get("confidence"),
        }
    )
    _append_policy_note(document_payload, {"policy": "accounting_transaction_required", "status": "derived", "source_entity": source_entity.get("entity_name")})
    payload["entities"] = entities
    payload["document"] = document_payload


def _derive_hr_entities(routed: dict[str, Any], payload: dict[str, Any], class_defs: dict[str, Any]) -> None:
    entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
    relationships = payload.get("relationships") if isinstance(payload.get("relationships"), list) else []
    document_payload = payload.get("document") if isinstance(payload.get("document"), dict) else {}

    entity_by_id = {
        str(entity.get("entity_id") or "").strip(): entity
        for entity in entities
        if isinstance(entity, dict)
    }
    existing_classes = {
        _normalized_class(entity.get("class_name") or entity.get("entity_type"))
        for entity in entities
        if isinstance(entity, dict)
    }

    if "employee_profile" in class_defs and "employee_profile" not in existing_classes:
        for entity in list(entities):
            if not isinstance(entity, dict):
                continue
            if _normalized_class(entity.get("class_name") or entity.get("entity_type")) != "person":
                continue
            attrs = _entity_attributes(entity)
            if not any(attrs.get(key) for key in {"employee_no", "ahv_number", "source_tax_code", "employment_status"}):
                continue
            entities.append(
                {
                    "entity_id": _next_entity_id(entities),
                    "entity_name": str(entity.get("entity_name") or entity.get("name") or "employee").strip(),
                    "class_name": "employee_profile",
                    "operation": entity.get("operation") or "ingest",
                    "effective_from": entity.get("effective_from") or document_payload.get("document_effective_date") or document_payload.get("doc_date"),
                    "attributes": {
                        key: attrs.get(key)
                        for key in ["employee_no", "ahv_number", "permit_type", "source_tax_code", "current_employer_ref", "current_department_ref", "current_role_ref", "employment_status"]
                        if attrs.get(key) is not None
                    },
                    "confidence": entity.get("confidence"),
                }
            )
            _append_policy_note(document_payload, {"policy": "employee_profile_derivation", "status": "derived", "source_entity": entity.get("entity_name")})
            break

    if "employment_contract" not in class_defs or "employment_contract" in existing_classes:
        payload["entities"] = entities
        payload["document"] = document_payload
        return

    role_types = {"has_role", "holds_role", "assigned_role", "works_as", "position_in"}
    employer_types = {"employed_by", "works_for", "member_of", "position_in", "belongs_to_company"}
    department_types = {"belongs_to_department", "assigned_to_department", "in_department"}
    manager_types = {"reports_to", "managed_by"}
    person_like = {"person", "employee_profile"}
    company_like = {"company", "organization", "legal_entity"}
    role_like = {"role", "employment_position"}
    department_like = {"department"}

    for person_id, person_entity in entity_by_id.items():
        person_class = _normalized_class(person_entity.get("class_name") or person_entity.get("entity_type"))
        if person_class not in person_like:
            continue

        employer_ref = ""
        role_ref = ""
        department_ref = ""
        manager_ref = ""
        person_attrs = _entity_attributes(person_entity)
        role_entity_id = ""

        for rel in relationships:
            if not isinstance(rel, dict):
                continue
            rel_type = str(rel.get("relationship_type") or "related_to").strip().lower()
            src_id = str(rel.get("source_entity_id") or "")
            tar_id = str(rel.get("target_entity_id") or "")
            if src_id != person_id:
                continue

            target = entity_by_id.get(tar_id)
            target_class = _normalized_class(target.get("class_name") or target.get("entity_type")) if isinstance(target, dict) else ""
            target_name = str(target.get("entity_name") or target.get("name") or tar_id).strip() if isinstance(target, dict) else tar_id
            if not role_ref and rel_type in role_types and target_class in role_like:
                role_ref = target_name
                role_entity_id = tar_id
            if not employer_ref and rel_type in employer_types and target_class in company_like:
                employer_ref = target_name
            if not department_ref and rel_type in department_types and target_class in department_like:
                department_ref = target_name
            if not manager_ref and rel_type in manager_types and target_class in person_like:
                manager_ref = target_name

        if role_entity_id and not employer_ref:
            for rel in relationships:
                if not isinstance(rel, dict):
                    continue
                rel_type = str(rel.get("relationship_type") or "related_to").strip().lower()
                if str(rel.get("source_entity_id") or "") != role_entity_id:
                    continue
                target = entity_by_id.get(str(rel.get("target_entity_id") or ""))
                target_class = _normalized_class(target.get("class_name") or target.get("entity_type")) if isinstance(target, dict) else ""
                if rel_type in employer_types and target_class in company_like and isinstance(target, dict):
                    employer_ref = str(target.get("entity_name") or target.get("name") or "").strip()
                    break

        if not employer_ref:
            employer_ref = str(person_attrs.get("current_employer_ref") or "").strip()
        if not role_ref:
            role_ref = str(person_attrs.get("current_role_ref") or "").strip()
        if not department_ref:
            department_ref = str(person_attrs.get("current_department_ref") or "").strip()
        if not employer_ref:
            continue

        entities.append(
            {
                "entity_id": _next_entity_id(entities),
                "entity_name": f"employment:{person_entity.get('entity_name') or person_id}",
                "class_name": "employment_contract",
                "operation": "ingest",
                "effective_from": document_payload.get("document_effective_date") or document_payload.get("doc_date") or person_entity.get("effective_from"),
                "attributes": {
                    "person_ref": str(person_entity.get("entity_name") or person_id).strip(),
                    "employer_ref": employer_ref,
                    "department_ref": department_ref or None,
                    "role_ref": role_ref or None,
                    "manager_ref": manager_ref or None,
                    "employment_type": person_attrs.get("employment_type"),
                    "work_percentage": person_attrs.get("work_percentage"),
                    "salary_amount": person_attrs.get("salary_amount"),
                    "salary_currency": person_attrs.get("salary_currency"),
                    "source_tax_code": person_attrs.get("source_tax_code"),
                    "start_date": document_payload.get("document_effective_date") or document_payload.get("doc_date"),
                    "status": person_attrs.get("employment_status") or "active",
                },
                "confidence": person_entity.get("confidence"),
            }
        )
        _append_policy_note(document_payload, {"policy": "employment_contract_derivation", "status": "derived", "person": person_entity.get("entity_name"), "employer_ref": employer_ref, "role_ref": role_ref})
        break

    payload["entities"] = entities
    payload["document"] = document_payload


def apply_document_action_policy(routed: dict[str, Any], extracted: dict[str, Any], class_defs: dict[str, Any]) -> dict[str, Any]:
    payload = dict(extracted)
    payload["entities"] = list(extracted.get("entities") or [])
    payload["relationships"] = list(extracted.get("relationships") or [])
    payload["document"] = dict(extracted.get("document") or {}) if isinstance(extracted.get("document"), dict) else {}
    _derive_accounting_transaction_entity(routed, payload, class_defs)
    _derive_hr_entities(routed, payload, class_defs)
    return payload


def _payload_entity_classes(payload: dict[str, Any]) -> set[str]:
    return {
        _normalized_class(entity.get("class_name") or entity.get("entity_type"))
        for entity in (payload.get("entities") or [])
        if isinstance(entity, dict)
    }


def _find_policy_note_reason(document_payload: dict[str, Any], policy_name: str) -> str:
    metadata = document_payload.get("metadata") if isinstance(document_payload.get("metadata"), dict) else {}
    notes = metadata.get("action_policy_notes") if isinstance(metadata.get("action_policy_notes"), list) else []
    for note in reversed(notes):
        if not isinstance(note, dict):
            continue
        if str(note.get("policy") or "").strip() != policy_name:
            continue
        return str(note.get("reason") or note.get("status") or "policy_not_satisfied")
    return "policy_not_satisfied"


def _parse_required_domain_action_policy_string(policy_text: str) -> dict[str, str]:
    defaults = {
        "invoice": "fail",
        "bill": "fail",
        "receipt": "warn",
        "employment_contract": "fail",
        "employment_pattern": "fail",
    }
    raw_policy = str(policy_text or "").strip()
    if not raw_policy:
        return dict(defaults)
    parsed = dict(defaults)
    for item in raw_policy.split(","):
        chunk = item.strip()
        if not chunk or ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        policy_key = key.strip().lower()
        policy_value = value.strip().lower()
        if policy_key and policy_value in {"fail", "warn", "ignore"}:
            parsed[policy_key] = policy_value
    return parsed


def _parse_required_domain_action_policy() -> dict[str, str]:
    return _parse_required_domain_action_policy_string(REQUIRED_DOMAIN_ACTION_POLICY)


def _payload_has_employment_pattern(payload: dict[str, Any]) -> bool:
    entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
    relationships = payload.get("relationships") if isinstance(payload.get("relationships"), list) else []
    entity_by_id = {
        str(entity.get("entity_id") or "").strip(): entity
        for entity in entities
        if isinstance(entity, dict)
    }
    person_like = {"person", "employee_profile"}
    company_like = {"company", "organization", "legal_entity"}
    role_like = {"role", "employment_position"}
    employer_types = {"employed_by", "works_for", "member_of", "position_in", "belongs_to_company"}
    role_types = {"has_role", "holds_role", "assigned_role", "works_as", "position_in"}
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        rel_type = str(rel.get("relationship_type") or "related_to").strip().lower()
        src = entity_by_id.get(str(rel.get("source_entity_id") or ""))
        tar = entity_by_id.get(str(rel.get("target_entity_id") or ""))
        if not isinstance(src, dict) or not isinstance(tar, dict):
            continue
        src_class = _normalized_class(src.get("class_name") or src.get("entity_type"))
        tar_class = _normalized_class(tar.get("class_name") or tar.get("entity_type"))
        if src_class in person_like and tar_class in company_like and rel_type in employer_types:
            return True
        if src_class in person_like and tar_class in role_like and rel_type in role_types:
            return True
    return False


def enforce_required_domain_actions(
    routed: dict[str, Any],
    payload: dict[str, Any],
    class_defs: dict[str, Any],
    enforce_required_actions: bool | None = None,
    required_action_policy_text: str | None = None,
) -> None:
    if enforce_required_actions is None:
        enforce_required_actions = ENFORCE_REQUIRED_DOMAIN_ACTIONS
    if not enforce_required_actions:
        return

    document_type = str(routed.get("document_type") or "").strip().lower()
    document_payload = payload.get("document") if isinstance(payload.get("document"), dict) else {}
    entity_classes = _payload_entity_classes(payload)
    enforcement_policy = _parse_required_domain_action_policy() if required_action_policy_text is None else _parse_required_domain_action_policy_string(required_action_policy_text)
    failures: list[str] = []

    def handle_violation(policy_key: str, message: str) -> None:
        mode = enforcement_policy.get(policy_key, "fail")
        if mode == "ignore":
            _append_enforcement_note(document_payload, {"policy": policy_key, "mode": mode, "message": message})
            return
        if mode == "warn":
            LOGGER.warning("Required domain action warning [%s]: %s", policy_key, message)
            _append_enforcement_note(document_payload, {"policy": policy_key, "mode": mode, "message": message})
            return
        failures.append(f"{policy_key}: {message}")
        _append_enforcement_note(document_payload, {"policy": policy_key, "mode": mode, "message": message})

    if document_type in {"invoice", "bill", "receipt"} and "accounting_transaction" in class_defs:
        if "accounting_transaction" not in entity_classes:
            reason = _find_policy_note_reason(document_payload, "accounting_transaction_required")
            handle_violation(document_type, f"{document_type} requires accounting_transaction ({reason})")

    if document_type == "employment_contract" and "employment_contract" in class_defs:
        if "employment_contract" not in entity_classes:
            reason = _find_policy_note_reason(document_payload, "employment_contract_derivation")
            handle_violation("employment_contract", f"employment_contract document requires employment_contract entity ({reason})")

    if "employment_contract" in class_defs and _payload_has_employment_pattern(payload):
        if "employment_contract" not in entity_classes:
            reason = _find_policy_note_reason(document_payload, "employment_contract_derivation")
            handle_violation("employment_pattern", f"employment pattern detected but employment_contract was not derived ({reason})")

    payload["document"] = document_payload
    if failures:
        raise RuntimeError("Required domain action enforcement failed: " + "; ".join(failures))


def db_validate_ledger_payload(payload: Any) -> dict[str, Any] | bool:
    entity = solf_function._normalize_entity_payload(payload)
    if domain_db is None:
        return {"valid": False, "reason": "domain_db_unavailable"}
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    lines = domain_db.normalize_ledger_lines(attributes)
    legal_entity_ref = str(
        attributes.get("legal_entity_ref")
        or attributes.get("company_ref")
        or attributes.get("employer_ref")
        or attributes.get("current_employer_ref")
        or entity.get("legal_entity_ref")
        or ""
    ).strip() or None
    connection, owns_connection = solf_function._connection_scope()
    try:
        lines = domain_db.align_ledger_lines_to_chart_of_accounts(
            connection=connection,
            lines=lines,
            legal_entity_ref=legal_entity_ref,
            description_hint=str(attributes.get("description") or entity.get("object_name") or ""),
        )
        return domain_db.validate_double_entry(lines, connection=connection, legal_entity_ref=legal_entity_ref)
    finally:
        if owns_connection:
            connection.close()


def _resolve_company_from_relationships(connection: Any, person_ref: str) -> str:
    employer_types = {"employed_by", "works_for", "member_of", "position_in", "belongs_to_company"}
    company_like = {"company", "organization", "legal_entity"}
    role_like = {"role", "employment_position"}

    direct_relationships = object_db.get_relationships(connection, src_object_name=person_ref, limit=25)
    for rel in direct_relationships:
        rel_name = str(rel.get("relationship_name") or "").strip().lower()
        tar_class = _normalized_class(rel.get("tar_class_name"))
        if rel_name in employer_types and tar_class in company_like:
            return str(rel.get("tar_object_name") or "").strip()

    role_names = [
        str(rel.get("tar_object_name") or "").strip()
        for rel in direct_relationships
        if str(rel.get("relationship_name") or "").strip().lower() in employer_types
        and _normalized_class(rel.get("tar_class_name")) in role_like
    ]
    for role_name in role_names:
        role_relationships = object_db.get_relationships(connection, src_object_name=role_name, limit=25)
        for rel in role_relationships:
            rel_name = str(rel.get("relationship_name") or "").strip().lower()
            tar_class = _normalized_class(rel.get("tar_class_name"))
            if rel_name in employer_types and tar_class in company_like:
                return str(rel.get("tar_object_name") or "").strip()
    return ""


def _resolve_company_for_person(connection: Any, person_ref: str) -> str:
    normalized_person = str(person_ref or "").strip()
    if not normalized_person:
        return ""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT employer_ref
            FROM hr_employment
            WHERE LOWER(employee_ref) = LOWER(%s)
              AND employer_ref IS NOT NULL
              AND TRIM(employer_ref) <> ''
            ORDER BY updated_at DESC NULLS LAST, hr_employment_id DESC
            LIMIT 1
            """,
            (normalized_person,),
        )
        row = cursor.fetchone()
        if row and str(row[0] or "").strip():
            return str(row[0]).strip()

        cursor.execute(
            """
            SELECT hr.current_employer_ref
            FROM hr_employee hr
            JOIN object_instance oi ON oi.object_id = hr.object_id
            WHERE LOWER(oi.object_name) = LOWER(%s)
              AND hr.current_employer_ref IS NOT NULL
              AND TRIM(hr.current_employer_ref) <> ''
            ORDER BY hr.updated_at DESC NULLS LAST, hr.hr_employee_id DESC
            LIMIT 1
            """,
            (normalized_person,),
        )
        row = cursor.fetchone()
        if row and str(row[0] or "").strip():
            return str(row[0]).strip()

    return _resolve_company_from_relationships(connection, normalized_person)


def _looks_like_company_name(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return bool(
        re.search(
            r"\b(gmbh|ag|ltd|llc|inc|corp|corporation|company|co\.?|sarl|sa|bv|kg|gbr)\b",
            text,
        )
    )


def _looks_like_entity_reference_token(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return bool(re.fullmatch(r"e\d+", text))


def _resolve_doc_local_entity_name(connection: Any, doc_id: int | None, entity_token: str) -> str:
    token = str(entity_token or "").strip()
    if not _looks_like_entity_reference_token(token):
        return ""
    if doc_id is None or int(doc_id) <= 0:
        return ""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT object_name
            FROM object_instance
            WHERE metadata->>'doc_id' = %s
              AND metadata->>'entity_id' = %s
            ORDER BY entry_date DESC, object_id DESC
            LIMIT 1
            """,
            (str(int(doc_id)), token),
        )
        row = cursor.fetchone()
    if not row:
        return ""
    return str(row[0] or "").strip()


def _resolve_company_from_invoice_context(connection: Any, entity: dict[str, Any], attributes: dict[str, Any]) -> str:
    candidate_keys = (
        "bill_to",
        "bill_to_name",
        "bill_to_company",
        "invoice_to",
        "invoice_to_name",
        "recipient",
        "recipient_name",
        "customer",
        "customer_name",
        "customer_ref",
        "payer",
        "payer_name",
        "payer_company",
        "payable_by",
        "debtor",
    )

    candidates: list[str] = []
    seen: set[str] = set()
    for key in candidate_keys:
        value = attributes.get(key)
        text = str(value or "").strip()
        if not text:
            continue
        token = text.lower()
        if token in seen:
            continue
        seen.add(token)
        candidates.append(text)

    # As a last fallback for invoice-like entities, a company-style entity_name
    # is often the payer/recipient when dedicated ref fields are missing.
    class_name = _normalized_class(entity.get("class_name") or entity.get("entity_type"))
    entity_name = str(entity.get("entity_name") or entity.get("name") or "").strip()
    if class_name in {"invoice", "bill", "receipt"} and entity_name and _looks_like_company_name(entity_name):
        token = entity_name.lower()
        if token not in seen:
            candidates.append(entity_name)

    company_classes = {"company", "organization", "legal_entity"}
    for candidate in candidates:
        matches = object_db.search_objects(
            connection=connection,
            name_query=candidate,
            class_name=None,
            limit=20,
        )
        for match in matches:
            if not isinstance(match, dict):
                continue
            match_class = _normalized_class(match.get("class_name"))
            match_name = str(match.get("object_name") or "").strip()
            match_canonical = str(match.get("canonical_full_name") or "").strip()
            if match_class not in company_classes or not match_name:
                continue
            if match_name.lower() == candidate.lower() or (match_canonical and match_canonical.lower() == candidate.lower()):
                return candidate

    for candidate in candidates:
        if _looks_like_company_name(candidate):
            return candidate

    return ""


def resolve_accounting_booking_company(payload: Any) -> dict[str, Any]:
    entity = solf_function._normalize_entity_payload(payload)
    enriched = dict(entity)
    attributes = dict(entity.get("attributes") or {})
    enriched["attributes"] = attributes
    doc_id_raw = attributes.get("doc_id") or entity.get("doc_id")
    try:
        doc_id = int(doc_id_raw) if doc_id_raw is not None else None
    except (TypeError, ValueError):
        doc_id = None

    legal_entity_ref = str(
        attributes.get("legal_entity_ref")
        or attributes.get("company_ref")
        or attributes.get("employer_ref")
        or attributes.get("current_employer_ref")
        or entity.get("legal_entity_ref")
        or ""
    ).strip()
    connection, owns_connection = solf_function._connection_scope()
    try:
        if _looks_like_entity_reference_token(legal_entity_ref):
            resolved_name = _resolve_doc_local_entity_name(connection, doc_id, legal_entity_ref)
            if resolved_name and _looks_like_company_name(resolved_name):
                legal_entity_ref = resolved_name
            else:
                if resolved_name:
                    attributes.setdefault("person_ref", resolved_name)
                    attributes.setdefault("employee_ref", resolved_name)
                for key in ("legal_entity_ref", "company_ref", "employer_ref", "current_employer_ref"):
                    token_value = str(attributes.get(key) or "").strip()
                    if _looks_like_entity_reference_token(token_value):
                        attributes.pop(key, None)
                legal_entity_ref = ""

        if legal_entity_ref:
            attributes.setdefault("company_ref", legal_entity_ref)
            attributes.setdefault("employer_ref", legal_entity_ref)
            attributes.setdefault("current_employer_ref", legal_entity_ref)
            attributes["legal_entity_ref"] = legal_entity_ref
            return enriched

        inferred_company = _resolve_company_from_invoice_context(connection, enriched, attributes)
        if inferred_company:
            attributes.setdefault("company_ref", inferred_company)
            attributes.setdefault("employer_ref", inferred_company)
            attributes.setdefault("current_employer_ref", inferred_company)
            attributes["legal_entity_ref"] = inferred_company
            return enriched

        person_ref = ""
        for key in ("employee_ref", "person_ref", "payer_ref", "owner_ref", "submitted_by_ref", "entered_by_ref"):
            value = str(attributes.get(key) or entity.get(key) or "").strip()
            if not value:
                continue
            if _looks_like_entity_reference_token(value):
                resolved_name = _resolve_doc_local_entity_name(connection, doc_id, value)
                if resolved_name:
                    value = resolved_name
            person_ref = value
            break
        if not person_ref:
            return enriched

        employer_ref = _resolve_company_for_person(connection, person_ref)
    finally:
        if owns_connection:
            connection.close()

    if not employer_ref:
        return enriched

    attributes.setdefault("person_ref", person_ref)
    attributes.setdefault("employee_ref", person_ref)
    attributes.setdefault("company_ref", employer_ref)
    attributes.setdefault("employer_ref", employer_ref)
    attributes.setdefault("current_employer_ref", employer_ref)
    attributes["legal_entity_ref"] = employer_ref
    return enriched


def db_accounting_ingest(payload: Any) -> dict[str, Any] | bool:
    entity = resolve_accounting_booking_company(payload)
    if domain_db is None:
        LOGGER.error("domain_db module is required for db_accounting_ingest")
        return False
    connection, owns_connection = solf_function._connection_scope()
    try:
        object_row = solf_function.db_ingest(entity)
        if not isinstance(object_row, dict) or "object_id" not in object_row:
            return False
        domain_result = domain_db.upsert_transaction_and_lines(connection=connection, payload=entity, object_row=object_row)
        if not domain_result.get("ok"):
            return {
                "object_id": object_row.get("object_id"),
                "object_name": object_row.get("object_name"),
                "class_name": object_row.get("class_name"),
                "accounting_written": False,
                "accounting_validation": domain_result.get("validation"),
                "transaction_id": None,
                "ledger_lines_written": 0,
            }
        merged = dict(object_row)
        merged.update(
            {
                "accounting_written": True,
                "accounting_validation": domain_result.get("validation"),
                "transaction_id": domain_result.get("transaction_id"),
                "ledger_lines_written": domain_result.get("ledger_lines_written"),
            }
        )
        return merged
    except Exception:
        LOGGER.exception("db_accounting_ingest failed")
        return False
    finally:
        if owns_connection:
            connection.close()


def db_accounting_delete(payload: Any) -> dict[str, Any] | bool:
    entity = solf_function._normalize_entity_payload(payload)
    connection, owns_connection = solf_function._connection_scope()
    try:
        row = solf_function.db_delete(entity)
        if not isinstance(row, dict) or not row.get("deleted"):
            return row
        deleted_tx = 0
        if domain_db is not None:
            deleted_tx = domain_db.delete_transaction_by_object_id(connection, int(row["object_id"]))
        row["transactions_deleted"] = deleted_tx
        return row
    except Exception:
        LOGGER.exception("db_accounting_delete failed")
        return False
    finally:
        if owns_connection:
            connection.close()


def db_hr_validate_payload(payload: Any) -> dict[str, Any] | bool:
    entity = solf_function._normalize_entity_payload(payload)
    if domain_db is None:
        return {"valid": False, "reason": "domain_db_unavailable"}
    class_name = str(entity.get("class_name") or "").strip().lower()
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    connection, owns_connection = solf_function._connection_scope()
    try:
        return domain_db.validate_hr_payload(class_name, attributes, connection=connection)
    finally:
        if owns_connection:
            connection.close()


def db_hr_ingest(payload: Any) -> dict[str, Any] | bool:
    entity = solf_function._normalize_entity_payload(payload)
    if domain_db is None:
        LOGGER.error("domain_db module is required for db_hr_ingest")
        return False
    connection, owns_connection = solf_function._connection_scope()
    try:
        object_row = solf_function.db_ingest(entity)
        if not isinstance(object_row, dict) or "object_id" not in object_row:
            return False
        domain_result = domain_db.upsert_hr_domain_record(connection=connection, payload=entity, object_row=object_row)
        merged = dict(object_row)
        merged.update(
            {
                "hr_written": bool(domain_result.get("written")),
                "hr_table": domain_result.get("domain_table"),
                "hr_domain_id": domain_result.get("domain_id"),
            }
        )
        return merged
    except Exception:
        LOGGER.exception("db_hr_ingest failed")
        return False
    finally:
        if owns_connection:
            connection.close()


def db_hr_delete(payload: Any) -> dict[str, Any] | bool:
    entity = solf_function._normalize_entity_payload(payload)
    connection, owns_connection = solf_function._connection_scope()
    try:
        row = solf_function.db_delete(entity)
        if not isinstance(row, dict) or not row.get("deleted"):
            return row
        hr_deleted = {"hr_employee_deleted": 0, "hr_employment_deleted": 0}
        if domain_db is not None:
            hr_deleted = domain_db.delete_hr_records_by_object_id(connection, int(row["object_id"]))
        row.update(hr_deleted)
        return row
    except Exception:
        LOGGER.exception("db_hr_delete failed")
        return False
    finally:
        if owns_connection:
            connection.close()