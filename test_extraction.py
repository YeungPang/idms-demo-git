import os
from pathlib import Path
from dotenv import load_dotenv, find_dotenv
import nest_asyncio
from datetime import datetime
import time
import sys
import csv
import argparse

import requests
import json
import re
import uuid
import traceback
from contextlib import redirect_stdout, redirect_stderr
from llama_cloud_services import LlamaParse
from llama_index.core import SimpleDirectoryReader


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
nest_asyncio.apply()
    
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# def _env_bool(name: str, default: bool) -> bool:
#   raw = os.getenv(name)
#   if raw is None:
#     return default
#   return raw.strip().lower() in {"1", "true", "yes", "on"}


# def _env_list(name: str) -> list[str]:
#   raw = os.getenv(name, "")
#   return [item.strip() for item in raw.split(","), if item.strip()]


#OPENROUTER_PROVIDER_ORDER = _env_list("OPENROUTER_PROVIDER_ORDER")
#OPENROUTER_ALLOW_FALLBACKS = _env_bool("OPENROUTER_ALLOW_FALLBACKS", True)

OPENROUTER_PROVIDER_ORDER = ["Weights & Biases", "Alibaba Cloud Int", "Google Vertex", "Parasail", "NovitaAI", ]
OPENROUTER_ALLOW_FALLBACKS = "true"
#OPENROUTER_PROVIDER_ORDER = None

def _coerce_bool(value, default: bool = True) -> bool:
  if isinstance(value, bool):
    return value
  if value is None:
    return default
  return str(value).strip().lower() in {"1", "true", "yes", "on"}

LLM_MODEL = "qwen/qwen3-235b-a22b-2507"
#"openai/gpt-4o-mini"
#"openai/gpt-4o-mini"
#"google/gemini-3-flash-preview"
#"openai/gpt-5.4-nano"
#"meta-llama/llama-3.3-70b-instruct"
#"openai/gpt-4o-mini"
 #"deepseek/deepseek-v3.2"

