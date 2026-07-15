import time
from pathlib import Path
from dotenv import load_dotenv, find_dotenv


import nest_asyncio

from llama_index.core import SimpleDirectoryReader, PropertyGraphIndex, Settings
from llama_index.core import Document, KnowledgeGraphIndex
from llama_index.core.graph_stores import SimpleGraphStore
from llama_index.core.storage.storage_context import StorageContext
from llama_index.llms.openai import OpenAI
from llama_index.core.indices.property_graph.transformations import SimpleLLMPathExtractor

from llama_index.embeddings.openai import OpenAIEmbedding
import pprint
import json

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
nest_asyncio.apply()

model="gpt-4o-mini" #"gpt-5.1"
Settings.llm = OpenAI(model=model, temperature=0.1)
Settings.embed_model = OpenAIEmbedding(model_name="text-embedding-3-small")


def test_llama_module_smoke() -> None:
    assert isinstance(model, str)
    assert bool(model.strip())

def property_graph_extraction(documents):
    # This step calls the LLM to extract triples (nodes, relationships, properties)
    index = PropertyGraphIndex.from_documents(
        documents,
        graph_store=SimpleGraphStore(),
        transformations=[SimpleLLMPathExtractor()],
    )
    return index

def knowledge_graph_extraction(documents):
    # This step calls the LLM to extract triples (nodes, relationships, properties)
    index = KnowledgeGraphIndex.from_documents(
        documents,
        graph_store=SimpleGraphStore(),
        max_triplets_per_chunk=3,
        transformations=[SimpleLLMPathExtractor()],
    )
    return index

def extract_doc(input, extraction_type="property_graph"):
    if input[:2] == "./":
        documents = SimpleDirectoryReader(input_files=[input]).load_data()
    else:
        documents = [Document(text=input)]
    if extraction_type == "property_graph":
        print("Extracting Property Graph...")
        return property_graph_extraction(documents)
    else:
        print("Extracting Knowledge Graph...")
        return knowledge_graph_extraction(documents)
    
def print_graph(index, query):
    query_engine = index.as_query_engine(
        include_text=True, # Include the source text chunks for better synthesis
        similarity_top_k=3 # Number of paths/nodes to retrieve
    )
    response = query_engine.query(query)
    return response

prompt = "Provide the theme, categorized document type with no specific detail, and the entities and relationships in the document with their types for the document. The response should be in JSON format."
INGESTION_PROMPT = """
Extract all entities, attributes, and relationships from the document.
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

invoice_prompt = """
You are an information extraction system. Extract all entities, attributes, and relationships from the invoice document.
The document may contain multiple languages. Do not translate attribute names. Use attribute names exactly as they appear in the text.
Do NOT infer schema names. Do NOT guess field names.

Use ONLY the following controlled vocabularies to ensure consistent classification.

============================================================
ENTITY TYPES (choose the closest match)
============================================================
INVOICE
BILL
QUOTATION
RECEIPT
ORGANISATION
PERSON
PRODUCT
SERVICE
LINE_ITEM
DATE
NUMBER
CURRENCY
BANK_ACCOUNT
OTHER

============================================================
DOCUMENT CATEGORY (choose one)
============================================================
invoice
bill
quotation
receipt
financial_record
accounting_record
general_information

============================================================
RELATIONSHIP TYPES (choose the closest match)
============================================================
issued_by
issued_to
billed_to
billed_from
paid_by
paid_to
sent_by
sent_to
contains_item
related_to
other

============================================================
OUTPUT FORMAT
============================================================

Return a JSON object with:

1. theme
   - A short summary of what the invoice is about.

2. document_category
   - Choose one from the controlled vocabulary above.

3. entities: [
    {
      "entity_id": "e1",
      "entity_type": "...",
      "entity_name": "...",
      "attributes": {
          // Use attribute names exactly as in the text.
          // Extract as many invoice-relevant fields as present, including:
          // - invoice_number
          // - issue_date
          // - due_date
          // - total_amount
          // - net_amount
          // - tax_amount
          // - currency
          // - payment_terms
          // - payment_reference
          // - bank_account
          // - iban
          // - bic
          // - account_number
          // - reason_for_payment
          // - line_items (description, quantity, unit_price, total)
          // - purchase_order_number
          // - contract_reference
      },
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
- Extract all invoice-relevant financial fields when present.
- Do NOT invent information.
- Do NOT normalize or translate attribute names.
- Use only information explicitly present in the document.
- Output valid JSON only.
"""

query = "What is the due date and amount for the invoice mentioned in the document?"
if __name__ == "__main__":
    path = "./data/aphotonix_reg.md"
    print(f"Using model: {model}")
    startts = time.time()
    index = extract_doc(path)
    response = print_graph(index, invoice_prompt)   
    duration = time.time() - startts
    print(f"Elapsed time: {duration}")
    print(response)