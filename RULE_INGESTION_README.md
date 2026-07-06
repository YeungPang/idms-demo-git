# Rule Ingestion Pipeline: NL-to-Pattern/Clause/Fact/Class

## Overview

The **Rule Ingestion Pipeline** enables you to describe business rules in natural language and automatically classify them into the appropriate IDMS system component:

| Rule Type | Storage | Purpose |
|-----------|---------|---------|
| **PATTERN** | `semantic_patterns` table | Query templates for information lookup (e.g., "invoice due date from X to Y?") |
| **CLAUSE** | `solf_clauses` table | Procedural rules with conditions and actions (e.g., "when overdue, escalate") |
| **FACT** | `solf_clauses` table | Domain assertions and knowledge (e.g., "Company X is in Canton Y") |
| **CLASS** | `object_class` table | Schema entity definitions (e.g., "invoice has amount, due_date") |

## Architecture

### Components

1. **RuleClassifier** (`rule_ingestion.py`)
   - Analyzes NL rules using heuristic matching
   - Extracts metadata and parameters
   - Supports optional LLM-based classification

2. **RuleIngestionHandler** (`rule_ingestion.py`)
   - Routes classified rules to appropriate backends
   - Stores patterns, clauses, facts, and classes
   - Returns confirmation with created IDs

3. **API Router** (`idms_api_server/routers/rule_ingestion.py`)
   - FastAPI endpoints for classification and ingestion
   - Batch processing support
   - Health checks and metadata queries

### Classification Strategy

The classifier uses a two-tier approach:

**Tier 1: Heuristic Matching** (Always Available)
- Keyword-based scoring for each rule type
- Structural pattern matching (templates, conditionals, assertions, definitions)
- Fast, deterministic, no external dependencies

**Tier 2: LLM Classification** (Optional)
- Sent to GenAI for semantic analysis
- Returns: type, confidence, extracted parameters
- Used if GenAI client is available and configured

## How It Works

### Pattern Classification

**Input Pattern:** Natural language query template
```
"What is the invoice due date from X to Y?"
```

**Extracted Metadata:**
- Wildcards: None detected (literal query)
- Entity class: invoice
- Semantic concept: what_is_the_invoice_due_date

**Action:** Stored in `semantic_patterns` table for query parsing

---

### Clause Classification

**Input Pattern:** Conditional business rule
```
"When invoice is overdue for 30 days, escalate to management"
```

**Extracted Metadata:**
- Condition: "invoice is overdue for 30 days"
- Action: "escalate to management"
- Clause type: resolve_policy

**Action:** Stored in `solf_clauses` table as executable procedure

---

### Fact Classification

**Input Pattern:** Domain assertion
```
"Company ExampleCo GmbH is registered in Canton Zug"
```

**Extracted Metadata:**
- Subject: "Company ExampleCo GmbH"
- Predicate: "registered in Canton Zug"
- Entity class: company

**Action:** Stored in `solf_clauses` table (fact type) for knowledge reasoning

---

### Class Classification

**Input Pattern:** Schema entity definition
```
"Invoice class has attributes: invoice_number, due_date, amount, status"
```

**Extracted Metadata:**
- Class name: Invoice
- Attributes: [invoice_number, due_date, amount, status]
- Parent class: None

**Action:** Stored in `object_class` table with attributes indexed

## API Usage

### 1. Classify a Rule

Analyze a rule without storing it.

```bash
curl -X POST http://localhost:8002/api/rules/classify \
  -H 'Content-Type: application/json' \
  -d '{
    "rule_text": "What is the invoice due date from X to Y?",
    "use_llm": false
  }'
```

**Response:**
```json
{
  "rule_text": "What is the invoice due date from X to Y?",
  "classification": {
    "rule_type": "pattern",
    "confidence": 0.85,
    "rationale": "Heuristic classification: template with query keywords",
    "extracted_metadata": {
      "entity_class": "invoice"
    },
    "suggested_parameters": {
      "semantic_concept": "invoice_due_date_query",
      "pattern_language": "en"
    }
  },
  "next_steps": {
    "action": "ingest",
    "endpoint": "/api/rules/ingest",
    "confidence_threshold": 0.70,
    "current_confidence": 0.85
  }
}
```