ORIG_GENERAL_PROMPT = """
You are an information extraction engine for an IDMS (Information and Document Management System).

Extract only what is explicitly present in the document.
Do NOT infer schema names. Do NOT guess field names. Do NOT hallucinate.

============================================================
PRIMARY COMPANY CONTEXT (MANDATORY)
============================================================
The home company is: "Aphotonix GmbH". If given in the document, it would have a role of home_company.

Apply these rules for invoice-like documents:
- If an invoice is issued TO Aphotonix GmbH, classify it as "bill".
- If an invoice is issued BY Aphotonix GmbH, classify it as "invoice".

Always treat Aphotonix GmbH as the primary business context when deciding document category and party roles.

============================================================
EXTRACTION GOAL
============================================================
For every document, extract:
1. document metadata
2. keywords
3. main entities only
4. attributes of each main entity
5. relationships between main entities
6. multilingual qa_pairs grounded in extracted facts

IMPORTANT ENTITY SCOPE RULE:
- Output only main entities in the "entities" array.
- Sub-components of a main entity (for example: line items, table rows, amounts per row, addresses, identifiers, bank fields, contact fields) must be represented as attributes of the main entity.
- Do NOT output attribute parts as standalone entities.
- Make sure to inclue all main entities that are explicitly present in the document, even if they are not directly related to Aphotonix GmbH. For example, if the document contains a bill from a supplier to Aphotonix GmbH, you should extract both the supplier and the bill as main entities, even if the supplier is not directly related to Aphotonix GmbH in other parts of the document.

============================================================
CONTROLLED VOCABULARIES
============================================================

DOCUMENT CATEGORY (choose one or more):
employment
HR_record
organisation_profile
person_profile
crm_contact
crm_interaction
crm_lead
crm_opportunity
invoice
bill
quotation
receipt
purchase_order
sales_order
delivery_note
payment_record
accounting_record
financial_record
tax_declaration
tax_document
fine_record
contract
policy
employment_contract
lease_agreement
employment_agreement
employment_offer
employment_reference
job_description
job_application
employee_record
employee_profile
memo
meeting_minutes
email
announcement
event_record
administrative_record
government_record
general_information

ENTITY TYPES (choose closest):
PERSON
ORGANISATION
DEPARTMENT
ROLE
EMPLOYMENT
CUSTOMER
SUPPLIER
PRODUCT
SERVICE
PROJECT
TASK
EVENT
MEETING
DOCUMENT
EMAIL
INVOICE
BILL
QUOTATION
RECEIPT
ORDER
DELIVERY
PAYMENT
CONTRACT
POLICY
TAX_FORM
GOVERNMENT_ENTITY
OTHER

RELATIONSHIP TYPES (choose closest):
has_role
employed_by
manages
reports_to
member_of
part_of
contact_of
customer_of
supplier_of
purchased_from
sold_to
issued_by
issued_to
sent_by
sent_to
paid_by
paid_to
approved_by
related_to
scheduled_for
participates_in
responsible_for
action_of
other

============================================================
TABLE AND LINE-ITEM HANDLING (MANDATORY)
============================================================
- Do NOT output a top-level "tables" field.
- If the document contains tabular information, map each row to the relevant main entity attributes.
- Preserve all row values exactly as written.
- For each extracted row, include stable row/value identifiers inside entity attributes.

Use this JSON shape inside the relevant entity attributes:
"line_items": [
  {
    "line_item_id": "<unique_row_id>",
    "row_index": 1,
    "value_identifiers": {
      "<column_name>": "<exact_cell_value>",
      "<column_name_2>": "<exact_cell_value_2>"
    }
  }
]

If a table is not related to a financial line item, still represent it as an attribute list under the most relevant main entity using the same identifier pattern (line_item_id, row_index, value_identifiers).

============================================================
OUTPUT JSON SCHEMA
============================================================
Return one valid JSON object with this structure:

{
  "document_metadata": {
    "theme": "",
    "document_category": "", 
    "language": "",
    "date_issued": "",
    "valid_from": "",
    "valid_until": ""
  },
  "keywords": ["k1", "k2"],
  "entities": [
    {
      "entity_id": "",
      "entity_type": "",
      "entity_name": "",
      "attributes": {
        "key": "value",
        "line_items": [
          {
            "line_item_id": "",
            "row_index": 1,
            "value_identifiers": {
              "column_name": "value"
            }
          }
        ]
      },
      "roles": [],
      "source_text": "",
      "confidence": 0.0
    }
  ],
  "relationships": [
    {
      "source_entity": "",
      "relationship_type": "",
      "target_entity": "",
      "attributes": {},
      "source_text": "",
      "confidence": 0.0
    }
  ],
  "qa_pairs": [
    {
      "id": "",
      "type": "",
      "entity": "",
      "relationship": "",
      "attribute": "",
      "answer": "",
      "source_text": "",
      "confidence": 0.0,
      "questions": {
        "de": {
          "original": "",
          "paraphrases": [""]
        },
        "en": {
          "original": "",
          "paraphrases": [""]
        }
      }
    }
  ],
  "booking_recommendation": {
    "required_for_posting": true,
    "basis": "extracted_facts_only",
    "entries": [
      {
        "entry_id": "",
        "amount": "",
        "currency": "",
        "tax_code": "",
        "debit": {
          "account_number": "",
          "account_name": "",
          "account_type": "expense"
        },
        "credit": {
          "account_number": "",
          "account_name": "",
          "account_type": "liability"
        },
        "reasoning": "",
        "confidence": 0.0,
        "is_inferred": true
      }
    ]
  }
}

============================================================
CATEGORY REQUIREMENTS
============================================================

For invoice, bill, and receipt categories:
- Include all attributes needed for general ledger entries when present in the document.
- Minimum expected finance/accounting attributes on the main financial entity include:
  - document_number
  - issue_date
  - posting_date
  - due_date
  - payment_terms
  - currency
  - exchange_rate
  - net_amount
  - tax_amount
  - gross_amount or total_amount
  - tax_rate
  - tax_code
  - debit_account
  - credit_account
  - debit_account_type (one of: asset, liability, equity, revenue, expense)
  - credit_account_type (one of: asset, liability, equity, revenue, expense)
  - account_type_classification (object mapping each extracted account to one of: asset, liability, equity, revenue, expense)
  - cost_center
  - profit_center
  - project_code
  - reference_number
  - payment_reference
  - iban
  - bic
  - account_number
  - payment_recipient
  - payment_address
  - payment_status
  - line_items (with line_item_id, row_index, value_identifiers)
- include "due_date" if it not only appears explicitly in the document, but also when it can be computed from the presence of both:
  - the bill contains a payment term (e.g., “pay within 30 days”, “zahlbar innert 30 Tagen”)
  - AND the bill contains an issue date
  If both are present, compute the due date. Otherwise, do NOT guess.

If account information is missing in the source document (common for invoice/bill/receipt):
- Still provide "booking_recommendation" entries so downstream systems can prepare journal posting drafts.
- Prefer extracted values when present; otherwise infer only advisory booking fields.
- For inferred booking fields, always set "is_inferred": true and provide "reasoning" plus "confidence".
- Use account_type only from: asset, liability, equity, revenue, expense.
- If account_number is unknown, leave it empty and provide account_name + account_type suggestion.
- Never present inferred booking suggestions as if they were directly extracted facts.

For other document categories:
- Ensure required category-specific entities and attributes are included when explicitly present.
- Examples:
  - employment / HR: employee, employer, role, start/end dates, salary, department, contract references.
  - contract / lease / policy: contracting parties, effective/validity dates, obligations, terms, references.
  - tax / government: issuing authority, taxpayer/entity, identifiers, periods, assessed amounts, references.
  - CRM / correspondence: sender/recipient/contact, subject, event dates, actions, status, references.

============================================================
IDENTIFIER EXTRACTION RULES
============================================================
Extract critical identifiers wherever they appear (header, footer, paragraph, table, signature block, stamp).
Never omit identifiers even if they appear only once.

Critical identifiers include, but are not limited to:
- eori_number
- vat_number
- uid_number
- ahv_number
- tax_identification_number
- registration_number
- permit_number
- passport_number
- id_card_number
- iban
- bic
- contract_reference_number
- application_number
- case_number
- reference_number
- customs identifiers
- any alphanumeric code uniquely identifying a person, organisation, document, event, or posting

If uncertain, keep the value under "other_identifiers" in the relevant main entity attributes.

============================================================
BOOKING ADVISORY RULES
============================================================
For document_category invoice, bill, or receipt:
- You MUST output "booking_recommendation" even when account numbers are absent.
- Build recommendation entries from explicit document facts: party role, amounts, tax, payment direction, and document type.
- Typical advisory pattern examples:
  - supplier bill to Aphotonix GmbH: debit expense/asset, credit liability.
  - sales invoice by Aphotonix GmbH: debit asset (receivable), credit revenue.
  - receipt/payment evidence: classify by cash/bank movement and counterparty context.
- Keep these as recommendations only; do not claim they are source-extracted unless explicitly present.

============================================================
QA GENERATION RULES
============================================================
For every extracted fact, generate qa_pairs in document language and English.
- Keep answers exactly as in the source.
- Do not translate answer values.
- Use unique qa id values.
- Ensure "attribute" matches the exact attribute key used in entities.

============================================================
GLOBAL RULES
============================================================
- Use only explicit information from the document.
- Do NOT invent facts.
- Inferred values are allowed only inside "booking_recommendation" and must be clearly marked with "is_inferred": true.
- Do NOT normalize away identifier formatting.
- Output valid JSON only.
"""
GENERAL_PROMPT = """
You are an information extraction engine for an IDMS (Information and Document Management System).

Extract only what is explicitly present in the document.
Do NOT infer schema names. Do NOT guess field names. Do NOT hallucinate.

============================================================
PRIMARY COMPANY CONTEXT (MANDATORY)
============================================================
The home company is: "Aphotonix GmbH".

Apply these rules for invoice-like documents:
- If an invoice is issued TO Aphotonix GmbH, classify it as "bill".
- If an invoice is issued BY Aphotonix GmbH, classify it as "invoice".

Always treat Aphotonix GmbH as the primary business context when deciding document category and party roles.

============================================================
EXTRACTION GOAL
============================================================
For every document, extract:
1. document metadata
2. keywords
3. main entities only
4. attributes of each main entity
5. relationships between main entities
6. multilingual qa_pairs grounded in extracted facts
7. Events with date, venue, and involved parties (if explicitly present in the document)
8. Actions with actors, actions, objects, due dates, and status (if explicitly present in the document)

IMPORTANT ENTITY SCOPE RULE:
- Output only main entities in the "entities" array.
- Sub-components of a main entity (for example: line items, table rows, amounts per row, addresses, identifiers, bank fields, contact fields) must be represented as attributes of the main entity.
- Do NOT output attribute parts as standalone entities.

============================================================
CONTROLLED VOCABULARIES
============================================================

DOCUMENT TYPE (choose one or more):
employment
HR_record
organisation_profile
person_profile
crm_contact
crm_interaction
crm_lead
crm_opportunity
invoice
bill
quotation
receipt
purchase_order
sales_order
delivery_note
inventory_record
warehouse_record
payment_record
accounting_record
financial_record
journal_entry
tax_declaration
vat_return
social_contribution_report
tax_document
fine_record
contract
policy
employment_contract
lease_agreement
employment_agreement
employment_offer
employment_reference
job_description
job_application
employee_record
employee_profile
payroll_record
memo
meeting_minutes
email
announcement
event_record
administrative_record
government_record
workflow_case
approval_record
general_information
informational_record

ENTITY TYPES (choose closest):
PERSON
ORGANIZATION
LEGAL_ENTITY
COMPANY
INSTITUTION
GOVERNMENT_ENTITY
TAX_AUTHORITY
DEPARTMENT
ROLE
EMPLOYEE_PROFILE
EMPLOYMENT_CONTRACT
EMPLOYMENT_POSITION
CUSTOMER
SUPPLIER
PRODUCT
SERVICE
WAREHOUSE
INVENTORY_ITEM
PROJECT
PROJECT_TASK
PROJECT_MILESTONE
PROJECT_RISK
WORKFLOW_CASE
APPROVAL
COMPLIANCE_ACTION
EVENT
MEETING
VENUE
DOCUMENT
EMAIL
INVOICE
BILL
QUOTATION
RECEIPT
ORDER
DELIVERY
PAYMENT
CONTRACT
POLICY
AGREEMENT
TAX_FORM
VAT_RETURN
TAX_DECLARATION
SOCIAL_CONTRIBUTION_REPORT
PAYROLL_RECORD
ACCOUNT
JOURNAL_ENTRY
JOURNAL_LINE
OTHER

RELATIONSHIP TYPES (choose closest):
has_role
employed_by
has_contract
manages
reports_to
member_of
part_of
contact_of
customer_of
supplier_of
purchased_from
sold_to
issued_by
issued_to
billed_by
billed_to
sent_by
sent_to
paid_by
paid_to
approved_by
assigned_to
depends_on
belongs_to_project
recorded_in
references_document
hosted_at
requires_action
related_to
scheduled_for
participates_in
responsible_for
action_of
child_of
other

============================================================
TABLE AND LINE-ITEM HANDLING (MANDATORY)
============================================================
- Do NOT output a top-level "tables" field.
- If the document contains tabular information, map each row to the relevant main entity attributes.
- Preserve all row values exactly as written.
- For each extracted row, include stable row/value identifiers inside entity attributes.

Use this JSON shape inside the relevant entity attributes:
"line_items": [
  {
    "line_item_id": "<unique_row_id>",
    "row_index": 1,
    "value_identifiers": {
      "<column_name>": "<exact_cell_value>",
      "<column_name_2>": "<exact_cell_value_2>"
    }
  }
]

If a table is not related to a financial line item, still represent it as an attribute list under the most relevant main entity using the same identifier pattern (line_item_id, row_index, value_identifiers).

============================================================
OUTPUT JSON SCHEMA
============================================================
Return one valid JSON object with this structure:

{
  "document_metadata": {
    "theme": "",
    "document_type": "", 
    "language": "",
    "date_issued": "",
    "valid_from": "",
    "valid_until": ""
  },
  "keywords": ["k1", "k2"],
  "entities": [
    {
      "entity_id": "",
      "entity_type": "",
      "entity_name": "",
      "attributes": {
        "key": "value",
        "line_items": [
          {
            "line_item_id": "",
            "row_index": 1,
            "value_identifiers": {
              "column_name": "value"
            }
          }
        ]
      },
      "roles": [],
      "source_text": "",
      "confidence": 0.0
    }
  ],
  "relationships": [
    {
      "source_entity": "",
      "relationship_type": "",
      "target_entity": "",
      "attributes": {},
      "source_text": "",
      "confidence": 0.0
    }
  ],
  "qa_pairs": [
    {
      "id": "",
      "type": "",
      "entity": "",
      "relationship": "",
      "attribute": "",
      "answer": "",
      "source_text": "",
      "confidence": 0.0,
      "questions": {
        "de": {
          "original": "",
          "paraphrases": [""]
        },
        "en": {
          "original": "",
          "paraphrases": [""]
        }
      }
    }
  ],
  "booking_recommendation": {
    "required_for_posting": true,
    "basis": "extracted_facts_only",
    "entries": [
      {
        "entry_id": "",
        "amount": "",
        "currency": "",
        "tax_code": "",
        "debit": {
          "account_number": "",
          "account_name": "",
          "account_type": "expense"
        },
        "credit": {
          "account_number": "",
          "account_name": "",
          "account_type": "liability"
        },
        "reasoning": "",
        "confidence": 0.0,
        "is_inferred": true
      }
    ]
  }
}

============================================================
CATEGORY REQUIREMENTS
============================================================

For invoice, bill, and receipt document types:
- Include all attributes needed for general ledger entries when present in the document.
- Minimum expected finance/accounting attributes on the main financial entity include:
  - document_number
  - issue_date
  - posting_date
  - payment_terms
  - currency
  - exchange_rate
  - net_amount
  - tax_amount
  - gross_amount or total_amount
  - tax_rate
  - tax_code
  - debit_account
  - credit_account
  - debit_account_type (one of: asset, liability, equity, revenue, expense)
  - credit_account_type (one of: asset, liability, equity, revenue, expense)
  - account_type_classification (object mapping each extracted account to one of: asset, liability, equity, revenue, expense)
  - cost_center
  - profit_center
  - project_code
  - reference_number
  - payment_reference
  - iban
  - bic
  - account_number
  - payment_recipient
  - payment_address
  - payment_status
  - line_items (with line_item_id, row_index, value_identifiers)
- include "due_date" if it not only appears explicitly in the document, but also when it can be computed from the presence of both:
  - the bill contains a payment term (e.g., “pay within 30 days”, “zahlbar innert 30 Tagen”)
  - AND the bill contains an issue date
  If both are present, compute the due date. Otherwise, do NOT guess.

If account information is missing in the source document (common for invoice/bill/receipt):
- Still provide "booking_recommendation" entries so downstream systems can prepare journal posting drafts.
- Prefer extracted values when present; otherwise infer only advisory booking fields.
- For inferred booking fields, always set "is_inferred": true and provide "reasoning" plus "confidence".
- Use account_type only from: asset, liability, equity, revenue, expense.
- If account_number is unknown, leave it empty and provide account_name + account_type suggestion.
- Never present inferred booking suggestions as if they were directly extracted facts.

For other document types:
- Ensure required type-specific entities and attributes are included when explicitly present.
- Examples:
  - employment / HR: employee, employer, role, start/end dates, salary, department, contract references.
  - contract / lease / policy: contracting parties, effective/validity dates, obligations, terms, references.
  - tax / government: issuing authority, taxpayer/entity, identifiers, periods, assessed amounts, references.
  - CRM / correspondence: sender/recipient/contact, subject, event dates, actions, status, references.

============================================================
IDENTIFIER EXTRACTION RULES
============================================================
Extract critical identifiers wherever they appear (header, footer, paragraph, table, signature block, stamp).
Never omit identifiers even if they appear only once.

Critical identifiers include, but are not limited to:
- eori_number
- vat_number
- uid_number
- ahv_number
- tax_identification_number
- registration_number
- permit_number
- passport_number
- id_card_number
- iban
- bic
- contract_reference_number
- application_number
- case_number
- reference_number
- customs identifiers
- any alphanumeric code uniquely identifying a person, organisation, document, event, or posting

If uncertain, keep the value under "other_identifiers" in the relevant main entity attributes.

============================================================
BOOKING ADVISORY RULES
============================================================
For document_category invoice, bill, or receipt:
- You MUST output "booking_recommendation" even when account numbers are absent.
- Build recommendation entries from explicit document facts: party role, amounts, tax, payment direction, and document type.
- Typical advisory pattern examples:
  - supplier bill to Aphotonix GmbH: debit expense/asset, credit liability.
  - sales invoice by Aphotonix GmbH: debit asset (receivable), credit revenue.
  - receipt/payment evidence: classify by cash/bank movement and counterparty context.
- Keep these as recommendations only; do not claim they are source-extracted unless explicitly present.

============================================================
QA GENERATION RULES
============================================================
For every extracted fact, generate qa_pairs in document language and English.
- Keep answers exactly as in the source.
- Do not translate answer values.
- Use unique qa id values.
- Ensure "attribute" matches the exact attribute key used in entities.

============================================================
GLOBAL RULES
============================================================
- Use only explicit information from the document.
- Do NOT invent facts.
- Inferred values are allowed only inside "booking_recommendation" and must be clearly marked with "is_inferred": true.
- Do NOT normalize away identifier formatting.
- Output valid JSON only.
"""

