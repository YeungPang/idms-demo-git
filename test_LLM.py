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

from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, SparseVector,
    SparseVectorParams, SparseIndexParams, Prefetch, FusionQuery, Fusion
)
from sentence_transformers import SentenceTransformer
import uuid
from transformers import AutoTokenizer, AutoModelForMaskedLM
import torch

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
nest_asyncio.apply()
    
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

LLM_MODEL = "openai/gpt-4o-mini"
#"meta-llama/llama-3.3-70b-instruct"
#"deepseek/deepseek-v3.2" 
#"google/gemini-3-flash-preview"
#"openai/gpt-5.4-nano"
#"meta-llama/llama-3.3-70b-instruct"


GENERAL_PROMPT = """
You are an information extraction system. Extract all entities, attributes, and relationships from the document.
The document may contain multiple languages. Do not translate attribute names. Use attribute names exactly as they appear in the text.
Do NOT infer schema names. Do NOT guess field names.

Use ONLY the following controlled vocabularies to ensure consistent classification.

============================================================
ENTITY TYPES (choose the closest match)
============================================================
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
LOCATION
DATE
NUMBER
CURRENCY
NAME_VALUE_PAIR
OTHER

============================================================
DOCUMENT CATEGORY (choose one or more)
============================================================
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
tax_document
contract
policy
memo
email
announcement
meeting_minutes
event_record
administrative_record
government_record
general_information

============================================================
RELATIONSHIP TYPES (choose the closest match)
============================================================
has_role
employed_by
manages
reports_to
member_of
part_of
located_in
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
other

============================================================
OUTPUT FORMAT
============================================================

Return a JSON object with:

1. theme
   - A short summary of what the document is about.

2. document_category
   - Choose one from the controlled vocabulary above.

3. entities: [
    {
      "entity_id": "e1",
      "entity_type": "...",
      "entity_name": "...",
      "attributes": { ... },   // Use attribute names exactly as in the text
      "source_text": "...",
      "confidence": 0.0-1.0
    }
]

4. relationships: [
    {
      "relationship_type": "...",   // Choose from controlled vocabulary
      "source_entity_id": "e1",
      "target_entity_id": "e2",
      "attributes": { ... },
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
BILL_PROMPT = """
"""
RECEIPT_PROMPT = """
"""
QUOTATION_PROMPT = """
"""

DOCUMENT_TYPE_EXAMPLES = {
    "invoice": [
        "Invoice INV-2024-001. Amount due. Issued by. Due date. Payment terms.",
        "Rechnung Nr. 2024-001. Fälligkeitsdatum. Ausgestellt von. Zahlungsbedingungen. Gesamtbetrag."
    ],
    "bill": [
        "Bill for services rendered. Amount payable. Billed to. Billed from.",
        "Kostenaufstellung. Rechnung für erbrachte Dienstleistungen. Zu zahlen von. Ausgestellt von."
    ],
    "receipt": [
        "Receipt #12345. Payment received. Paid by. Paid to. Total amount.",
        "Quittung Nr. 12345. Zahlung erhalten. Bezahlt von. Bezahlt an. Gesamtbetrag."
    ],
    "quotation": [
        "Quotation valid until. Price offer. Quote number. Customer.",
        "Angebot gültig bis. Preisangebot. Angebotsnummer. Kunde."
    ],
    "purchase_order": [
        "Purchase Order PO-2024-001. Ordered items. Supplier. Delivery date.",
        "Bestellung Nr. PO-2024-001. Bestellte Artikel. Lieferant. Lieferdatum."
    ],
    "contract": [
        "This contract is made between. Terms and conditions. Agreement.",
        "Dieser Vertrag wird geschlossen zwischen. Vertragsbedingungen. Vereinbarung."
    ],
    "hr_record": [
        "Employment record. Employee name. Position. Start date. Department.",
        "Personalakte. Mitarbeitername. Position. Eintrittsdatum. Abteilung."
    ],
    "crm_interaction": [
        "Email to customer. Follow-up. Meeting scheduled. Contact details.",
        "E-Mail an Kunden. Rückmeldung. Termin vereinbart. Kontaktdaten."
    ],
    "financial_record": [
        "Balance sheet. Profit and loss. Financial statement.",
        "Bilanz. Gewinn- und Verlustrechnung. Finanzbericht."
    ],
    "tax_document": [
        "Tax return. VAT declaration. Taxpayer ID.",
        "Steuererklärung. Umsatzsteuervoranmeldung. Steueridentifikationsnummer."
    ],
    "memo": [
        "Internal memo. Announcement. Notice.",
        "Internes Memo. Ankündigung. Mitteilung."
    ],
    "general_information": [
        "General information. Miscellaneous content.",
        "Allgemeine Informationen. Verschiedene Inhalte."
    ]
}
    
aphotonix_reg = """
Volkswirtschaftsdirektion
Handelsregister- und Konkursamt

