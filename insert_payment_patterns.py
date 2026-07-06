import psycopg2
from psycopg2.extras import Json
from idms_config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

# Get database connection
conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASSWORD)

with conn.cursor() as cur:
    # Insert new pattern for payment term queries
    insert_sql = """
    INSERT INTO semantic_patterns 
    (pattern_text, semantic_concept, mapped_attributes, computation_rule, entity_class, confidence, pattern_language, source_type)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
    RETURNING pattern_id, pattern_text;
    """
    
    mapped_attrs = {
        "accounting_days_to_pay": ["invoice_date", "due_date"]
    }
    
    patterns_to_insert = [
        ("within how many days must * be paid", "accounting_days_to_pay", mapped_attrs, "due_date - invoice_date", "invoice", 0.92, "en", "seeded"),
        ("how many days to pay from * to *", "accounting_days_to_pay", mapped_attrs, "due_date - invoice_date", "invoice", 0.93, "en", "seeded"),
    ]
    
    for pattern_text, semantic_concept, attrs, rule, entity_class, confidence, lang, source_type in patterns_to_insert:
        try:
            cur.execute(
                insert_sql,
                (pattern_text, semantic_concept, Json(attrs), rule, entity_class, confidence, lang, source_type)
            )
            result = cur.fetchone()
            if result:
                print(f"✓ Inserted pattern: '{result[1]}'")
            else:
                print(f"⊘ Pattern already exists: '{pattern_text}'")
        except Exception as e:
            print(f"✗ Error inserting pattern '{pattern_text}': {e}")
    
    conn.commit()
    print("\nDone!")

conn.close()