"""
If any critical identifier appears anywhere in the document (header, footer, table, paragraph, stamp, signature block), you MUST extract it.
Never omit identifiers even if they appear only once.

E. Tables
   Extract all tables as structured rows only when explicitly present in the document with rows and columns. Each row must contain column–value mappings.

  "tables": [
    {
      "table_name": "",
      "rows": [
        {
          "row_index": 1,
          "cells": {
            "column_name": "value"
          }
        }
      ]
    }
  ],

6. Tables (only if present within the document with rows and columns)
- Extract every table as structured rows.
- Do NOT summarize or merge rows.
- Each row must have:
  - "row_index": the row number (starting at 1)
  - "cells": a mapping of "column_name" → "value" exactly as in the table.

4. Synonyms
   - A list of synonyms or related terms.
     This should include any important terms that have multiple forms or variations in the document.
     For terms already included in the keywords list, they should not be included in the synonyms list and only the synonyms that have corresponding entries in the keywords list should be included.
     For example, if the document contains the term "salary", the synonyms mapping might include "wage", "income", etc.
     If the document contains terms in multiple languages, include synonyms in their original language form as well as their English translations.
"""
"""============================================================
CRITICAL IDENTIFIER RULES (MUST EXTRACT)
============================================================
Critical identifiers include (but are not limited to):
- EORI number
- VAT number
- UID number (CHE-xxx.xxx.xxx)
- AHV number
- tax identification number
- registration number
- permit number
- passport number
- ID card number
- bank account IBAN
- bank account BIC
- contract reference numbers
- application numbers
- case numbers
- reference numbers
- customs identifiers
- any alphanumeric code uniquely identifying a person, organisation, or document

If unsure whether a value is an identifier, include it in "other_identifiers".

4. identifiers_key_value_pairs
   - A map of critical identifiers as well as important information that appear anywhere in the document (header, footer, table, paragraph, stamp, signature block).
   - Important information includes any alphanumeric code that uniquely identifies a person, organisation, document, or specific time and date and location of an event, specific action, special term, specific amount, specific refernce, .

"""