**Handelsregisteramt**

Aabachstrasse 5, Postfach, 6301 Zug
Tel. Buchhaltung: 041 594 55 60
Internet: www.hrazg.ch
Email: buchhaltung.hra@zg.ch

CHE-115.115.718 MWST
Bitte Original-Einzahlungsschein verwenden
IBAN: CH87 0900 0000 1649 5813 8
BIC: POFICHBEXXX

Handelsregisteramt Zug, Postfach, 6301 Zug

**A-Post**
APHOTONIX GmbH
c/o RSA Consulting AG
Neugasse 4
6300 Zug

**Rechnung Nr. 112479923 vom 25.09.2025** - Geschäftsnummer: 23/434/2025
APHOTONIX GmbH (CHE-399.737.068)
Tagesregister-Nr. 17062 Datum: 23.09.2025

Gegen diesen Entscheid kann innert 30 Tagen seit Zustellung beim Verwaltungsgericht des Kantons Zug, 6301 Zug, schriftlich Beschwerde erhoben werden. Die Beschwerdefrist enthält Begründung und Begründung. Der Entscheid ist beizulegen oder genau zu bezeichnen. Beweismittel sind zu bezeichnen und soweit möglich beizulegen.

| | | CHF |
|---|---|---|
| 3 | Eintrag von Funktion(en) zu CHF 20.00 | 60.00 |
| 3 | Eintrag von Zeichnungsberechtigung(en) zu CHF 20.00 | 60.00 |
| 1 | Grundgebühr für die Neueintragung einer GmbH zu CHF 420.00 | 420.00 |
| 1 | Eintragung, Änderung und Löschung Domizil zu CHF 30.00 | 30.00 |
| 1 | Eintrag / Löschung Verzicht auf eine Revisionsstelle zu CHF 30.00 | 30.00 |
| 1 | Porto: A-Post zu CHF 1.20 | 1.20 |
| 1 | Brieftz-Mail zu CHF 30.00 | 30.00 |
| 1 | Auszug nach Art. 34 HRegV zu CHF 75.00 | 75.00 |
| | Steuerfreier Betrag | CHF 706.20 |
| | Mehrwertsteuerpflichtiger Betrag | CHF 0.00 |
| | 8.1 % Mehrwertsteuer | CHF 0.00 |
| | **Unser Guthaben zahlbar innert 30 Tagen, netto** | **CHF 706.20** |

Möchten Sie statt in Schweizer Franken mit Bitcoin oder Ether bezahlen? Senden Sie eine E-Mail an buchhaltung.hra@zg.ch!

----

