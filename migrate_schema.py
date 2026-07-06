#!/usr/bin/env python
"""Migrate document schema to add markdown_path column and update doc_id 1."""

import psycopg2
import object_db
from idms_config import DB_NAME, DB_HOST, DB_USER, DB_PASSWORD, DB_PORT

def migrate_and_update():
    conn = psycopg2.connect(
        dbname=DB_NAME,
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT
    )
    
    print("Step 1: Running schema migration...")
    object_db._migrate_document_schema(conn)
    print("✓ Migration completed")
    
    # Verify column exists
    cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='document' AND column_name='markdown_path'")
    if cur.fetchone():
        print("✓ markdown_path column exists")
    else:
        print("✗ markdown_path column not found")
        cur.close()
        conn.close()
        return
    
    # Get current doc_id 1 data
    print("\nStep 2: Fetching current data for doc_id 1...")
    cur.execute("SELECT doc_id, doc_key, doc_path, markdown_path, doc_name FROM document WHERE doc_id = 1")
    row = cur.fetchone()
    
    if row:
        print(f"  doc_id: {row[0]}")
        print(f"  doc_key: {row[1]}")
        print(f"  doc_path: {row[2]}")
        print(f"  markdown_path (current): {row[3]}")
        print(f"  doc_name: {row[4]}")
        
        # Update markdown_path for doc_id 1 with a reasonable path
        print("\nStep 3: Updating markdown_path for doc_id 1...")
        markdown_path = f"generated/markdown/document-{row[1]}.md"
        cur.execute("UPDATE document SET markdown_path = %s WHERE doc_id = 1", (markdown_path,))
        conn.commit()
        print(f"✓ Updated markdown_path to: {markdown_path}")
        
        # Verify update
        cur.execute("SELECT markdown_path FROM document WHERE doc_id = 1")
        updated = cur.fetchone()
        print(f"✓ Verified: markdown_path = {updated[0]}")
    else:
        print("✗ No document found with doc_id = 1")
    
    cur.close()
    conn.close()
    print("\n✓ All done!")

if __name__ == "__main__":
    migrate_and_update()