BILL_PROMPT = """
You are an information extraction system. Extract all entities, attributes, and relationships from the bill document.
The document may contain multiple languages. Do not translate attribute names. Use attribute names exactly as they appear in the text.
Do NOT infer schema names. Do NOT guess field names.

You should include "due_date" if it not only appears explicitly in the document, but also when it can be computed from the presence of both:
- the bill contains a payment term (e.g., “pay within 30 days”, “zahlbar innert 30 Tagen”)
- AND the bill contains an issue date
If both are present, compute the due date. Otherwise, do NOT guess.

Use ONLY the following controlled vocabularies.

============================================================
ENTITY TYPES
============================================================
BILL, INVOICE, RECEIPT, QUOTATION,
ORGANISATION, PERSON,
PRODUCT, SERVICE, LINE_ITEM,
DATE, NUMBER, CURRENCY, BANK_ACCOUNT, OTHER

============================================================
DOCUMENT CATEGORY
============================================================
bill, invoice, receipt, quotation,
financial_record, accounting_record, general_information

============================================================
RELATIONSHIP TYPES
============================================================
billed_to, billed_from,
issued_by, issued_to,
paid_by, paid_to,
contains_item, related_to, other

============================================================
OUTPUT FORMAT
============================================================

Return a JSON object with:

1. theme (the document theme in the language of the document)
2. document_category

3. entities: [
    {
      "entity_id": "e1",
      "entity_type": "...",
      "entity_name": "...",
      "attributes": {
          "bill_number": "...",
          "issue_date": "...",
          "due_date": "...",
          "computed_due_date": "...",
          "payment_terms": "...",
          "total_amount": "...",
          "net_amount": "...",
          "tax_amount": "...",
          "currency": "...",
          "reason_for_payment": "...",
          "bank_account": "...",
          "iban": "...",
          "bic": "...",
          "account_number": "...",
          "line_items": [
              {
                "description": "...",
                "quantity": "...",
                "unit_price": "...",
                "total": "..."
              }
          ]
      },
      "source_text": "...",
      "confidence": 0.0-1.0
    }
]

4. relationships: [
    {
      "relationship_type": "...",
      "source_entity_id": "e1",
      "target_entity_id": "e2",
      "attributes": {},
      "source_text": "...",
      "confidence": 0.0-1.0
    }
]

============================================================
RULES
============================================================
- Use only the controlled vocabularies above.
- Compute "computed_due_date" ONLY when both issue date and payment terms are present.
- Do NOT invent information.
- Do NOT normalize or translate attribute names.
- Use only information explicitly present in the document.
- Output valid JSON only.
"""
RECEIPT_PROMPT = """
You are an information extraction system. Extract all entities, attributes, and relationships from the receipt document.
The document may contain multiple languages. Do not translate attribute names. Use attribute names exactly as they appear in the text.
Do NOT infer schema names. Do NOT guess field names.

Use ONLY the following controlled vocabularies.

============================================================
ENTITY TYPES
============================================================
RECEIPT, INVOICE, BILL,
ORGANISATION, PERSON,
PRODUCT, SERVICE, LINE_ITEM,
DATE, NUMBER, CURRENCY, PAYMENT_METHOD, OTHER

============================================================
DOCUMENT CATEGORY
============================================================
receipt, invoice, bill,
financial_record, accounting_record, general_information

============================================================
RELATIONSHIP TYPES
============================================================
paid_by, paid_to,
issued_by, issued_to,
contains_item, related_to, other

============================================================
OUTPUT FORMAT
============================================================

Return a JSON object with:

1. theme
2. document_category

3. entities: [
    {
      "entity_id": "e1",
      "entity_type": "...",
      "entity_name": "...",
      "attributes": {
          "receipt_number": "...",
          "payment_date": "...",
          "total_amount": "...",
          "currency": "...",
          "payment_method": "...",
          "transaction_id": "...",
          "reason_for_payment": "...",
          "line_items": [
              {
                "description": "...",
                "quantity": "...",
                "unit_price": "...",
                "total": "..."
              }
          ]
      },
      "source_text": "...",
      "confidence": 0.0-1.0
    }
]

4. relationships: [
    {
      "relationship_type": "...",
      "source_entity_id": "e1",
      "target_entity_id": "e2",
      "attributes": {},
      "source_text": "...",
      "confidence": 0.0-1.0
    }
]

============================================================
RULES
============================================================
- Use only the controlled vocabularies above.
- Do NOT invent information.
- Do NOT normalize or translate attribute names.
- Use only information explicitly present in the document.
- Output valid JSON only.
"""
QUOTATION_PROMPT = """
You are an information extraction system. Extract all entities, attributes, and relationships from the quotation document.
The document may contain multiple languages. Do not translate attribute names. Use attribute names exactly as they appear in the text.
Do NOT infer schema names. Do NOT guess field names.

Use ONLY the following controlled vocabularies.

============================================================
ENTITY TYPES
============================================================
QUOTATION, INVOICE, BILL,
ORGANISATION, PERSON,
PRODUCT, SERVICE, LINE_ITEM,
DATE, NUMBER, CURRENCY, OTHER

============================================================
DOCUMENT CATEGORY
============================================================
quotation, invoice, bill,
financial_record, accounting_record, general_information

============================================================
RELATIONSHIP TYPES
============================================================
issued_by, issued_to,
sent_by, sent_to,
contains_item, related_to, other

============================================================
OUTPUT FORMAT
============================================================

Return a JSON object with:

1. theme
2. document_category

3. entities: [
    {
      "entity_id": "e1",
      "entity_type": "...",
      "entity_name": "...",
      "attributes": {
          "quotation_number": "...",
          "issue_date": "...",
          "valid_until": "...",
          "total_amount": "...",
          "currency": "...",
          "payment_terms": "...",
          "line_items": [
              {
                "description": "...",
                "quantity": "...",
                "unit_price": "...",
                "total": "..."
              }
          ]
      },
      "source_text": "...",
      "confidence": 0.0-1.0
    }
]

4. relationships: [
    {
      "relationship_type": "...",
      "source_entity_id": "e1",
      "target_entity_id": "e2",
      "attributes": {},
      "source_text": "...",
      "confidence": 0.0-1.0
    }
]

============================================================
RULES
============================================================
- Use only the controlled vocabularies above.
- Do NOT invent information.
- Do NOT normalize or translate attribute names.
- Use only information explicitly present in the document.
- Output valid JSON only.
"""