| **Empfangsschein**<br/><br/>**Konto / Zahlbar an**<br/>CH38 3000 0001 1649 5813 8<br/>Handelsregisteramt<br/>Aabachstrasse 5<br/>6301 Zug<br/><br/>**Referenz**<br/>00 00020 80430 00000 11247 99230<br/><br/>**Zahlbar durch**<br/>APHOTONIX GmbH<br/>Neugasse 4<br/>6300 Zug<br/><br/><br/>**Währung Betrag**<br/>CHF 706.20 | **Zahlteil**<br/><br/>[QR Code]<br/><br/><br/>**Währung Betrag**<br/>CHF 706.20 | **Konto / Zahlbar an**<br/>CH38 3000 0001 1649 5813 8<br/>Handelsregisteramt<br/>Aabachstrasse 5<br/>6301 Zug<br/><br/>**Referenz**<br/>00 00020 80430 00000 11247 99230<br/><br/>**Zahlbar durch**<br/>APHOTONIX GmbH<br/>Neugasse 4<br/>6300 Zug |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |


Annahmestelle
"""

INVOICE_PROMPT = """
You are an information extraction system. Extract all entities, attributes, and relationships from the invoice document.
The document may contain multiple languages (e.g., German). Do not translate attribute names. Use attribute names exactly as they appear in the text.

IMPORTANT:
The document may contain Markdown tables. You MUST parse all tables and extract all financial and payment information contained inside them.

============================================================
MANDATORY DUE DATE COMPUTATION
============================================================
You MUST compute "computed_due_date" if BOTH:
- an issue date exists, AND
- payment terms exist (e.g., “zahlbar innert 30 Tagen”, “30 Tage netto”, “Net 30”, “pay within 30 days”)
If either is missing, set "computed_due_date" to null.

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
tokenizer_sparse = AutoTokenizer.from_pretrained(
    "naver/splade-cocondenser-ensembledistil"
)
model_sparse = AutoModelForMaskedLM.from_pretrained(
    "naver/splade-cocondenser-ensembledistil"
)

def encode_sparse(text: str):
    inputs = tokenizer_sparse(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=512,
    )
    with torch.no_grad():
        logits = model_sparse(**inputs).logits.squeeze(0)

    # SPLADE activation
    relu = torch.relu(logits)
    sparse_vec = torch.max(relu, dim=0).values

    indices = sparse_vec.nonzero().squeeze().tolist()
    values = sparse_vec[indices].tolist()

    return indices, values

def route_to_prompt(doc_type: str):
    if doc_type == "invoice":
        return INVOICE_PROMPT
    elif doc_type == "bill":
        return BILL_PROMPT
    elif doc_type == "receipt":
        return RECEIPT_PROMPT
    elif doc_type == "quotation":
        return QUOTATION_PROMPT
    else:
        return GENERAL_PROMPT
    
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

    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()

    data = response.json()
    content = data["choices"][0]["message"]["content"]

    # Parse JSON output
    return json.loads(content)

def run_llm_extraction(document_text):
    print(f"Model: {LLM_MODEL}")
    startts = time.time()
    doc_type, score, results = classify_document_type(document_text)
    print(f"Predicted document type: {doc_type} (score: {score:.4f})")
    prompt = route_to_prompt(doc_type)
    duration = time.time() - startts
    print(f"Lookup took {duration:.2f} seconds")
    startts = time.time()
    result = extract_from_document(document_text, prompt=prompt)
    duration = time.time() - startts
    print(f"Extraction took {duration:.2f} seconds")
    return result

# Initialize Qdrant (local or remote)
qdrant = QdrantClient(":memory:")  # or "localhost:6333"

# Embedding model
embedder = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
#all-MiniLM-L6-v2

# Create collection
collection_name = "document_types"
if qdrant.collection_exists(collection_name=collection_name):
    qdrant.delete_collection(collection_name=collection_name)
qdrant.create_collection(
    collection_name=collection_name,
    vectors_config={"dense": VectorParams(size=384, distance=Distance.COSINE)},
    sparse_vectors_config={"sparse": SparseVectorParams(index=SparseIndexParams())}
)