### 2. Ingest a Single Rule

Classify and store a rule in the appropriate backend.

```bash
curl -X POST http://localhost:8002/api/rules/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "rule_text": "When invoice is overdue for 30 days, escalate to management",
    "auto_classify": true,
    "store_if_confident": true
  }'
```

**Response:**
```json
{
  "rule_text": "When invoice is overdue for 30 days, escalate to management",
  "rule_type": "clause",
  "confidence": 0.78,
  "storage_location": "solf_clauses",
  "created_id": 42,
  "status": "stored_clause"
}
```

### 3. Batch Ingest Multiple Rules

Process multiple rules in a single request.

```bash
curl -X POST http://localhost:8002/api/rules/ingest-batch \
  -H 'Content-Type: application/json' \
  -d '{
    "rules": [
      "What is the invoice due date of invoice from X to Y?",
      "When payment is received, update status to completed",
      "Company X is registered in Canton Y",
      "Invoice entity has: number, date, amount, status"
    ],
    "auto_classify": true,
    "store_if_confident": true
  }'
```

**Response:**
```json
{
  "total_rules": 4,
  "successfully_stored": 3,
  "failed": 1,
  "results": [
    {
      "rule_text": "What is the invoice due date of invoice from X to Y?",
      "rule_type": "pattern",
      "confidence": 0.92,
      "storage_location": "semantic_patterns",
      "created_id": 101,
      "status": "stored_pattern"
    },
    // ... more results
  ],
  "summary": {
    "type_distribution": {
      "pattern": 1,
      "clause": 1,
      "fact": 1,
      "class": 1
    },
    "storage_distribution": {
      "semantic_patterns": 1,
      "solf_clauses": 2,
      "object_class": 1
    },
    "success_rate": 0.75
  }
}
```

### 4. Get Rule Types Reference

```bash
curl -X GET http://localhost:8002/api/rules/types
```

Returns detailed information about each rule type with examples.

## Classification Accuracy

Based on comprehensive test set (20 rules):

- **Overall:** 85% accuracy
- **PATTERN:** 80% (4/5 correct)
- **CLAUSE:** 60% (3/5 correct) - can be improved with more keywords
- **FACT:** 100% (5/5 correct)
- **CLASS:** 100% (5/5 correct)

### Confidence Thresholds

- **Default:** 0.70 (70%) minimum confidence required to store
- **Pattern:** Must pass lexical compatibility checks
- **Clause:** Requires identifiable condition/action structure
- **Fact:** Requires subject-predicate-object structure
- **Class:** Requires entity/type keywords

## Python Integration

### Standalone Classification

```python
from rule_ingestion import RuleClassifier

classifier = RuleClassifier()

result = classifier.classify(
    "What is the invoice due date from X to Y?"
)

print(f"Type: {result.rule_type.value}")
print(f"Confidence: {result.confidence:.0%}")
print(f"Metadata: {result.extracted_metadata}")
print(f"Parameters: {result.suggested_parameters}")
```

### Ingestion with Database

```python
from rule_ingestion import RuleIngestionHandler
from database import get_db_connection
from pattern_library import PatternLibrary

handler = RuleIngestionHandler(
    db_connection_fn=get_db_connection,
    pattern_library=PatternLibrary(get_db_connection())
)

result = handler.ingest_rule(
    "When invoice is overdue for 30 days, escalate to management"
)

if result.get("status").startswith("stored"):
    print(f"Stored in {result['storage_location']}")
    print(f"ID: {result['created_id']}")
else:
    print(f"Error: {result.get('error_message')}")
```

## Storage Details

### Semantic Patterns

Rules classified as PATTERN are stored in `semantic_patterns` table:

```sql
INSERT INTO semantic_patterns (
  pattern_text,
  semantic_concept,
  mapped_attributes,
  entity_class,
  confidence,
  pattern_language
) VALUES (...)
```

Used by `query_engine.parse_query()` to match user queries.

### SOLF Clauses

Rules classified as CLAUSE or FACT are stored in `solf_clauses` table:

```sql
INSERT INTO solf_clauses (
  clause_name,
  entity_class,
  clause_type,  -- 'resolve_policy', 'computation_rule', 'fact'
  clause_body,
  confidence
) VALUES (...)
```

Used by `SOLFInterpreter` for procedural execution or reasoning.

### Object Classes

Rules classified as CLASS are stored in `object_class` table:

```sql
INSERT INTO object_class (
  class_name,
  parent_class_id,
  description
) VALUES (...)
```

Integrated into schema system and entity type definitions.

## Advanced Features

### LLM-Powered Classification

Enable more accurate classification with LLM:

```python
from rule_ingestion import RuleClassifier

classifier = RuleClassifier(
    genai_client=your_genai_client  # Pass configured GenAI client
)

result = classifier.classify(
    "When invoice is overdue for 30 days, escalate to management",
    use_llm=True
)
```

### Custom Metadata Extraction

Extend `RuleClassifier._extract_metadata()` to extract domain-specific parameters:

```python
def _extract_metadata(self, rule_text, rule_type):
    metadata = super()._extract_metadata(rule_text, rule_type)
    
    if rule_type == RuleType.CLAUSE:
        # Extract SLA targets, escalation levels, etc.
        metadata["sla_days"] = self._extract_sla_days(rule_text)
    
    return metadata
```

### Confidence Calibration

Adjust keyword weighting for your domain:

```python
# In RuleClassifier.__init__:
self.PATTERN_KEYWORDS = {
    "query", "ask", "find", ..., "invoice_status"  # Add domain terms
}
```

## Common Patterns

### Query Patterns

```
"What is the invoice due date from X to Y?"
"Find all companies in Canton Z"
"Search for contracts between dates"
"Show relationships between entities X and Y"
```

### Business Rules (Clauses)

```
"When invoice overdue 30 days, escalate to management"
"If payment received, update transaction status"
"Trigger compliance check for documents with PII"
"Compute total transaction amount for account"
```

### Domain Facts

```
"Company X is registered in Canton Y"
"Invoice INV-001 has due date 2026-07-15"
"John Smith is CEO of TechCorp"
"Account has balance of CHF 10,000"
```

### Schema Definitions

```
"Invoice entity has: number, due_date, amount, status"
"Company type extends LegalEntity"
"Transaction has relationships to accounts and ledger"
"Person properties: first_name, last_name, birth_date"
```

## Troubleshooting

### Low Confidence Scores

If classification confidence is below 0.70:

1. **Check keyword coverage** - Ensure domain keywords are in classifier
2. **Use more specific terms** - "When invoice payment is overdue..." vs "When payment is overdue..."
3. **Consider LLM classification** - More accurate for nuanced cases
4. **Manual review** - Flag for human review before storing

### Misclassification

If rules are classified incorrectly:

1. **Verify keywords** - Check if relevant keywords are missing
2. **Add structural indicators** - Use clearer template syntax (`from X to Y` for patterns)
3. **Use explicit types** - Prefix with "Define class:" for schema definitions
4. **Escalate to LLM** - Use LLM classification for edge cases

## Testing

Run the comprehensive test suite:

```bash
python IDMS-Proto/test_rule_ingestion.py
```

This demonstrates:
- Classification accuracy on diverse rule set
- API request examples
- Full pipeline workflow

## Files

- **`rule_ingestion.py`** - Core classifier and handler (310 lines)
- **`idms_api_server/routers/rule_ingestion.py`** - FastAPI router (320 lines)
- **`test_rule_ingestion.py`** - Test suite and demos (280 lines)
- **`idms_api_server/application.py`** - Updated to include router

## Performance

- **Classification:** ~5-50ms per rule (heuristic), ~200-500ms with LLM
- **Storage:** Depends on backend (pattern table insert typically <10ms)
- **Batch:** Process 100 rules in ~2-10 seconds (heuristic only)

## Future Enhancements

1. **Active Learning** - Improve classifier by learning from human corrections
2. **Custom Validators** - Domain-specific rule validation before storage
3. **Audit Trail** - Track rule origin, modifications, and usage
4. **Versioning** - Support multiple versions of rules with rollback
5. **Impact Analysis** - Analyze how new rules affect existing queries/clauses
6. **A/B Testing** - Test rule variations before full deployment