INVOICE_PROMPT = """
You are an information extraction system. Extract all entities, attributes, and relationships from the invoice document.
The document may contain multiple languages (e.g., German). Do not translate attribute names. Use attribute names exactly as they appear in the text.

IMPORTANT:
The document may contain Markdown tables. You MUST parse all tables and extract all financial and payment information contained inside them.

============================================================
MANDATORY DUE DATE COMPUTATION
============================================================
If "due_date" cannot be identified, you MUST compute "due_date" if BOTH:
- an issue date exists, AND
- payment terms exist (e.g., “zahlbar innert 30 Tagen”, “30 Tage netto”, “Net 30”, “pay within 30 days”)
If either is missing, set "due_date" to null.

============================================================
MANDATORY PAYMENT DETAIL EXTRACTION
============================================================
You MUST extract all payment-related information, including:
- IBAN
- BIC
- account number
- payment address
- payment recipient
- payment reference (e.g., Swiss QR reference numbers)
- “Konto / Zahlbar an” blocks
- QR-bill payment section fields

Do NOT invent missing fields.

============================================================
ENTITY TYPES
============================================================
INVOICE, ORGANISATION, PERSON,
PRODUCT, SERVICE, LINE_ITEM,
BANK_ACCOUNT, DATE, NUMBER, CURRENCY, OTHER

============================================================
DOCUMENT CATEGORY
============================================================
invoice, financial_record, accounting_record

============================================================
RELATIONSHIP TYPES
============================================================
issued_by, issued_to, billed_to, billed_from,
paid_by, paid_to, contains_item, related_to, other

============================================================
OUTPUT FORMAT
============================================================

Return a JSON object with:

1. theme
2. document_category

3. entities: [
  {
    "entity_id": "e1",
    "entity_type": "...",
    "entity_name": "...",
    "attributes": {
        "invoice_number": "...",
        "issue_date": "...",
        "due_date": "...",                // only if explicitly present
        "payment_terms": "...",
        "computed_due_date": "...",       // MUST compute when possible

        // Payment details
        "iban": "...",
        "bic": "...",
        "account_number": "...",
        "payment_address": "...",
        "payment_recipient": "...",
        "payment_reference": "...",

        // Amounts
        "total_amount": "...",
        "net_amount": "...",
        "tax_amount": "...",
        "currency": "...",

        // Line items
        "line_items": [
            {
              "description": "...",
              "quantity": "...",
              "unit_price": "...",
              "total": "..."
            }
        ]
    },
    "source_text": "...",
    "confidence": 0.0-1.0
  }
]

4. relationships: [
  {
    "relationship_type": "...",
    "source_entity_id": "e1",
    "target_entity_id": "e2",
    "attributes": {},
    "source_text": "...",
    "confidence": 0.0-1.0
  }
]

============================================================
RULES
============================================================
- You MUST compute computed_due_date when possible.
- You MUST extract all payment details, including IBAN/BIC/payment address.
- You MUST parse Markdown tables.
- Do NOT invent information.
- Output valid JSON only.
"""
mparser = LlamaParse(
    result_type="markdown"  # "markdown" and "text" are available  
)