# Insert canonical examples
points = []
for doc_type, example_texts in DOCUMENT_TYPE_EXAMPLES.items():
    for example_text in example_texts:
        vector = embedder.encode(example_text).tolist()
        sparse_idx, sparse_val = encode_sparse(example_text)
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector={
                    "dense": vector,
                    "sparse": SparseVector(
                        indices=sparse_idx,
                        values=sparse_val
                    )
                },
                payload={"doc_type": doc_type}
            )
        )

qdrant.upsert(collection_name=collection_name, points=points)

DOC_TYPE_KEYWORDS = {
    "invoice": [
        "invoice", "rechnung", "fällig", "due date", "amount due",
        "zahlbar", "payment terms", "iban", "bic", "qr code"
    ],
    "bill": [
        "bill", "billed to", "billed from", "amount payable", "kostenaufstellung"
    ],
    "receipt": [
        "receipt", "quittung", "payment received", "bezahlt", "empfangsschein"
    ],
    "quotation": [
        "quotation", "quote", "angebot", "price offer", "gültig bis"
    ],
    "purchase_order": [
        "purchase order", "po-", "bestellung", "ordered items", "lieferdatum"
    ],
    "contract": [
        "contract", "agreement", "vertrag", "terms and conditions"
    ],
    "hr_record": [
        "employee", "employment", "personalakte", "department", "position"
    ],
    "crm_interaction": [
        "follow-up", "meeting", "email", "kontakt", "customer"
    ],
    "financial_record": [
        "balance sheet", "profit and loss", "bilanz", "finanzbericht"
    ],
    "tax_document": [
        "tax", "vat", "steuer", "umsatzsteuer"
    ],
    "memo": [
        "memo", "notice", "mitteilung", "announcement"
    ],
    "general_information": [
        "general information", "miscellaneous", "allgemeine informationen"
    ],
}

DOC_TYPE_REGEX_PRIORS = {
    "invoice": [
        r"\binvoice\b",
        r"\brechnung\b",
        r"\binv[-\s]?\d+",
        r"\brechnung\s*nr\.?",
        r"\bzahlbar\b",
        r"\bdue\s+date\b",
        r"\biban\b",
        r"\bbic\b",
    ],
    "receipt": [
        r"\breceipt\b",
        r"\bquittung\b",
    ],
    "quotation": [
        r"\bquotation\b",
        r"\bquote\b",
        r"\bangebot\b",
    ],
    "purchase_order": [
        r"\bpurchase\s+order\b",
        r"\bpo[-\s]?\d+",
        r"\bbestellung\b",
    ],
    "contract": [
        r"\bcontract\b",
        r"\bvertrag\b",
    ],
}


def _normalize_semantic_scores(semantic_scores: dict):
    total = sum(semantic_scores.values())
    if total <= 0:
        return {k: 0.0 for k in semantic_scores}
    return {k: v / total for k, v in semantic_scores.items()}


def _keyword_score(text: str, doc_type: str):
    lowered_text = text.lower()
    keywords = DOC_TYPE_KEYWORDS.get(doc_type, [])
    if not keywords:
        return 0.0
    matches = sum(1 for keyword in keywords if keyword in lowered_text)
    return matches / len(keywords)


def _regex_prior_score(text: str, doc_type: str):
    patterns = DOC_TYPE_REGEX_PRIORS.get(doc_type, [])
    if not patterns:
        return 0.0
    matches = sum(1 for pattern in patterns if re.search(pattern, text, flags=re.IGNORECASE))
    return min(1.0, matches * 0.2)