pparser = LlamaParse(
    result_type="markdown",  # "markdown" and "text" are available
    premium_mode=True
)

def load_input_file(path: str, mode: str = "premium", max_retries: int = 3) -> tuple[str | None, int]:
    """
    Load input file using LlamaParse with retry logic for transient failures.
    
    Args:
        path: Path to the input file
        mode: Parsing mode - 'simple' (txt/md), 'medium', or 'premium' (default)
        max_retries: Maximum number of retry attempts (default 3)
    
    Returns:
        Tuple of (file content as string or None on failure, attempt number on which it succeeded or 0 on failure)
    """
    # Auto-detect mode based on file extension
    sp = path.split('.')
    ft = sp[-1].lower() if sp else ""
    if ft in ['txt', 'md']:
        mode = 'simple'

    use_llamaparse = mode in ['medium', 'premium']
    
    for attempt in range(max_retries):
        try:
            if mode == 'simple':
                reader = SimpleDirectoryReader(input_files=[path]).load_data()
            elif mode == 'medium':
                reader = mparser.load_data(path)
            else:  # premium mode
                reader = pparser.load_data(path)
            
            if reader and len(reader) > 0:
                content_parts = [doc.get_content() for doc in reader]
                content = "\n\n".join(part for part in content_parts if part)

                # Persist parsed markdown only for LlamaParse parsing modes.
                if use_llamaparse:
                    md_output_path = f"{os.path.splitext(path)[0]}.md"
                    with open(md_output_path, "w", encoding="utf-8") as md_file:
                        md_file.write(content)
                    print(f"Saved parsed markdown to: {md_output_path}")
                    print(f"Combined {len(content_parts)} parsed page(s) into markdown output.")

                return content, attempt + 1
            else:
                raise ValueError("No content returned from parser")
        
        except Exception as exc:
            if attempt < max_retries - 1:
                wait_seconds = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                print(f"Attempt {attempt + 1}/{max_retries} failed: {exc}")
                print(f"Retrying in {wait_seconds} seconds...")
                time.sleep(wait_seconds)
            else:
                print(f"All {max_retries} attempts failed. Returning None.")
                print(f"Last error: {exc}")
                return None, 0
    return None, 0

def extract_from_document(document_text, prompt=GENERAL_PROMPT):
    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": LLM_MODEL,   # cheapest + best for extraction
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": document_text}
        ],
        "temperature": 0.0,  # deterministic extraction
        "response_format": {"type": "json_object"}
    }

    # Optional provider routing controls for OpenRouter.
    if OPENROUTER_PROVIDER_ORDER:
        payload["provider"] = {
            "order": OPENROUTER_PROVIDER_ORDER,
            "allow_fallbacks": _coerce_bool(OPENROUTER_ALLOW_FALLBACKS),
        }

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 400 and payload.get("provider"):
        print("Provider routing rejected by OpenRouter. Retrying with default auto-routing.")
        print(f"Provider error response: {response.text}")
        payload.pop("provider", None)
        response = requests.post(url, headers=headers, json=payload)

    response.raise_for_status()

    data = response.json()

    # OpenRouter chat/completions standard shape
    if isinstance(data, dict) and data.get("choices"):
      content = data["choices"][0]["message"]["content"]
      return json.loads(content)

    # Some providers can proxy non-chat-like envelopes.
    if isinstance(data, dict) and data.get("output"):
      output = data.get("output") or []
      if output and isinstance(output[0], dict):
        first = output[0]
        if first.get("content") and isinstance(first["content"], list):
          text_parts = [
            part.get("text", "")
            for part in first["content"]
            if isinstance(part, dict)
          ]
          content = "".join(text_parts).strip()
          if content:
            return json.loads(content)

    # Surface the actual response body when choices are missing.
    raise RuntimeError(
      "OpenRouter response did not contain expected completion content. "
      f"status={response.status_code}, body={json.dumps(data)[:1200]}"
    )