def classify_document_type(
    text: str,
    top_k: int = 8,
    return_details: bool = False
):
    # -----------------------------
    # 1. Encode dense + sparse
    # -----------------------------
    dense_vec = embedder.encode(text).tolist()
    sparse_idx, sparse_val = encode_sparse(text)   # your SPLADE/Jina sparse encoder

    # -----------------------------
    # 2. Hybrid search in Qdrant via RRF fusion
    # -----------------------------
    query_response = qdrant.query_points(
        collection_name=collection_name,
        prefetch=[
            Prefetch(query=dense_vec, using="dense", limit=top_k),
            Prefetch(
                query=SparseVector(indices=sparse_idx, values=sparse_val),
                using="sparse",
                limit=top_k,
            ),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=top_k,
    )
    results = query_response.points

    if not results:
        raise RuntimeError("No document type candidates returned from Qdrant.")

    # -----------------------------
    # 3. Aggregate semantic evidence
    # -----------------------------
    semantic_scores = {}
    for hit in results:
        doc_type = hit.payload["doc_type"]
        semantic_scores[doc_type] = semantic_scores.get(doc_type, 0.0) + float(hit.score)

    normalized_semantic = _normalize_semantic_scores(semantic_scores)

    # -----------------------------
    # 4. Combine semantic + keyword + regex
    # -----------------------------
    combined_scores = {}
    candidate_doc_types = set(
        list(DOCUMENT_TYPE_EXAMPLES.keys()) +
        list(normalized_semantic.keys())
    )

    for doc_type in candidate_doc_types:
        semantic_component = normalized_semantic.get(doc_type, 0.0)
        keyword_component = _keyword_score(text, doc_type)
        regex_component = _regex_prior_score(text, doc_type)

        combined_scores[doc_type] = (
            0.75 * semantic_component +
            0.15 * keyword_component +
            0.10 * regex_component
        )

    predicted_type = max(combined_scores, key=combined_scores.get)
    score = combined_scores[predicted_type]

    if return_details:
        ranked_scores = sorted(
            combined_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )
        return predicted_type, score, results, ranked_scores

    return predicted_type, score, results


def run_classification_evaluation(
    top_k: int = 8,
    export_dir: str = "eval_results",
    export_prefix: str = "classification_eval",
):
    eval_samples = []
    for expected_type, examples in DOCUMENT_TYPE_EXAMPLES.items():
        for example in examples:
            eval_samples.append((expected_type, example))

    # Add a few harder mixed-language examples to make regression checks more useful.
    eval_samples.extend([
        (
            "invoice",
            "Rechnung Nr. INV-2026-09. Zahlbar innert 30 Tagen. IBAN CH12 3000 0000 0000 0000 0. Gesamtbetrag CHF 1250.00."
        ),
        (
            "purchase_order",
            "Purchase Order PO-2026-145. Supplier: Alpha GmbH. Ordered items include 50 adapters. Delivery date 2026-06-01."
        ),
        (
            "receipt",
            "Quittung Nr. 7781. Zahlung erhalten von Max Muster. Betrag CHF 80.00. Bezahlt an Stadtkasse."
        ),
        (
            "quotation",
            "Angebot gültig bis 31.12.2026. Angebotsnummer Q-2026-21. Preisangebot für Implementierung."
        ),
    ])

    confusion = {}
    per_type_total = {}
    per_type_correct = {}
    misclassified = []
    sample_rows = []

    for expected, text in eval_samples:
        predicted, score, _results, ranked_scores = classify_document_type(
            text,
            top_k=top_k,
            return_details=True,
        )

        per_type_total[expected] = per_type_total.get(expected, 0) + 1
        if predicted == expected:
            per_type_correct[expected] = per_type_correct.get(expected, 0) + 1

        if expected not in confusion:
            confusion[expected] = {}
        confusion[expected][predicted] = confusion[expected].get(predicted, 0) + 1

        if predicted != expected:
            top3 = ranked_scores[:3]
            misclassified.append((expected, predicted, score, top3, text))

        sample_rows.append({
            "expected": expected,
            "predicted": predicted,
            "score": score,
            "correct": predicted == expected,
            "top3": ranked_scores[:3],
            "text": text,
        })

    total = len(eval_samples)
    correct = sum(per_type_correct.values())
    accuracy = correct / total if total else 0.0

    print("=== Classification Evaluation ===")
    print(f"Samples: {total}")
    print(f"Correct: {correct}")
    print(f"Accuracy: {accuracy:.2%}")
    print()

    print("Per-type accuracy:")
    for doc_type in sorted(per_type_total.keys()):
        t = per_type_total[doc_type]
        c = per_type_correct.get(doc_type, 0)
        a = c / t if t else 0.0
        print(f"- {doc_type}: {c}/{t} ({a:.2%})")
    print()

    print("Confusion summary (expected -> predicted: count):")
    for expected in sorted(confusion.keys()):
        preds = confusion[expected]
        pairs = ", ".join(
            f"{pred}:{count}"
            for pred, count in sorted(preds.items(), key=lambda x: x[1], reverse=True)
        )
        print(f"- {expected} -> {pairs}")
    print()

    if misclassified:
        print("Misclassified samples:")
        for idx, (expected, predicted, score, top3, text) in enumerate(misclassified, start=1):
            top3_text = ", ".join(f"{k}={v:.4f}" for k, v in top3)
            print(f"{idx}. expected={expected}, predicted={predicted}, score={score:.4f}")
            print(f"   top3: {top3_text}")
            print(f"   text: {text}")
    else:
        print("No misclassifications found.")

    per_type_accuracy = {}
    for doc_type in sorted(per_type_total.keys()):
        t = per_type_total[doc_type]
        c = per_type_correct.get(doc_type, 0)
        per_type_accuracy[doc_type] = {
            "correct": c,
            "total": t,
            "accuracy": (c / t) if t else 0.0,
        }

    export_payload = {
        "generated_at": datetime.now().isoformat(),
        "samples": total,
        "correct": correct,
        "accuracy": accuracy,
        "top_k": top_k,
        "per_type_accuracy": per_type_accuracy,
        "confusion": confusion,
        "misclassified_count": len(misclassified),
        "sample_predictions": sample_rows,
    }

    os.makedirs(export_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(export_dir, f"{export_prefix}_{timestamp}.json")
    csv_path = os.path.join(export_dir, f"{export_prefix}_{timestamp}.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(export_payload, f, indent=2, ensure_ascii=False)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["expected", "predicted", "correct", "score", "top3", "text"],
        )
        writer.writeheader()
        for row in sample_rows:
            writer.writerow({
                "expected": row["expected"],
                "predicted": row["predicted"],
                "correct": row["correct"],
                "score": f"{row['score']:.6f}",
                "top3": "; ".join(f"{k}={v:.4f}" for k, v in row["top3"]),
                "text": row["text"],
            })

    print()
    print(f"Exported JSON: {json_path}")
    print(f"Exported CSV:  {csv_path}")

    return json_path, csv_path


def parse_cli_args():
    parser = argparse.ArgumentParser(description="LLM extraction and document-type evaluation utility")
    parser.add_argument(
        "--eval",
        action="store_true",
        help="Run classifier evaluation and export results",
    )
    parser.add_argument(
        "--eval-output-dir",
        default="eval_results",
        help="Directory where evaluation exports are written",
    )
    parser.add_argument(
        "--eval-prefix",
        default="classification_eval",
        help="Filename prefix for evaluation export files",
    )
    parser.add_argument(
        "--eval-top-k",
        type=int,
        default=8,
        help="Top-k retrieval value used during evaluation",
    )
    return parser.parse_args()

# Example usage
if __name__ == "__main__":
    args = parse_cli_args()

    if args.eval:
        run_classification_evaluation(
            top_k=args.eval_top_k,
            export_dir=args.eval_output_dir,
            export_prefix=args.eval_prefix,
        )
        sys.exit(0)

    doc = """
    Benjamin Pang is the Head of Finance at SIM International since 2021.
    Invoice INV-2024-001 was issued by SIM International to ABC Consulting for USD 12,500.
    """
    doc = aphotonix_reg
    result = run_llm_extraction(doc)
    print(json.dumps(result, indent=2))


def test_llm_module_smoke() -> None:
    """Basic collection-time smoke check for pytest runs."""
    assert isinstance(LLM_MODEL, str)
    assert bool(LLM_MODEL.strip())