def test_llm_extraction(document_text):
    print(f"Model: {LLM_MODEL}")
    if OPENROUTER_PROVIDER_ORDER:
        print(
            "Provider routing: "
            f"order={OPENROUTER_PROVIDER_ORDER}, "
      f"allow_fallbacks={_coerce_bool(OPENROUTER_ALLOW_FALLBACKS)}"
        )
    else:
        print("Provider routing: OpenRouter default auto-routing")
    startts = time.time()
    result = extract_from_document(document_text)
    duration = time.time() - startts
    print(f"Extraction took {duration:.2f} seconds")
    return result


class Tee:
  def __init__(self, *streams):
    self.streams = streams

  def write(self, data):
    for stream in self.streams:
      stream.write(data)
    return len(data)

  def flush(self):
    for stream in self.streams:
      stream.flush()



def parse_cli_args():
  parser = argparse.ArgumentParser(description="Run extraction for a text file and append output to a log file")
  parser.add_argument("input_path", help="Path to the input text/markdown file")
  parser.add_argument(
    "--mode",
    default="premium",
    choices=["simple", "medium", "premium"],
    help="Parsing mode: 'simple' for txt/md files, 'medium' for LlamaParse medium, 'premium' for LlamaParse premium (default: premium)",
  )
  parser.add_argument(
    "--log-file",
    default="log.txt",
    help="Path to the log file (append mode)",
  )
  return parser.parse_args()


def main():
  args = parse_cli_args()
  input_path = os.path.abspath(args.input_path)
  log_path = os.path.abspath(args.log_file)

  os.makedirs(os.path.dirname(log_path), exist_ok=True)

  with open(log_path, "a", encoding="utf-8") as log_file:
    tee = Tee(sys.stdout, log_file)
    with redirect_stdout(tee), redirect_stderr(tee):
      print("=" * 80)
      print(f"Run timestamp: {datetime.now().isoformat()}")
      print(f"Input file: {input_path}")
      print(f"Log file: {log_path}")

      if not os.path.isfile(input_path):
        print("ERROR: Input file does not exist.")
        return 1

      try:
        print(f"Parsing mode: {args.mode}")
        document_text, attempt_count = load_input_file(input_path, mode=args.mode)
        if document_text is None:
          print("ERROR: Failed to load input file after all retries.")
          return 1
        print(f"Successfully loaded file after attempt {attempt_count}.")
        result = test_llm_extraction(document_text)
        print("Extraction result JSON:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("Run completed successfully.")
        return 0
      except Exception as exc:
        print(f"ERROR: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
  raise SystemExit(main())

